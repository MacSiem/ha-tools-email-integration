"""Whole-period facts use the actual accumulator and historical pricing once.

Import fixture modules, never their TestCase classes (no duplicate collection).
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch
from tests import test_energy_analysis as fixture
from tests import test_tariff_history as history_fixture
from tests.test_energy_weighted_comparison import readings

analysis = fixture.analysis


class EnergyPeriodSummaryTest(unittest.TestCase):
    def build(self, quantities, *, book=None, start='2026-01-01T00:00:00+00:00', zone='UTC'):
        first = datetime.fromisoformat(start)
        end = first + timedelta(hours=len(quantities))
        period = {'kind': 'day', 'start': first.isoformat(), 'end': end.isoformat(), 'timezone': zone}
        builder = analysis.EnergyAnalysisAccumulator(book, period,
            {'entry_id': 'synthetic', 'tariff_revision': 1, 'registry_revision': 0,
             'measurement_timezone': zone}, generated_at=end + timedelta(minutes=1))
        for index, quantity in enumerate(quantities):
            a = first + timedelta(hours=index)
            b = a + timedelta(hours=1)
            builder.add_chunk(readings(a.isoformat(), b.isoformat(), value=quantity,
                                       sources=('sensor.grid',)), a, b)
        return builder

    def classified_book(self, *, cheap_rate=9, expensive_rate=1):
        b = history_fixture.book()
        p = b['tariffs'][0]['versions'][0]['profile']
        p['defaultZone'] = 'low'
        p['zones'] = [
            {'id': 'low', 'label': 'Night', 'energyRate': cheap_rate, 'variableRate': 0, 'classification': 'cheap'},
            {'id': 'high', 'label': 'Day', 'energyRate': expensive_rate, 'variableRate': 0, 'classification': 'expensive'}]
        p['rules'] = [{'zone': 'high', 'intervals': [{'from': '01:00', 'to': '24:00'}]}]
        return b

    def test_explicit_energy_share_and_peak_do_not_follow_prices(self):
        result = self.build([6, 4], book=self.classified_book()).finish()
        summary = result['period_summary']
        share = summary['cheap_zone_share']
        self.assertEqual('ready', share['status'])
        self.assertEqual('0.6', share['ratio'])
        self.assertEqual('6.0', share['known_cheap_kwh'])
        self.assertEqual('10.0', share['classified_kwh'])
        self.assertEqual('10.0', share['total_kwh'])
        self.assertEqual('explicit_tariff_classification', share['classification_source'])
        self.assertEqual('complete_household_hours', share['scope'])
        peak = summary['peak_hour']
        self.assertEqual('ready', peak['status'])
        self.assertEqual('6.0', peak['kwh'])
        self.assertEqual(1, peak['tied_count'])
        self.assertEqual('2026-01-01T00:00:00+00:00', peak['start'])
        self.assertEqual({'start': result['period']['start'], 'end': result['period']['end'],
                          'timezone': 'UTC'}, summary['window'])

    def test_name_and_low_or_negative_price_never_replace_classification(self):
        for rate in (0, -1):
            b = history_fixture.book()
            p = b['tariffs'][0]['versions'][0]['profile']
            p.update(allowNegativeEnergy=True)
            p['zones'][0].update(label='cheap Night', energyRate=rate)
            summary = self.build([6, 4], book=b).finish()['period_summary']
            self.assertEqual('unknown', summary['cheap_zone_share']['status'])
            self.assertIsNone(summary['cheap_zone_share']['ratio'])
            self.assertEqual('0', summary['cheap_zone_share']['classified_kwh'])
        b = history_fixture.book()
        p = b['tariffs'][0]['versions'][0]['profile']
        p['allowNegativeEnergy'] = True
        p['zones'][0].update(energyRate=-1, classification='expensive')
        share = self.build([6, 4], book=b).finish()['period_summary']['cheap_zone_share']
        self.assertEqual('ready', share['status'])
        self.assertEqual('0', share['ratio'])
        self.assertEqual('0', share['known_cheap_kwh'])

    def test_equal_price_multiple_zones_can_have_uniform_classification_without_attribution(self):
        b = self.classified_book(cheap_rate=1, expensive_rate=1)
        p = b['tariffs'][0]['versions'][0]['profile']
        p['zones'][1]['classification'] = 'cheap'
        p['rules'] = [{'zone': 'high', 'intervals': [{'from': '00:30', 'to': '24:00'}]}]
        summary = self.build([6], book=b).finish()['period_summary']
        self.assertEqual('1', summary['cheap_zone_share']['ratio'])
        self.assertEqual([], summary['zone_breakdown']['zones'])
        self.assertEqual('6.0', summary['zone_breakdown']['unattributed']['recorded_kwh'])
        self.assertIn('multiple_zones_in_measurement', summary['zone_breakdown']['unattributed']['reasons'])

    def dynamic_book(self, classes):
        b = self.classified_book(cheap_rate=1, expensive_rate=1)
        start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        intervals = []
        for index, classification in enumerate(classes):
            intervals.append({'from': start+index*1800000, 'to': start+(index+1)*1800000,
                              'energyRate': 1, 'variableRate': 0, 'classification': classification})
        b['tariffs'][0]['versions'][0]['profile']['dynamic'] = {'intervals': intervals}
        return b

    def test_dynamic_override_uniformity_is_checked_inside_the_actual_hour(self):
        for classes, ratio in ((['cheap', 'cheap'], '1'), (['cheap', 'expensive'], None),
                               (['cheap', None], None), (['mixed', 'mixed'], None)):
            with self.subTest(classes=classes):
                result = self.build([6], book=self.dynamic_book(classes)).finish()
                share = result['period_summary']['cheap_zone_share']
                self.assertEqual(ratio, share['ratio'])
                self.assertEqual('6.00', result['totals']['usage_cost'])
                if ratio is None:
                    self.assertEqual('0', share['classified_kwh'])
        # The profile's zone says cheap; missing feed classification stays unknown.
        share = self.build([6], book=self.dynamic_book([None, None])).finish()['period_summary']['cheap_zone_share']
        self.assertIsNone(share['ratio'])

    def test_partial_measurement_and_missing_rate_keep_known_quantities(self):
        result = self.build([6, None], book=self.classified_book()).finish()
        share = result['period_summary']['cheap_zone_share']
        self.assertEqual('partial', share['status'])
        self.assertEqual('incomplete_household_measurements', share['reason'])
        self.assertEqual('6.0', share['known_cheap_kwh'])
        self.assertEqual('6.0', share['classified_kwh'])
        self.assertIsNone(share['total_kwh'])
        self.assertIsNone(share['ratio'])
        peak = result['period_summary']['peak_hour']
        self.assertEqual('partial', peak['status'])
        self.assertEqual('incomplete_period', peak['reason'])
        self.assertEqual('6.0', peak['kwh'])
        b = self.dynamic_book(['cheap', 'cheap'])
        share = self.build([6, 4], book=b).finish()['period_summary']['cheap_zone_share']
        self.assertEqual('6.0', share['known_cheap_kwh'])
        self.assertEqual('6.0', share['classified_kwh'])
        self.assertEqual('10.0', share['total_kwh'])
        self.assertIsNone(share['ratio'])

    def test_historical_classification_and_currency_mismatch_do_not_change_energy_share(self):
        b = history_fixture.book([history_fixture.version(), history_fixture.version('2026-01-02', version_id='b')])
        for index, version in enumerate(b['tariffs'][0]['versions']):
            version['profile']['zones'][0]['classification'] = 'cheap' if index == 0 else 'expensive'
        b['tariffs'][0]['versions'][1]['profile']['currency'] = 'PLN'
        b['tariffs'][0]['versions'][1]['profile']['priceBasis'] = 'net'
        result = self.build([6, 4], book=b, start='2026-01-01T23:00:00+00:00').finish()
        summary = result['period_summary']
        self.assertEqual('0.6', summary['cheap_zone_share']['ratio'])
        self.assertEqual('partial', summary['zone_breakdown']['status'])
        self.assertEqual('currency_or_basis_mismatch', summary['zone_breakdown']['reason'])
        self.assertIsNone(summary['zone_breakdown']['currency'])
        self.assertIsNone(result['totals']['total_cost'])
        self.assertEqual(['a', 'b'], [z['version_id'] for z in summary['zone_breakdown']['zones']])
        self.assertTrue(all(z['usage_cost'] is None for z in summary['zone_breakdown']['zones']))

    def test_zero_and_missing_hours_are_distinct(self):
        zero = self.build([0, 0], book=self.classified_book()).finish()['period_summary']
        self.assertEqual('0.0', zero['cheap_zone_share']['total_kwh'])
        self.assertEqual('0.0', zero['cheap_zone_share']['known_cheap_kwh'])
        self.assertIsNone(zero['cheap_zone_share']['ratio'])
        self.assertEqual('zero_total_kwh', zero['cheap_zone_share']['reason'])
        self.assertEqual('ready', zero['peak_hour']['status'])
        self.assertEqual('0.0', zero['peak_hour']['kwh'])
        self.assertEqual(2, zero['peak_hour']['tied_count'])
        missing = self.build([None, None], book=self.classified_book()).finish()['period_summary']
        self.assertIsNone(missing['cheap_zone_share']['known_cheap_kwh'])
        self.assertIsNone(missing['cheap_zone_share']['total_kwh'])
        self.assertEqual('unknown', missing['peak_hour']['status'])
        self.assertEqual('no_recorded_hours', missing['peak_hour']['reason'])
        self.assertIsNone(missing['peak_hour']['id'])
        self.assertEqual(0, missing['peak_hour']['tied_count'])

    def test_dst_ties_keep_earliest_real_hour_and_stable_id(self):
        result = self.build([5, 5], start='2026-10-25T00:00:00+00:00', zone='Europe/Warsaw').finish()
        peak = result['period_summary']['peak_hour']
        self.assertEqual(2, peak['tied_count'])
        self.assertEqual(0, peak['fold'])
        self.assertIn('02:00:00+02:00', peak['local_start'])
        self.assertIn('02:00:00+01:00', peak['local_end'])
        self.assertEqual(1, peak['end_fold'])
        second = self.build([4, 5], start='2026-10-25T00:00:00+00:00', zone='Europe/Warsaw').finish()['period_summary']['peak_hour']
        self.assertEqual(1, second['fold'])
        self.assertIn('02:00:00+01:00', second['local_start'])
        self.assertNotEqual(peak['id'], second['id'])
        self.assertEqual(peak['id'], self.build([5, 5], start='2026-10-25T00:00:00+00:00', zone='Europe/Warsaw').finish()['period_summary']['peak_hour']['id'])

    def test_period_cents_reconcile_to_period_targets_not_sum_of_rounded_chart_zones(self):
        b = self.classified_book(cheap_rate=.005, expensive_rate=.005)
        result = self.build([1, 1], book=b).finish()
        summary = result['period_summary']['zone_breakdown']
        zones = {z['zone_id']: z for z in summary['zones']}
        self.assertEqual('0.01', result['totals']['energy_cost'])
        self.assertEqual('0.00', zones['low']['energy_cost'])
        self.assertEqual('0.01', result['chart']['bins'][0]['zone_breakdown']['zones'][0]['energy_cost'])
        self.assertEqual('0.01', zones['high']['energy_cost'])
        self.assertTrue(summary['reconciles_known_costs'])
        self.assertTrue(summary['reconciles_recorded_kwh'])
        self.assertEqual(Decimal('0.01'), sum((Decimal(z['energy_cost']) for z in zones.values()), Decimal(0)))
        self.assertTrue(all('fixed_cost' not in z for z in zones.values()))

    def test_single_pricing_pass_frozen_summary_hash_and_unchanged_old_fields(self):
        builder = self.build([6, 4], book=self.classified_book())
        with patch.object(analysis, 'price_book', wraps=analysis.price_book) as pricing:
            result = builder.finish()
            self.assertEqual(1, pricing.call_count)
            original = deepcopy(result)
            result['period_summary']['cheap_zone_share']['ratio'] = 'tampered'
            again = builder.finish()
            self.assertEqual(1, pricing.call_count)
        self.assertEqual(original, again)
        expected = deepcopy(again)
        snapshot_id = expected.pop('snapshot_id')
        self.assertEqual(snapshot_id, analysis._digest(expected))
        self.assertEqual('58.00', again['totals']['usage_cost'])
        self.assertEqual('58.00', again['totals']['total_cost'])
        self.assertEqual([6, 4], [row['kwh'] for row in again['buckets']])
        self.assertEqual('54.00', again['chart']['bins'][0]['usage_cost'])
        self.assertEqual('4.00', again['chart']['bins'][1]['usage_cost'])

    def test_partial_source_hour_does_not_promote_other_sources_to_household_energy(self):
        start, end = '2026-01-01T00:00:00+00:00', '2026-01-01T02:00:00+00:00'
        builder = analysis.EnergyAnalysisAccumulator(self.classified_book(),
            {'kind': 'day', 'start': start, 'end': end, 'timezone': 'UTC'},
            {'entry_id': 'synthetic'}, generated_at=datetime.fromisoformat(end))
        snapshot = readings(start, end, value=3, sources=('sensor.a', 'sensor.b'),
                            missing=(('sensor.b', 1),))
        builder.add_chunk(snapshot, start, end)
        summary = builder.finish()['period_summary']
        self.assertEqual('6.0', summary['cheap_zone_share']['known_cheap_kwh'])
        self.assertIsNone(summary['cheap_zone_share']['total_kwh'])
        self.assertEqual('6.0', summary['peak_hour']['kwh'])
        self.assertEqual('partial', summary['peak_hour']['status'])

    def test_classification_projection_adds_no_rate_lookup_to_checkpoint_loop(self):
        model = history_fixture.engine.EnergyTariff(self.classified_book()['tariffs'][0]['versions'][0]['profile'])
        with patch.object(model, 'rate_at', wraps=model.rate_at) as lookup:
            result = model.price_bucket({'from': '2026-01-01T00:00Z', 'to': '2026-01-01T01:00Z', 'kwh': 6})
        self.assertEqual(61, lookup.call_count)  # first, 59 minute boundaries, end-1ms
        self.assertEqual('cheap', result['classification'])
        self.assertEqual((54, 0, 54), (result['energyCost'], result['variableCost'], result['usageCost']))

    def test_peak_decimal_order_and_literal_do_not_round_to_context_precision(self):
        values = ('1.' + '0'*40 + '1', '1.' + '0'*40 + '2')
        rows = [{'from': 1767225600000+i*3600000, 'to': 1767229200000+i*3600000, 'kwh': value}
                for i, value in enumerate(values)]
        summary = analysis.build_period_summary(rows,
            {'start': '2026-01-01T00:00:00+00:00', 'end': '2026-01-01T02:00:00+00:00', 'timezone': 'UTC'},
            None, {'measurement_complete': True, 'kwh': '2', 'recorded_household_kwh': '2'})
        self.assertEqual(values[1], summary['peak_hour']['kwh'])
        self.assertEqual(1767229200000, summary['peak_hour']['from'])
        self.assertEqual(1, summary['peak_hour']['tied_count'])
