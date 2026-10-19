"""Bounded internal facts reuse existing prices, without public DTO additions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from tests.test_energy_analysis import analysis
from tests.test_energy_consumer_analysis import CONSUMERS, MEMBERSHIP, consumer_readings
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version

UTC = timezone.utc


class StatisticsHoursTest(unittest.TestCase):
    def builder(self, hours=24, *, enabled=True, tariff=None, start=None, domain='grid_import'):
        start = start or datetime(2026, 1, 1, tzinfo=UTC)
        end = start + timedelta(hours=hours)
        tariff = book([version(rate=.005)]) if tariff is None else tariff
        return analysis.EnergyAnalysisAccumulator(tariff,
            {'start': start.isoformat(), 'end': end.isoformat(), 'kind': 'week', 'timezone': 'UTC'},
            {'entry_id': 'synthetic', 'tariff_revision': 2, 'registry_revision': 2},
            generated_at=end, consumers=CONSUMERS, membership=MEMBERSHIP,
            consumer_domain=domain, include_statistics_hours=enabled)

    def add(self, model, *, grid=130, values=(100, 40, 10), data=None):
        start, end = model._period['start'], model._period['end']
        return model.add_chunk(readings(start, end, value=grid, sources=('sensor.grid',)),
            start, end, consumer_snapshot=data or consumer_readings(start, end, values))

    def facts(self, model, **kw):
        self.add(model, **kw)
        public = model.finish()
        self.assertNotIn('statistics_hours', public)
        return model.statistics_hours()

    def test_exact_disjoint_costs_without_period_cent_rounding_or_public_changes(self):
        enabled, disabled = self.builder(2), self.builder(2, enabled=False)
        facts = self.facts(enabled)
        self.add(disabled)
        self.assertEqual(disabled.finish(), enabled.finish())
        first = facts['hours'][0]
        self.assertEqual(Decimal('.30'), Decimal(first['groups']['areas']['kitchen']['usage_cost']))
        self.assertEqual(Decimal('.20'), Decimal(first['groups']['areas']['office']['usage_cost']))
        self.assertEqual(Decimal('.05'), Decimal(first['groups']['unassigned']['usage_cost']))
        self.assertEqual(Decimal('.10'), Decimal(first['groups']['unmeasured']['usage_cost']))
        parts = [*first['groups']['areas'].values(), first['groups']['unassigned'], first['groups']['unmeasured']]
        self.assertEqual(Decimal(first['household']['usage_cost']), sum(Decimal(r['usage_cost']) for r in parts))
        self.assertEqual(first['household']['usage_cost'], first['zone']['usage_cost'])
        self.assertTrue(first['partition_complete'])
        self.assertEqual('current_snapshot', facts['membership_mode'])
        self.assertEqual(MEMBERSHIP['captured_at'], facts['membership_as_of'])
        self.assertEqual(['sensor.grid'], facts['grid_source_ids'])
        self.assertEqual('recorder_hourly_change_v1', facts['grid_provenance']['sensor.grid']['method'])
        self.assertEqual(sorted(row['stat_consumption'] for row in CONSUMERS), facts['consumer_source_ids'])
        self.assertEqual('EUR', first['groups']['currency'])
        self.assertFalse(any('fixed_cost' in r for r in parts))
        facts['hours'][0]['groups']['areas']['kitchen']['usage_cost'] = '999'
        self.assertNotEqual('999', enabled.statistics_hours()['hours'][0]['groups']['areas']['kitchen']['usage_cost'])
        with self.assertRaisesRegex(ValueError, 'statistics_projection_not_enabled'):
            disabled.statistics_hours()

    def test_same_hour_is_identical_in_24_and_168_hour_requests(self):
        short = self.facts(self.builder(24), grid=1.5, values=(1, .5, .5))
        long = self.facts(self.builder(168), grid=1.5, values=(1, .5, .5))
        self.assertEqual(short['hours'], long['hours'][:24])
        self.assertEqual(Decimal('.0025'), Decimal(short['hours'][0]['groups']['areas']['office']['usage_cost']))

    def test_known_zero_and_negative_do_not_become_unknown_or_absolute(self):
        zero = self.facts(self.builder(1), grid=0, values=(0, 0, 0))['hours'][0]
        self.assertEqual(Decimal(0), Decimal(zero['groups']['areas']['kitchen']['usage_cost']))
        self.assertTrue(zero['groups']['areas']['kitchen']['complete'])
        b = book([version(rate=-.5)])
        b['tariffs'][0]['versions'][0]['profile']['allowNegativeEnergy'] = True
        negative = self.facts(self.builder(1, tariff=b))['hours'][0]
        self.assertEqual(Decimal(-30), Decimal(negative['groups']['areas']['kitchen']['usage_cost']))
        self.assertLess(Decimal(negative['household']['usage_cost']), 0)

    def test_unknown_price_even_on_measured_zero_and_missing_child_are_not_zero(self):
        b = book([version()]); b['active_tariff_id'] = None
        unknown = self.facts(self.builder(1, tariff=b), grid=0, values=(0, 0, 0))['hours'][0]
        self.assertIsNone(unknown['groups']['areas']['kitchen']['usage_cost'])
        self.assertFalse(unknown['groups']['areas']['kitchen']['complete'])
        b = book([version()]); b['tariffs'][0]['versions'][0]['profile']['zones'][0]['variableRate'] = None
        unknown_zero = self.facts(self.builder(1, tariff=b), grid=0, values=(0, 0, 0))['hours'][0]
        self.assertIsNone(unknown_zero['groups']['areas']['kitchen']['usage_cost'])
        self.assertFalse(unknown_zero['groups']['areas']['kitchen']['pricing_complete'])
        self.assertIsNone(unknown_zero['zone']['usage_cost'])
        m = self.builder(1)
        data = consumer_readings(m._period['start'], m._period['end'], (100, None, 10))
        incomplete = self.facts(m, data=data)['hours'][0]
        self.assertFalse(incomplete['partition_complete'])
        self.assertIsNone(incomplete['groups']['areas']['kitchen']['usage_cost'])
        self.assertFalse(incomplete['groups']['areas']['office']['measurement_complete'])
        site = self.facts(self.builder(1, domain='site_consumption'))['hours'][0]
        self.assertIsNone(site['groups']['unmeasured']['usage_cost'])
        self.assertFalse(site['partition_complete'])

    def test_default_does_not_build_projection_or_add_price_calls_or_mutate_input(self):
        consumer_globals = analysis.ConsumerPeriod._prepare_chunk.__globals__
        original = consumer_globals['calculate_energy_amounts']
        before = deepcopy(MEMBERSHIP)
        disabled, enabled = self.builder(2, enabled=False), self.builder(2)
        plain_calls = Mock(wraps=original)
        with patch.dict(consumer_globals, calculate_energy_amounts=plain_calls):
            self.add(disabled); plain = disabled.finish()
            count = plain_calls.call_count
        projected_calls = Mock(wraps=original)
        with patch.dict(consumer_globals, calculate_energy_amounts=projected_calls):
            self.add(enabled); projected = enabled.finish()
            self.assertEqual(count, projected_calls.call_count)
        self.assertEqual(plain, projected)
        self.assertIsNone(disabled._statistics_result)
        self.assertIsNone(disabled._consumers._statistics_hours)
        self.assertEqual(before, MEMBERSHIP)

    def test_limit_flags_and_unfinished_access_fail_before_projection_or_pricing(self):
        for flag in (1, None, 'yes'):
            with self.assertRaisesRegex(ValueError, 'invalid_statistics_projection'):
                self.builder(enabled=flag)
        with self.assertRaisesRegex(ValueError, 'statistics_projection_too_large'):
            self.builder(169)
        with self.assertRaisesRegex(ValueError, 'statistics_projection_incomplete'):
            self.builder(1).statistics_hours()
        # The default still permits a normal year; no new annual retained map.
        self.builder(8760, enabled=False)
        with self.assertRaisesRegex(ValueError, 'statistics_projection_too_large'):
            analysis.ConsumerPeriod(CONSUMERS, MEMBERSHIP, 'grid_import', None, 169,
                include_statistics_hours=True)

    def test_rejected_chunk_does_not_advance_retained_statistics_and_retry_is_exact(self):
        m = self.builder(2)
        start, end = m._period['start'], m._period['end']
        malformed = consumer_readings(start, end, (100, 40, 10))
        malformed['source_ids'].append('sensor.other')
        with self.assertRaises(ValueError):
            m.add_chunk(readings(start, end, value=130, sources=('sensor.grid',)), start, end,
                consumer_snapshot=malformed)
        self.assertEqual([], m._consumers._statistics_hours)
        self.assertEqual(m._start, m._cursor)
        actual = self.facts(m)
        expected = self.facts(self.builder(2))
        self.assertEqual(expected, actual)

    def test_dst_absolute_hours_are_distinct_and_captured_membership_is_detached(self):
        from zoneinfo import ZoneInfo
        start = datetime(2026, 10, 25, 0, tzinfo=UTC)
        m = self.builder(2, start=start)
        facts = self.facts(m)
        stamps = [row['from'] for row in facts['hours']]
        local = [datetime.fromtimestamp(x/1000, ZoneInfo('Europe/Warsaw')) for x in stamps]
        self.assertEqual([2, 2], [d.hour for d in local])
        self.assertEqual([0, 1], [d.fold for d in local])
        self.assertEqual(3600000, stamps[1]-stamps[0])
        facts['group_membership']['areas']['kitchen'].clear()
        self.assertTrue(m.statistics_hours()['group_membership']['areas']['kitchen'])

    def test_zone_helper_rejects_multiple_mismatched_rows_and_keeps_historical_currency(self):
        # Exercise real rows produced by price_book, then mutate only attribution
        # to model a mixed-zone hour that cannot be allocated proportionally.
        m = self.builder(1); self.add(m); m.finish()
        base = m._rows
        coverage = {'from': m._period['start'], 'to': m._period['end'], 'complete': True, 'unit': 'kWh'}
        priced = analysis.price_book(m._book, base, coverage)['tariffs'][0]
        result = analysis.household_cost_hours(base, priced)
        self.assertTrue(result[0]['zone']['complete'])
        changed = deepcopy(priced); changed['rows'][0]['zoneAttribution'] = 'multiple'
        self.assertIsNone(analysis.household_cost_hours(base, changed)[0]['zone']['usage_cost'])
        changed = deepcopy(priced); changed['rows'][0]['from'] += 3600000
        self.assertEqual('priced_row_mismatch', analysis.household_cost_hours(base, changed)[0]['zone']['reason'])
        changed = deepcopy(priced); changed['rates'][0]['currency'] = 'PLN'
        changed['rates'][0]['profile']['currency'] = 'PLN'
        self.assertEqual('PLN', analysis.household_cost_hours(base, changed)[0]['zone']['currency'])


    def test_projection_failure_is_staged_and_group_projection_stays_unchanged(self):
        m = self.builder(2)
        consumer_globals = analysis.ConsumerPeriod._prepare_chunk.__globals__
        with patch.dict(consumer_globals, group_cost_hour=Mock(side_effect=ValueError('synthetic_failure'))):
            with self.assertRaisesRegex(ValueError, 'synthetic_failure'):
                self.add(m)
        self.assertEqual([], m._rows)
        self.assertEqual([], m._consumers._statistics_hours)
        self.assertIsNone(m._ids)
        self.assertEqual(m._start, m._cursor)
        start, end = m._period['start'], m._period['end']
        sources = consumer_readings(start, end, (100, 40, 10))
        grid = readings(start, end, value=130, sources=('sensor.grid',))
        old, new = self.builder(2, enabled=False), m
        old_projection = old.add_chunk(grid, start, end, consumer_snapshot=sources, include_group_hours=True)
        new_projection = new.add_chunk(grid, start, end, consumer_snapshot=sources, include_group_hours=True)
        self.assertEqual(old_projection, new_projection)
        self.assertEqual(old.finish(), new.finish())
        self.assertEqual(2, len(new.statistics_hours()['hours']))

    def test_historical_currency_and_basis_are_per_hour_when_period_is_incompatible(self):
        first, second = version(), version(start='2026-01-02', version_id='b')
        first['valid_to'] = '2026-01-02'
        second['profile']['currency'] = 'PLN'
        second['profile']['priceBasis'] = 'net'
        m = self.builder(2, start=datetime(2026, 1, 1, 23, tzinfo=UTC), tariff=book([first, second]))
        facts = self.facts(m)
        self.assertIsNone(m.finish()['totals']['currency'])
        for index, expected in enumerate((('EUR', 'gross', 'a'), ('PLN', 'net', 'b'))):
            group, zone = facts['hours'][index]['groups'], facts['hours'][index]['zone']
            self.assertEqual(expected, (group['currency'], group['price_basis'], group['version_id']))
            self.assertEqual(expected, (zone['currency'], zone['price_basis'], zone['version_id']))
            self.assertTrue(zone['complete'])

    def test_missing_household_and_mixed_version_zone_are_unknown(self):
        m = self.builder(1)
        fact = self.facts(m, grid=None)['hours'][0]
        self.assertEqual('missing_measurement', fact['zone']['reason'])
        self.assertFalse(fact['measurement_complete'])
        self.assertFalse(fact['partition_complete'])
        self.assertIsNone(fact['groups']['unmeasured']['usage_cost'])
        active = {'tariff_id': 'active', 'rates': [],
                  'rows': [{'index': 0, 'available': False, 'reason': 'mixed_version_bucket'}]}
        hour = {'from': 1767225600000, 'to': 1767229200000, 'kwh': 1}
        fact = analysis.household_cost_hours([hour], active)[0]
        self.assertEqual('mixed_version_bucket', fact['zone']['reason'])
        self.assertIsNone(fact['zone']['usage_cost'])


    def test_omitted_flag_and_multiple_accepted_chunks_keep_original_dto(self):
        configured = self.builder(2)
        default = analysis.EnergyAnalysisAccumulator(configured._book, configured._period, configured._context,
            generated_at=configured._generated, consumers=CONSUMERS, membership=MEMBERSHIP)
        self.add(default)
        for hour in range(2):
            start = (configured._start + timedelta(hours=hour)).isoformat()
            end = (configured._start + timedelta(hours=hour+1)).isoformat()
            configured.add_chunk(readings(start, end, value=130, sources=('sensor.grid',)), start, end,
                consumer_snapshot=consumer_readings(start, end, (100, 40, 10)))
        self.assertEqual(default.finish(), configured.finish())
        self.assertEqual(2, len(configured.statistics_hours()['hours']))
        self.assertIsNone(default._statistics_result)
        self.assertIsNone(default._consumers._statistics_hours)
        # Direct ConsumerPeriod consumers cannot retain a second request on the
        # same instance after committing its declared hour budget.
        with self.assertRaisesRegex(ValueError, 'statistics_projection_too_large'):
            configured._consumers.prepare_chunk(consumer_readings(start, end, (100, 40, 10)),
                configured._rows[-1:], start, end)
        self.assertEqual(2, len(configured.statistics_hours()['hours']))
