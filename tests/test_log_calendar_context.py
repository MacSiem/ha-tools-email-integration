"""A restarted HA buffer is not proof of a quiet previous day."""
import asyncio
import types
import unittest
from datetime import datetime, timedelta, timezone
from tests.test_composer import composer


class CalendarLogContextTest(unittest.TestCase):
    def test_today_diagnostics_are_visible_but_not_counted_as_yesterday(self):
        now = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
        end = now.replace(hour=0)
        entries = [{"name": "demo", "level": "ERROR", "message": "Current <failure>",
                    "timestamp": now.timestamp() - 10, "count": 3}]
        hass = types.SimpleNamespace(data={"system_log": types.SimpleNamespace(records=entries)})
        payload = asyncio.run(composer.async_build_log_digest_payload(
            hass, object(), cadence="daily", now=now, window_start=end-timedelta(days=1),
            window_end=end, manual_window=True))
        caveat="Occurrences in retained aggregates; not exact events or complete event history."
        for field in ('body','html'):
            self.assertEqual(1,payload[field].count(caveat))
            self.assertEqual(1,payload[field].count('How to read these numbers'))
            self.assertIn('¹',payload[field])
            self.assertIn('rows displayed;',payload[field])
            self.assertIn('Unknown means a gap or unavailable value; zero is measured zero.',payload[field])
            self.assertGreater(payload[field].index('How to read these numbers'),payload[field].index('Current diagnostics'))
        self.assertIsNone(payload["summary"]["total"])
        self.assertEqual(0, payload["summary"]["unique_problems"])
        self.assertEqual(3, payload["summary"]["current_context"]["total"])
        self.assertIn("Current diagnostics", payload["html"])
        self.assertIn("Current &lt;failure&gt;", payload["html"])
        self.assertIn("outside the requested report period", payload["body"])
        self.assertNotIn("Total counted warnings/errors: 0", payload["body"])

