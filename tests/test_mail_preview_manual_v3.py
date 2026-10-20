"""Shared real mail composer/dispatcher parity; no external SMTP.

Prepared for owner FIFO execution after applying the accompanying backend patch.
"""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from email import policy
from email.parser import BytesParser
from tests import test_send_semantics as transport

try:
    from tests.test_schedule_timezone_v3 import load_websocket, store, authorized_report_harness
except ImportError:
    from test_schedule_timezone_v3 import load_websocket, store, authorized_report_harness

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, 37, 14, tzinfo=UTC)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def row(name, stamp, level='ERROR', count=3):
    return dict(id=name, name=name, message=[name], level=level,
                timestamp=stamp.timestamp() if isinstance(stamp, datetime) else stamp,
                count=count)


class MailPreviewManualTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws = load_websocket(self)
        composer = sys.modules[ws.__package__ + '.composer']
        scheduler = sys.modules[ws.__package__ + '.scheduler']
        _, storage = store()
        persisted = {
            'digest_state': {'log_digest': {
                cadence: {'last_digest': NOW.isoformat(), 'seen_keys': ['ERROR|inside|inside']}
                for cadence in ('daily', 'weekly', 'monthly')}},
            'attempt_history': {'schema': 1, 'attempts': []},
        }
        storage._store.loaded = copy.deepcopy(persisted)
        storage.async_get_tariff = AsyncMock(return_value={'profile': None})
        # New manual windows must not even consult incremental schedule state.
        storage.async_get_digest_state = AsyncMock(side_effect=AssertionError('manual digest state read'))
        forbidden = Mock(side_effect=AssertionError('preview touched SMTP/config'))
        self.sent = []

        async def executor(fn, *args):
            return fn(*args)

        self.report_harness = h = authorized_report_harness(self, ws, now=NOW, storage=storage)
        hass = h.hass
        hass.config.time_zone = 'Europe/Warsaw'
        hass.data['ha_tools_email'].update(send_email=forbidden, load_config=forbidden)
        hass.data['system_log'] = SimpleNamespace(records=[row('inside', NOW - timedelta(hours=1))])
        self.results, self.errors, self.prepared = [], [], []
        conn = SimpleNamespace(user=h.user,
            send_result=lambda *a: self.results.append(a), send_error=lambda *a: self.errors.append(a))
        original_prepare = h.subject.async_prepare_report
        async def prepare(*args, **kwargs):
            value = await original_prepare(*args, **kwargs)
            self.prepared.append(copy.deepcopy(value))
            return value
        # No old composer.usage stub: actual service/provider/renderer consume
        # source-bucket Recorder fixtures and reauthorize the actual harness user.
        for context in (patch.object(ws, '_utcnow', lambda: NOW),
                        patch.object(scheduler, 'datetime', FrozenDateTime),
                        patch.object(h.subject, 'async_prepare_report', prepare),
                        patch.object(composer, 'async_get_energy_usage',
                            side_effect=AssertionError('legacy energy engine must not run'))):
            context.start()
            self.addCleanup(context.stop)
        return ws, composer, scheduler, hass, conn, storage, forbidden, persisted

    async def preview(self, ws, hass, conn, kind, cadence='daily', **extra):
        handler = ws._ws_preview_log_digest if kind == 'log_digest' else ws._ws_preview_energy_report
        await handler(hass, conn, dict(id=1, cadence=cadence, format='mail', **extra))
        return self.results[-1][1] if self.results else None

    def enable_send(self, hass):
        async def config():
            return {'default_recipient': 'fixture@example.invalid',
                'server': 'smtp.example.invalid', 'username': 'sender@example.invalid',
                'password': 'SYNTHETIC-ONLY-NO-NETWORK'}
        shipping_send, smtp = transport.SendSemanticsTests()._send_function({})
        def send(*args):
            shipping_send(*args)
            raw = smtp.sendmail.call_args.args[2].encode('ascii')
            message = BytesParser(policy=policy.default).parsebytes(raw)
            self.assertEqual('multipart/alternative', message.get_content_type())
            self.assertEqual('fixture@example.invalid', str(message['To']))
            self.assertEqual([], message.defects)
            self.sent.append(dict(subject=str(message['Subject']),
                body=message.get_body(preferencelist=('plain',)).get_content(),
                html=message.get_body(preferencelist=('html',)).get_content()))
        hass.data['ha_tools_email'].update(load_config=config, send_email=send)

    def assert_delivery_parity(self, mail, kind):
        if kind == 'log_digest':
            self.assertEqual({key: mail[key] for key in ('subject', 'body', 'html')}, self.sent[-1])
            return
        read, execute = self.prepared[-2:]
        self.assertEqual('report_read', read['context']['access_purpose'])
        self.assertEqual('report_execute', execute['context']['access_purpose'])
        self.assertNotEqual(read['snapshot_id'], execute['snapshot_id'])
        # Purpose-bound identity differs; business facts and bounds do not.
        self.assertEqual(read['period'], execute['period'])
        self.assertEqual({k:v for k,v in read['summary'].items() if k != 'snapshot_id'},
                         {k:v for k,v in execute['summary'].items() if k != 'snapshot_id'})
        for artifact in (read, execute):
            identity = artifact['snapshot_id']
            self.assertEqual(identity, artifact['summary']['snapshot_id'])
            for field in ('html', 'csv'):
                self.assertIn(identity, artifact[field])
            if artifact['context'].get('report_model'):
                self.assertIn('<!-- ha-tools-snapshot:' + identity + ' -->', artifact['html'])
                self.assertNotIn(identity, artifact['body'])
            else:
                self.assertIn(identity, artifact['body'])
        self.assertEqual({key: read[key] for key in ('snapshot_id', 'subject', 'body', 'html', 'csv')},
                         {key: mail[key] for key in ('snapshot_id', 'subject', 'body', 'html', 'csv')})
        self.assertEqual({key: execute[key] for key in ('subject', 'body', 'html')}, self.sent[-1])
        if execute['context'].get('report_model'):
            self.assertNotIn(execute['snapshot_id'], self.sent[-1]['body'])
        else:
            self.assertIn(execute['snapshot_id'], self.sent[-1]['body'])
        self.assertIn(execute['snapshot_id'], self.sent[-1]['html'])

    async def test_frozen_preview_equals_actual_dispatch_for_two_kinds_three_cadences_and_modes(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = self.fixture()
        for language in ('en','pl-PL'):
            hass.config.language=language
            for mode in ('rolling', 'calendar'):
                for kind in ('energy_report', 'log_digest'):
                    for cadence in ('daily', 'weekly', 'monthly'):
                        with self.subTest(mode=mode, kind=kind, cadence=cadence):
                            # Require every preview to stay read-only even after sends.
                            hass.data['ha_tools_email'].update(load_config=forbidden, send_email=forbidden)
                            before = copy.deepcopy(storage._store.saved)
                            mail = await self.preview(ws, hass, conn, kind, cadence, period_mode=mode)
                            self.assertEqual([], self.errors)
                            self.assertEqual(before, storage._store.saved)
                            self.assertEqual(0, forbidden.call_count)
                            storage.async_get_digest_state.assert_not_awaited()
                            json.dumps(mail, allow_nan=False)
                            self.assertEqual(NOW.isoformat(), mail['generated_at'])
                            self.assertEqual(cadence, mail['cadence'])
                            self.assertIn('<!doctype html>', mail['html'])
                            if kind == 'energy_report' and mode == 'calendar':
                                self.assertIn('<!-- block:12 -->', mail['html'])
                                self.assertIn('Jak czytać te liczby.' if language.startswith('pl') else 'How to read these numbers.', mail['html'])
                                self.assertIn('Energia —' if language.startswith('pl') else 'Energy —', mail['body'])
                            elif kind == 'energy_report':
                                self.assertIn('data-section="period"', mail['html'])
                                self.assertIn('Źródło i jakość danych' if language.startswith('pl') else 'Source and data quality', mail['html'])
                                self.assertIn('Okres raportu:' if language.startswith('pl') else 'Report period:', mail['body'])
                            else:
                                self.assertIn('Okres raportu:' if language.startswith('pl') else 'Report period:', mail['html'])
                                self.assertIn('Wygenerowano przez HA Tools Email' if language.startswith('pl') else 'Generated by HA Tools Email',mail['html'])
                            if kind == 'log_digest':
                                self.assertFalse(mail['source']['complete'])
                                self.assertEqual('retained_aggregates', mail['source']['count_semantics'])
                                self.assertIn('not exact events or complete history', mail['html'])
                            self.enable_send(hass)
                            sent_before = len(self.sent)
                            # Exercise the existing WS -> send_now -> send_report ->
                            # real async_dispatch_attempt -> executor seam.
                            await ws._ws_send_now(hass, conn, dict(id=2, kind=kind, cadence=cadence, period_mode=mode))
                            self.assertEqual([], self.errors)
                            self.assertEqual(sent_before + 1, len(self.sent))
                            self.assert_delivery_parity(mail, kind)
                            self.assertEqual('accepted_by_smtp', self.results[-1][1]['status'])
                            if kind == 'log_digest':
                                self.assertEqual(state['digest_state'], storage._data['digest_state'])

    async def test_rolling_bounds_missing_future_and_limit_ignore_incremental_state(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = self.fixture()
        for cadence, days in (('daily', 1), ('weekly', 7), ('monthly', 30)):
            with self.subTest(cadence=cadence):
                start = NOW - timedelta(days=days)
                records = [row('left-bound', start), row('too-old', start-timedelta(microseconds=1)),
                    row('right-bound', NOW), row('future', NOW+timedelta(seconds=1)), row('missing', None),
                    row('invalid', 'bad timestamp')]
                for level in ('ERROR', 'WARNING'):
                    records.extend(row(f'{level}-{i}', NOW-timedelta(minutes=i+1), level=level) for i in range(15))
                hass.data['system_log'].records = records
                before_records = copy.deepcopy(records)
                options = await scheduler.async_resolve_manual_period(hass, kind='log_digest', cadence=cadence,
                    now=NOW, period_mode='rolling')
                a, b = options.pop('report_window')
                payload = await composer.async_build_report_payload(hass, storage, kind='log_digest',
                    cadence=cadence, now=NOW, window_start=a, window_end=b, max_entries=3, **options)
                summary = payload['summary']
                self.assertEqual(start, a)
                self.assertEqual(NOW, b)
                names = {entry['name'] for entry in summary['entries']}
                self.assertIn('left-bound', names)
                self.assertTrue(names.isdisjoint({'too-old', 'right-bound', 'future', 'missing', 'invalid'}))
                self.assertEqual(3, len(summary['recent_errors']))
                self.assertEqual(3, len(summary['recent_warnings']))
                self.assertEqual(31, summary['unique_problems'])
                self.assertEqual(93, summary['total'])
                self.assertEqual(before_records, records)
                mail = await self.preview(ws, hass, conn, 'log_digest', cadence, period_mode='rolling', max_entries=3)
                self.enable_send(hass)
                await ws._ws_send_now(hass, conn, dict(id=2, kind='log_digest', cadence=cadence,
                    period_mode='rolling', max_entries=3))
                self.assertEqual(mail['html'], self.sent[-1]['html'])
                self.assertIn('Top 3 recent errors', mail['html'])
                await ws._ws_send_now(hass, conn, dict(id=3, kind='log_digest', cadence=cadence,
                    period_mode='rolling', max_entries=3))
                self.assertEqual(mail['html'], self.sent[-1]['html'])
                self.assertEqual(self.sent[-2], self.sent[-1])
                self.assertEqual(state['digest_state'], storage._data['digest_state'])
                self.assertEqual(state['digest_state'], storage._store.saved[-1]['digest_state'])
                storage.async_get_digest_state.assert_not_awaited()

    async def test_default_entry_limit_remains_ten_and_invalid_limits_are_rejected(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = self.fixture()
        records = [row(str(i), NOW-timedelta(seconds=i+1)) for i in range(15)]
        default = composer.build_log_digest_payload(records, cadence='daily', now=NOW)
        self.assertEqual(10, len(default['summary']['recent_errors']))
        for limit in (True, False, 0, -1, 1001, 1.5, '3'):
            with self.subTest(limit=limit):
                self.results.clear()
                await self.preview(ws, hass, conn, 'log_digest', period_mode='rolling', max_entries=limit)
                self.assertEqual([], self.results)
                self.assertEqual('invalid_payload', self.errors[-1][1])
                with self.assertRaises(ValueError):
                    await scheduler.async_send_now(hass, kind='log_digest', cadence='daily',
                        period_mode='rolling', max_entries=limit)
        self.assertEqual([], storage._store.saved)
        self.assertEqual(0, forbidden.call_count)

    async def test_admin_guard_denies_both_mail_previews_before_any_data_read(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = self.fixture()
        conn.user.is_admin = False
        with patch.object(ws, 'async_build_report_payload', new=AsyncMock()) as build:
            for kind in ('energy_report', 'log_digest'):
                await self.preview(ws, hass, conn, kind, period_mode='rolling')
            build.assert_not_awaited()
        self.assertEqual([], self.results)
        self.assertEqual(['unauthorized']*2, [error[1] for error in self.errors])
        self.assertEqual([], storage._store.saved)
        self.assertEqual(0, forbidden.call_count)

    async def test_calendar_uses_existing_zone_resolver_and_dst_bounds(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = self.fixture()
        for now, hours in ((datetime(2026, 3, 30, 12, tzinfo=UTC), 23),
                           (datetime(2026, 10, 26, 12, tzinfo=UTC), 25)):
            for kind in ('energy_report', 'log_digest'):
                options = await scheduler.async_resolve_manual_period(hass, kind=kind, cadence='daily',
                    now=now, period_mode='calendar', report_timezone='Europe/Warsaw')
                a, b = options['report_window']
                self.assertEqual(hours, (b-a).total_seconds()/3600)
                self.assertEqual(scheduler.calendar_report_window({'cadence':'daily', 'timezone':'Europe/Warsaw'}, now), (a,b))
                self.assertEqual('previous_completed_calendar_period', options['period_semantics'])
        for mode, zone in (('rolling', 'UTC'), ('bad-mode', None), ('calendar', 'No/SuchZone')):
            self.results.clear()
            await self.preview(ws, hass, conn, 'energy_report', period_mode=mode,
                               **({'timezone': zone} if zone is not None else {}))
            self.assertEqual([], self.results)
            self.assertEqual('invalid_payload', self.errors[-1][1])
        self.assertEqual(0, forbidden.call_count)

    async def test_legacy_send_now_has_no_window_or_new_composer_options(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = self.fixture()
        with patch.object(scheduler, 'async_send_report', new=AsyncMock(return_value={})) as send:
            await scheduler.async_send_now(hass, kind='log_digest', cadence='daily')
            options = send.await_args.kwargs
        for key in ('report_window', 'report_timezone', 'period_semantics', 'max_entries'):
            self.assertNotIn(key, options)
        self.assertEqual([], storage._store.saved)
        self.assertEqual(0, forbidden.call_count)
