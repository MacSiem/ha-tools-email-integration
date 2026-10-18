"""Registry attribution and disjoint household energy, without HA imports."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('energy_registry', Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email/energy_registry.py')
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def memberships():
    return registry.resolve_membership(['sensor.parent', 'sensor.child', 'sensor.free'],
        {'entities': {'sensor.parent': {'device_id': 'dev'},
                      'sensor.child': {'device_id': 'dev', 'area_id': 'office'},
                      'sensor.free': {}},
         'devices': {'dev': {'area_id': 'kitchen'}},
         'areas': {'kitchen': {'name': 'Kitchen', 'floor_id': 'ground'},
                   'office': {'name': 'Office', 'floor_id': 'upper'}},
         'floors': {'ground': {'name': 'Ground'}, 'upper': {'name': 'Upper'}}},
        captured_at='2026-10-18T12:00:00Z', revision=3)


def consumer(sid, kwh, parent=None, domain='grid_import'):
    return {'stat_consumption': sid, 'kwh': kwh, 'included_in_stat': parent, 'domain': domain}


class EnergyRegistryTest(unittest.TestCase):
    def test_entity_override_device_fallback_and_external_stat(self):
        m = memberships()
        self.assertEqual('kitchen', m['sources']['sensor.parent']['area_id'])
        self.assertEqual('device', m['sources']['sensor.parent']['attribution'])
        self.assertEqual('office', m['sources']['sensor.child']['area_id'])
        self.assertEqual('upper', m['sources']['sensor.child']['floor_id'])
        self.assertIsNone(m['sources']['sensor.free']['area_id'])
        external = registry.resolve_membership(['external:meter'], {}, captured_at='now', revision=1)
        self.assertIsNone(external['sources']['external:meter']['area_id'])
        self.assertFalse(external['floor_supported'])
        self.assertEqual('current_snapshot', external['membership_mode'])

    def test_move_rename_floor_removal_new_snapshot_does_not_mutate_old(self):
        data = {'entities': {'sensor.a': {'area_id': 'a'}},
                'areas': {'a': {'name': 'Old', 'floor_id': 'f'}}, 'floors': {'f': {'name': 'Up'}}}
        a = registry.resolve_membership(['sensor.a'], data, captured_at='first', revision=1)
        data['entities']['sensor.a']['area_id'] = 'b'
        data['areas']['b'] = {'name': 'New'}
        b = registry.resolve_membership(['sensor.a'], data, captured_at='second', revision=2)
        self.assertEqual('Old', a['sources']['sensor.a']['area_name'])
        self.assertEqual('New', b['sources']['sensor.a']['area_name'])
        self.assertIsNone(b['sources']['sensor.a']['floor_id'])
        self.assertNotEqual(a['revision'], b['revision'])

    def test_parent_child_partition_and_unassigned_are_not_double_counted(self):
        rows = [consumer('sensor.parent', 100), consumer('sensor.child', 40, 'sensor.parent'),
                consumer('sensor.free', 10)]
        before = copy.deepcopy(rows)
        r = registry.build_interval_breakdown(130, rows, memberships())
        self.assertEqual('ready', r['status'])
        self.assertEqual(60, r['areas']['kitchen']['kwh'])
        self.assertEqual(40, r['areas']['office']['kwh'])
        self.assertEqual(10, r['unassigned']['kwh'])
        self.assertEqual(20, r['unmeasured']['kwh'])
        self.assertEqual(130, r['partition_kwh'])
        self.assertEqual(before, rows)

    def test_nested_parent_residuals_are_disjoint(self):
        rows = [consumer('sensor.parent', 100), consumer('sensor.child', 40, 'sensor.parent'),
                consumer('sensor.free', 10, 'sensor.child')]
        r = registry.build_interval_breakdown(100, rows, memberships())
        self.assertEqual([60, 30, 10], [r['devices'][sid]['kwh'] for sid in
                         ('sensor.parent', 'sensor.child', 'sensor.free')])
        self.assertEqual(0, r['unmeasured']['kwh'])
        self.assertEqual(100, r['partition_kwh'])

    def test_invalid_graph_does_not_claim_complete_breakdown(self):
        cases = [[consumer('sensor.parent', 1), consumer('sensor.parent', 1)],
                 [consumer('sensor.child', 1, 'absent')],
                 [consumer('sensor.parent', 1, 'sensor.child'), consumer('sensor.child', 1, 'sensor.parent')]]
        for rows in cases:
            with self.subTest(rows=rows):
                r = registry.build_interval_breakdown(100, rows, memberships())
                self.assertEqual('unknown', r['status'])
                self.assertIsNone(r['partition_kwh'])
                self.assertTrue(r['issues'])

    def test_overcount_and_domain_mismatch_do_not_create_negative_remainder(self):
        cases = [[consumer('sensor.parent', 100), consumer('sensor.child', 110, 'sensor.parent')],
                 [consumer('sensor.parent', 150)],
                 [consumer('sensor.parent', 50, domain='site_consumption')]]
        for rows in cases:
            with self.subTest(rows=rows):
                r = registry.build_interval_breakdown(100, rows, memberships())
                self.assertNotEqual('ready', r['status'])
                self.assertIsNone(r['unmeasured']['kwh'])
                self.assertIsNone(r['partition_kwh'])

    def test_missing_is_not_zero_but_measured_zero_is_known(self):
        r = registry.build_interval_breakdown(100, [consumer('sensor.parent', None)], memberships())
        self.assertIsNone(r['devices']['sensor.parent']['kwh'])
        self.assertIsNone(r['unmeasured']['kwh'])
        self.assertIsNone(r['partition_kwh'])
        r = registry.build_interval_breakdown(0, [consumer('sensor.parent', 0)], memberships())
        self.assertEqual('ready', r['status'])
        self.assertEqual(0, r['partition_kwh'])
        self.assertEqual(0, r['areas']['kitchen']['kwh'])


if __name__ == '__main__':
    unittest.main()
