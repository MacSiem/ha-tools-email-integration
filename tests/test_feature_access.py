"""Pure policy behavior; no HA setup or integrations are imported."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
spec = importlib.util.spec_from_file_location('feature_access_under_test', ROOT / 'feature_access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


def policy(feature='energy_reports', **operations):
    return {'schema': 1, 'features': {feature: operations}}


_DEFAULT = object()


def allowed(value=_DEFAULT, feature='energy_reports', operation='read', **user):
    identity = {'user_id': 'alice', 'is_active': True, 'is_admin': False}
    identity.update(user)
    return access.feature_access_allowed(
        access.default_feature_access() if value is _DEFAULT else value,
        feature, operation, **identity)


class FeatureAccessTest(unittest.TestCase):
    def test_default_catalog_roles_and_supported_operations(self):
        expected = {
            'energy_analysis': {'visible': True, 'read': True},
            'energy_tariff': {'visible': True, 'read': True, 'execute': False},
            'energy_reports': {'visible': False, 'read': False, 'execute': False},
            'log_reports': {'visible': False, 'read': False, 'execute': False},
        }
        for feature, operations in expected.items():
            for operation, household in operations.items():
                with self.subTest(feature=feature, operation=operation):
                    self.assertIs(allowed(feature=feature, operation=operation), household)
                    self.assertTrue(allowed(feature=feature, operation=operation, is_admin=True))
                    self.assertFalse(allowed(feature=feature, operation=operation, is_active=False, is_admin=True))
        self.assertEqual(access.default_feature_access(), {'schema': 1, 'features': {}})
        self.assertFalse(allowed(feature='energy_analysis', operation='execute', is_admin=True))

    def test_operations_are_independent_and_missing_inherits(self):
        value = policy('energy_tariff', visible={'mode': 'admin'}, execute={'mode': 'household'})
        self.assertFalse(allowed(value, 'energy_tariff', 'visible'))
        self.assertTrue(allowed(value, 'energy_tariff', 'read'))
        self.assertTrue(allowed(value, 'energy_tariff', 'execute'))
        value = policy(visible={'mode': 'household'}, read={'mode': 'admin'})
        self.assertTrue(allowed(value, operation='visible'))
        self.assertFalse(allowed(value))
        self.assertFalse(allowed(value, operation='execute'))
        self.assertFalse(allowed(policy(read={'mode': 'inherit'})))
        self.assertTrue(allowed(policy('energy_analysis', read={'mode': 'inherit'}), 'energy_analysis'))

    def test_selected_users_revocation_empty_and_admin_repair(self):
        value = policy(read={'mode': 'selected_users', 'user_ids': ['alice']})
        self.assertTrue(allowed(value))
        self.assertFalse(allowed(value, user_id='bob'))
        self.assertFalse(allowed(value, is_active=False))
        value['features']['energy_reports']['read']['user_ids'] = []
        self.assertFalse(allowed(value))
        self.assertTrue(allowed(value, is_admin=True))
        self.assertFalse(allowed(policy(read={'mode': 'selected_users'})))
        self.assertTrue(allowed(policy(visible={'mode': 'selected_users', 'user_ids': []}),
                                operation='visible', is_admin=True))

    def test_identity_and_flags_are_strict(self):
        for uid in (None, '', ' alice', 'alice ', True, 7, 'a\x00b', 'x' * 129):
            self.assertFalse(allowed(feature='energy_analysis', user_id=uid, is_admin=True))
        for active in (False, None, 1, 'true'):
            self.assertFalse(allowed(feature='energy_analysis', is_active=active, is_admin=True))
        for admin in (False, None, 1, 'true'):
            self.assertFalse(allowed(is_admin=admin))

    def test_detached_normalization_and_no_input_mutation(self):
        value = policy(read={'mode': 'selected_users', 'user_ids': ['bob', 'alice', 'bob']})
        before = copy.deepcopy(value)
        normalized = access.validate_feature_access(value)
        self.assertEqual(normalized['features']['energy_reports']['read']['user_ids'], ['alice', 'bob'])
        self.assertTrue(allowed(value))
        self.assertEqual(value, before)
        normalized['features']['energy_reports']['read']['user_ids'].append('eve')
        self.assertEqual(value, before)
        fresh = access.default_feature_access()
        fresh['features']['log_reports'] = {}
        self.assertEqual(access.default_feature_access()['features'], {})

    def test_projection_only_catalog_booleans(self):
        value = policy(read={'mode': 'selected_users', 'user_ids': ['alice']})
        projection = access.effective_feature_capabilities(value, user_id='alice', is_active=True, is_admin=False)
        self.assertEqual(set(projection), {'energy_analysis', 'energy_tariff', 'energy_reports', 'log_reports'})
        self.assertEqual(set(projection['energy_analysis']), {'visible', 'read'})
        self.assertTrue(projection['energy_reports']['read'])
        self.assertNotIn('alice', repr(projection))
        self.assertTrue(all(type(flag) is bool for ops in projection.values() for flag in ops.values()))
        inactive = access.effective_feature_capabilities(value, user_id=None, is_active=True, is_admin=True)
        self.assertFalse(any(flag for ops in inactive.values() for flag in ops.values()))

    def test_forbidden_catalog_and_unsupported_queries(self):
        for feature in ('smtp', 'smtp_config', 'list_secrets', 'settings', 'feature_access', 'core', 'panel', '', None, []):
            self.assertFalse(allowed(feature=feature, is_admin=True))
        for operation in ('delete', 'settings', '', None, []):
            self.assertFalse(allowed(operation=operation, is_admin=True))
        for feature in ('smtp', 'list_secrets', 'settings', 'feature_access'):
            self.assert_invalid(policy(feature, read={'mode': 'household'}))

    def assert_invalid(self, value):
        for check in (
            lambda: access.validate_feature_access(value),
            lambda: allowed(value, is_admin=True),
            lambda: access.effective_feature_capabilities(value, user_id='alice', is_active=True, is_admin=True),
        ):
            with self.assertRaisesRegex(ValueError, '^invalid_feature_access$'):
                check()

    def test_bad_schema_and_rule_shapes(self):
        for value in (None, [], {}, {'schema': 1}, {'schema': True, 'features': {}},
                      {'schema': 1.0, 'features': {}}, {'schema': 2, 'features': {}},
                      {'schema': 1, 'features': []},
                      {'schema': 1, 'features': [], 'validated': True},
                      {'schema': 1, 'features': {}, 'validated': True},
                      policy('unknown'), policy('energy_analysis', execute={'mode': 'household'}),
                      policy(read=[]), policy(read={}), policy(read={'mode': None}),
                      policy(read={'mode': []}), policy(read={'mode': 'all'}),
                      policy(read={'mode': 'household', 'user_ids': []}),
                      policy(read={'mode': 'admin', 'extra': True}),
                      policy(read={'mode': 'selected_users', 'user_ids': [], 'extra': True})):
            self.assert_invalid(value)

    def test_bad_user_lists_and_control_characters(self):
        for ids in ('alice', ('alice',), None, [True], [7], [''], [' alice'], ['alice '],
                    ['a\nb'], ['a\x7fb'], ['a\x85b'], ['x' * 129], ['alice'] * 129):
            self.assert_invalid(policy(read={'mode': 'selected_users', 'user_ids': ids}))
        opaque = 'user:opaque/ą'
        value = policy(read={'mode': 'selected_users', 'user_ids': [opaque]})
        self.assertTrue(allowed(value, user_id=opaque))
        self.assertTrue(allowed(policy(read={'mode': 'selected_users', 'user_ids': ['x' * 128]}), user_id='x' * 128))

    def test_bounded_json_cycles_size_depth_and_non_json(self):
        cycle = {}
        cycle['self'] = cycle
        nested = []
        cursor = nested
        for _ in range(70):
            child = []
            cursor.append(child)
            cursor = child
        for value in (cycle, nested, [None] * 65537, {'x' * 65537: None},
                      {'x': 'y' * 65537}, {'x': '\x00' * 12000},
                      {'x': float('nan')}, {'x': float('inf')}, {'x': object()},
                      {1: {}}, {'x': b'bytes'}, {'x': ('tuple',)}, {'x': 1 << 70000}):
            self.assert_invalid(value)
        # Individually valid rules whose aggregate encoding exceeds 64 KiB.
        ids = [str(i).zfill(4) + 'x' * 124 for i in range(128)]
        value = {'schema': 1, 'features': {
            feature: {operation: {'mode': 'selected_users', 'user_ids': ids}
                      for operation in ('visible', 'read', 'execute')}
            for feature in ('energy_tariff', 'energy_reports', 'log_reports')}}
        self.assert_invalid(value)

    def test_shared_input_lists_are_detached_without_false_cycle(self):
        rule = {'mode': 'selected_users', 'user_ids': ['alice']}
        value = policy(read=rule, visible=rule)
        normalized = access.validate_feature_access(value)
        normalized['features']['energy_reports']['read']['user_ids'].clear()
        self.assertEqual(normalized['features']['energy_reports']['visible']['user_ids'], ['alice'])
        self.assertEqual(rule['user_ids'], ['alice'])
