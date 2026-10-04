"""Regress actual composer with controlled Energy/Recorder boundary responses."""
import asyncio
import copy
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests.test_composer import composer


class EnergyGridProviderTest(unittest.TestCase):
    def compose(self, prefs, rows, *, cadence="daily", gap=None, mutation=None):
        end = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        hours = 168 if cadence == "weekly" else 24
        start = end - timedelta(hours=hours)
        points = {
            sid: [{"start": (start + timedelta(hours=i)).timestamp(),
                   "change": row.get("change", 1)}
                  for i in range(hours) if not (gap == sid and i == 8)]
            for sid, row in rows.items()
        }
        if mutation:
            mutation(points)
        calls = {}
        stats = types.ModuleType("homeassistant.components.recorder.statistics")

        def metadata(hass, *, statistic_ids):
            calls["metadata_ids"] = set(statistic_ids)
            return {sid: (1, {"has_sum": row.get("has_sum", True),
                             "unit_of_measurement": row.get("unit", "kWh"),
                             "unit_class": row.get("unit_class", "energy")})
                    for sid, row in rows.items()}

        def statistics(hass, actual_start, actual_end, ids, *args):
            calls.update(statistics_ids=set(ids), start=actual_start, end=actual_end)
            # Include unrelated output as defense against summing arbitrary rows.
            return points

        stats.get_metadata = metadata
        stats.statistics_during_period = statistics

        class Recorder:
            async def async_add_executor_job(self, fn, *args, **kwargs):
                return fn(*args, **kwargs)

        util = types.ModuleType("homeassistant.components.recorder.util")
        util.get_instance = lambda hass: Recorder()
        energy = types.ModuleType("homeassistant.components.energy.data")

        async def manager(hass):
            return types.SimpleNamespace(data=copy.deepcopy(prefs))

        energy.async_get_manager = manager
        states = {sid: types.SimpleNamespace(attributes={"friendly_name": sid}) for sid in rows}
        hass = types.SimpleNamespace(states=states,
                                     config=types.SimpleNamespace(currency="EUR", time_zone="UTC"))
        with patch.dict(sys.modules, {stats.__name__: stats, util.__name__: util,
                                      energy.__name__: energy}):
            result = asyncio.run(composer.async_build_energy_report_payload(
                hass, cadence=cadence, now=end))
        return result["summary"], calls

    def assert_ready(self, summary, calls, ids, total, cadence="daily"):
        self.assertEqual("ready", summary["status"])
        self.assertAlmostEqual(total, summary["total_kwh"])
        self.assertEqual(set(ids), {r["entity_id"] for r in summary["devices"]})
        self.assertEqual(len(ids), len(summary["devices"]))
        self.assertEqual(set(ids), calls["metadata_ids"])
        self.assertEqual(set(ids), calls["statistics_ids"])
        self.assertEqual(168 if cadence == "weekly" else 24,
                         int((calls["end"] - calls["start"]).total_seconds() / 3600))
        self.assertEqual(cadence, summary["period"]["cadence"])
        self.assertIsNone(summary["total_cost"])

    def test_nested_grid_unique_multiimport_excludes_export_solar_and_unrelated(self):
        prefs = {"energy_sources": [
            {"type": "grid", "flow_from": [{"stat_energy_from": "sensor.a"},
                                             {"stat_energy_from": "sensor.a"},
                                             {"stat_energy_from": "sensor.b"}],
             "flow_to": [{"stat_energy_to": "sensor.export"}]},
            {"type": "solar", "stat_energy_from": "sensor.solar"},
            {"type": "battery", "stat_energy_from": "sensor.battery"}]}
        rows = {"sensor.a": {"change": 1}, "sensor.b": {"change": 2},
                "sensor.export": {"change": 100}, "sensor.solar": {"change": 100},
                "sensor.battery": {"change": 100}, "sensor.unrelated": {"change": 100}}
        for cadence, expected in [("daily", 72), ("weekly", 504)]:
            with self.subTest(cadence=cadence):
                summary, calls = self.compose(prefs, rows, cadence=cadence)
                self.assert_ready(summary, calls, ["sensor.a", "sensor.b"], expected, cadence)
                self.assertEqual(["sensor.a", "sensor.b"], summary["source_ids"])

    def test_truthy_flat_wins_over_nested_and_empty_flat_falls_back(self):
        prefs = {"energy_sources": [
            {"type": "grid", "stat_energy_from": "sensor.flat",
             "flow_from": [{"stat_energy_from": "sensor.ignored"}]},
            {"type": "grid", "stat_energy_from": "",
             "flow_from": [{"stat_energy_from": "sensor.nested"}]}]}
        summary, calls = self.compose(prefs, {"sensor.flat": {}, "sensor.nested": {},
                                            "sensor.ignored": {"change": 100}})
        self.assert_ready(summary, calls, ["sensor.flat", "sensor.nested"], 48)

    def test_invalid_ids_or_source_rows_do_not_hide_valid_nested_import(self):
        prefs = {"energy_sources": [None, "invalid", {"type": "grid", "flow_from": [
            None, "bad", {}, {"stat_energy_from": None}, {"stat_energy_from": 1},
            {"stat_energy_from": ""}, {"stat_energy_from": "   "},
            {"stat_energy_from": "sensor.valid"}, {"stat_energy_from": "sensor.valid"}]},
            {"type": "grid", "stat_energy_from": 123,
             "flow_from": [{"stat_energy_from": "sensor.suppressed"}]}]}
        summary, calls = self.compose(prefs, {"sensor.valid": {},
                                            "sensor.suppressed": {"change": 100}})
        self.assert_ready(summary, calls, ["sensor.valid"], 24)

    def test_wh_kwh_mwh_convert_daily_and_weekly_using_actual_metadata(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.grid"}]}
        for cadence, hours in [("daily", 24), ("weekly", 168)]:
            for unit, change in [("Wh", 1000), ("kWh", 1), ("MWh", 0.001)]:
                with self.subTest(cadence=cadence, unit=unit):
                    summary, calls = self.compose(prefs, {"sensor.grid": {"unit": unit,
                                                                         "change": change}}, cadence=cadence)
                    self.assert_ready(summary, calls, ["sensor.grid"], hours, cadence)

    def test_mixed_units_are_normalized_per_source_before_sum(self):
        prefs = {"energy_sources": [{"type": "grid", "flow_from": [
            {"stat_energy_from": sid} for sid in ["sensor.wh", "sensor.kwh", "sensor.mwh"]]}]}
        rows = {"sensor.wh": {"unit": "Wh", "change": 1000},
                "sensor.kwh": {"unit": "kWh", "change": 3},
                "sensor.mwh": {"unit": "MWh", "change": 0.002}}
        for cadence, hours in [("daily", 24), ("weekly", 168)]:
            with self.subTest(cadence=cadence):
                summary, calls = self.compose(prefs, rows, cadence=cadence)
                self.assert_ready(summary, calls, list(rows), hours * 6, cadence)
                values = {r["entity_id"]: r["kwh"] for r in summary["devices"]}
                self.assertAlmostEqual(hours, values["sensor.wh"])
                self.assertAlmostEqual(hours * 3, values["sensor.kwh"])
                self.assertAlmostEqual(hours * 2, values["sensor.mwh"])

    def test_gap_in_one_mixed_unit_source_withholds_the_entire_period(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": sid}
                                    for sid in ["sensor.wh", "sensor.mwh"]]}
        rows = {"sensor.wh": {"unit": "Wh", "change": 1000},
                "sensor.mwh": {"unit": "MWh", "change": 0.001}}
        for cadence in ["daily", "weekly"]:
            with self.subTest(cadence=cadence):
                summary, calls = self.compose(prefs, rows, cadence=cadence, gap="sensor.mwh")
                self.assertEqual("partial", summary["status"])
                self.assertIsNone(summary["total_kwh"])
                self.assertIsNone(summary["total_cost"])
                self.assertEqual([], summary["devices"])
                self.assertEqual(set(rows), calls["statistics_ids"])

    def test_complete_zero_is_measured_zero_for_nested_mwh(self):
        prefs = {"energy_sources": [{"type": "grid", "flow_from": [
            {"stat_energy_from": "sensor.grid"}]}]}
        for cadence in ["daily", "weekly"]:
            with self.subTest(cadence=cadence):
                summary, calls = self.compose(prefs, {"sensor.grid": {"unit": "MWh",
                                                                     "change": 0}}, cadence=cadence)
                self.assert_ready(summary, calls, ["sensor.grid"], 0, cadence)

    def test_unsupported_metadata_still_withholds_all_values(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.grid"}]}
        for invalid in [{"unit": "kW", "unit_class": "power"}, {"unit": "MWh", "has_sum": False},
                        {"unit": "MWh", "unit_class": "power"}]:
            with self.subTest(metadata=invalid):
                summary, _ = self.compose(prefs, {"sensor.grid": invalid})
                self.assertEqual("unsupported", summary["status"])
                self.assertIsNone(summary["total_kwh"])
                self.assertEqual([], summary["devices"])

    def test_duplicate_negative_or_nonfinite_bucket_is_truthful_partial(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.grid"}]}
        for kind in ["duplicate", "negative", "nan"]:
            with self.subTest(kind=kind):
                def change(points):
                    if kind == "duplicate":
                        points["sensor.grid"].append(dict(points["sensor.grid"][0]))
                    else:
                        points["sensor.grid"][0]["change"] = -1 if kind == "negative" else float("nan")
                summary, _ = self.compose(prefs, {"sensor.grid": {"unit": "MWh"}}, mutation=change)
                self.assertEqual("partial", summary["status"])
                self.assertIsNone(summary["total_kwh"])
                self.assertEqual([], summary["devices"])

    def test_no_grid_import_remains_no_data_not_measured_zero(self):
        summary, calls = self.compose({"energy_sources": [{"type": "solar",
                    "stat_energy_from": "sensor.solar"}]}, {"sensor.solar": {}})
        self.assertEqual("no_data", summary["status"])
        self.assertIsNone(summary["total_kwh"])
        self.assertEqual([], summary["devices"])
        self.assertEqual([], summary["source_ids"])
        self.assertEqual({}, calls)
