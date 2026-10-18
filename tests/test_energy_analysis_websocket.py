"""Detailed schema2 route must use authenticated identity and final context fences."""
import unittest
from tests import test_tariff_book_websocket as fixtures


class AnalysisWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws, hass, storage, lane, connection, results, errors = fixtures.BookWebsocketTest().fixture()
        connection.user.is_admin = False
        calls = []
        class Analysis:
            valid = True
            validation_result = True
            get_hook = None
            validate_hook = None
            async def async_get_analysis(self, kind, user_id):
                calls.append((kind, user_id))
                if self.get_hook:
                    self.get_hook()
                return {'schema': 2, 'snapshot_id': 'synthetic', 'totals': {'kwh': '3', 'total_cost': None}}
            async def async_validate_result(self, result, user_id):
                user = await hass.auth.async_get_user(user_id)
                if user is None or not user.is_active:
                    raise PermissionError('denied')
                if self.validate_hook:
                    self.validate_hook()
                return self.validation_result
            def context_is_current(self, result):
                return self.valid
        service = Analysis(); hass.data['ha_tools_email']['energy_analysis'] = service
        return ws, hass, service, connection, results, errors, calls

    async def test_household_read_uses_authenticated_identity_and_requested_period(self):
        ws, hass, service, connection, results, errors, calls = self.fixture()
        await ws._ws_energy_analysis(hass, connection, {'id': 1, 'period': 'year', 'user_id': 'untrusted-input'})
        self.assertEqual([], errors)
        self.assertEqual([('year', 'synthetic')], calls)
        self.assertEqual('3', results[-1][1]['totals']['kwh'])
        await ws._ws_energy_analysis(hass, connection, {'id': 2})
        self.assertEqual(('day', 'synthetic'), calls[-1])

    async def test_inactive_user_never_starts_analysis(self):
        ws, hass, service, connection, results, errors, calls = self.fixture()
        connection.user.is_active = False
        await ws._ws_energy_analysis(hass, connection, {'id': 1})
        self.assertEqual([], calls)
        self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[-1][1])

    async def test_role_revoked_during_calculation_does_not_return_detailed_data(self):
        ws, hass, service, connection, results, errors, calls = self.fixture()
        service.get_hook = lambda: setattr(hass.auth.current, 'is_active', False)
        await ws._ws_energy_analysis(hass, connection, {'id': 1})
        self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[-1][1])

    async def test_replaced_service_or_changed_context_after_final_await_cannot_send(self):
        for replacement in (True, False):
            ws, hass, service, connection, results, errors, calls = self.fixture()
            if replacement:
                service.validate_hook = lambda: hass.data['ha_tools_email'].update(energy_analysis=object())
            else:
                service.validate_hook = lambda: setattr(service, 'valid', False)
            await ws._ws_energy_analysis(hass, connection, {'id': 1})
            self.assertEqual([], results)
            self.assertEqual('energy_analysis_unavailable', errors[-1][1])

    async def test_failed_async_validation_is_rejected_even_when_sync_context_matches(self):
        ws, hass, service, connection, results, errors, calls = self.fixture()
        service.validation_result = False
        await ws._ws_energy_analysis(hass, connection, {'id': 1})
        self.assertEqual([], results)
        self.assertEqual('energy_analysis_unavailable', errors[-1][1])

    async def test_source_denial_or_private_exception_is_mapped_to_safe_error(self):
        for denied in (True, False):
            ws, hass, service, connection, results, errors, calls = self.fixture()
            def fail():
                if denied:
                    raise PermissionError('synthetic-private-reference')
                raise ValueError('synthetic-private-reference')
            service.validate_hook = fail
            await ws._ws_energy_analysis(hass, connection, {'id': 1})
            self.assertEqual([], results)
            self.assertEqual('unauthorized' if denied else 'energy_analysis_unavailable', errors[-1][1])
            self.assertNotIn('synthetic-private-reference', str(errors))


if __name__ == '__main__':
    unittest.main()
