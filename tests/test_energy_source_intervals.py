"""Keep measured per-source intervals for honest coverage and room analysis.

The real Recorder adapter runs through the established controlled boundary.
These catch discarding valid source readings when another source is missing,
duplicating a Recorder row, and applying unit conversion only to the total.
"""
import copy
import unittest
from unittest.mock import patch

from tests import test_energy_grid_provider as fixture

composer = fixture.composer


class EnergySourceIntervalsTest(unittest.TestCase):
    def measurement(self, rows, *, gap=None, mutation=None):
        original = composer.async_get_energy_usage
        captured = {}

        async def capture(*args, **kwargs):
            result = await original(*args, **kwargs)
            captured.update(copy.deepcopy(result))
            return result

        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": sid}
                                    for sid in rows]}
        with patch.object(composer, "async_get_energy_usage", new=capture):
            summary, _ = fixture.EnergyGridProviderTest().compose(prefs, rows, gap=gap, mutation=mutation)
        self.assertNotIn("source_buckets", summary,
                         "Private raw intervals must not silently expand rendered mail summaries")
        self.assertIn("source_buckets", captured,
                      "Coverage/grouping needs actual normalized intervals for each source")
        return captured

    def test_each_source_retains_its_normalized_measured_values(self):
        result = self.measurement({"sensor.wh": {"unit": "Wh", "change": 1000},
                                   "sensor.mwh": {"unit": "MWh", "change": .002}})
        a, b = result["source_buckets"]["sensor.wh"], result["source_buckets"]["sensor.mwh"]
        self.assertEqual(24, len(a))
        self.assertEqual({1.0}, {row["kwh"] for row in a})
        self.assertEqual({2.0}, {row["kwh"] for row in b})
        self.assertEqual({3.0}, {row["kwh"] for row in result["hourly_buckets"]})
        self.assertEqual(1790683200000, a[0]["from"])
        self.assertEqual(1790686800000, a[0]["to"])

    def test_one_gap_keeps_other_source_measured_without_inventing_zero(self):
        result = self.measurement({"sensor.a": {}, "sensor.b": {}}, gap="sensor.b")
        a, b = result["source_buckets"]["sensor.a"], result["source_buckets"]["sensor.b"]
        self.assertEqual(1, a[8]["kwh"])
        self.assertIsNone(b[8]["kwh"])
        self.assertIsNone(result["hourly_buckets"][8]["kwh"])
        self.assertEqual(47, result["coverage"]["measured_source_hours"])
        self.assertEqual(48, result["coverage"]["expected_source_hours"])
        self.assertEqual("partial", result["status"])

    def test_duplicate_invalidates_that_source_interval_only(self):
        def duplicate(points):
            points["sensor.a"].append(copy.deepcopy(points["sensor.a"][0]))

        result = self.measurement({"sensor.a": {}, "sensor.b": {}}, mutation=duplicate)
        self.assertIsNone(result["source_buckets"]["sensor.a"][0]["kwh"])
        self.assertEqual(1, result["source_buckets"]["sensor.b"][0]["kwh"])
        self.assertIsNone(result["hourly_buckets"][0]["kwh"])
        self.assertEqual(47, result["coverage"]["measured_source_hours"])


if __name__ == "__main__":
    unittest.main()
