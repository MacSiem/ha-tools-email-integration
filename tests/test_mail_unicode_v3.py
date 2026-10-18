"""Unicode scalars, malformed UTF-16 and escaping through the shipping mail path."""
import copy
from datetime import timedelta
from html import escape
import json
import unittest
from unittest.mock import patch

from tests import test_mail_charts_v3 as charts
from tests import test_mail_mime_v3 as mime
from tests import test_mail_preview_manual_v3 as preview


class MailUnicodeTests(unittest.TestCase):
    def assert_mime_fields(self, payload):
        raw, message = mime.mime_roundtrip(payload)
        self.assertEqual(payload['subject'], str(message['Subject']))
        for kind, field in (('plain', 'body'), ('html', 'html')):
            part = message.get_body(preferencelist=(kind,))
            self.assertEqual('utf-8', part.get_content_charset())
            self.assertEqual(payload[field], part.get_content())
            self.assertEqual([], part.defects)
        self.assertEqual([], message.defects)
        self.assertNotIn(b'SYNTHETIC-TRANSPORT-ONLY', raw)

    def test_polish_generated_presentation_survives_actual_mime_encoding(self):
        payload=charts.c.build_log_digest_payload([],cadence='monthly',now=charts.NOW,
            presentation=charts.c._presentation('pl_PL','Europe/Warsaw'))
        self.assertIn('HA Log Digest – miesięczny',payload['subject'])
        self.assertIn('paź',payload['html'])
        self.assertIn('Wygenerowano przez HA Tools Email',payload['body'])
        self.assert_mime_fields(payload)

    def test_long_valid_polish_and_emoji_are_complete_and_html_is_escaped(self):
        # Cross the old client 200 UTF-16-unit boundary with a non-BMP scalar.
        text = 'a' * 199 + '😀 Żółć 🧑🏽\u200d💻 <script>alert("x")</script> & koniec' + 'z' * 500
        entry = dict(id='valid', name='Źródło <img src=x>', level='ERROR',
                     message=[text], count=2, timestamp=charts.NOW.timestamp()-1)
        before = copy.deepcopy(entry)
        payload = charts.c.build_log_digest_payload([entry], cadence='daily', now=charts.NOW)
        self.assertIn(text, payload['body'])
        self.assertIn(escape(text), payload['html'])
        self.assertIn('Źródło &lt;img src=x&gt;', payload['html'])
        self.assertNotIn('<script>', payload['html'])
        self.assertNotIn('<img src=x>', payload['html'])
        self.assertEqual(before, entry)
        self.assert_mime_fields(payload)

    def test_unpaired_high_low_and_explicit_valid_pair_survive_actual_mime(self):
        for malformed in ('\ud800', '\udfff', '\ud800X\udfff'):
            with self.subTest(malformed=ascii(malformed)):
                text = 'Żółć 😀 <b>& ' + malformed + ' \ud83d\ude00 koniec'
                expected = 'Żółć 😀 <b>& ' + malformed.replace('\ud800', '\ufffd').replace('\udfff', '\ufffd') + ' 😀 koniec'
                entry = dict(id='malformed', name=text, level='ERROR', message=[text],
                             source=[text, 7], exception=text,
                             count=3, timestamp=charts.NOW.timestamp()-1)
                before = copy.deepcopy(entry)
                payload = charts.c.build_log_digest_payload([entry], cadence='daily', now=charts.NOW)
                self.assertIn(expected, payload['body'])
                self.assertIn(escape(expected), payload['html'])
                for field in ('subject', 'body', 'html'):
                    payload[field].encode('utf-8', errors='strict')
                    self.assertFalse(any(0xD800 <= ord(char) <= 0xDFFF for char in payload[field]))
                self.assertEqual(before, entry)
                self.assert_mime_fields(payload)

    def test_source_and_exception_do_not_break_identity_or_leak_into_mail(self):
        for field in ('source', 'exception'):
            with self.subTest(field=field):
                rows = [dict(name='Źródło', level='ERROR', message=['same'], count=2,
                             timestamp=charts.NOW.timestamp()-1, **{field: value})
                        for value in ('PRIVATE-\ud800', 'PRIVATE-\udfff', 'PRIVATE-\ufffd')]
                before = copy.deepcopy(rows)
                payload = charts.c.build_log_digest_payload(rows, cadence='daily', now=charts.NOW)
                self.assertEqual(3, payload['summary']['unique_problems'])
                self.assertEqual(6, payload['summary']['total'])
                self.assertEqual(before, rows)
                for target in ('body', 'html'):
                    self.assertNotIn('PRIVATE-', payload[target])
                self.assert_mime_fields(payload)

    def test_direct_manual_service_subject_and_parts_are_utf8_safe(self):
        # The manual service can bypass the report composer and accepts a subject.
        malformed = 'Żółć 😀 \ud800 <b>& \udfff \ud83d\ude00'
        expected = 'Żółć 😀 \ufffd <b>& \ufffd 😀'
        _, message = mime.mime_roundtrip(dict(subject=malformed, body=malformed, html=malformed))
        self.assertEqual(expected, str(message['Subject']))
        self.assertEqual(expected, message.get_body(preferencelist=('plain',)).get_content())
        self.assertEqual(expected, message.get_body(preferencelist=('html',)).get_content())


class MailUnicodePreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_energy_source_unicode_preview_equals_actual_mime_without_changing_identity(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = preview.MailPreviewManualTests.fixture(self)
        raw = 'Grid Żółć 😀 \ud800 \udfff \ud83d\ude00 <tag>&'
        expected = 'Grid Żółć 😀 \ufffd \ufffd 😀 <tag>&'
        devices = self.report_harness.manager.data['device_consumption']
        devices[0]['name'] = raw
        original = copy.deepcopy(devices)
        # Raw Energy configuration is unchanged; presentation sanitizes Unicode.
        for mode in ('rolling', 'calendar'):
            for cadence in ('daily', 'weekly', 'monthly'):
                with self.subTest(mode=mode, cadence=cadence):
                    hass.data['ha_tools_email'].update(load_config=forbidden, send_email=forbidden)
                    before = copy.deepcopy(storage._store.saved)
                    mail = await preview.MailPreviewManualTests.preview(self, ws, hass, conn,
                        'energy_report', cadence, period_mode=mode)
                    self.assertEqual([], self.errors)
                    json.dumps(mail, ensure_ascii=False, allow_nan=False).encode('utf-8', errors='strict')
                    self.assertIn(expected, mail['body'])
                    self.assertIn(escape(expected), mail['html'])
                    self.assertEqual(before, storage._store.saved)
                    self.assertEqual(0, forbidden.call_count)
                    self.assertEqual(original, devices)
                    preview.MailPreviewManualTests.enable_send(self, hass)
                    await ws._ws_send_now(hass, conn, dict(id=2, kind='energy_report',
                        cadence=cadence, period_mode=mode))
                    self.assertEqual([], self.errors)
                    self.assertEqual('accepted_by_smtp', self.results[-1][1]['status'])
                    preview.MailPreviewManualTests.assert_delivery_parity(self, mail, 'energy_report')
                    self.assertEqual(original, devices)

    async def test_mail_preview_is_utf8_json_read_only_and_equals_actual_send(self):
        ws, composer, scheduler, hass, conn, storage, forbidden, state = preview.MailPreviewManualTests.fixture(self)
        raw = 'Żółć 😀 \ud800 <img src=x> & \udfff \ud83d\ude00 ' + 'długi ' * 100
        expected = 'Żółć 😀 \ufffd <img src=x> & \ufffd 😀 ' + 'długi ' * 100
        records = [dict(id='unicode', level='ERROR', name=raw, message=[raw],
                        source=[raw, 42], exception=raw, count=5,
                        timestamp=(preview.NOW-timedelta(hours=1)).timestamp())]
        hass.data['system_log'].records = records
        before = copy.deepcopy(records)
        mail = await preview.MailPreviewManualTests.preview(self, ws, hass, conn,
                    'log_digest', period_mode='rolling')
        self.assertEqual([], self.errors)
        # ensure_ascii=True alone would hide a surrogate bug in the JSON object.
        json.dumps(mail, ensure_ascii=False, allow_nan=False).encode('utf-8', errors='strict')
        self.assertIn(expected, mail['body'])
        self.assertIn(escape(expected), mail['html'])
        self.assertEqual(before, records)
        self.assertEqual([], storage._store.saved)
        self.assertEqual(0, forbidden.call_count)
        storage.async_get_digest_state.assert_not_awaited()
        preview.MailPreviewManualTests.enable_send(self, hass)
        await ws._ws_send_now(hass, conn, dict(id=2, kind='log_digest', cadence='daily', period_mode='rolling'))
        self.assertEqual([], self.errors)
        self.assertEqual('accepted_by_smtp', self.results[-1][1]['status'])
        self.assertEqual({key: mail[key] for key in ('subject', 'body', 'html')}, self.sent[-1])
        self.assertEqual(state['digest_state'], storage._data['digest_state'])


if __name__ == '__main__':
    unittest.main()
