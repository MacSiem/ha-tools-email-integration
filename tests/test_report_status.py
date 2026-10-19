"""Report-status projection and real storage/WS guards; no SMTP boundary called."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import types
import unittest
from unittest.mock import patch

from tests.test_attempt_history import record
from tests.test_tariff_book_websocket import BookWebsocketTest
from tests.test_tariff_profile import load_storage

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
ROOT_KEYS = {'schema', 'purpose', 'generated_at', 'schedule_state', 'next_report_at',
             'history_state', 'last_attempt'}


def schedule(**changes):
    return {'id': 'schedule-private', 'kind': 'energy_report', 'cadence': 'daily',
            'time': '15:00', 'enabled': True, 'label': 'PRIVATE_LABEL',
            'recipients': ['PRIVATE_ADDRESS@example.invalid'], **changes}


def data(rows=None, attempts=None):
    return {'schedules': rows if rows is not None else [schedule()],
            'attempt_history': {'schema': 1, 'attempts': attempts if attempts is not None else [record()]},
            'smtp_config': {'password': 'PRIVATE_SECRET', 'default_recipient': 'PRIVATE_DEFAULT@example.invalid'},
            'subject': 'PRIVATE_SUBJECT', 'body': 'PRIVATE_BODY'}


class ReportStatusProjectionTests(unittest.TestCase):
    def project(self, state, purpose='energy_report', now=NOW, consumed=None):
        return load_storage().project_report_status(state, purpose, now=now,
            local_timezone='Europe/Warsaw', consumed=consumed)

    def test_exact_allowlist_only_actual_attempt_count_and_no_private_fields(self):
        source = data(attempts=[record(subject='PRIVATE_HISTORY', recipient_count=0)])
        before = deepcopy(source)
        result = self.project(source)
        self.assertEqual(ROOT_KEYS, set(result))
        self.assertEqual({'timestamp', 'outcome', 'recipient_count'}, set(result['last_attempt']))
        self.assertEqual(0, result['last_attempt']['recipient_count'])
        self.assertEqual('accepted_by_smtp', result['last_attempt']['outcome'])
        self.assertEqual('2026-10-12T13:00:00+00:00', result['next_report_at'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('schedule-private', json.dumps(result))
        self.assertEqual(before, source)
        for count in (None, True, -1, 100001, '3'):
            self.assertIsNone(self.project(data(attempts=[record(recipient_count=count)]))['last_attempt']['recipient_count'])
        empty = self.project(data(attempts=[]))
        self.assertIsNone(empty['last_attempt'])  # Never infer a default recipient count.

    def test_purpose_aggregate_earliest_and_latest_are_deterministic(self):
        rows = [schedule(id='later', time='17:00'), schedule(id='first', time='14:00'),
                schedule(id='other', kind='log_digest', time='13:00')]
        attempts = [record(id='00000000-0000-4000-8000-000000000002', kind='log_digest',
                           timestamp=(NOW - timedelta(minutes=1)).isoformat()),
                    record(id='00000000-0000-4000-8000-000000000003', outcome='send_failed'),
                    record(id='00000000-0000-4000-8000-000000000001', outcome='accepted_by_smtp')]
        a = self.project(data(rows, attempts))
        b = self.project(data(list(reversed(rows)), list(reversed(attempts))))
        self.assertEqual(a, b)
        self.assertEqual('2026-10-12T15:00:00+00:00', a['next_report_at'])  # 14:00 today is exactly now.
        self.assertEqual('send_failed', a['last_attempt']['outcome'])
        log = self.project(data(rows, attempts), purpose='log_digest')
        self.assertEqual('2026-10-13T11:00:00+00:00', log['next_report_at'])
        self.assertEqual((NOW - timedelta(minutes=1)).isoformat(), log['last_attempt']['timestamp'])

    def test_disabled_unconfigured_and_bounded_unavailable_do_not_invent_zero(self):
        for rows, expected in (([], 'unconfigured'), ([schedule(enabled=False)], 'disabled'),
                               ([schedule()] * 129, 'unavailable'), ([schedule(time='bad')], 'unavailable'),
                               ([schedule(time='08:00'), schedule(time='bad')], 'unavailable')):
            result = self.project(data(rows, []))
            self.assertEqual(expected, result['schedule_state'])
            self.assertIsNone(result['next_report_at'])
            self.assertIsNone(result['last_attempt'])
        for history in ([], {'schema': 99}, {'schema': 1, 'attempts': [record()] * 129}):
            state = data(); state['attempt_history'] = history
            self.assertEqual('unavailable', self.project(state)['history_state'])
        for stamp in ((NOW + timedelta(seconds=1)).isoformat(), (NOW - timedelta(days=91)).isoformat()):
            self.assertIsNone(self.project(data(attempts=[record(timestamp=stamp)]))['last_attempt'])
        with self.assertRaises(ValueError):
            self.project(data(), now=NOW.replace(tzinfo=None))

    def test_dst_weekday_monthday_and_consumed_period_use_existing_scheduler(self):
        cases = [
            (schedule(time='02:30', timezone='Europe/Warsaw'), '2026-03-28T23:00:00+00:00', '2026-03-29T01:30:00+00:00'),
            (schedule(time='02:30', timezone='Europe/Warsaw'), '2026-10-24T22:00:00+00:00', '2026-10-25T00:30:00+00:00'),
            (schedule(time='02:30', timezone='Europe/Warsaw'), '2026-10-25T00:31:00+00:00', '2026-10-26T01:30:00+00:00'),
            (schedule(cadence='weekly', weekday=2, time='07:00', timezone='UTC'), NOW.isoformat(), '2026-10-14T07:00:00+00:00'),
            (schedule(cadence='monthly', month_day=31, time='07:00', timezone='UTC'), '2026-02-01T00:00:00+00:00', '2026-02-28T07:00:00+00:00'),
        ]
        for row, now, expected in cases:
            with self.subTest(now=now, row=row):
                self.assertEqual(expected, self.project(data([row], []), now=datetime.fromisoformat(now))['next_report_at'])
        row = schedule(time='15:00', timezone='UTC')
        state = data([row], [])
        state['scheduler_state'] = {row['id']: {'last_fired_period': 'daily:2026-10-12'}}
        self.assertEqual('2026-10-13T15:00:00+00:00', self.project(state)['next_report_at'])
        state['scheduler_state'] = {}
        self.assertEqual('2026-10-13T15:00:00+00:00', self.project(state, consumed={row['id']: 'daily:2026-10-12'})['next_report_at'])


class ReportStatusWebsocketTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        values = BookWebsocketTest.fixture(self)
        ws, hass, storage, lane, connection, results, errors = values
        hass.config = types.SimpleNamespace(time_zone='Europe/Warsaw', path=lambda *args: self.fail('SMTP file IO'))
        storage._store.loaded = data()
        async def forbidden(*args, **kwargs):
            self.fail('Report status must never read SMTP config or full schedule APIs')
        storage.async_get_smtp_config = storage.async_list_schedules = forbidden
        return values

    async def call(self, ws, hass, conn, purpose='energy_report'):
        with patch.object(ws, '_utcnow', return_value=NOW):
            await ws._ws_get_report_status(hass, conn, {'id': 1, 'purpose': purpose, 'user_id': 'untrusted'})

    async def test_read_only_unconfigured_smtp_and_command_registration(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        await self.call(ws, hass, conn)
        self.assertEqual([], errors)
        self.assertEqual(ROOT_KEYS, set(results[-1][1]))
        self.assertEqual([], storage._store.saved)
        self.assertFalse(getattr(ws._ws_get_report_status, 'tariff_requires_admin', False))
        self.assertNotIn('PRIVATE', json.dumps(results))
        registered = []
        ws.websocket_api.async_register_command = lambda hass, handler: registered.append(handler)
        ws.async_register_commands(hass)
        self.assertIn(ws._ws_get_report_status, registered)

    async def test_purpose_policy_default_admin_and_delegation_independent_of_visibility(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        conn.user.is_admin = False
        await self.call(ws, hass, conn)
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], results)
        await storage.async_save_feature_access({'schema': 1, 'features': {'energy_reports': {
            'visible': {'mode': 'admin'}, 'read': {'mode': 'selected_users', 'user_ids': ['synthetic']}}}}, expected_revision=0)
        errors.clear()
        await self.call(ws, hass, conn)
        self.assertEqual([], errors)
        await self.call(ws, hass, conn, 'log_digest')
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual(1, len(results))

    async def test_revoked_identity_or_policy_after_storage_read_cannot_publish(self):
        for mutation in ('inactive', 'role', 'policy'):
            with self.subTest(mutation=mutation):
                ws, hass, storage, lane, conn, results, errors = self.fixture()
                original = storage.async_get_report_status
                async def changed(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    if mutation == 'inactive':
                        hass.auth.current = types.SimpleNamespace(id='synthetic', is_active=False, is_admin=True)
                    elif mutation == 'role':
                        hass.auth.current = types.SimpleNamespace(id='synthetic', is_active=True, is_admin=False)
                    else:
                        await storage.async_save_feature_access({'schema': 1, 'features': {}}, expected_revision=0)
                    return result
                storage.async_get_report_status = changed
                await self.call(ws, hass, conn)
                self.assertEqual([], results)
                self.assertEqual('unauthorized', errors[-1][1])

    async def test_context_and_generation_changes_during_final_auth_never_publish(self):
        for mutation in ('storage', 'lane', 'stop', 'shutdown', 'schedule_generation', 'consumed', 'timezone', 'confirmed_write'):
            with self.subTest(mutation=mutation):
                ws, hass, storage, lane, conn, results, errors = self.fixture()
                calls = 0
                async def user(uid):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        bucket = hass.data['ha_tools_email']
                        if mutation == 'storage': bucket['storage'] = object()
                        elif mutation == 'lane': bucket['energy_snapshot'] = object()
                        elif mutation == 'stop': lane.running = False
                        elif mutation == 'shutdown': hass.state = 'stopping'
                        elif mutation == 'schedule_generation': bucket['schedule_generation'] = 9
                        elif mutation == 'consumed': bucket['schedule_consumed_periods'] = {'schedule-private': 'daily:2026-10-12'}
                        elif mutation == 'timezone': hass.config.time_zone = 'UTC'
                        else: await storage.async_append_attempt(record(id='00000000-0000-4000-8000-000000000099'), now=NOW)
                    return hass.auth.current
                hass.auth.async_get_user = user
                await self.call(ws, hass, conn)
                self.assertEqual([], results)
                self.assertIn(errors[-1][1], ('unauthorized', 'report_status_unavailable'))

    async def test_denial_before_status_io_and_safe_read_error(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        calls = []
        async def failed(*args, **kwargs):
            calls.append('read')
            raise OSError('PRIVATE filesystem/password/address')
        storage.async_get_report_status = failed
        conn.user.is_admin = False
        await self.call(ws, hass, conn)
        self.assertEqual([], calls)
        conn.user.is_admin = True
        await self.call(ws, hass, conn)
        self.assertEqual(['read'], calls)
        self.assertNotIn('PRIVATE', repr(errors))
        self.assertEqual([], results)
        await self.call(ws, hass, conn, 'manual')
        self.assertEqual('invalid_report_status', errors[-1][1])

    async def test_confirmed_changes_emit_empty_invalidation_and_failed_save_does_not(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        events = []
        hass.bus = types.SimpleNamespace(async_fire=lambda *args: events.append(args))
        await storage.async_append_attempt(record(), now=NOW)
        await storage.async_upsert_schedule(schedule(), author_user_id='synthetic')
        await storage.async_set_last_fired('schedule-private', 'daily:2026-10-12', NOW.isoformat())
        await storage.async_delete_schedule('schedule-private')
        self.assertEqual([('ha_tools_email_report_status_updated', {})] * 4, events)
        storage._store.fail = True
        with self.assertRaises(OSError):
            await storage.async_append_attempt(record(id='00000000-0000-4000-8000-000000000098'), now=NOW)
        self.assertEqual(4, len(events))
        self.assertNotIn('PRIVATE', repr(events))

    async def test_existing_subscription_only_emits_empty_invalidation_and_unsubscribes(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        handlers, removed, received = {}, [], []
        def listen(event, handler):
            handlers[event] = handler
            return lambda: removed.append(event)
        hass.bus = types.SimpleNamespace(async_listen=listen)
        service = types.SimpleNamespace(_stopped=False, _update_subscriptions=set())
        hass.data['ha_tools_email']['energy_analysis'] = service
        conn.subscriptions = {}
        conn.send_event = lambda *args: received.append(args)
        await ws._ws_subscribe_analysis_updates(hass, conn, {'id': 9})
        self.assertIn('ha_tools_email_report_status_updated', handlers)
        self.assertIn('ha_tools_email_schedules_changed', handlers)
        handlers['ha_tools_email_schedules_changed'](types.SimpleNamespace(data={'schedules': ['PRIVATE_ADDRESS']}))
        handlers['ha_tools_email_report_status_updated'](types.SimpleNamespace(data={'PRIVATE': 'SECRET'}))
        self.assertEqual([(9, {}), (9, {})], received)
        conn.subscriptions[9]()
        conn.subscriptions[9]()
        self.assertEqual(len(handlers), len(removed))
        self.assertFalse(service._update_subscriptions)
