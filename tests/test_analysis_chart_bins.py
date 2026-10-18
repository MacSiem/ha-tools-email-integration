"""Chart/table/export bins come from the same actual household hours and prices."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from tests.test_energy_analysis import analysis
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version


class AnalysisChartBinsTest(unittest.TestCase):
    def build(self, start, end, kind, *, zone='Europe/Warsaw', missing=(), value=1, tariff=None):
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        builder = analysis.EnergyAnalysisAccumulator(tariff,
            {'start': start, 'end': end, 'kind': kind, 'timezone': zone}, {},
            generated_at=b + timedelta(hours=1))
        cursor = a
        while cursor < b:
            stop = min(cursor+timedelta(hours=168), b)
            offset = int((cursor-a).total_seconds()/3600)
            absent = [('sensor.grid', hour-offset) for hour in missing
                      if offset <= hour < offset + (stop-cursor).total_seconds()/3600]
            builder.add_chunk(readings(cursor.isoformat(), stop.isoformat(), sources=('sensor.grid',),
                                       missing=absent, value=value), cursor, stop)
            cursor = stop
        return builder.finish()

    def test_dst_daily_chart_retains_two_distinct_two_oclock_hours(self):
        result = self.build('2026-10-24T22:00Z', '2026-10-25T23:00Z', 'day')
        chart = result['chart']; bins = chart['bins']
        self.assertEqual('ready', chart['status']); self.assertEqual('hour', chart['grain'])
        self.assertEqual(25, len(bins))
        repeated = [row for row in bins if row['local_start'][11:13] == '02']
        self.assertEqual([0, 1], [row['fold'] for row in repeated])
        self.assertNotEqual(repeated[0]['from'], repeated[1]['from'])
        self.assertEqual(['+02:00', '+01:00'], [row['local_start'][-6:] for row in repeated])
        self.assertEqual(Decimal(result['totals']['kwh']), sum(Decimal(row['kwh']) for row in bins))

    def test_week_bins_cover_actual_twenty_three_hour_day(self):
        result = self.build('2026-03-22T23:00Z', '2026-03-29T22:00Z', 'week')
        bins = result['chart']['bins']
        self.assertEqual('day', result['chart']['grain']); self.assertEqual(7, len(bins))
        self.assertEqual([24]*6+[23], [row['expected_seconds']/3600 for row in bins])
        self.assertEqual([Decimal(24)]*6+[Decimal(23)], [Decimal(row['kwh']) for row in bins])

    def test_leap_year_month_bins_sum_exactly_across_53_chunks(self):
        result = self.build('2024-01-01T00:00Z', '2025-01-01T00:00Z', 'year', zone='UTC')
        bins = result['chart']['bins']
        self.assertEqual('month', result['chart']['grain']); self.assertEqual(12, len(bins))
        self.assertEqual(Decimal(696), Decimal(bins[1]['kwh']))
        self.assertEqual(Decimal(8784), sum(Decimal(row['kwh']) for row in bins))

    def test_missing_hour_withholds_bin_total_but_retains_known_observations_and_zero(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-03T00:00Z', 'month',
                            zone='UTC', missing=(4,), value=0)
        first, second = result['chart']['bins']
        self.assertIsNone(first['kwh']); self.assertEqual(Decimal(0), Decimal(first['recorded_kwh']))
        self.assertEqual(23*3600, first['measured_seconds'])
        self.assertEqual(24*3600, first['expected_seconds'])
        self.assertFalse(first['complete']); self.assertTrue(second['complete'])
        self.assertEqual(Decimal(0), Decimal(second['kwh']))
        self.assertIsNone(first['usage_cost']); self.assertIsNone(first['fixed_cost'])

    def test_cost_bins_reconcile_component_cents_without_duplicating_household_fees(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-01T03:00Z', 'day', zone='UTC',
                            value=.005, tariff=book([version(rate=1, monthly=31)]))
        bins = result['chart']['bins']
        self.assertEqual(Decimal('.02'), sum(Decimal(row['energy_cost']) for row in bins))
        self.assertEqual(Decimal(result['totals']['usage_cost']), sum(Decimal(row['usage_cost']) for row in bins))
        self.assertTrue(all(row['fixed_cost'] is None and row['total_cost'] is None for row in bins))
        self.assertEqual('household_period_only', result['chart']['fixed_fee_scope'])
        self.assertEqual('0.13', result['totals']['fixed_cost'])

    def test_unavailable_measurement_zone_is_explicit_not_host_timezone(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-01T02:00Z', 'day', zone='Invalid/Timezone')
        self.assertEqual('unknown', result['chart']['status'])
        self.assertEqual('measurement_timezone_unavailable', result['chart']['reason'])
        self.assertEqual([], result['chart']['bins'])


class AnalysisZoneBinsTest(unittest.TestCase):
    """Real prices and measurements: no synthetic availability/proof flags."""
    build = AnalysisChartBinsTest.build

    def partition(self, row):
        part = row['zone_breakdown']
        pieces = [*part['zones'], part['unattributed']]
        observed = [Decimal(x['recorded_kwh']) for x in pieces if x['recorded_kwh'] is not None]
        self.assertEqual(None if row['recorded_kwh'] is None else Decimal(row['recorded_kwh']),
                         sum(observed) if observed else None)
        self.assertTrue(part['reconciles_recorded_kwh'])
        for component, target in row['known_costs'].items():
            amounts = [Decimal(x['known_costs'][component]) for x in pieces
                       if x['known_costs'][component] is not None]
            if target is not None:
                self.assertEqual(Decimal(target), sum(amounts))
            else:
                self.assertEqual([], amounts)
        return part

    def split_book(self, *, start='00:30', rate=1):
        b = book([version()])
        p = b['tariffs'][0]['versions'][0]['profile']
        p['zones'].append({'id': 'night', 'label': 'Night', 'energyRate': rate, 'variableRate': 0})
        p['rules'] = [{'zone': 'night', 'intervals': [{'from': start, 'to': '01:00'}]}]
        return b

    def test_two_chunks_keep_bill_alternative_and_detached_snapshot(self):
        from tests.test_energy_analysis import EnergyAnalysisTest
        fixture = EnergyAnalysisTest()
        acc = fixture.builder()
        fixture.add(acc, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        fixture.add(acc, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 2)
        result = acc.finish()
        self.assertEqual(('3.00', '0.08', '3.08'), tuple(result['totals'][k]
                         for k in ('energy_cost', 'fixed_cost', 'total_cost')))
        zones = [self.partition(row)['zones'][0] for row in result['chart']['bins']]
        self.assertEqual([Decimal(1), Decimal(2)], [Decimal(z['kwh']) for z in zones])
        self.assertEqual(['1.00', '2.00'], [z['energy_cost'] for z in zones])
        self.assertEqual([('active', 'a', 'day')]*2,
                         [(z['tariff_id'], z['version_id'], z['zone_id']) for z in zones])
        alternative = next(x for x in result['alternatives'] if x['tariff_id'] == 'alternative')
        self.assertEqual(('1.58', '-1.50'), (alternative['total_cost'], alternative['delta_cost']))
        self.assertEqual(result['measurement_fingerprint'], alternative['measurement_fingerprint'])
        before = result['snapshot_id']
        zones[0]['recorded_kwh'] = '999'
        again = acc.finish()
        self.assertEqual(before, again['snapshot_id'])
        self.assertEqual(Decimal(1), Decimal(again['chart']['bins'][0]['zone_breakdown']['zones'][0]['recorded_kwh']))
        self.assertNotIn('fixed_cost', zones[1])

    def test_cent_targets_follow_existing_hour_bins_including_negative(self):
        for rate, expected, total in [(1, ['0.01', '0.01', '0.00'], '0.02'),
                                      (-1, ['0.00', '-0.01', '-0.01'], '-0.02')]:
            with self.subTest(rate=rate):
                b = book([version(rate=rate, monthly=31)])
                b['tariffs'][0]['versions'][0]['profile']['allowNegativeEnergy'] = True
                result = self.build('2026-01-01T00:00Z', '2026-01-01T03:00Z', 'day',
                                    zone='UTC', value=.005, tariff=b)
                parts = [self.partition(row) for row in result['chart']['bins']]
                self.assertEqual(expected, [p['zones'][0]['energy_cost'] for p in parts])
                self.assertEqual(total, result['totals']['energy_cost'])
                self.assertEqual('0.13', result['totals']['fixed_cost'])

    def test_equal_half_hour_zones_keep_whole_energy_and_cost_unattributed(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-01T01:00Z', 'day',
                            zone='UTC', tariff=self.split_book())
        part = self.partition(result['chart']['bins'][0])
        self.assertEqual('partial', part['status'])
        self.assertEqual([], part['zones'])
        self.assertEqual(Decimal(1), Decimal(part['unattributed']['recorded_kwh']))
        self.assertEqual('1.00', part['unattributed']['known_costs']['energy_cost'])
        self.assertIn('multiple_zones_in_measurement', part['unattributed']['reasons'])
        self.assertTrue(part['reconciles_known_costs'])

    def test_different_half_hour_prices_never_invent_cost(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-01T01:00Z', 'day',
                            zone='UTC', tariff=self.split_book(rate=2))
        part = self.partition(result['chart']['bins'][0])
        self.assertEqual([], part['zones'])
        self.assertEqual(Decimal(1), Decimal(part['unattributed']['recorded_kwh']))
        self.assertIsNone(part['unattributed']['known_costs']['energy_cost'])
        self.assertIn('mixed_rate_bucket', part['unattributed']['reasons'])

    def test_day_bin_cent_tie_is_stable_across_two_zone_keys(self):
        # day 00-01 and night 01-02: raw .005 each, target .01.
        b = self.split_book(start='01:00')
        b['tariffs'][0]['versions'][0]['profile']['rules'][0]['intervals'][0]['to'] = '02:00'
        row = self.build('2026-01-01T00:00Z', '2026-01-01T02:00Z', 'month',
                         zone='UTC', value=.005, tariff=b)['chart']['bins'][0]
        part = self.partition(row)
        self.assertEqual(['day', 'night'], [z['zone_id'] for z in part['zones']])
        self.assertEqual(['0.01', '0.00'], [z['energy_cost'] for z in part['zones']])
        self.assertEqual('0.01', row['energy_cost'])

    def test_missing_zero_and_no_tariff_remain_distinct(self):
        row = self.build('2026-01-01T00:00Z', '2026-01-01T02:00Z', 'month',
                         zone='UTC', value=0, missing=(0,), tariff=book())['chart']['bins'][0]
        part = self.partition(row)
        self.assertEqual('partial', part['status'])
        self.assertIsNone(part['zones'][0]['kwh'])
        self.assertEqual(Decimal(0), Decimal(part['zones'][0]['recorded_kwh']))
        self.assertEqual('0.00', part['zones'][0]['known_costs']['energy_cost'])
        self.assertIsNone(part['zones'][0]['energy_cost'])
        self.assertEqual(3600, part['unattributed']['missing_seconds'])
        self.assertIsNone(part['unattributed']['recorded_kwh'])
        no_tariff = self.build('2026-01-01T00:00Z', '2026-01-01T01:00Z', 'day',
                               zone='UTC', value=0)['chart']['bins'][0]
        absent = self.partition(no_tariff)
        self.assertEqual(('unknown', 'tariff_unavailable'), (absent['status'], absent['reason']))
        self.assertEqual(Decimal(0), Decimal(absent['unattributed']['recorded_kwh']))
        self.assertFalse(absent['reconciles_known_costs'])

    def test_partial_source_hour_never_becomes_household_zone_measurement(self):
        a, b = '2026-01-01T00:00Z', '2026-01-01T01:00Z'
        acc = analysis.EnergyAnalysisAccumulator(book(),
            {'start': a, 'end': b, 'kind': 'day', 'timezone': 'UTC'}, {}, generated_at=b)
        acc.add_chunk(readings(a, b, value=2, sources=('sensor.a', 'sensor.b'),
                               missing=(('sensor.b', 0),)), a, b)
        row = acc.finish()['chart']['bins'][0]
        part = self.partition(row)
        self.assertEqual([], part['zones'])
        self.assertIsNone(part['unattributed']['recorded_kwh'])
        self.assertEqual(3600, part['unattributed']['missing_seconds'])

    def test_missing_variable_and_legacy_keep_unknown_components(self):
        for semantics, energy, variable, combined in [
                ('components', '2.00', None, '0.00'),
                ('legacy_combined', None, None, '2.00')]:
            with self.subTest(semantics=semantics):
                v = version(rate_semantics=semantics)
                v['profile']['zones'][0]['variableRate'] = None
                row = self.build('2026-01-01T00:00Z', '2026-01-01T01:00Z', 'day',
                                 zone='UTC', value=2, tariff=book([v]))['chart']['bins'][0]
                z = self.partition(row)['zones'][0]
                self.assertEqual(Decimal(2), Decimal(z['kwh']))
                self.assertEqual((energy, variable, combined),
                                 tuple(z[k] for k in ('energy_cost', 'variable_cost', 'legacy_combined_cost')))
                self.assertEqual(None if semantics == 'components' else '2.00', z['usage_cost'])

    def test_version_identity_survives_same_zone_id_and_money_mismatch(self):
        for change in ({}, {'currency': 'PLN'}, {'priceBasis': 'net'}):
            with self.subTest(change=change):
                va, vb = version(), version('2026-01-02', rate=2, version_id='b')
                vb['profile'].update(change)
                row = self.build('2026-01-01T23:00Z', '2026-01-02T01:00Z', 'year',
                                 zone='UTC', tariff=book([va, vb]))['chart']['bins'][0]
                part = self.partition(row)
                self.assertEqual([('a', 'day'), ('b', 'day')],
                                 [(z['version_id'], z['zone_id']) for z in part['zones']])
                self.assertEqual([Decimal(1), Decimal(1)], [Decimal(z['kwh']) for z in part['zones']])
                if change:
                    self.assertEqual('currency_or_basis_mismatch', part['reason'])
                    self.assertTrue(all(z['energy_cost'] is None for z in part['zones']))
                    self.assertFalse(part['reconciles_known_costs'])
                else:
                    self.assertEqual(['1.00', '2.00'], [z['energy_cost'] for z in part['zones']])

    def test_pre_validity_history_keeps_measured_energy_unattributed(self):
        row = self.build('2025-12-31T23:00Z', '2026-01-01T00:00Z', 'day',
                         zone='UTC', tariff=book())['chart']['bins'][0]
        part = self.partition(row)
        self.assertEqual([], part['zones'])
        self.assertIn('unknown_historical_version', part['unattributed']['reasons'])
        self.assertEqual(Decimal(1), Decimal(part['unattributed']['recorded_kwh']))

    def test_non_string_zone_label_cannot_export_private_configuration(self):
        b = book()
        b['tariffs'][0]['versions'][0]['profile']['zones'][0]['label'] = {'private': '!secret fixture'}
        row = self.build('2026-01-01T00:00Z', '2026-01-01T01:00Z', 'day',
                         zone='UTC', tariff=b)['chart']['bins'][0]
        z = self.partition(row)['zones'][0]
        self.assertEqual('day', z['label'])
        self.assertNotIn('profile', z)
        self.assertNotIn('provenance', z)

    def test_dst_fold_and_annual_bins_keep_bounded_public_aggregates(self):
        b = book([version('2024-01-01')])
        dst = self.build('2026-10-24T22:00Z', '2026-10-25T23:00Z', 'day', tariff=b)
        bins = dst['chart']['bins']
        self.assertEqual(25, len(bins))
        repeated = [row for row in bins if row['local_start'][11:13] == '02']
        self.assertEqual([0, 1], [r['fold'] for r in repeated])
        self.assertNotEqual(repeated[0]['from'], repeated[1]['from'])
        self.assertEqual(Decimal(25), sum(Decimal(self.partition(r)['zones'][0]['kwh']) for r in bins))
        annual = self.build('2024-01-01T00:00Z', '2025-01-01T00:00Z', 'year', zone='UTC', tariff=b)
        months = annual['chart']['bins']
        self.assertEqual(12, len(months))
        self.assertEqual(Decimal(696), Decimal(self.partition(months[1])['zones'][0]['kwh']))
        self.assertEqual(Decimal(8784), sum(Decimal(self.partition(r)['zones'][0]['kwh']) for r in months))
        self.assertTrue(all(len(r['zone_breakdown']['zones']) == 1 for r in months))
        self.assertTrue(all('hours' not in r['zone_breakdown']['zones'][0] for r in months))

    def test_unknown_start_and_mid_hour_version_change_stay_unattributed(self):
        unknown = book([version(start=None, provenance='migrated_unknown_start')])
        unknown['tariffs'][0]['origin'] = {'kind': 'legacy', 'reference': 'schema1'}
        row = self.build('2026-01-01T00:00Z', '2026-01-01T01:00Z', 'day',
                         zone='UTC', tariff=unknown)['chart']['bins'][0]
        part = self.partition(row)
        self.assertEqual([], part['zones'])
        self.assertIn('unknown_historical_version', part['unattributed']['reasons'])
        va, vb = version(), version('2026-01-02', version_id='b')
        for v in (va, vb):
            v['profile']['timezone'] = 'Asia/Kathmandu'
        row = self.build('2026-01-01T18:00Z', '2026-01-01T19:00Z', 'day',
                         zone='UTC', tariff=book([va, vb]))['chart']['bins'][0]
        part = self.partition(row)
        self.assertEqual([], part['zones'])
        self.assertEqual(Decimal(1), Decimal(part['unattributed']['recorded_kwh']))
        self.assertIn('mixed_version_bucket', part['unattributed']['reasons'])
        self.assertIsNone(part['unattributed']['known_costs']['energy_cost'])

    def test_chunk_layout_does_not_change_zone_cents_or_measurement_fingerprint(self):
        from tests.test_energy_analysis import EnergyAnalysisTest
        fixture = EnergyAnalysisTest()
        split = fixture.builder()
        fixture.add(split, '2026-01-01T00:00Z', '2026-01-01T01:00Z', .005)
        fixture.add(split, '2026-01-01T01:00Z', '2026-01-01T02:00Z', .005)
        whole = fixture.builder()
        fixture.add(whole, '2026-01-01T00:00Z', '2026-01-01T02:00Z', .005)
        left, right = split.finish(), whole.finish()
        self.assertEqual('0.01', left['totals']['energy_cost'])
        self.assertEqual(['0.01', '0.00'], [self.partition(r)['zones'][0]['energy_cost']
                                        for r in left['chart']['bins']])
        self.assertEqual(left['chart'], right['chart'])
        self.assertEqual(left['measurement_fingerprint'], right['measurement_fingerprint'])
        self.assertEqual(left['snapshot_id'], right['snapshot_id'])

    def test_zone_data_is_inside_snapshot_hash(self):
        import hashlib
        import json
        result = self.build('2026-01-01T00:00Z', '2026-01-01T01:00Z', 'day',
                            zone='UTC', tariff=book())
        snapshot_id = result.pop('snapshot_id')
        encode = lambda: json.dumps(result, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')
        self.assertEqual(snapshot_id, hashlib.sha256(encode()).hexdigest())
        result['chart']['bins'][0]['zone_breakdown']['zones'][0]['label'] = 'Changed'
        self.assertNotEqual(snapshot_id, hashlib.sha256(encode()).hexdigest())
