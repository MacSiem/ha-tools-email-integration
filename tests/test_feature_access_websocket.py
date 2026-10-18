"""Actual policy routes: fresh identity, confirmed writes and private allowlists."""
import types
import unittest
from tests import test_tariff_book_websocket as fixtures
from tests.test_feature_access_storage import DEFAULT, SELECTED


class FeatureAccessWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws, hass, storage, lane, conn, results, errors = fixtures.BookWebsocketTest.fixture(self)
        events = []
        hass.bus = types.SimpleNamespace(async_fire=lambda *args: events.append(args))
        # Policy administration belongs to this tool without requiring Energy.
        hass.data['ha_tools_email'].pop('energy_snapshot')
        hass.config = types.SimpleNamespace(path=lambda *args: self.fail('No SMTP migration'))
        return ws, hass, storage, conn, results, errors, events

    async def test_admin_policy_save_read_and_stale_revision(self):
        ws, hass, storage, conn, results, errors, events = self.fixture()
        await ws._ws_set_feature_access(hass, conn, {'id': 1, 'policy': SELECTED, 'expected_revision': 0})
        self.assertEqual([], errors)
        self.assertEqual(1, results[-1][1]['revision'])
        self.assertEqual(SELECTED, results[-1][1]['policy'])
        self.assertEqual([('ha_tools_email_feature_access_updated', {'revision': 1})], events)
        await ws._ws_get_feature_access(hass, conn, {'id': 2})
        self.assertEqual(results[0][1], results[-1][1])
        await ws._ws_set_feature_access(hass, conn, {'id': 3, 'policy': DEFAULT, 'expected_revision': 0})
        self.assertEqual('feature_access_revision_conflict', errors[-1][1])
        self.assertEqual(1, len(storage._store.saved))
        self.assertEqual(1, len(events))

    async def test_household_cannot_read_or_change_policy_but_gets_own_capabilities(self):
        ws, hass, storage, conn, results, errors, events = self.fixture()
        await storage.async_save_feature_access(SELECTED, expected_revision=0)
        conn.user.is_admin = False
        await ws._ws_get_feature_access(hass, conn, {'id': 1})
        await ws._ws_set_feature_access(hass, conn, {'id': 2, 'policy': DEFAULT, 'expected_revision': 1})
        self.assertEqual(['unauthorized', 'unauthorized'], [e[1] for e in errors])
        await ws._ws_feature_capabilities(hass, conn, {'id': 3, 'user_id': 'synthetic-household'})
        projected = results[-1][1]
        self.assertEqual(1, projected['revision'])
        self.assertFalse(projected['capabilities']['energy_analysis']['read'])
        self.assertTrue(projected['capabilities']['energy_analysis']['visible'])
        self.assertNotIn('synthetic-household', repr(projected))
        self.assertNotIn('policy', projected)
        self.assertEqual(1, len(storage._store.saved))
        self.assertEqual([], events)

    async def test_admin_revoked_during_load_cannot_write(self):
        ws, hass, storage, conn, results, errors, events = self.fixture()
        original = storage._store.async_load
        async def load():
            loaded = await original()
            conn.user.is_admin = False
            return loaded
        storage._store.async_load = load
        await ws._ws_set_feature_access(hass, conn, {'id': 1, 'policy': SELECTED, 'expected_revision': 0})
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], results)
        self.assertEqual([], storage._store.saved)
        self.assertEqual([], events)

    async def test_unloaded_during_final_admin_check_never_publishes_private_policy(self):
        ws, hass, storage, conn, results, errors, events = self.fixture()
        calls = 0
        async def get_user(uid):
            nonlocal calls
            calls += 1
            if calls == 2:
                hass.data['ha_tools_email']['storage'] = object()
            return conn.user
        hass.auth.async_get_user = get_user
        await ws._ws_get_feature_access(hass, conn, {'id': 1})
        self.assertEqual([], results)
        self.assertEqual('feature_access_changed', errors[-1][1])

    async def test_revoked_user_and_changed_policy_after_read_do_not_publish_capabilities(self):
        for mutation in ('revoke', 'policy'):
            with self.subTest(mutation=mutation):
                ws, hass, storage, conn, results, errors, events = self.fixture()
                calls = 0
                async def get_user(uid):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if mutation == 'revoke':
                            conn.user.is_active = False
                        else:
                            await storage.async_save_feature_access(SELECTED, expected_revision=0)
                    return conn.user
                hass.auth.async_get_user = get_user
                await ws._ws_feature_capabilities(hass, conn, {'id': 1})
                self.assertEqual([], results)
                self.assertEqual('unauthorized' if mutation == 'revoke' else 'feature_access_changed', errors[-1][1])

    async def test_failed_write_has_safe_error_no_event_or_new_revision(self):
        ws, hass, storage, conn, results, errors, events = self.fixture()
        storage._store.swallowed_failure = True
        await ws._ws_set_feature_access(hass, conn, {'id': 1, 'policy': SELECTED, 'expected_revision': 0})
        self.assertEqual('feature_access_save_failed', errors[-1][1])
        self.assertNotIn('lower write', repr(errors))
        self.assertEqual([], results)
        self.assertEqual([], events)
        self.assertEqual(0, storage.current_feature_access_revision)

    async def test_invalid_persisted_policy_denies_capabilities_but_admin_can_repair(self):
        ws, hass, storage, conn, results, errors, events = self.fixture()
        storage._store.loaded = {'feature_access': {'schema': 99}}
        await ws._ws_feature_capabilities(hass, conn, {'id': 1})
        self.assertEqual([], errors)
        self.assertEqual('invalid_configuration', results[-1][1]['status'])
        self.assertFalse(any(flag for ops in results[-1][1]['capabilities'].values() for flag in ops.values()))
        await ws._ws_get_feature_access(hass, conn, {'id': 2})
        self.assertEqual({'schema': 0}, results[-1][1]['policy'])
        await ws._ws_set_feature_access(hass, conn, {'id': 3, 'policy': DEFAULT, 'expected_revision': 0})
        self.assertEqual('ready', results[-1][1]['status'])
        self.assertEqual(1, storage.current_feature_access_revision)
