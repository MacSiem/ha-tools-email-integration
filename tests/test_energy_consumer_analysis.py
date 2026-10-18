"""One snapshot shares exact disjoint consumer totals and household costs."""
from decimal import Decimal
import unittest

from tests import test_energy_analysis as fixtures
analysis = fixtures.analysis
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version

START, MID, END = '2026-01-01T00:00Z', '2026-01-01T01:00Z', '2026-01-01T02:00Z'
IDS = ('sensor.parent', 'sensor.child', 'sensor.free')
CONSUMERS = [{'stat_consumption': IDS[0]},
             {'stat_consumption': IDS[1], 'included_in_stat': IDS[0]},
             {'stat_consumption': IDS[2]}]
MEMBERSHIP = {'revision': 2, 'captured_at': END, 'membership_mode': 'current_snapshot',
              'floor_supported': True, 'sources': {
    IDS[0]: {'name': 'Kitchen meter', 'area_id': 'kitchen', 'area_name': 'Kitchen', 'floor_id': 'ground', 'floor_name': 'Ground'},
    IDS[1]: {'name': 'Office meter', 'area_id': 'office', 'area_name': 'Office', 'floor_id': 'upper', 'floor_name': 'Upper'},
    IDS[2]: {'name': 'External', 'area_id': None, 'floor_id': None}}}


def consumer_readings(start, end, values):
    result = readings(start, end, sources=IDS)
    for sid, value in zip(IDS, values):
        for row in result['source_buckets'][sid]:
            row['kwh'] = value
    return result


class ConsumerAnalysisTest(unittest.TestCase):
    def builder(self, b=None, domain='grid_import'):
        base = fixtures.EnergyAnalysisTest().builder(book([version(monthly=31)]) if b is None else b)
        return analysis.EnergyAnalysisAccumulator(base._book, base._period, base._context,
            generated_at=base._generated, consumers=CONSUMERS, membership=MEMBERSHIP,
            consumer_domain=domain)

    def add(self, builder, start, end, grid, values):
        builder.add_chunk(readings(start, end, sources=('sensor.grid',), value=grid), start, end,
                          consumer_snapshot=consumer_readings(start, end, values))

    def test_disjoint_rooms_and_floors_costs_share_snapshot_and_fixed_fees_stay_household(self):
        builder = self.builder()
        self.add(builder, START, MID, 130, (100, 40, 10))
        self.add(builder, MID, END, 130, (100, 40, 10))
        result = builder.finish(); groups = result['groups']
        self.assertEqual('120', groups['areas']['kitchen']['kwh'])
        self.assertEqual('80', groups['areas']['office']['kwh'])
        self.assertEqual('20', groups['unassigned']['kwh'])
        self.assertEqual('40', groups['unmeasured']['kwh'])
        self.assertEqual('200', groups['devices'][IDS[0]]['measured_kwh'])
        self.assertEqual('parent_residual', groups['devices'][IDS[0]]['attribution'])
        self.assertEqual('120.00', groups['floors']['ground']['usage_cost'])
        self.assertEqual('80.00', groups['floors']['upper']['usage_cost'])
        self.assertEqual('20.00', groups['floors']['__no_floor__']['usage_cost'])
        self.assertEqual('260.00', result['totals']['usage_cost'])
        self.assertEqual('0.08', result['totals']['fixed_cost'])
        self.assertTrue(groups['partition_complete'])
        self.assertEqual('current_snapshot', groups['membership_mode'])
        self.assertEqual(2, groups['registry_revision'])
        self.assertFalse(any('fixed_cost' in row for row in groups['devices'].values()))
        result['groups']['areas']['kitchen']['usage_cost'] = 'tampered'
        self.assertEqual('120.00', builder.finish()['groups']['areas']['kitchen']['usage_cost'])

    def test_period_component_cents_reconcile_devices_rooms_and_household(self):
        b = book([version(rate=.005, monthly=0)])
        b['tariffs'][0]['versions'][0]['profile']['zones'][0]['variableRate'] = .005
        builder = self.builder(b)
        # Three independent .5kWh parts (parent residual .5, child .5, free .5),
        # two hours: each costs .005 per component, but whole component is .015.
        self.add(builder, START, MID, 1.5, (1, .5, .5))
        self.add(builder, MID, END, 1.5, (1, .5, .5))
        result = builder.finish(); groups = result['groups']
        for key in ('energy_cost', 'variable_cost', 'usage_cost'):
            parts = list(groups['devices'].values()) + [groups['unmeasured']]
            self.assertEqual(Decimal(result['totals'][key]), sum(Decimal(row[key]) for row in parts))
            rooms = list(groups['areas'].values()) + [groups['unassigned'], groups['unmeasured']]
            self.assertEqual(Decimal(result['totals'][key]), sum(Decimal(row[key]) for row in rooms))
        self.assertEqual('0.04', result['totals']['usage_cost'])
        self.assertEqual('0.00', groups['unmeasured']['usage_cost'])
        self.assertEqual('partition_components_largest_remainder', groups['rounding']['mode'])

    def test_missing_grid_does_not_hide_measured_consumer_but_missing_consumer_is_not_zero(self):
        builder = self.builder()
        self.add(builder, START, MID, None, (100, 40, None))
        self.add(builder, MID, END, 130, (100, 40, 10))
        result = builder.finish(); groups = result['groups']
        self.assertIsNone(result['totals']['usage_cost'])
        self.assertEqual('120.00', groups['areas']['kitchen']['usage_cost'])
        self.assertEqual('80.00', groups['areas']['office']['usage_cost'])
        self.assertIsNone(groups['unassigned']['kwh'])
        self.assertEqual('10', groups['unassigned']['recorded_kwh'])
        self.assertIsNone(groups['unassigned']['usage_cost'])
        self.assertEqual('10.00', groups['unassigned']['known_costs']['usage_cost'])
        self.assertFalse(groups['partition_complete'])

    def test_pv_site_consumption_keeps_rooms_but_cannot_price_them_as_grid_import(self):
        builder = self.builder(domain='site_consumption')
        self.add(builder, START, MID, 30, (100, 40, 10))
        self.add(builder, MID, END, 30, (100, 40, 10))
        groups = builder.finish()['groups']
        self.assertEqual('120', groups['areas']['kitchen']['kwh'])
        self.assertIsNone(groups['areas']['kitchen']['usage_cost'])
        self.assertIsNone(groups['unmeasured']['kwh'])
        self.assertFalse(groups['partition_complete'])
        self.assertIn('domain_mismatch', groups['issues'])

    def test_changed_sources_or_provenance_reject_chunk_without_poisoning_retry(self):
        builder = self.builder()
        self.add(builder, START, MID, 130, (100, 40, 10))
        changed = consumer_readings(MID, END, (100, 40, 10))
        changed['measurement_provenance'][IDS[0]]['source_unit'] = 'Wh'
        with self.assertRaisesRegex(ValueError, 'consumer_sources_changed'):
            builder.add_chunk(readings(MID, END, sources=('sensor.grid',), value=130), MID, END,
                              consumer_snapshot=changed)
        self.add(builder, MID, END, 130, (100, 40, 10))
        self.assertEqual('120', builder.finish()['groups']['areas']['kitchen']['kwh'])

    def test_legacy_combined_and_unknown_variable_preserve_named_unknowns(self):
        for semantics in ('legacy_combined', 'components'):
            b = book([version(monthly=0)])
            v = b['tariffs'][0]['versions'][0]
            v['rate_semantics'] = semantics
            v['profile']['zones'][0]['variableRate'] = None
            if semantics == 'legacy_combined':
                v['fixed_monthly'] = dict.fromkeys(v['fixed_monthly'], None)
                v['legacy_combined_monthly'] = 0
            builder = self.builder(b)
            self.add(builder, START, MID, 130, (100, 40, 10))
            self.add(builder, MID, END, 130, (100, 40, 10))
            room = builder.finish()['groups']['areas']['kitchen']
            self.assertIsNone(room['variable_cost'])
            if semantics == 'legacy_combined':
                self.assertIsNone(room['energy_cost'])
                self.assertEqual('120.00', room['usage_cost'])
            else:
                self.assertEqual('120.00', room['energy_cost'])
                self.assertIsNone(room['usage_cost'])


if __name__ == '__main__':
    unittest.main()
