"""Transport uses native connection identity and fresh post-await admin checks."""
from unittest.mock import patch
import unittest

from tests import test_tariff_book_websocket as websocket_fixture


class StatisticsWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws, hass, storage, lane, connection, results, errors = websocket_fixture.BookWebsocketTest().fixture()
        api = ws.async_register_statistics_commands.__globals__
        calls = []
        class Publisher:
            hook = None
            async def async_status(self, actor):
                calls.append(actor)
                if self.hook: self.hook()
                return {'enabled': False, 'revision': 0}
            def _check(self): pass
        subject = Publisher(); hass.data['ha_tools_email']['energy_statistics'] = subject
        return ws, api, hass, connection, subject, calls, results, errors

    async def test_status_identity_comes_from_connection_not_message(self):
        ws, api, hass, conn, subject, calls, results, errors = self.fixture()
        with patch.dict(api, _publisher=lambda _: subject):
            await api['ws_statistics_status'](hass, conn, {'id': 1, 'user_id': 'forged-admin'})
        self.assertEqual(['synthetic'], calls); self.assertEqual([], errors)
        self.assertEqual(False, results[0][1]['enabled'])

    async def test_nonadmin_is_rejected_before_kernel_and_storage(self):
        ws, api, hass, conn, subject, calls, results, errors = self.fixture()
        conn.user.is_admin = False
        with patch.dict(api, _publisher=lambda _: subject):
            await api['ws_statistics_status'](hass, conn, {'id': 1})
        self.assertEqual([], calls); self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[0][1])

    async def test_revoke_or_replacement_after_kernel_await_never_sends_result(self):
        for revoke in (True, False):
            ws, api, hass, conn, subject, calls, results, errors = self.fixture()
            subject.hook = (lambda: setattr(conn.user, 'is_admin', False)) if revoke else (
                lambda: hass.data['ha_tools_email'].update(energy_statistics=object()))
            with patch.dict(api, _publisher=lambda _: subject):
                await api['ws_statistics_status'](hass, conn, {'id': 1})
            self.assertEqual([], results)
            self.assertEqual('unauthorized' if revoke else 'energy_statistics_unavailable', errors[0][1])

    async def test_all_five_routes_are_registered_without_creating_kernel(self):
        ws, api, hass, conn, subject, calls, results, errors = self.fixture()
        hass.data['ha_tools_email'].pop('energy_statistics')
        commands = []
        with patch.object(ws.websocket_api, 'async_register_command', lambda h, fn: commands.append(fn), create=True):
            ws.async_register_commands(hass)
        for name in ('status', 'prepare', 'enable', 'disable', 'refresh'):
            function = api['ws_statistics_'+name]
            self.assertIn(function, commands)
            self.assertTrue(getattr(function, 'tariff_requires_admin', False))
        self.assertNotIn('energy_statistics', hass.data['ha_tools_email'])
