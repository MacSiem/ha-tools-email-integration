"""Feature rules apply to legacy tariff read, pricing and explicit edits too."""
import unittest
from tests import test_tariff_book_websocket as fixtures
from tests.test_tariff_history import book
from tests.test_tariff_profile import profile


class FeatureTariffWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        return fixtures.BookWebsocketTest.fixture(self)

    async def test_denied_tariff_read_blocks_legacy_and_book_routes(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        await storage.async_save_feature_access({'schema': 1, 'features': {
            'energy_tariff': {'read': {'mode': 'admin'}}}}, expected_revision=0)
        conn.user.is_admin = False
        for handler in (ws._ws_get_tariff, ws._ws_get_tariff_book, ws._ws_energy_price_queries):
            await handler(hass, conn, {'id': 1, 'queries': []})
            self.assertTrue(errors)
            self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], results)
        self.assertEqual(1, len(storage._store.saved))

    async def test_default_admin_execute_and_selected_household_delegation(self):
        for method, payload in (('_ws_set_tariff', {'profile': profile()}),
                                ('_ws_set_tariff_book', {'book': book()})):
            with self.subTest(method=method):
                ws, hass, storage, lane, conn, results, errors = self.fixture()
                conn.user.is_admin = False
                msg = {'id': 1, **payload, 'expected_revision': 0}
                await getattr(ws, method)(hass, conn, msg)
                self.assertEqual('unauthorized', errors[-1][1])
                self.assertEqual([], storage._store.saved)
                await storage.async_save_feature_access({'schema': 1, 'features': {
                    'energy_tariff': {'execute': {'mode': 'selected_users', 'user_ids': ['synthetic']}}}},
                    expected_revision=0)
                errors.clear()
                await getattr(ws, method)(hass, conn, msg)
                self.assertEqual([], errors)
                self.assertEqual(1, results[-1][1]['revision'])
                self.assertEqual(2, len(storage._store.saved))

    async def test_visibility_does_not_grant_execute_and_read_is_independent(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        await storage.async_save_feature_access({'schema': 1, 'features': {
            'energy_tariff': {'visible': {'mode': 'household'}, 'read': {'mode': 'admin'}}}},
            expected_revision=0)
        conn.user.is_admin = False
        await ws._ws_set_tariff(hass, conn, {'id': 1, 'profile': profile(), 'expected_revision': 0})
        self.assertEqual('unauthorized', errors[-1][1])
        await ws._ws_get_tariff(hass, conn, {'id': 2})
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], results)
        self.assertEqual(1, len(storage._store.saved))

    async def test_read_policy_revoked_during_store_io_never_returns_tariff(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        conn.user.is_admin = False
        original = storage.async_get_tariff
        async def read():
            state = await original()
            await storage.async_save_feature_access({'schema': 1, 'features': {
                'energy_tariff': {'read': {'mode': 'admin'}}}}, expected_revision=0)
            return state
        storage.async_get_tariff = read
        await ws._ws_get_tariff(hass, conn, {'id': 1})
        self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[-1][1])

    async def test_actor_role_revoked_during_legacy_store_load_cannot_write(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        # Policy load is the first Store await. Explicit execution must recheck
        # fresh role after every later validation/load before the write begins.
        async def get_user(uid):
            user = hass.auth.current
            get_user.calls += 1
            if get_user.calls == 2:
                user.is_admin = False
            return user
        get_user.calls = 0
        hass.auth.async_get_user = get_user
        await ws._ws_set_tariff(hass, conn, {'id': 1, 'profile': profile(), 'expected_revision': 0})
        self.assertEqual([], storage._store.saved)
        self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[-1][1])
