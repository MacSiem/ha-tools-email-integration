"""Actual MIME handoff on synthetic reports; transport is replaced before invocation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import unittest

import test_mail_charts_v3 as charts
import test_send_semantics as transport


class ParsedHTML(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.tags = []
        self.text = []
        self.links = []
        self.attributes = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)
        self.links.extend(value for key, value in attrs if key == 'href')

    def handle_data(self, value):
        self.text.append(value)


def mime_roundtrip(payload):
    """Execute the shipping sender's MIME path with a local Mock connection only."""
    send, smtp = transport.SendSemanticsTests()._send_function({})
    send(object(), {'server': 'smtp.example.invalid', 'username': 'sender@example.invalid',
                    'password': 'SYNTHETIC-TRANSPORT-ONLY'}, 'reader@example.invalid',
         payload['subject'], payload['body'], payload.get('html'))
    smtp.sendmail.assert_called_once()
    smtp.quit.assert_called_once()
    raw = smtp.sendmail.call_args.args[2].encode('ascii')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return raw, message


class MailMIMEV3Tests(unittest.TestCase):
    def check_payload(self, payload):
        raw, message = mime_roundtrip(payload)
        self.assertEqual('multipart/alternative', message.get_content_type())
        self.assertEqual(payload['subject'], str(message['Subject']))
        self.assertNotIn(b'SYNTHETIC-TRANSPORT-ONLY', raw)
        parts = list(message.iter_parts())
        self.assertEqual(['text/plain', 'text/html'], [p.get_content_type() for p in parts])
        for part, field in zip(parts, ['body', 'html']):
            self.assertEqual('utf-8', part.get_content_charset())
            self.assertEqual(payload[field], part.get_content())
        for part in message.walk():
            self.assertEqual([], part.defects)
            self.assertIsNone(part.get_filename())
        html = ParsedHTML(parts[1].get_content())
        self.assertFalse({'script', 'iframe', 'object', 'canvas', 'svg', 'img', 'form'} & set(html.tags))
        self.assertFalse(any(key.startswith('on') or key == 'src' for key, value in html.attributes))
        for chart in payload['summary']['visualization']['charts']:
            for row in chart['rows']:
                value = f"{row['display']} {chart['unit']}"
                self.assertIn(value, ''.join(html.text))
                self.assertIn(value, parts[0].get_content())
        self.assertEqual(1, parts[1].get_content().count(charts.m.HTML_START))
        return raw, message, html

    def test_energy_mime_preserves_unicode_values_and_safe_interactive_link(self):
        payload = charts.c.build_energy_report_payload([
            {'name': 'Żółć 漢字 <img src="private">', 'kwh': 0.1 + 0.2},
            {'name': 'Measured zero', 'kwh': 0}], cadence='daily', now=charts.NOW,
            currency='EUR', price_per_kwh=.8, interactive_url='https://ha.example.invalid')
        payload['subject'] = 'Energia — Żółć 漢字'
        _, _, html = self.check_payload(payload)
        self.assertIn('Żółć 漢字 <img src="private">', ''.join(html.text))
        self.assertIn('https://ha.example.invalid/energy', html.links)
        self.assertIn('0 · measured zero', ''.join(html.text))

    def test_log_mime_preserves_occurrences_and_unknown_separately(self):
        payload = charts.c.build_log_digest_payload([
            {'id': 'a', 'name': 'Źródło', 'level': 'ERROR', 'count': 7, 'timestamp': charts.NOW.timestamp()-1},
            {'id': 'b', 'name': 'Zero', 'level': 'WARNING', 'count': 0, 'timestamp': charts.NOW.timestamp()-2},
            {'id': 'c', 'name': 'Unknown', 'level': 'INFO', 'count': None, 'timestamp': charts.NOW.timestamp()-3},
        ], cadence='daily', now=charts.NOW)
        _, _, html = self.check_payload(payload)
        for value in ['7 occurrences', '0 occurrences', 'Unknown occurrences', 'Unique problems: 3']:
            self.assertIn(value, ''.join(html.text))

    def test_signed_cost_gap_and_dst_offsets_survive_actual_mime(self):
        start = datetime(2026, 10, 25, tzinfo=timezone.utc)
        buckets = charts.series(start, 3)
        buckets[1]['kwh'] = None
        costs = [{**row, 'energyCost': -.15 if row['kwh'] is not None else None,
                  'variableCost': 0 if row['kwh'] is not None else None} for row in buckets]
        model = charts.m.build_energy_chart_model(charts.summary(start, start+timedelta(hours=3)),
            buckets, costs, timezone_name='Europe/Warsaw', interactive_url='https://ha.example.invalid')
        payload = {'subject': 'Synthetic retained hourly report', 'body': 'Measured values',
                   'html': '<!doctype html><html><body><h1>Measured values</h1></body></html>', 'summary': {}}
        charts.m.attach_mail_charts(payload, model)
        _, message, _ = self.check_payload(payload)
        body = message.get_body(preferencelist=('plain',)).get_content()
        for value in ['02:00 → 02:00 (+02:00 → +01:00)', '02:00 → 03:00 (+01:00)', '-0.15 PLN', 'Unknown PLN', '0 PLN']:
            self.assertIn(value, body)

    def test_plain_only_legacy_handoff_still_has_utf8_and_no_html(self):
        payload = {'subject': 'Raport — Żółć', 'body': 'Wartość: 0 kWh. Brak: nieznane.'}
        _, message = mime_roundtrip(payload)
        parts = list(message.iter_parts())
        self.assertEqual(['text/plain'], [part.get_content_type() for part in parts])
        self.assertEqual(payload['body'], parts[0].get_content())


if __name__ == '__main__':
    unittest.main()
