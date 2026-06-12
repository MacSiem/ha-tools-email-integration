"""Pure scheduler tests for next-fire calculation."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
import importlib.util
from pathlib import Path


def _load_scheduler():
    path = Path(__file__).resolve().parents[1] / "custom_components/ha_tools_email/scheduler.py"
    spec = importlib.util.spec_from_file_location("ha_tools_email_scheduler", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


scheduler = _load_scheduler()
calculate_next_fire = scheduler.calculate_next_fire


class SchedulerNextFireTest(unittest.TestCase):
    """Verify cadence decisions without Home Assistant runtime imports."""

    def test_daily_fire_is_today_when_time_is_future(self) -> None:
        now = datetime(2026, 6, 12, 7, 0, tzinfo=timezone.utc)

        result = calculate_next_fire({"cadence": "daily", "time": "07:30"}, now)

        self.assertEqual(datetime(2026, 6, 12, 7, 30, tzinfo=timezone.utc), result)

    def test_daily_fire_moves_to_tomorrow_after_time_passed(self) -> None:
        now = datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)

        result = calculate_next_fire({"cadence": "daily", "time": "07:30"}, now)

        self.assertEqual(datetime(2026, 6, 13, 7, 30, tzinfo=timezone.utc), result)

    def test_weekly_fire_uses_monday_default(self) -> None:
        now = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)

        result = calculate_next_fire({"cadence": "weekly", "time": "08:00"}, now)

        self.assertEqual(datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc), result)

    def test_monthly_fire_uses_first_day(self) -> None:
        now = datetime(2026, 6, 12, 9, 0, tzinfo=timezone.utc)

        result = calculate_next_fire({"cadence": "monthly", "time": "08:00"}, now)

        self.assertEqual(datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc), result)


if __name__ == "__main__":
    unittest.main()
