"""Calendar persistence, real scheduler callbacks and guarded readonly APIs.

Only HA boundaries are stubbed. Run through the owner's FIFO after integration.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

try:
    from tests.test_tariff_profile import load_storage
except ImportError:
    from test_tariff_profile import load_storage

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
UTC = timezone.utc
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


def load_scheduler():
    spec = importlib.util.spec_from_file_location('schedule_v3_scheduler', ROOT / 'scheduler.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def schedule(**changes):
    row = dict(id='one', kind='log_digest', cadence='daily', time='07:30',
               enabled=True, recipients=['test@example.invalid'])
    row.update(changes)
    return row


def store():
    mod = load_storage()
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *a: (_ for _ in ()).throw(AssertionError('no SMTP migration'))))
    return mod, mod.EmailStorage(hass)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.s = load_scheduler()

    def test_country_timezone_overrides_input_clock_and_legacy_uses_ha(self):
        now = datetime(2026, 1, 1, 22, tzinfo=UTC)
        fire = self.s.calculate_next_fire(schedule(timezone='Asia/Tokyo'), now, local_timezone='America/New_York')
        self.assertEqual('2026-01-02T07:30:00+09:00', fire.isoformat())
        legacy = self.s.calculate_next_fire(schedule(), now, local_timezone='America/New_York')
        self.assertEqual('2026-01-02T07:30:00-05:00', legacy.isoformat())
        self.assertEqual('daily:2026-01-02', self.s.fire_period_key(schedule(timezone='Asia/Tokyo'), now))

    def test_spring_gap_shift_and_autumn_first_occurrence_only(self):
        row = schedule(time='02:30', timezone='Europe/Warsaw')
        gap = self.s.calculate_next_fire(row, datetime(2026, 3, 28, 22, tzinfo=UTC))
        self.assertEqual('2026-03-29T03:30:00+02:00', gap.isoformat())
        self.assertTrue(self.s.should_fire_schedule(row, gap))
        first = self.s.calculate_next_fire(row, datetime(2026, 10, 24, 22, tzinfo=UTC))
        self.assertEqual('2026-10-25T02:30:00+02:00', first.isoformat())
        self.assertTrue(self.s.should_fire_schedule(row, first))
        self.assertFalse(self.s.should_fire_schedule(row, first.astimezone(UTC) + timedelta(hours=1)))
        after = self.s.calculate_next_fire(row, first.astimezone(UTC) + timedelta(minutes=1))
        self.assertEqual('2026-10-26', after.date().isoformat())

    def test_dst_23_25_hours_and_non_hour_transition(self):
        for day, hours in ((datetime(2026, 3, 28, 12, tzinfo=UTC), 23),
                           (datetime(2026, 10, 24, 12, tzinfo=UTC), 25)):
            row = schedule(timezone='Europe/Warsaw', time='08:00')
            a = self.s.calculate_next_fire(row, day - timedelta(days=1))
            b = self.s.calculate_next_fire(row, a)
            self.assertEqual(hours * 3600, (b.astimezone(UTC) - a.astimezone(UTC)).total_seconds())
        row = schedule(timezone='Australia/Lord_Howe', time='02:15')
        fire = self.s.calculate_next_fire(row, datetime(2026, 10, 3, 12, tzinfo=UTC))
        self.assertEqual('2026-10-04T02:45:00+11:00', fire.isoformat())

    def test_weekly_and_month_clamp_same_in_next_and_callback(self):
        row = schedule(cadence='weekly', weekday=6, timezone='Pacific/Auckland')
        fire = self.s.calculate_next_fire(row, datetime(2026, 2, 1, tzinfo=UTC))
        self.assertEqual(6, fire.weekday())
        self.assertTrue(self.s.should_fire_schedule(row, fire))
        row = schedule(cadence='monthly', month_day=31, timezone='Asia/Tokyo')
        fire = self.s.calculate_next_fire(row, datetime(2026, 2, 1, tzinfo=UTC))
        self.assertEqual('2026-02-28', fire.date().isoformat())
        self.assertTrue(self.s.should_fire_schedule(row, fire))
        self.assertFalse(self.s.should_fire_schedule(row, fire + timedelta(days=1)))

    def test_completed_calendar_windows_and_leap_month(self):
        row = schedule(timezone='Europe/Warsaw')
        start, end = self.s.calendar_report_window(row, datetime(2026, 3, 30, 6, tzinfo=UTC))
        self.assertEqual(23, (end - start).total_seconds() / 3600)
        start, end = self.s.calendar_report_window(row, datetime(2026, 10, 26, 6, tzinfo=UTC))
        self.assertEqual(25, (end - start).total_seconds() / 3600)
        row = schedule(cadence='monthly', timezone='UTC')
        start, end = self.s.calendar_report_window(row, datetime(2028, 3, 12, tzinfo=UTC))
        self.assertEqual(('2028-02-01', '2028-03-01'), (start.date().isoformat(), end.date().isoformat()))


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_legacy_aliases_read_without_migration_or_writes(self):
        mod, s = store()
        original = {'smtp_config': {'future_smtp': 'keep'}, 'tariff_profile': {'schema': 0},
                    'attempt_history': {'schema': 1, 'attempts': []}, 'future': {'keep': True},
                    'digest_state': {'log_digest': {'daily': {'seen_keys': ['keep']}}},
                    'schedules': [schedule(cadence='weekly', weekly_day='sun', future_field=1),
                                  dict(schedule(cadence='monthly'), id='two', day=31)],
                    'scheduler_state': {'one': {'last_fired_period': 'weekly:2026-W41', 'future': 1}}}
        s._store.loaded = copy.deepcopy(original)
        rows = await s.async_list_schedules()
        self.assertEqual((6, 31), (rows[0]['weekday'], rows[1]['month_day']))
        await s.async_get_digest_state('log_digest', 'daily')
        self.assertEqual([], s._store.saved)
        self.assertEqual(original['schedules'], s._data['schedules'])
        saved = await s.async_upsert_schedule(dict(rows[0], timezone='America/New_York', weekday=4))
        self.assertEqual((4, 'America/New_York', 1), (saved['weekday'], saved['timezone'], saved['future_field']))
        snapshot = s._store.saved[-1]
        for key in ('smtp_config', 'tariff_profile', 'attempt_history', 'future', 'digest_state', 'scheduler_state'):
            self.assertEqual(original[key], snapshot[key])
        self.assertEqual(original['schedules'][1], snapshot['schedules'][1])
        _, restarted = store()
        restarted._store.loaded = snapshot
        self.assertEqual('America/New_York', (await restarted.async_get_schedule('one'))['timezone'])
        self.assertEqual([], restarted._store.saved)

    async def test_malformed_rejects_before_any_store_write(self):
        for changes in ({'timezone': 'No/SuchZone'}, {'timezone': ''}, {'timezone': True},
                        {'timezone': '../UTC'}, {'weekday': True}, {'weekday': 7},
                        {'weekday': 'nonsense'}, {'month_day': 0}, {'month_day': 32},
                        {'month_day': '2'}, {'month_day': True}, {'month_day': None},
                        {'weekday': 1, 'weekly_day': 6}, {'month_day': 2, 'day': 3}):
            with self.subTest(changes=changes):
                _, s = store()
                s._store.loaded = {'schedules': [], 'future': 1}
                with self.assertRaises(ValueError):
                    await s.async_upsert_schedule(schedule(**changes))
                self.assertEqual([], s._store.saved)

    async def test_idless_legacy_projection_is_stable_and_never_persisted_on_read(self):
        _, s = store()
        row = schedule()
        row.pop('id')
        s._store.loaded = {'schedules': [row]}
        first = (await s.async_list_schedules())[0]
        self.assertEqual(first['id'], (await s.async_list_schedules())[0]['id'])
        self.assertEqual(first, await s.async_get_schedule(first['id']))
        self.assertNotIn('id', s._data['schedules'][0])
        self.assertEqual([], s._store.saved)
        await s.async_upsert_schedule({'id': first['id'], 'time': '08:00'})
        self.assertEqual(1, len(s._data['schedules']))
        self.assertTrue(await s.async_delete_schedule(first['id']))
        self.assertEqual([], s._data['schedules'])

    async def test_patch_preserves_existing_fields_and_save_failure_does_not_publish(self):
        _, s = store()
        s._store.loaded = {'schedules': [schedule(timezone='Asia/Tokyo', weekday=5, month_day=30, future=2)]}
        saved = await s.async_upsert_schedule({'id': 'one', 'time': '09:00'})
        self.assertEqual(('Asia/Tokyo', 5, 30, ['test@example.invalid']),
                         tuple(saved[k] for k in ('timezone', 'weekday', 'month_day', 'recipients')))
        before = copy.deepcopy(s._data)
        s._store.fail = True
        with self.assertRaises(OSError):
            await s.async_upsert_schedule({'id': 'one', 'time': '10:00'})
        self.assertEqual(before, s._data)

    async def test_explicit_timezone_can_return_to_following_home_assistant(self):
        _, s = store()
        s._store.loaded = {'schedules': [schedule(timezone='Asia/Tokyo', future=1)], 'future': {'keep': True}}
        saved = await s.async_upsert_schedule({'id': 'one', 'use_ha_timezone': True})
        self.assertNotIn('timezone', saved)
        self.assertNotIn('use_ha_timezone', saved)
        self.assertEqual(1, saved['future'])
        self.assertEqual({'keep': True}, s._store.saved[-1]['future'])
        self.assertNotIn('timezone', (await s.async_get_schedule('one')))

    async def test_ha_timezone_choice_rejects_conflicts_and_malformed_flags_before_save(self):
        for changes in ({'use_ha_timezone': 'true'}, {'use_ha_timezone': None},
                        {'use_ha_timezone': 1}, {'use_ha_timezone': True, 'timezone': 'Asia/Tokyo'}):
            _, s = store()
            with self.assertRaises(ValueError):
                await s.async_upsert_schedule(schedule(**changes))
            self.assertEqual([], s._store.saved)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = load_scheduler()
        self.callbacks, self.unsubscribed, self.sends = [], [], []
        self.event = types.ModuleType('homeassistant.helpers.event')
        def track(hass, action, **kwargs):
            self.callbacks.append(action)
            self.assertEqual({'second': 0}, kwargs)
            return lambda: self.unsubscribed.append(action)
        self.event.async_track_time_change = track
        _, self.storage = store()
        self.row = schedule(timezone='Asia/Tokyo')
        self.storage._store.loaded = {'schedules': [self.row]}
        self.hass = types.SimpleNamespace(data={'ha_tools_email': {'storage': self.storage}},
                                         config=types.SimpleNamespace(time_zone='America/New_York'))
        self.stub = patch.dict(sys.modules, {self.event.__name__: self.event})
        self.stub.start()
        self.sendstub = patch.object(self.s, 'async_send_schedule', self.send)
        self.sendstub.start()

    async def asyncTearDown(self):
        self.stub.stop()
        self.sendstub.stop()

    async def send(self, *a, **kw):
        self.sends.append(kw)
        await asyncio.sleep(0)
        if getattr(self, 'fail_send', False):
            raise OSError('unknown SMTP outcome')
        return {'history_status': 'unavailable'}

    async def test_duplicate_and_reload_and_stop_invalidate_queued_callbacks(self):
        await self.s.async_reload_schedules(self.hass)
        callback = self.callbacks[0]
        due = datetime(2026, 10, 12, 22, 30, tzinfo=UTC)
        await asyncio.gather(callback(due), callback(due))
        self.assertEqual(1, len(self.sends))
        self.assertEqual('daily:2026-10-13', await self.storage.async_get_last_fired('one'))
        await self.s.async_reload_schedules(self.hass)
        self.assertEqual(1, len(self.unsubscribed))
        await callback(due + timedelta(days=1))
        self.assertEqual(1, len(self.sends))
        self.s.async_stop_scheduler(self.hass)
        await self.callbacks[-1](due + timedelta(days=1))
        self.assertEqual(1, len(self.sends))
        self.assertEqual(2, len(self.unsubscribed))

    async def test_edited_deleted_disabled_and_wrong_minute(self):
        await self.s.async_reload_schedules(self.hass)
        callback = self.callbacks[0]
        due = datetime(2026, 10, 12, 22, 30, tzinfo=UTC)
        await callback(due - timedelta(minutes=1))
        await self.storage.async_upsert_schedule({'id': 'one', 'time': '08:30'})
        await callback(due)
        self.assertEqual([], self.sends)
        await callback(due + timedelta(hours=1))
        self.assertEqual(1, len(self.sends))
        await self.storage.async_upsert_schedule({'id': 'one', 'enabled': False})
        await callback(due + timedelta(days=1, hours=1))
        await self.storage.async_delete_schedule('one')
        await callback(due + timedelta(days=2, hours=1))
        self.assertEqual(1, len(self.sends))

    async def test_unconfirmed_send_is_consumed_before_dispatch_and_never_retried(self):
        self.fail_send = True
        await self.s.async_reload_schedules(self.hass)
        due = datetime(2026, 10, 12, 22, 30, tzinfo=UTC)
        await self.callbacks[0](due)
        await self.callbacks[0](due)
        await self.s.async_reload_schedules(self.hass)
        await self.callbacks[-1](due)
        self.assertEqual(1, len(self.sends))
        self.assertEqual('daily:2026-10-13', await self.storage.async_get_last_fired('one'))

    async def test_guard_save_failure_never_sends(self):
        await self.s.async_reload_schedules(self.hass)
        self.storage._store.fail = True
        await self.callbacks[0](datetime(2026, 10, 12, 22, 30, tzinfo=UTC))
        self.assertEqual([], self.sends)

    async def test_swallowed_lower_period_write_never_sends(self):
        await self.s.async_reload_schedules(self.hass)
        self.storage._store.swallowed_failure = True
        await self.callbacks[0](datetime(2026, 10, 12, 22, 30, tzinfo=UTC))
        self.assertEqual([], self.sends)
        self.assertIsNone(await self.storage.async_get_last_fired('one'))


def load_websocket(testcase):
    """Use actual methods with a deny-capable trusted admin guard stub."""
    mod, unused = store()
    package = types.ModuleType('schedule_v3_ws_package')
    package.__path__ = [str(ROOT)]
    core = types.ModuleType('homeassistant.core')
    core.HomeAssistant = object
    ws = types.ModuleType('homeassistant.components.websocket_api')
    ws.ActiveConnection = object
    ws.websocket_command = lambda schema: lambda fn: fn
    ws.async_response = lambda fn: fn
    ws.async_register_command = lambda hass, fn: hass.commands.append(fn)
    def admin(fn):
        async def guarded(hass, connection, msg):
            if not connection.user.is_admin:
                connection.send_error(msg['id'], 'unauthorized', 'Admin required')
                return
            await fn(hass, connection, msg)
        return guarded
    ws.require_admin = admin
    components = types.ModuleType('homeassistant.components')
    components.websocket_api = ws
    vol = types.ModuleType('voluptuous')
    vol.Required = vol.Optional = lambda key, **kw: key
    vol.Any = vol.All = vol.In = vol.Range = lambda *a, **kw: object()
    modules = {package.__name__: package, package.__name__ + '.storage': mod,
                                  package.__name__ + '.tariff': mod._tariff_test_module,
                                  core.__name__: core, components.__name__: components,
                                  ws.__name__: ws, vol.__name__: vol}
    stubs = patch.dict(sys.modules, modules)
    stubs.start()
    testcase.addCleanup(stubs.stop)
    spec = importlib.util.spec_from_file_location(package.__name__ + '.websocket_api', ROOT / 'websocket_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReadonlyAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfirmed_schedule_write_is_safe_error_without_reload_or_ack(self):
        from unittest.mock import AsyncMock
        ws = load_websocket(self)
        _, s = store()
        s._store.swallowed_failure = True
        hass = types.SimpleNamespace(data={'ha_tools_email': {'storage': s}})
        results, errors = [], []
        conn = types.SimpleNamespace(user=types.SimpleNamespace(is_admin=True),
                                    send_result=lambda *a: results.append(a),
                                    send_error=lambda *a: errors.append(a))
        with patch.object(ws, 'async_reload_schedules', new=AsyncMock()) as reload:
            await ws._ws_set_schedule(hass, conn, {'id': 1, 'schedule': schedule()})
        self.assertEqual([], results)
        self.assertEqual('storage_write_failed', errors[0][1])
        self.assertNotIn('synthetic lower', str(errors))
        reload.assert_not_awaited()

    async def test_actual_list_metadata_and_preview_zero_writes_sends(self):
        ws = load_websocket(self)
        _, s = store()
        rows = [schedule(timezone='Asia/Tokyo'), dict(schedule(kind='energy_report'), id='energy')]
        state = {'schedules': rows, 'digest_state': {'keep': True}, 'smtp_config': {'keep': True},
                 'attempt_history': {'schema': 1, 'attempts': []}}
        s._store.loaded = copy.deepcopy(state)
        hass = types.SimpleNamespace(data={'ha_tools_email': {'storage': s},
                                          'system_log': types.SimpleNamespace(records=[
                                              {'level': 'ERROR', 'name': 'fixture', 'message': ['test'],
                                               'timestamp': (NOW - timedelta(days=1)).timestamp(), 'count': 9}])},
                                     config=types.SimpleNamespace(time_zone='America/New_York'), commands=[])
        smtp_send = Mock(side_effect=AssertionError('preview must not send'))
        smtp_config = Mock(side_effect=AssertionError('readonly API must not inspect SMTP'))
        hass.data['ha_tools_email'].update(send_email=smtp_send, load_config=smtp_config)
        results, errors = [], []
        conn = types.SimpleNamespace(user=types.SimpleNamespace(is_admin=True),
                                    send_result=lambda *a: results.append(a), send_error=lambda *a: errors.append(a))
        with patch.object(ws, '_utcnow', lambda: NOW):
            await ws._ws_list_schedules(hass, conn, {'id': 1})
            await ws._ws_preview_log_digest(hass, conn, {'id': 2, 'cadence': 'daily'})
        self.assertEqual([], errors)
        listed = results[0][1]['schedules']
        self.assertEqual(('Asia/Tokyo', 'America/New_York'), tuple(r['effective_timezone'] for r in listed))
        self.assertTrue(all(r['next_fire'] for r in listed))
        self.assertEqual('retained_aggregates', results[1][1]['source']['count_semantics'])
        self.assertFalse(results[1][1]['source']['complete'])
        self.assertEqual([], s._store.saved)
        self.assertEqual((0, 0), (smtp_send.call_count, smtp_config.call_count))
        for key in state:
            self.assertEqual(state[key], s._data[key])
        ws.async_register_commands(hass)
        self.assertIn(ws._ws_preview_log_digest, hass.commands)
        conn.user.is_admin = False
        before = len(results)
        await ws._ws_list_schedules(hass, conn, {'id': 3})
        await ws._ws_preview_log_digest(hass, conn, {'id': 4, 'cadence': 'daily'})
        self.assertEqual(before, len(results))
        self.assertEqual(['unauthorized', 'unauthorized'], [r[1] for r in errors])
        self.assertEqual([], s._store.saved)
        self.assertEqual((0, 0), (smtp_send.call_count, smtp_config.call_count))

    async def test_list_skips_consumed_period_and_disabled_is_null(self):
        ws = load_websocket(self)
        _, s = store()
        s._store.loaded = {'schedules': [schedule(timezone='Asia/Tokyo'),
                                       dict(schedule(enabled=False), id='off')],
                           'scheduler_state': {'one': {'last_fired_period': 'daily:2026-10-13'}}}
        hass = types.SimpleNamespace(data={'ha_tools_email': {'storage': s}},
                                     config=types.SimpleNamespace(time_zone='UTC'))
        values = []
        conn = types.SimpleNamespace(user=types.SimpleNamespace(is_admin=True),
                                    send_result=lambda *a: values.append(a), send_error=lambda *a: self.fail(str(a)))
        with patch.object(ws, '_utcnow', lambda: NOW):
            await ws._ws_list_schedules(hass, conn, {'id': 1})
        self.assertEqual('2026-10-14', values[0][1]['schedules'][0]['next_fire'][:10])
        self.assertIsNone(values[0][1]['schedules'][1]['next_fire'])
        self.assertEqual([], s._store.saved)

class DispatchCalendarTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_single_dispatch_calendar_bounds_history_and_late_delete(self):
        ws = load_websocket(self)
        package_name = ws.__package__
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        scheduler = sys.modules[package_name + '.scheduler']
        composer = sys.modules[package_name + '.composer']
        _, s = store()
        row = schedule(timezone='Europe/Warsaw')
        s._store.loaded = {'schedules': [row]}
        row = await s.async_get_schedule('one')
        composed, smtp_calls = [], []
        async def config():
            return {'default_recipient': 'test@example.invalid'}
        async def executor(fn, *args):
            return fn(*args)
        hass = types.SimpleNamespace(data={'ha_tools_email': {'storage': s,
                                         'load_config': config,
                                         'send_email': lambda *a: smtp_calls.append(a)}},
                                     config=types.SimpleNamespace(time_zone='America/New_York'),
                                     async_add_executor_job=executor)
        async def build(*a, **kw):
            composed.append(kw)
            if getattr(self, 'delete_during_compose', False):
                await s.async_delete_schedule('one')
            if getattr(self, 'stop_during_compose', False):
                scheduler.async_stop_scheduler(hass)
            return {'subject': 'fixture', 'body': 'fixture', 'html': 'fixture', 'dedup_keys': []}
        with patch.dict(sys.modules, {package_name: package}), patch.object(composer, 'async_build_report_payload', build):
            result = await scheduler.async_send_schedule(hass, row, now=datetime(2026, 3, 30, 7, 30, tzinfo=ZoneInfo('Europe/Warsaw')))
            self.assertEqual('accepted_by_smtp', result['status'])
            self.assertEqual('recorded', result['history_status'])
            self.assertEqual(1, len(smtp_calls))
            kw = composed[0]
            self.assertEqual(23, (kw['window_end'] - kw['window_start']).total_seconds() / 3600)
            self.stop_during_compose = True
            with self.assertRaises(ValueError):
                await scheduler.async_send_schedule(hass, row, now=datetime(2026, 10, 25, 7, 30, tzinfo=ZoneInfo('Europe/Warsaw')))
            self.assertEqual(1, len(smtp_calls))
            self.stop_during_compose = False
            self.delete_during_compose = True
            with self.assertRaises(ValueError):
                await scheduler.async_send_schedule(hass, row, now=datetime(2026, 10, 26, 7, 30, tzinfo=ZoneInfo('Europe/Warsaw')))
            self.assertEqual(1, len(smtp_calls))
        entries = (await s.async_get_attempt_history(now=datetime(2026, 10, 27, tzinfo=UTC)))['attempts']
        self.assertEqual(2, len(entries))  # March record aged out; stop/delete preparations retained.
        self.assertEqual('preparation_failed', entries[0]['outcome'])


class PreviewCalendarTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_preview_timezone_boundary_and_missing_timestamps(self):
        ws = load_websocket(self)
        hass = types.SimpleNamespace(data={'system_log': types.SimpleNamespace(records=[
            {'level': 'ERROR', 'name': 'fixture', 'message': ['start'], 'count': 2,
             'timestamp': datetime(2026, 3, 28, 23, tzinfo=UTC).timestamp()},
            {'level': 'WARNING', 'name': 'fixture', 'message': ['end'], 'count': 3,
             'timestamp': datetime(2026, 3, 29, 22, tzinfo=UTC).timestamp()},
            {'level': 'WARNING', 'name': 'fixture', 'message': ['undated'], 'count': 1}])},
                                     config=types.SimpleNamespace(time_zone='America/New_York'))
        results, errors = [], []
        conn = types.SimpleNamespace(user=types.SimpleNamespace(is_admin=True),
                                    send_result=lambda *a: results.append(a), send_error=lambda *a: errors.append(a))
        with patch.object(ws, '_utcnow', lambda: datetime(2026, 3, 30, 7, tzinfo=UTC)):
            await ws._ws_preview_log_digest(hass, conn, {'id': 1, 'cadence': 'daily', 'timezone': 'Europe/Warsaw'})
            await ws._ws_preview_log_digest(hass, conn, {'id': 2, 'cadence': 'daily', 'timezone': 'No/SuchZone'})
        self.assertEqual(3, results[0][1]['total'])
        self.assertEqual('Europe/Warsaw', results[0][1]['effective_timezone'])
        period = results[0][1]['period']
        self.assertEqual(23, (datetime.fromisoformat(period['to']) - datetime.fromisoformat(period['from'])).total_seconds() / 3600)
        self.assertEqual('invalid_payload', errors[0][1])
        import json
        json.dumps(results[0][1])


if __name__ == '__main__':
    unittest.main()
