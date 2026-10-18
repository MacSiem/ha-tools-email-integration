"""Execute book WebSocket routes with real storage and explicit auth IO."""
import types
import unittest
from tests.test_tariff_profile import load_websocket
from tests.test_tariff_history import book


class BookWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws, mod = load_websocket()
        user = types.SimpleNamespace(id='synthetic', is_active=True, is_admin=True)
        auth = types.SimpleNamespace(current=user)
        async def get_user(uid):
            return auth.current
        auth.async_get_user = get_user
        class Lane:
            running = True
            hook = None
            async def _async_run_cpu(self, job):
                result = job()
                if self.hook:
                    self.hook()
                return result
            async def _async_tick(self, now):
                pass
        lane = Lane()
        hass = types.SimpleNamespace(data={'ha_tools_email': {'energy_snapshot': lane}}, auth=auth,
                    state='running', async_create_task=lambda coro: coro.close())
        storage = mod.EmailStorage(hass)
        hass.data['ha_tools_email']['storage'] = storage
        results, errors = [], []
        connection = types.SimpleNamespace(user=user, send_result=lambda *args: results.append(args),
                                            send_error=lambda *args: errors.append(args))
        return ws, hass, storage, lane, connection, results, errors

    async def test_admin_preview_returns_changes_but_never_saves(self):
        ws, hass, storage, lane, connection, results, errors = self.fixture()
        await ws._ws_preview_tariff_book(hass, connection,
            {'id': 1, 'book': book(), 'expected_revision': 0})
        self.assertEqual([], errors)
        self.assertEqual([], results[-1][1]['historical_changes'])
        self.assertEqual([], storage._store.saved)
        lane.hook = lambda: setattr(hass.auth.current, 'is_active', False)
        await ws._ws_preview_tariff_book(hass, connection,
            {'id': 2, 'book': book(), 'expected_revision': 0})
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual(1, len(results))

    async def test_admin_save_and_household_read_have_confirmed_revision(self):
        ws, hass, storage, lane, connection, results, errors = self.fixture()
        await ws._ws_set_tariff_book(hass, connection, {'id': 1, 'book': book(), 'expected_revision': 0})
        self.assertEqual([], errors)
        self.assertEqual(1, results[-1][1]['revision'])
        self.assertEqual(1, len(storage._store.saved))
        hass.auth.current.is_admin = False
        await ws._ws_get_tariff_book(hass, connection, {'id': 2})
        self.assertEqual(1, results[-1][1]['revision'])
        await ws._ws_set_tariff_book(hass, connection, {'id': 3, 'book': book(), 'expected_revision': 1})
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual(1, len(storage._store.saved))

    async def test_role_revoked_during_validation_never_writes(self):
        ws, hass, storage, lane, connection, results, errors = self.fixture()
        lane.hook = lambda: setattr(hass.auth.current, 'is_admin', False)
        await ws._ws_set_tariff_book(hass, connection, {'id': 1, 'book': book(), 'expected_revision': 0})
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], results)
        self.assertEqual([], storage._store.saved)

    async def test_replaced_coordinator_during_validation_never_writes(self):
        ws, hass, storage, lane, connection, results, errors = self.fixture()
        lane.hook = lambda: hass.data['ha_tools_email'].update(energy_snapshot=object())
        await ws._ws_set_tariff_book(hass, connection, {'id': 1, 'book': book(), 'expected_revision': 0})
        self.assertEqual([], results)
        self.assertEqual([], storage._store.saved)
        self.assertEqual('tariff_book_save_failed', errors[-1][1])

    async def test_stale_revision_and_historical_ack_have_distinct_safe_errors(self):
        ws, hass, storage, lane, connection, results, errors = self.fixture()
        await ws._ws_set_tariff_book(hass, connection, {'id': 1, 'book': book(), 'expected_revision': 0})
        await ws._ws_set_tariff_book(hass, connection, {'id': 2, 'book': book(), 'expected_revision': 0})
        self.assertEqual('tariff_revision_conflict', errors[-1][1])
        changed = book(); changed['tariffs'][0]['versions'][0]['profile']['zones'][0]['energyRate'] = 2
        await ws._ws_set_tariff_book(hass, connection, {'id': 3, 'book': changed, 'expected_revision': 1})
        self.assertEqual('historical_edit_ack_required', errors[-1][1])
        self.assertEqual(1, len(storage._store.saved))
        await ws._ws_set_tariff_book(hass, connection, {'id': 4, 'book': changed, 'expected_revision': 1,
                                                      'historical_edit_ack': True})
        self.assertEqual(2, results[-1][1]['revision'])
        self.assertEqual(2, len(storage._store.saved))
