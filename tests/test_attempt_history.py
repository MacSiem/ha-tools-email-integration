"""Durable send history; real persistence/dispatch with only HA boundaries stubbed."""
import ast
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import smtplib
import sys
import types
import unittest
from unittest.mock import patch

try:
    from tests.test_tariff_profile import load_storage, load_websocket
except ImportError:
    from test_tariff_profile import load_storage, load_websocket

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def load_history():
    if "attempt_history_test" in sys.modules:
        return sys.modules["attempt_history_test"]
    spec = importlib.util.spec_from_file_location('attempt_history_test', ROOT / 'history.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["attempt_history_test"] = mod
    return mod


def storage():
    mod = load_storage()
    return mod.EmailStorage(types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *a: (_ for _ in ()).throw(AssertionError('no SMTP migration')))))


def record(**changes):
    value = dict(id='00000000-0000-4000-8000-000000000001', timestamp=NOW.isoformat(),
                 source='manual', kind='energy_report', cadence='daily', period='daily:2026-10-12',
                 schedule_ref=None, recipient_count=2, outcome='accepted_by_smtp')
    value.update(changes)
    return value


class AttemptStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_migration_preserves_private_and_unknown_store_sections(self):
        s = storage()
        original = {'smtp_config': {'server': 'private'}, 'tariff_profile': {'schema': 0},
                    'schedules': [{'future': 'untouched'}], 'future': {'keep': True}}
        s._store.loaded = copy.deepcopy(original)
        await s.async_append_attempt(record(), now=NOW)
        saved = s._store.saved[-1]
        for key, value in original.items():
            self.assertEqual(value, saved[key])
        restarted = storage()
        restarted._store.loaded = saved
        result = await restarted.async_get_attempt_history(now=NOW)
        self.assertEqual('accepted_by_smtp', result['attempts'][0]['outcome'])
        self.assertEqual(1, result['schema'])
        self.assertEqual([], restarted._store.saved)

    async def test_count_age_retention_and_concurrent_writes(self):
        s = storage()
        old = record(timestamp=(NOW - timedelta(days=91)).isoformat())
        await s.async_append_attempt(old, now=NOW)
        await asyncio.gather(*(s.async_append_attempt(record(id=f'00000000-0000-4000-8000-{i:012d}'), now=NOW)
                               for i in range(1, 141)))
        result = await s.async_get_attempt_history(now=NOW)
        self.assertEqual(128, len(result['attempts']))
        self.assertEqual(128, len({a['id'] for a in result['attempts']}))
        self.assertNotIn(old['timestamp'], [a['timestamp'] for a in result['attempts']])
        self.assertEqual('00000000-0000-4000-8000-000000000140', result['attempts'][0]['id'])

    async def test_save_failure_does_not_publish_or_reappear_after_other_save(self):
        s = storage()
        await s.async_append_attempt(record(), now=NOW)
        s._store.fail = True
        with self.assertRaises(OSError):
            await s.async_append_attempt(record(id='00000000-0000-4000-8000-000000000002'), now=NOW)
        self.assertEqual(1, len((await s.async_get_attempt_history(now=NOW))['attempts']))
        s._store.fail = False
        await s.async_save_tariff(None, expected_revision=0)
        self.assertEqual(1, len(s._store.saved[-1]['attempt_history']['attempts']))

    async def test_untrusted_history_is_allowlisted_and_future_schema_fails_closed(self):
        s = storage()
        dirty = record(subject='secret', address='private@example.invalid', exception='private')
        await s.async_append_attempt(dirty, now=NOW)
        result = await s.async_get_attempt_history(now=NOW)
        self.assertEqual(set(record()), set(result['attempts'][0]))
        for field in ('kind', 'period', 'schedule_ref', 'outcome', 'source'):
            with self.subTest(field=field):
                await s.async_append_attempt(record(**{field: 'private@example.invalid'}), now=NOW)
        text = repr(await s.async_get_attempt_history(now=NOW))
        self.assertNotIn('private', text)
        future = storage()
        future._store.loaded = {'attempt_history': {'schema': 9, 'attempts': [], 'future': True}}
        with self.assertRaises(ValueError):
            await future.async_append_attempt(record(), now=NOW)
        self.assertEqual([], future._store.saved)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def dispatch(self, *, failure=None, persist_failure=False, prepare_failure=False):
        mod, s, calls = load_history(), storage(), []
        s._store.fail = persist_failure
        def send(*args):
            calls.append(args)
            if failure:
                raise failure
        async def executor(fn, *args):
            return fn(*args)
        async def prepare():
            if prepare_failure:
                raise RuntimeError('private subject/credentials')
            return ({}, 'one@example.invalid, two@example.invalid', 'private subject', 'private body', None)
        hass = types.SimpleNamespace(async_add_executor_job=executor)
        try:
            result = await mod.async_dispatch_attempt(hass, s, send, prepare, source='manual',
                                                      kind='test', now=NOW)
        except Exception as exc:
            result = exc
        return result, s, calls

    async def test_accepted_history_failure_stays_accepted_and_never_retries(self):
        result, s, calls = await self.dispatch(persist_failure=True)
        self.assertEqual('unavailable', result['history_status'])
        self.assertEqual('accepted_by_smtp', result['status'])
        self.assertEqual(1, len(calls))
        self.assertEqual([], s._store.saved)

    async def test_safe_failure_categories_and_original_exception_semantics(self):
        for exc, outcome in ((smtplib.SMTPAuthenticationError(535, b'private'), 'authentication_failed'),
                             (smtplib.SMTPRecipientsRefused({'private': (550, b'private')}), 'recipients_refused'),
                             (TimeoutError('private'), 'transport_outcome_unknown'),
                             (load_history().PartialSMTPAcceptance('private'), 'partial_acceptance'),
                             (ValueError('private'), 'send_failed')):
            result, s, calls = await self.dispatch(failure=exc)
            self.assertIs(exc, result)
            history = await s.async_get_attempt_history(now=NOW)
            self.assertEqual(outcome, history['attempts'][0]['outcome'])
            self.assertNotIn('private', repr(history))
            self.assertEqual(1, len(calls))
        result, s, calls = await self.dispatch(prepare_failure=True)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual([], calls)
        self.assertEqual('preparation_failed', (await s.async_get_attempt_history(now=NOW))['attempts'][0]['outcome'])

    async def test_services_and_scheduler_share_single_dispatch(self):
        history = load_history()
        s, calls = storage(), []
        def send(*args):
            calls.append(args)
        async def executor(fn, *args):
            return fn(*args)
        async def config():
            return {'default_recipient': 'private@example.invalid'}
        async def compose(*a, **kw):
            return {'subject': 'private subject', 'body': 'private body', 'html': None}
        # Keep actual service handlers; only their external preparation boundary is fake.
        s.async_get_smtp_config = config
        hass = types.SimpleNamespace(async_add_executor_job=executor,
                                     data={'ha_tools_email': {'storage': s, 'load_config': config, 'send_email': send}})
        module = ast.parse((ROOT / '__init__.py').read_text())
        register = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_async_register_services')
        nodes = [n for n in register.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ('handle_send', 'handle_test')]
        ns = {'hass': hass, 'storage': s, 'ServiceCall': object, '_send_email': send,
              'async_dispatch_attempt': history.async_dispatch_attempt,
              'build_test_message': lambda cfg: ('private@example.invalid', 'subject', 'body', None), 'SETTINGS_HINT': 'settings'}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'handlers', 'exec'), ns)
        await ns['handle_send'](types.SimpleNamespace(data={}))
        await ns['handle_test'](types.SimpleNamespace(data={}))
        from tests.test_schedule_timezone_v3 import load_websocket, authorized_report_harness
        ws = load_websocket(self)
        h = authorized_report_harness(self, ws, now=datetime.now(timezone.utc), storage=s)
        h.hass.data['ha_tools_email'].update(load_config=config, send_email=send)
        scheduler = sys.modules[ws.__package__ + '.scheduler']
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return h.now.astimezone(tz) if tz else h.now.replace(tzinfo=None)
        with patch.object(scheduler, 'datetime', FrozenDateTime):
            manual = await scheduler.async_send_now(h.hass, kind='energy_report', cadence='daily', actor=h.user.id)
            await s.async_upsert_schedule({'id': 'private schedule name', 'kind': 'energy_report', 'cadence': 'daily'})
            scheduled = await scheduler.async_send_schedule(h.hass, await s.async_get_schedule('private schedule name'), now=h.now)
        attempts = (await s.async_get_attempt_history())['attempts']
        self.assertEqual(4, len(attempts))
        self.assertEqual(4, len(calls))
        self.assertEqual(2, sum('<!doctype html>' in str(call[-1]) for call in calls))
        self.assertGreater(len(h.queries), 0)
        self.assertEqual({'manual', 'schedule'}, {a['source'] for a in attempts})
        self.assertEqual('accepted_by_smtp', manual['status'])
        self.assertEqual('recorded', scheduled['history_status'])
        self.assertNotIn('private', repr(attempts))
        self.assertTrue(attempts[0]['schedule_ref'])


class HistoryWebsocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_gate_and_safe_unavailable_result(self):
        ws, mod = load_websocket()
        self.assertTrue(ws._ws_get_attempt_history.tariff_requires_admin)
        results, errors = [], []
        conn = types.SimpleNamespace(send_result=lambda *a: results.append(a), send_error=lambda *a: errors.append(a))
        s = storage()
        hass = types.SimpleNamespace(data={'ha_tools_email': {'storage': s}})
        await ws._ws_get_attempt_history(hass, conn, {'id': 1})
        self.assertEqual([], results[-1][1]['attempts'])
        s._data = None
        async def fail():
            raise OSError('private SMTP path')
        s._store.async_load = fail
        await ws._ws_get_attempt_history(hass, conn, {'id': 2})
        self.assertEqual('history_unavailable', errors[-1][1])
        self.assertNotIn('private', errors[-1][2])

    async def test_household_denied_before_storage_access(self):
        # Execute the actual decorated command with a HA-boundary role guard.
        # The guard stub models HA require_admin; removing the production
        # decorator would expose metadata and fail this test.
        module = ast.parse((ROOT / 'websocket_api.py').read_text())
        handler = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_ws_get_attempt_history')
        reads = []
        class Storage:
            async def async_get_attempt_history(self):
                reads.append(True)
                return {'attempts': []}
        def require_admin(fn):
            async def guarded(hass, connection, msg):
                if not connection.user.is_admin:
                    raise PermissionError('unauthorized')
                return await fn(hass, connection, msg)
            return guarded
        boundary = types.SimpleNamespace(websocket_command=lambda *a: lambda fn: fn,
                                         async_response=lambda fn: fn, require_admin=require_admin)
        ns = {'websocket_api': boundary, 'vol': types.SimpleNamespace(Required=lambda k: k),
              'DOMAIN': 'ha_tools_email', '_storage': lambda hass: Storage()}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'history_ws', 'exec'), ns)
        results = []
        conn = types.SimpleNamespace(user=types.SimpleNamespace(is_admin=False),
                                     send_result=lambda *a: results.append(a), send_error=lambda *a: None)
        with self.assertRaises(PermissionError):
            await ns['_ws_get_attempt_history'](object(), conn, {'id': 1})
        self.assertEqual([], reads)
        self.assertEqual([], results)
        conn.user.is_admin = True
        await ns['_ws_get_attempt_history'](object(), conn, {'id': 2})
        self.assertEqual({'attempts': []}, results[-1][1])


class HistoryTruthfulnessTests(unittest.TestCase):
    def test_unknown_metadata_does_not_invent_manual_source_or_send_failure(self):
        value = load_history().clean_attempt(record(source='future', kind='future', outcome='future'))
        self.assertEqual('unknown', value['source'])
        self.assertIsNone(value['kind'])
        self.assertEqual('outcome_unknown', value['outcome'])

    def test_boolean_history_schema_is_unavailable(self):
        for schema in (True, False):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                load_history().retained_attempts({'schema': schema, 'attempts': []}, NOW)


class OptionsDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_smtp_test_uses_the_same_single_private_history(self):
        import re
        import ssl
        history, s, calls = load_history(), storage(), []
        async def config():
            return {'password': 'fixture-reference'}
        async def save(*args, **kwargs):
            pass
        async def password_issue(*args):
            pass
        def send(*args):
            calls.append(True)
        async def executor(fn, *args):
            return fn(*args)
        s.async_get_smtp_config = config
        s.async_save_smtp_config = save
        package = types.ModuleType('attempt_options_test')
        package.__path__ = [str(ROOT)]
        package._list_secrets = lambda *args: []
        package._send_email = send
        package.async_update_password_issue = password_issue
        package.build_test_message = lambda cfg: ('test@example.invalid', 'fixture subject', 'fixture body', None)
        package.secret_reference = lambda key: key
        hass = types.SimpleNamespace(async_add_executor_job=executor, data={'ha_tools_email': {'storage': s}})
        flow = types.SimpleNamespace(hass=hass, async_create_entry=lambda **kwargs: kwargs)
        module = ast.parse((ROOT / 'config_flow.py').read_text())
        options = next(n for n in module.body if isinstance(n, ast.ClassDef) and any(isinstance(m, ast.AsyncFunctionDef) and m.name == 'async_step_init' for m in n.body))
        method = next(n for n in options.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'async_step_init')
        ns = {'__name__': package.__name__ + '.options', '__package__': package.__name__,
              'Any': object, 'config_entries': types.SimpleNamespace(ConfigFlowResult=dict),
              'DOMAIN': 'ha_tools_email', 'DATA_STORAGE': 'storage', 'CONF_PASSWORD_SECRET': 'password_secret',
              'CONF_SEND_TEST': 'send_test', '_EMAIL_RE': re.compile(r'.+@.+'), 'smtplib': smtplib, 'ssl': ssl}
        with patch.dict(sys.modules, {package.__name__: package, package.__name__ + '.history': history}):
            exec(compile(ast.Module(body=[method], type_ignores=[]), 'options_history', 'exec'), ns)
            await ns['async_step_init'](flow, {'sender': 'sender@example.invalid', 'default_recipient': 'test@example.invalid', 'send_test': True})
        self.assertEqual(1, len(calls))
        attempts = (await s.async_get_attempt_history())['attempts']
        self.assertEqual(1, len(attempts))
        self.assertEqual('test', attempts[0]['kind'])
        self.assertEqual('accepted_by_smtp', attempts[0]['outcome'])
        self.assertNotIn('example.invalid', repr(attempts))
