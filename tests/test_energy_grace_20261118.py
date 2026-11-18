"""Recorder grace remains stable across hour and local-day boundaries."""
import unittest
from datetime import datetime, timezone
from tests.test_energy_measurements import measurements  # Set up the integration package.
from custom_components.ha_tools_email.energy_native_short import cutoff_at
from custom_components.ha_tools_email.energy import measurement_window


class RecorderGraceTest(unittest.TestCase):
    def test_hour_boundary_and_local_midnight(self):
        for now, cutoff, end in (
            ('2026-01-01T11:02:00+00:00', '2026-01-01T10:55:00+00:00', '2026-01-01T10:00:00+00:00'),
            ('2026-01-01T11:05:00+00:00', '2026-01-01T11:00:00+00:00', '2026-01-01T11:00:00+00:00'),
            ('2026-01-01T00:02:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
        ):
            with self.subTest(now=now):
                now = datetime.fromisoformat(now)
                self.assertEqual(cutoff_at(now, 'UTC').isoformat(), cutoff)
                self.assertEqual(measurement_window(now, 'UTC')[1].isoformat(), end)

    def test_dst_fold_preserves_absolute_hour(self):
        now = datetime.fromisoformat('2026-10-25T01:02:00+00:00')
        self.assertEqual(cutoff_at(now, 'Europe/Warsaw').isoformat(), '2026-10-25T00:55:00+00:00')
        self.assertEqual(measurement_window(now, 'Europe/Warsaw')[1].isoformat(), '2026-10-25T00:00:00+00:00')
