"""Feature policy writes are additive, optimistic, confirmed and admin-guardable."""
import copy
import types
import unittest
from tests.test_tariff_profile import load_storage

DEFAULT = {'schema': 1, 'features': {}}
SELECTED = {'schema': 1, 'features': {'energy_analysis': {
    'read': {'mode': 'selected_users', 'user_ids': ['synthetic-household']}}}}


class FeatureAccessStorageTest(unittest.IsolatedAsyncioTestCase):
    def storage(self):
        module = load_storage()
        hass = types.SimpleNamespace(data={}, state='running',
            config=types.SimpleNamespace(path=lambda *args: self.fail('Policy IO must not migrate SMTP')))
        return module, module.EmailStorage(hass)

    async def test_missing_policy_inherits_existing_roles_without_write_or_smtp_read(self):
        module, storage = self.storage()
        storage._store.loaded = {'smtp_config': {'password': '!secret synthetic'}, 'future': [1]}
        self.assertIsNone(storage.current_feature_access_revision)
        state = await storage.async_get_feature_access()
        self.assertEqual({'policy': DEFAULT, 'revision': 0, 'updated_at': None, 'status': 'ready'}, state)
        self.assertEqual(0, storage.current_feature_access_revision)
        self.assertEqual([], storage._store.saved)
        state['policy']['features']['caller_mutation'] = {}
        self.assertEqual(DEFAULT, (await storage.async_get_feature_access())['policy'])

    async def test_confirmed_save_restart_preserves_foreign_state_and_independent_tariff_revision(self):
        module, storage = self.storage()
        foreign = {'smtp_config': {'password': '!secret synthetic'}, 'tariff_book': {'opaque': 'keep'},
                   'tariff_profile': {'schema': 0}, 'tariff_revision': 8, 'schedules': [{'id': 'keep'}],
                   'attempt_history': {'keep': True}, 'future': {'nested': [1]}}
        storage._store.loaded = copy.deepcopy(foreign)
        saved = await storage.async_save_feature_access(SELECTED, expected_revision=0)
        self.assertEqual(SELECTED, saved['policy'])
        self.assertEqual('ready', saved['status'])
        self.assertEqual(1, saved['revision'])
        self.assertEqual(1, storage.current_feature_access_revision)
        self.assertEqual(8, storage.current_tariff_revision)
        for key, value in foreign.items():
            self.assertEqual(value, storage._store.saved[-1][key], key)
        restarted = type(storage)(storage.hass)
        restarted._store.loaded = copy.deepcopy(storage._store.saved[-1])
        self.assertEqual(saved, await restarted.async_get_feature_access())
        saved['policy']['features'].clear()
        self.assertEqual(SELECTED, (await storage.async_get_feature_access())['policy'])
        with self.assertRaises(module.FeatureAccessRevisionConflict):
            await storage.async_save_feature_access(DEFAULT, expected_revision=0)
        self.assertEqual(1, len(storage._store.saved))

    async def test_swallowed_lower_write_failure_does_not_publish_policy_or_revision(self):
        module, storage = self.storage()
        old = await storage.async_get_feature_access()
        storage._store.swallowed_failure = True
        with self.assertRaises(module.StorageWriteError):
            await storage.async_save_feature_access(SELECTED, expected_revision=0)
        self.assertEqual(old, await storage.async_get_feature_access())
        self.assertEqual(0, storage.current_feature_access_revision)
        self.assertEqual([], storage._store.saved)

    async def test_fresh_authorization_callback_can_prevent_write_after_await(self):
        module, storage = self.storage()
        async def denied():
            return False
        with self.assertRaises(PermissionError):
            await storage.async_save_feature_access(SELECTED, expected_revision=0, before_save=denied)
        self.assertEqual([], storage._store.saved)
        self.assertEqual(DEFAULT, (await storage.async_get_feature_access())['policy'])
        async def allowed():
            return True
        saved = await storage.async_save_feature_access(SELECTED, expected_revision=0, before_save=allowed)
        self.assertEqual(1, saved['revision'])

    async def test_invalid_stored_policy_is_explicit_and_repairable_without_silent_permissions(self):
        module, storage = self.storage()
        raw = {'schema': 99, 'future': {'keep': True}}
        storage._store.loaded = {'feature_access': copy.deepcopy(raw), 'feature_access_revision': 5}
        state = await storage.async_get_feature_access()
        self.assertEqual('invalid_configuration', state['status'])
        self.assertEqual({'schema': 0}, state['policy'])
        self.assertEqual(5, state['revision'])
        self.assertEqual(raw, storage._data['feature_access'])
        self.assertEqual([], storage._store.saved)
        saved = await storage.async_save_feature_access(DEFAULT, expected_revision=5)
        self.assertEqual('ready', saved['status'])
        self.assertEqual(6, saved['revision'])
        with self.assertRaises(ValueError):
            await storage.async_save_feature_access({'schema': 2}, expected_revision=6)
        self.assertEqual(saved, await storage.async_get_feature_access())


if __name__ == '__main__':
    unittest.main()
