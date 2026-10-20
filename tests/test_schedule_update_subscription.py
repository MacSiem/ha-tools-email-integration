"""NOT_RUN. Real secured WS, feature storage and native source authorization; no private IO."""
import asyncio
import types
import unittest

from tests import test_report_status as report_status_tests


class ScheduleUpdateSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, *, read=True, visible=True, admin=True):
        ws, hass, storage, lane, conn, results, errors = report_status_tests.ReportStatusWebsocketTests.fixture(self)
        conn.user.is_admin = admin
        conn.user.permissions = types.SimpleNamespace(access_all_entities=lambda _: True,
                                                       check_entity=lambda *_: True)
        await storage.async_save_feature_access({'schema': 1, 'features': {
            'energy_analysis': {'read': {'mode': 'household'}},
            'energy_reports': {'visible': {'mode': 'household' if visible else 'admin'},
                               'read': {'mode': 'household' if read else 'admin'}}}}, expected_revision=0)
        handlers, received, tasks, source_checks = {}, [], [], []
        def listen(event, fn):
            handlers[event] = fn
            return lambda: handlers.pop(event, None)
        hass.bus = types.SimpleNamespace(async_listen=listen)
        def create(coro):
            task = asyncio.create_task(coro); tasks.append(task); return task
        hass.async_create_task = create
        conn.subscriptions = {}; conn.send_event = lambda *args: received.append(args)
        service = types.SimpleNamespace(_stopped=False, _update_subscriptions=set(), hook=None)
        def capture(kind, *, purpose):
            self.assertEqual(('day', 'report_read'), (kind, purpose))
            return {'ids': ['sensor.private_grid']}, None, {'revision': storage.current_feature_access_revision}
        def current(context):
            if context['revision'] != storage.current_feature_access_revision:
                raise RuntimeError('changed')
        async def require(context, config, uid, *, policy_state):
            source_checks.append(uid)
            await ws.async_require_energy_source_access(hass, uid, config['ids'],
                feature_policy=policy_state['policy'], feature='energy_reports', operation='read')
            if service.hook:
                await service.hook()
            current(context)
        service._capture = capture; service._assert_current = current; service._require_access = require
        hass.data['ha_tools_email']['energy_analysis'] = service
        await ws._ws_subscribe_analysis_updates(hass, conn, {'id': 9})
        self.assertEqual([], errors)
        def fire():
            handlers['ha_tools_email_schedules_changed'](types.SimpleNamespace(
                event_type='ha_tools_email_schedules_changed', data={'PRIVATE': 'ADDRESS/ID/RECIPIENT'}))
        async def drain():
            await asyncio.gather(*tasks, return_exceptions=True)
        return ws, hass, storage, conn, service, handlers, received, source_checks, fire, drain

    async def test_reason_is_only_boolean_and_burst_coalesced_after_fresh_source_and_reports_gates(self):
        ws, hass, storage, conn, service, handlers, received, checks, fire, drain = await self.fixture()
        fire(); fire(); await drain()
        self.assertEqual([(9, {'schedules_changed': True})], received)
        self.assertEqual(['synthetic'], checks)
        handlers['ha_tools_email_energy_analysis_updated'](types.SimpleNamespace(data={'PRIVATE': 'SECRET'}))
        self.assertEqual((9, {}), received[-1])
        self.assertNotIn('PRIVATE', repr(received))
        self.assertEqual([], storage._store.saved[1:])  # Only fixture policy write, never schedule/history IO.

    async def test_analysis_reader_cannot_learn_independently_denied_reports_reason(self):
        for permission in ('read', 'visible'):
            values = await self.fixture(admin=False, **{permission: False})
            ws, hass, storage, conn, service, handlers, received, checks, fire, drain = values
            fire(); await drain()
            self.assertEqual([(9, {})], received)
            self.assertEqual([], checks)  # No source/private lookup after Reports denial.

    async def test_policy_revoked_during_source_guard_withholds_reason(self):
        values = await self.fixture(admin=False)
        ws, hass, storage, conn, service, handlers, received, checks, fire, drain = values
        async def revoke():
            await storage.async_save_feature_access({'schema': 1, 'features': {
                'energy_analysis': {'read': {'mode': 'household'}}}},
                expected_revision=storage.current_feature_access_revision)
        service.hook = revoke
        fire(); await drain()
        self.assertEqual([(9, {})], received)
        self.assertEqual(['synthetic'], checks)

    async def test_native_source_revocation_withholds_reason_even_when_reports_policy_allows(self):
        values = await self.fixture(admin=False)
        ws, hass, storage, conn, service, handlers, received, checks, fire, drain = values
        conn.user.permissions.access_all_entities = lambda _: False
        conn.user.permissions.check_entity = lambda *_: False
        fire(); await drain()
        self.assertEqual([(9, {})], received)

    async def test_final_fresh_identity_and_lifecycle_fences_never_leak_reason(self):
        for edge in ('inactive', 'role', 'source_context', 'connection_identity', 'service_replaced', 'unsubscribe'):
            values = await self.fixture()
            ws, hass, storage, conn, service, handlers, received, checks, fire, drain = values
            async def mutate():
                if edge == 'inactive': hass.auth.current.is_active = False
                elif edge == 'role': hass.auth.current.is_admin = False
                elif edge == 'source_context':
                    def changed_context(_): raise RuntimeError('source changed')
                    service._assert_current = changed_context
                elif edge == 'connection_identity': conn.user = types.SimpleNamespace(id='other', is_active=True)
                elif edge == 'service_replaced': hass.data['ha_tools_email']['energy_analysis'] = object()
                else: conn.subscriptions[9]()
            service.hook = mutate
            fire()
            if edge == 'unsubscribe':
                # Cancellation is lifecycle-owned; no late event can be published.
                await drain()
            else:
                await drain()
            self.assertEqual([] if edge in ('service_replaced', 'unsubscribe') else [(9, {})], received)

    async def test_delegated_reports_reader_without_native_admin_gets_only_empty_invalidation(self):
        ws, hass, storage, conn, service, handlers, received, checks, fire, drain = await self.fixture(admin=False)
        fire(); await drain()
        self.assertEqual([(9, {})], received)
        self.assertEqual(['synthetic'], checks)
