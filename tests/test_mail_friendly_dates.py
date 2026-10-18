"""Composed mail dates are presentation only; owner executes RED then GREEN."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from html import unescape
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_mail_charts_v3 import c, m, chart, series, summary

UTC = timezone.utc


class FriendlyEnergyMailDatesTest(unittest.TestCase):
    def compose(self, start, end, *, language='pl', display='America/Los_Angeles',
                report='Europe/Warsaw', cadence='weekly', gap=False):
        inputs = []
        async def measured(hass, sensors, a, b, cadence):
            hours = int((b-a).total_seconds()/3600)
            buckets = [{**r, 'kwh': 0 if i < 24 else 1}
                       for i, r in enumerate(series(a, hours))]
            if gap and a == start:
                buckets[24]['kwh'] = None
            total = sum(r['kwh'] for r in buckets if r['kwh'] is not None)
            result = {'status': 'partial' if gap and a == start else 'ready',
                      'source_ids': ['sensor.grid'], 'hourly_buckets': buckets,
                      'devices': [{'entity_id': 'sensor.grid', 'name': '2026-10-05', 'kwh': total}],
                      'measurement_provenance': {'sensor.grid': {
                          'method': 'recorder_hourly_change_v1', 'unit': 'kWh',
                          'source_unit': 'kWh', 'unit_class': 'energy', 'has_sum': True}}}
            inputs.append((result, copy.deepcopy(result)))
            return result
        async def sensors(hass):
            return [{'entity_id': 'sensor.grid', 'name': '2026-10-05'}]
        hass = SimpleNamespace(data={}, config=SimpleNamespace(
            language=language, time_zone=display, currency='PLN'))
        with patch.object(c, 'async_get_energy_usage', measured), patch.object(c, 'async_grid_sensors', sensors):
            payload = asyncio.run(c.async_build_energy_report_payload(
                hass, cadence=cadence, now=end, window_start=start, window_end=end,
                report_timezone=report))
        self.assertTrue(all(actual == before for actual, before in inputs))
        expected_model = m.build_energy_chart_model(payload['summary'], inputs[0][1]['hourly_buckets'],
                                                   timezone_name=report, source_kind='grid')
        self.assertEqual(expected_model, payload['summary']['visualization'])
        return payload

    def test_actual_weekly_mail_keeps_calendar_day_identity_in_report_zone(self):
        start = datetime(2026, 10, 18, 22, tzinfo=UTC)
        end = datetime(2026, 10, 25, 23, tzinfo=UTC)
        summaries = []
        for language, month in [('pl', 'paź'), ('en', 'Oct')]:
            with self.subTest(language=language):
                payload = self.compose(start, end, language=language)
                summaries.append(payload['summary'])
                rows = chart(payload['summary']['visualization'], 'energy-usage')['rows']
                self.assertEqual('2026-10-19 (24h; +02:00)', rows[0]['label'])
                self.assertEqual('2026-10-25 (25h; +02:00 → +01:00)', rows[-1]['label'])
                self.assertEqual((1792360800000, 1792447200000), (rows[0]['from'], rows[0]['to']))
                self.assertEqual(25, rows[-1]['hours'])
                self.assertEqual('2026-10-18T22:00:00+00:00', payload['summary']['period']['start'])
                self.assertEqual({
                    'current_start': '2026-10-18T22:00:00+00:00',
                    'current_end': '2026-10-25T23:00:00+00:00',
                    'previous_start': '2026-10-11T22:00:00+00:00',
                    'previous_end': '2026-10-18T22:00:00+00:00',
                    'current_local_start': '2026-10-19T00:00:00+02:00',
                    'current_local_end': '2026-10-26T00:00:00+01:00'},
                    {key: payload['summary']['comparison'][key] for key in (
                        'current_start', 'current_end', 'previous_start', 'previous_end',
                        'current_local_start', 'current_local_end')})
                for field in ('body', 'html'):
                    body = unescape(payload[field])
                    # Calendar row keeps the measured Warsaw day, not the preceding LA day.
                    self.assertIn(f'19 {month} 2026, 00:00 CEST (+0200) → 20 {month} 2026, 00:00 CEST (+0200)', body)
                    self.assertIn(f'25 {month} 2026, 00:00 CEST (+0200) → 26 {month} 2026, 00:00 CET (+0100)', body)
                    self.assertIn('(25h; Europe/Warsaw)', body)
                    # Absolute comparison bounds use HA presentation zone.
                    self.assertIn(f'Current: [18 {month} 2026, 15:00 PDT (-0700) → 25 {month} 2026, 16:00 PDT (-0700)', body)
                    self.assertIn(f'Previous: [11 {month} 2026, 15:00 PDT (-0700)', body)
                    self.assertIn('America/Los_Angeles', body)
                    self.assertNotIn('2026-10-19 (24h;', body)
                    self.assertNotIn('Current: [2026-', body)
                    self.assertIn('unequal month or DST lengths are not normalized', body)
                    self.assertIn('historical configuration is not reconstructed', body)
        self.assertEqual(summaries[0], summaries[1])

    def test_actual_hourly_mail_retains_both_dst_fold_offsets_and_epochs(self):
        start = datetime(2026, 10, 25, tzinfo=UTC)
        end = start + timedelta(hours=3)
        for language, month in [('pl', 'paź'), ('en', 'Oct')]:
            with self.subTest(language=language):
                payload = self.compose(start, end, language=language, display='Europe/Warsaw', report='UTC', cadence='daily')
                rows = chart(payload['summary']['visualization'], 'energy-usage')['rows']
                self.assertEqual([1792886400000, 1792890000000, 1792893600000], [r['from'] for r in rows])
                self.assertEqual('2026-10-25 · 00:00 → 01:00 (+00:00)', rows[0]['label'])
                for field in ('body', 'html'):
                    body = unescape(payload[field])
                    self.assertIn(f'25 {month} 2026, 02:00 CEST (+0200) → 25 {month} 2026, 02:00 CET (+0100)', body)
                    self.assertIn(f'25 {month} 2026, 02:00 CET (+0100) → 25 {month} 2026, 03:00 CET (+0100)', body)
                    self.assertNotIn('2026-10-25 · 00:00', body)

    def test_zero_missing_days_and_date_shaped_categories_keep_their_values(self):
        start = datetime(2026, 10, 18, 22, tzinfo=UTC)
        payload = self.compose(start, datetime(2026, 10, 25, 23, tzinfo=UTC), gap=True)
        rows = chart(payload['summary']['visualization'], 'energy-usage')['rows']
        self.assertEqual([0, None], [r['value'] for r in rows[:2]])
        self.assertEqual([True, False], [r['complete'] for r in rows[:2]])
        for field in ('body', 'html'):
            body = unescape(payload[field])
            self.assertIn('20 paź 2026, 00:00 CEST (+0200)', body)
            self.assertIn('Unknown', body)
            self.assertIn('2026-10-05', body)  # Category name is not a date bucket.
        self.assertIn('0 · measured zero', payload['html'])
        self.assertIn('Unknown / gap', payload['html'])
        self.assertEqual('unknown', payload['summary']['comparison']['status'])

    def test_source_and_logger_categories_and_unique_caveats_stay_verbatim(self):
        name = '2026-10-05 (24h; +02:00)'
        for language in ('pl', 'en'):
            energy = c.build_energy_report_payload([{'name': name, 'kwh': 0}], cadence='daily',
                presentation=c._presentation(language, 'Europe/Warsaw'))
            log = c.build_log_digest_payload([{'name': name, 'level': 'ERROR', 'count': 1,
                'timestamp': datetime(2026, 10, 18, tzinfo=UTC).timestamp()}], cadence='daily',
                now=datetime(2026, 10, 18, 1, tzinfo=UTC), presentation=c._presentation(language, 'Europe/Warsaw'))
            for payload in (energy, log):
                for field in ('body', 'html'):
                    self.assertIn(name, unescape(payload[field]))
            for field in ('body', 'html'):
                self.assertEqual(1, unescape(log[field]).count(m.LOG_CAVEAT))
                self.assertIn('Unique problems: 1', log[field])

    def test_direct_energy_html_formats_cost_and_usage_rows_without_mutating_model(self):
        start = datetime(2026, 10, 25, tzinfo=UTC)
        end = start + timedelta(hours=2)
        buckets = series(start, 2)
        costs = [{**r, 'energyCost': -.15 if i == 0 else 0, 'variableCost': None}
                 for i, r in enumerate(buckets)]
        model = m.build_energy_chart_model(summary(start, end), buckets, costs, timezone_name='UTC')
        before = copy.deepcopy(model)
        html = unescape(c.render_energy_report_html('Energy', [], 3, None, 'PLN', end,
            visualization=model, presentation=c._presentation('pl', 'Europe/Warsaw')))
        self.assertEqual(before, model)
        self.assertEqual(3, html.count('<strong>25 paź 2026, 02:00 CEST (+0200) → 25 paź 2026, 02:00 CET (+0100)'))
        self.assertIn('-0.15 PLN', html)
        self.assertIn('Unknown PLN', html)
        for item in model['charts']:
            self.assertIn(item['note'], html)
