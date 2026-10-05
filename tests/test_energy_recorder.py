"""Exercise composition with HA's Recorder and Energy manager boundaries."""
import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests.test_composer import composer


class EnergyRecorderTest(unittest.TestCase):
    def compose(self, *, gap=False, unit="kWh", unit_class="energy"):
        end = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        start = end - timedelta(days=1)
        points = [{"start": (start + timedelta(hours=i)).timestamp(),
                   "end": (start + timedelta(hours=i+1)).timestamp(), "change": 1}
                  for i in range(24) if not (gap and i == 8)]
        states = [types.SimpleNamespace(entity_id=sid, state="9999", attributes={
            "device_class": "energy", "state_class": "total_increasing",
            "unit_of_measurement": "kWh", "friendly_name": sid})
            for sid in ["sensor.grid", "sensor.unrelated"]]
        hass = types.SimpleNamespace(states=types.SimpleNamespace(async_all=lambda *args: states),
                                     config=types.SimpleNamespace(currency="EUR", time_zone="UTC"))
        stats = types.ModuleType("homeassistant.components.recorder.statistics")
        stats.statistics_during_period = lambda *args, **kwargs: {
            "sensor.grid": points, "sensor.unrelated": [dict(p, change=100) for p in points]}
        stats.get_metadata = lambda *args, **kwargs: {"sensor.grid": (1, {
            "has_sum": True, "unit_of_measurement": unit, "unit_class": unit_class})}
        class Recorder:
            async def async_add_executor_job(self, fn, *args, **kwargs):
                return fn(*args, **kwargs)
        util = types.ModuleType("homeassistant.components.recorder.util")
        util.get_instance = lambda hass: Recorder()
        energy = types.ModuleType("homeassistant.components.energy.data")
        async def manager(hass):
            return types.SimpleNamespace(data={"energy_sources": [
                {"type": "grid", "stat_energy_from": "sensor.grid"},
                {"type": "grid", "stat_energy_from": "sensor.grid"}]})
        energy.async_get_manager = manager
        with patch.dict(sys.modules, {stats.__name__: stats, util.__name__: util, energy.__name__: energy}):
            return asyncio.run(composer.async_build_energy_report_payload(hass, cadence="daily", now=end))

    def test_configured_grid_roots_are_unique_and_unrelated_meters_are_excluded(self):
        result = self.compose()
        self.assertEqual(24, result["summary"]["total_kwh"])
        self.assertEqual(["sensor.grid"], [r["entity_id"] for r in result["summary"]["devices"]])
        self.assertIsNone(result["summary"]["total_cost"])

    def test_missing_recorder_hour_withholds_period_total(self):
        result = self.compose(gap=True)
        self.assertIsNone(result["summary"]["total_kwh"])
        self.assertIn("Incomplete", result["body"])

    def test_recorder_metadata_controls_wh_conversion(self):
        self.assertEqual(0.024, self.compose(unit="Wh")["summary"]["total_kwh"])

    def test_non_energy_metadata_cannot_be_counted_as_kwh(self):
        self.assertIsNone(self.compose(unit="kW", unit_class="power")["summary"]["total_kwh"])
