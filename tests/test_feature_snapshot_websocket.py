"""Legacy snapshot must pass a concrete source guard into every request."""
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch
import sys
from tests import test_tariff_book_websocket as fixtures


@contextmanager
def native_permissions():
    core = types.ModuleType('homeassistant.core')
    core.valid_entity_id = lambda sid: ':' not in sid and len(sid.split('.')) == 2
    const = types.ModuleType('homeassistant.auth.permissions.const')
    const.POLICY_READ = 'read'
    with patch.dict(sys.modules, {core.__name__: core, const.__name__: const}):
        yield


class FeatureSnapshotWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws, hass, storage, lane, conn, results, errors = fixtures.BookWebsocketTest.fixture(self)
        conn.user.is_admin = False
        allowed = {'sensor.allowed'}
        conn.user.permissions = types.SimpleNamespace(access_all_entities=lambda _: False,
                                                       check_entity=lambda sid, _: sid in allowed)
        calls, private = [], []
        lane.after_read = None
        lane.skip_guard = False
        async def snapshot(**kwargs):
            calls.append(kwargs)
            if not lane.skip_guard:
                self.assertTrue(callable(kwargs.get('guard')), 'WS must provide the IO guard')
                await kwargs['guard'](['sensor.allowed'])
            private.append('read')
            if lane.after_read:
                await lane.after_read()
            return {'safe': True}
        lane.async_snapshot = snapshot
        return ws, hass, storage, lane, conn, results, errors, calls, private, allowed

    async def test_allowed_household_preserves_override_and_uses_authenticated_identity(self):
        ws, hass, storage, lane, conn, results, errors, calls, private, allowed = self.fixture()
        identities = []
        original = hass.auth.async_get_user
        async def auth(uid):
            identities.append(uid)
            return await original(uid)
        hass.auth.async_get_user = auth
        with native_permissions():
            await ws._ws_energy_snapshot(hass, conn, {'id': 1, 'profile': None, 'user_id': 'untrusted'})
        self.assertEqual([], errors)
        self.assertEqual({'safe': True}, results[-1][1])
        self.assertIsNone(calls[0]['profile'])
        self.assertEqual(['read'], private)
        self.assertTrue(identities)
        self.assertEqual({'synthetic'}, set(identities))

    async def test_tool_denial_prevents_start_and_visibility_is_not_read(self):
        ws, hass, storage, lane, conn, results, errors, calls, private, allowed = self.fixture()
        await storage.async_save_feature_access({'schema': 1, 'features': {'energy_analysis': {
            'visible': {'mode': 'household'}, 'read': {'mode': 'admin'}}}}, expected_revision=0)
        with native_permissions():
            await ws._ws_energy_snapshot(hass, conn, {'id': 1})
        self.assertEqual([], calls)
        self.assertEqual([], private)
        self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[-1][1])

    async def test_native_source_denial_prevents_private_io_even_with_feature_allowed(self):
        ws, hass, storage, lane, conn, results, errors, calls, private, allowed = self.fixture()
        allowed.clear()
        with native_permissions():
            await ws._ws_energy_snapshot(hass, conn, {'id': 1})
        self.assertEqual([], private)
        self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[-1][1])

    async def test_native_or_policy_revocation_during_snapshot_cannot_publish(self):
        for mutation in ('native', 'policy'):
            with self.subTest(mutation=mutation):
                ws, hass, storage, lane, conn, results, errors, calls, private, allowed = self.fixture()
                async def revoke():
                    if mutation == 'native':
                        allowed.clear()
                    else:
                        await storage.async_save_feature_access({'schema': 1, 'features': {
                            'energy_analysis': {'read': {'mode': 'admin'}}}}, expected_revision=0)
                lane.after_read = revoke
                with native_permissions():
                    await ws._ws_energy_snapshot(hass, conn, {'id': 1})
                self.assertEqual(['read'], private)
                self.assertEqual([], results)
                self.assertIn(errors[-1][1], ('unauthorized', 'energy_unavailable'))

    async def test_coordinator_that_omits_required_guard_cannot_publish(self):
        ws, hass, storage, lane, conn, results, errors, calls, private, allowed = self.fixture()
        lane.skip_guard = True
        with native_permissions():
            await ws._ws_energy_snapshot(hass, conn, {'id': 1})
        self.assertEqual([], results)
        self.assertEqual('energy_unavailable', errors[-1][1])
