"""Historical calendar selection preserves complete periods and server cutoffs."""
import unittest
from datetime import datetime
from tests.test_analysis_periods import periods


class AnalysisAnchorTest(unittest.TestCase):
    def resolve(self, kind, anchor, *, now='2026-11-02T12:34:00+00:00', zone='Europe/Warsaw'):
        result = periods.resolve_analysis_periods(kind, datetime.fromisoformat(now), zone, anchor=anchor)
        self.assertEqual('ready', result['status'], result)
        return result

    def test_completed_historical_day_keeps_23_and_25_hour_dst_totals(self):
        for anchor, start, end, hours in (
            ('2026-03-29', '2026-03-29T00:00:00+01:00', '2026-03-30T00:00:00+02:00', 23),
            ('2026-10-25', '2026-10-25T00:00:00+02:00', '2026-10-26T00:00:00+01:00', 25),
        ):
            with self.subTest(anchor=anchor):
                result = self.resolve('day', anchor)
                self.assertEqual(start, result['current']['local_start'])
                self.assertEqual(end, result['current']['local_end'])
                self.assertEqual(hours, result['current']['hours'])
                self.assertFalse(result['current']['partial'])
                prior = result['comparisons']['previous']['baseline_periods'][0]
                self.assertEqual(24, prior['hours'])
                self.assertFalse(prior['partial'])
                self.assertEqual(7, len(result['comparisons']['mean7']['baseline_periods']))
                self.assertTrue(all(not p['partial'] for p in result['comparisons']['mean7']['baseline_periods']))

    def test_completed_week_month_and_leap_year_are_calendar_periods(self):
        for kind, anchor, start, end, hours in (
            ('week', '2026-03-29', '2026-03-23T00:00:00+01:00', '2026-03-30T00:00:00+02:00', 167),
            ('month', '2026-03-29', '2026-03-01T00:00:00+01:00', '2026-04-01T00:00:00+02:00', 743),
            ('year', '2024-06-15', '2024-01-01T00:00:00+01:00', '2025-01-01T00:00:00+01:00', 8784),
        ):
            with self.subTest(kind=kind):
                result = self.resolve(kind, anchor)
                self.assertEqual(start, result['current']['local_start'])
                self.assertEqual(end, result['current']['local_end'])
                self.assertEqual(hours, result['current']['hours'])
                self.assertFalse(result['current']['partial'])
        leap = self.resolve('year', '2024-06-15')
        self.assertEqual(8760, leap['comparisons']['previous']['baseline_periods'][0]['hours'])
        self.assertEqual(8760, leap['comparisons']['year_ago']['baseline_periods'][0]['hours'])

    def test_current_selected_period_still_stops_at_server_completed_hour(self):
        now = '2026-10-18T12:34:00+02:00'
        for kind in ('day', 'week', 'month', 'year'):
            with self.subTest(kind=kind):
                selected = self.resolve(kind, '2026-10-18', now=now)
                automatic = periods.resolve_analysis_periods(kind, datetime.fromisoformat(now), 'Europe/Warsaw')
                self.assertEqual(automatic, selected)
                self.assertEqual('2026-10-18T12:00:00+02:00', selected['current']['local_end'])
                self.assertTrue(selected['current']['partial'])

    def test_yesterday_is_explicitly_available_at_new_calendar_boundary(self):
        now = '2026-10-18T00:10:00+02:00'
        automatic = periods.resolve_analysis_periods('day', datetime.fromisoformat(now), 'Europe/Warsaw')
        self.assertEqual('no_completed_interval', automatic['reason'])
        selected = self.resolve('day', '2026-10-17', now=now)
        self.assertEqual(24, selected['current']['hours'])
        self.assertFalse(selected['current']['partial'])

    def test_invalid_or_future_anchor_is_rejected_without_guessing(self):
        now = datetime.fromisoformat('2026-10-18T12:34:00+00:00')
        for anchor in ('', '20261017', '2026-2-01', '2026-02-30', '2026-10-17T00:00Z', True, 1, [], {}):
            with self.subTest(anchor=anchor):
                result = periods.resolve_analysis_periods('day', now, 'UTC', anchor=anchor)
                self.assertEqual('unknown', result['status'])
                self.assertEqual('invalid_analysis_anchor', result['reason'])
                self.assertIsNone(result['current'])
        future = periods.resolve_analysis_periods('day', now, 'UTC', anchor='2026-10-19')
        self.assertEqual('future_analysis_anchor', future['reason'])
        self.assertIsNone(future['current'])

    def test_future_selection_uses_measurement_zone_local_date(self):
        now = datetime.fromisoformat('2026-10-18T00:30:00+00:00')
        result = periods.resolve_analysis_periods('day', now, 'America/Los_Angeles', anchor='2026-10-18')
        self.assertEqual('future_analysis_anchor', result['reason'])
        today = self.resolve('day', '2026-10-17', now=now.isoformat(), zone='America/Los_Angeles')
        self.assertEqual(17, today['current']['hours'])
