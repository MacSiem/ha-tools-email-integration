"""Literal calendar fixtures; load pure modules without Home Assistant setup."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tests.test_composer import composer


def _load_periods():
    package = "analysis_periods_fixture"
    path = Path(__file__).resolve().parents[1] / "custom_components/ha_tools_email/analysis_periods.py"
    spec = importlib.util.spec_from_file_location(package + ".periods", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {package: types.ModuleType(package),
                                 package + ".composer": composer}):
        spec.loader.exec_module(module)
    return module


periods = _load_periods()


class AnalysisPeriodsTest(unittest.TestCase):
    def resolve(self, kind, now, zone="Europe/Warsaw"):
        result = periods.resolve_analysis_periods(kind, datetime.fromisoformat(now), zone)
        self.assertEqual("ready", result["status"], result)
        json.dumps(result, allow_nan=False)
        return result

    def baseline(self, result, key="previous", index=0):
        return result["comparisons"][key]["baseline_periods"][index]

    def test_monday_to_saturday_matches_previous_iso_week(self):
        result = self.resolve("week", "2026-10-17T12:59:59+02:00")
        self.assertEqual("2026-10-12T00:00:00+02:00", result["current"]["local_start"])
        self.assertEqual("2026-10-17T12:00:00+02:00", result["current"]["local_end"])
        self.assertEqual(132, result["current"]["hours"])
        self.assertEqual("2026-10-05T00:00:00+02:00", self.baseline(result)["local_start"])
        self.assertEqual("2026-10-10T12:00:00+02:00", self.baseline(result)["local_end"])

    def test_day_noon_has_all_seven_literal_prior_days(self):
        result = self.resolve("day", "2026-10-18T12:34:56+02:00")
        self.assertEqual("2026-10-18T10:00:00+00:00", result["current"]["end"])
        self.assertEqual(12, result["current"]["hours"])
        self.assertEqual("2026-10-17T12:00:00+02:00", self.baseline(result)["local_end"])
        self.assertEqual("2026-10-11T12:00:00+02:00", self.baseline(result, "same_weekday")["local_end"])
        self.assertEqual([
            "2026-10-17T12:00:00+02:00", "2026-10-16T12:00:00+02:00",
            "2026-10-15T12:00:00+02:00", "2026-10-14T12:00:00+02:00",
            "2026-10-13T12:00:00+02:00", "2026-10-12T12:00:00+02:00",
            "2026-10-11T12:00:00+02:00"],
            [p["local_end"] for p in result["comparisons"]["mean7"]["baseline_periods"]])

    def test_month_eighteenth_noon_previous_and_year_ago(self):
        result = self.resolve("month", "2026-10-18T12:00:00+02:00")
        self.assertEqual("2026-09-01T00:00:00+02:00", self.baseline(result)["local_start"])
        self.assertEqual("2026-09-18T12:00:00+02:00", self.baseline(result)["local_end"])
        self.assertEqual("2025-10-01T00:00:00+02:00", self.baseline(result, "year_ago")["local_start"])
        self.assertEqual("2025-10-18T12:00:00+02:00", self.baseline(result, "year_ago")["local_end"])
        self.assertEqual(420, self.baseline(result)["hours"])

    def test_leap_year_fragment_clamps_february_29(self):
        result = self.resolve("year", "2024-02-29T12:00:00+01:00")
        self.assertEqual(1428, result["current"]["hours"])
        for key in ("previous", "year_ago"):
            prior = self.baseline(result, key)
            self.assertEqual("2023-02-28T12:00:00+01:00", prior["local_end"])
            self.assertEqual(1404, prior["hours"])
            self.assertTrue(prior["calendar_clamped"])

    def test_month_31_clamps_to_actual_february_end(self):
        result = self.resolve("month", "2026-03-31T12:00:00+02:00")
        self.assertEqual("2026-03-01T00:00:00+01:00", self.baseline(result)["local_end"])
        self.assertEqual(672, self.baseline(result)["hours"])
        self.assertFalse(self.baseline(result)["partial"], "A clamped complete February is not partial")
        self.assertTrue(self.baseline(result)["calendar_clamped"])
        self.assertEqual(731, result["current"]["hours"])

    def test_23_and_25_hour_local_days_preserve_absolute_hours(self):
        spring = self.resolve("day", "2026-03-29T23:59:00+02:00")
        autumn = self.resolve("day", "2026-10-25T23:59:00+01:00")
        self.assertEqual(23, spring["current"]["calendar_hours"])
        self.assertEqual(25, autumn["current"]["calendar_hours"])
        self.assertEqual(22, spring["current"]["hours"])
        self.assertEqual(24, autumn["current"]["hours"])
        self.assertEqual(23, self.baseline(spring)["hours"])
        self.assertEqual(23, self.baseline(autumn)["hours"])

    def test_seven_baselines_across_dst_are_local_dates(self):
        result = self.resolve("day", "2026-04-02T12:00:00+02:00")
        windows = result["comparisons"]["mean7"]["baseline_periods"]
        self.assertEqual([
            "2026-04-01T12:00:00+02:00", "2026-03-31T12:00:00+02:00",
            "2026-03-30T12:00:00+02:00", "2026-03-29T12:00:00+02:00",
            "2026-03-28T12:00:00+01:00", "2026-03-27T12:00:00+01:00",
            "2026-03-26T12:00:00+01:00"], [p["local_end"] for p in windows])
        self.assertEqual([12, 12, 12, 11, 12, 12, 12], [p["hours"] for p in windows])
        self.assertEqual("2026-03-26T12:00:00+01:00", self.baseline(result, "same_weekday")["local_end"])

    def test_dst_gap_and_repeated_hour_metadata(self):
        gap = self.resolve("day", "2026-03-30T02:30:00+02:00")
        self.assertEqual("2026-03-29T03:00:00+02:00", self.baseline(gap)["local_end"])
        self.assertTrue(self.baseline(gap)["calendar_clamped"])
        first = self.resolve("day", "2026-10-25T00:30:00+00:00")
        second = self.resolve("day", "2026-10-25T01:30:00+00:00")
        self.assertEqual(0, first["current"]["end_fold"])
        self.assertEqual(1, second["current"]["end_fold"])
        self.assertEqual(2, first["current"]["hours"])
        self.assertEqual(3, second["current"]["hours"])

    def test_fixed_utc_zone_and_server_instant(self):
        fixed = self.resolve("day", "2026-10-18T10:45:00+00:00", "Etc/GMT-2")
        self.assertEqual("2026-10-18T12:00:00+02:00", fixed["current"]["local_end"])
        a = self.resolve("day", "2026-10-18T10:45:00+00:00", "UTC")
        b = self.resolve("day", "2026-10-18T12:45:00+02:00", "UTC")
        self.assertEqual(a, b)

    def test_nonhour_zones_unknown_instead_of_rounding_local_midnight(self):
        for zone in ("Asia/Kolkata", "Asia/Kathmandu", "Australia/Adelaide"):
            with self.subTest(zone=zone):
                result = periods.resolve_analysis_periods("day", datetime(2026, 10, 18, 12, tzinfo=timezone.utc), zone)
                self.assertEqual("unknown", result["status"])
                self.assertEqual("calendar_boundary_not_recorder_hour", result["reason"])

    def test_invalid_clock_zone_kind_and_no_completed_interval(self):
        for now in (None, "2026-10-18", datetime(2026, 10, 18, 12)):
            self.assertEqual("aware_server_clock_required",
                             periods.resolve_analysis_periods("day", now, "UTC")["reason"])
        self.assertEqual("invalid_period_kind", periods.resolve_analysis_periods([], None, "UTC")["reason"])
        self.assertEqual("report_timezone_unavailable", periods.resolve_analysis_periods("day", None, "bad/zone")["reason"])
        self.assertEqual("no_completed_interval", periods.resolve_analysis_periods(
            "day", datetime(2026, 10, 18, 0, 59, tzinfo=timezone.utc), "UTC")["reason"])
        self.assertEqual("calendar_out_of_range", periods.resolve_analysis_periods(
            "year", datetime(1, 6, 1, 12, tzinfo=timezone.utc), "UTC")["reason"])

