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
        self.assertEqual(Decimal(40), Decimal(groups['unmeasured']['kwh']))
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

    def test_configured_energy_device_name_overrides_registry_label_without_mutation(self):
        import copy
        config = copy.deepcopy(CONSUMERS); config[0]['name'] = 'Configured house meter'
        base = fixtures.EnergyAnalysisTest().builder()
        builder = analysis.EnergyAnalysisAccumulator(base._book, base._period, base._context,
            generated_at=base._generated, consumers=config, membership=MEMBERSHIP)
        config[0]['name'] = 'mutated caller config'
        self.add(builder, START, MID, 130, (100, 40, 10))
        self.add(builder, MID, END, 130, (100, 40, 10))
        self.assertEqual('Configured house meter', builder.finish()['groups']['devices'][IDS[0]]['name'])
        self.assertEqual('Kitchen meter', MEMBERSHIP['sources'][IDS[0]]['name'])

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

    def test_partial_household_keeps_complete_room_component_period_rounding(self):
        b = book([version(rate=.0025, monthly=0)])
        b['tariffs'][0]['versions'][0]['profile']['zones'][0]['variableRate'] = .0025
        builder = self.builder(b)
        self.add(builder, START, MID, None, (1, 0, 0))
        self.add(builder, MID, END, 2, (1, 0, 0))
        result = builder.finish(); groups = result['groups']
        self.assertFalse(groups['partition_complete'])
        self.assertIsNone(result['totals']['usage_cost'])
        for row in (groups['devices'][IDS[0]], groups['areas']['kitchen'], groups['floors']['ground']):
            self.assertEqual('0.01', row['energy_cost'])
            self.assertEqual('0.01', row['variable_cost'])
            self.assertEqual('0.02', row['usage_cost'])
            self.assertEqual('0.02', row['known_costs']['usage_cost'])

    def test_mixed_legacy_and_component_versions_reconcile_known_parts_before_usage(self):
        b = book([version(rate=.005, monthly=0)])
        legacy = b['tariffs'][0]['versions'][0]
        legacy['valid_from'] = '2025-12-31'
        legacy['valid_to'] = '2026-01-02'
        legacy['rate_semantics'] = 'legacy_combined'
        legacy['fixed_monthly'] = dict.fromkeys(legacy['fixed_monthly'], None)
        legacy['legacy_combined_monthly'] = 0
        new = version(rate=.005, monthly=0)
        new['id'] = 'new'; new['valid_from'] = '2026-01-02'
        new['profile']['zones'][0]['variableRate'] = .005
        b['tariffs'][0]['versions'].append(new)
        base = fixtures.EnergyAnalysisTest().builder(b)
        first, middle, last = '2026-01-01T23:00Z', '2026-01-02T00:00Z', '2026-01-02T01:00Z'
        from datetime import datetime
        builder = analysis.EnergyAnalysisAccumulator(b, {'start': first, 'end': last, 'timezone': 'UTC'},
            base._context, generated_at=datetime.fromisoformat(last.replace('Z', '+00:00')),
            consumers=CONSUMERS, membership=MEMBERSHIP)
        self.add(builder, first, middle, 1, (1, 0, 0))
        self.add(builder, middle, last, 1, (1, 0, 0))
        result = builder.finish(); groups = result['groups']
        self.assertEqual('0.03', result['totals']['usage_cost'])
        self.assertEqual('0.03', groups['areas']['kitchen']['usage_cost'])
        self.assertIsNone(groups['areas']['kitchen']['energy_cost'])
        self.assertEqual('0.01', groups['areas']['kitchen']['known_costs']['energy_cost'])

    def test_negative_price_cents_and_unpriced_or_invalid_inputs(self):
        b = book([version(rate=-.005, monthly=0)])
        b['tariffs'][0]['versions'][0]['profile']['allowNegativeEnergy'] = True
        builder = self.builder(b)
        self.add(builder, START, MID, 1.5, (1, .5, .5))
        self.add(builder, MID, END, 1.5, (1, .5, .5))
        result = builder.finish(); groups = result['groups']
        self.assertEqual('-0.02', result['totals']['usage_cost'])
        self.assertEqual(Decimal('-.02'), sum(Decimal(row['usage_cost'])
            for row in list(groups['devices'].values()) + [groups['unmeasured']]))
        self.assertEqual('0.00', groups['unmeasured']['usage_cost'])
        base = fixtures.EnergyAnalysisTest().builder(None)
        no_price = analysis.EnergyAnalysisAccumulator(None, base._period, base._context,
            generated_at=base._generated, consumers=CONSUMERS, membership=MEMBERSHIP)
        self.add(no_price, START, MID, 130, (100, 40, 10))
        self.add(no_price, MID, END, 130, (100, 40, 10))
        self.assertEqual('120', no_price.finish()['groups']['areas']['kitchen']['kwh'])
        self.assertIsNone(no_price.finish()['groups']['areas']['kitchen']['usage_cost'])
        with self.assertRaisesRegex(ValueError, 'invalid_consumer_configuration'):
            analysis.EnergyAnalysisAccumulator(None, base._period, base._context,
                generated_at=base._generated, consumers=[{'stat_consumption': 'sensor.cycle',
                'included_in_stat': 'sensor.cycle'}], membership=MEMBERSHIP)

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
