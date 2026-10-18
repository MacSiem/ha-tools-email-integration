"""A gapped meter must not erase measurements from the rest of the house."""
import unittest
from tests import test_energy_grid_provider as provider


class PartialRetentionTest(unittest.TestCase):
    def test_external_statistics_use_their_recorder_display_names(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "utility:import"}]}
        summary, calls = provider.EnergyGridProviderTest().compose(
            prefs, {"utility:import": {"name": "Utility Annex <meter>", "change": 1}})
        self.assertEqual("Utility Annex <meter>", calls['snapshot']['devices'][0]['name'])

    def test_one_missing_hour_keeps_both_sources_and_recorded_profile(self):
        prefs = {"energy_sources": [{"type": "grid", "flow_from": [
            {"stat_energy_from": "sensor.complete"}, {"stat_energy_from": "sensor.gapped"}]}]}
        summary, calls = provider.EnergyGridProviderTest().compose(
            prefs, {"sensor.complete": {"change": 2}, "sensor.gapped": {"change": 1}},
            gap="sensor.gapped")
        self.assertEqual("partial", summary["status"])
        self.assertIsNone(summary["total_kwh"])
        self.assertIsNone(summary["total_cost"])
        self.assertEqual(71, calls["snapshot"]["recorded_kwh"])
        rows = {row["entity_id"]: row for row in calls["snapshot"]["devices"]}
        self.assertEqual(48, rows["sensor.complete"]["kwh"])
        self.assertEqual(23, rows["sensor.gapped"]["kwh"])
        self.assertEqual(23, rows["sensor.gapped"]["measured_hours"])
        self.assertFalse(rows["sensor.gapped"]["complete"])
        self.assertEqual(47, calls["snapshot"]["coverage"]["measured_source_hours"])
        self.assertEqual(48, calls["snapshot"]["coverage"]["expected_source_hours"])
        bins = calls['producer']['chart']['bins']
        self.assertEqual(23, sum(row['kwh'] is not None for row in bins))
        self.assertIsNone(bins[8]['kwh'])
        self.assertNotIn('source_buckets', summary)
        self.assertNotIn('devices', summary)

    def test_partial_mail_names_recorded_usage_and_source_coverage(self):
        from tests.test_composer import composer
        report = composer.build_energy_report_payload(
            [{"name": "Kitchen <meter>", "entity_id": "sensor.a", "kwh": 9,
              "measured_hours": 18, "expected_hours": 24, "complete": False}],
            cadence="daily", coverage={"complete": False, "measured_source_hours": 18,
                "expected_source_hours": 24, "complete_sources": 0, "sources": 1})
        self.assertIn("Recorded usage", report["html"])
        self.assertIn("18/24", report["html"])
        self.assertIn("Data coverage: 18/24 source-hours", report['body'])
        self.assertIn("18/24 measured hours", report['body'])
        self.assertNotIn("No energy statistics", report["html"])
        self.assertIn("Kitchen &lt;meter&gt;", report["html"])
        self.assertIsNone(report["summary"]["total_kwh"])

    def test_missing_provider_does_not_discard_the_measured_one(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": sid}
                                    for sid in ["sensor.good", "sensor.absent"]]}
        summary, calls = provider.EnergyGridProviderTest().compose(prefs, {"sensor.good": {"change": 2}})
        self.assertEqual("partial", summary["status"])
        self.assertIsNone(summary["total_kwh"])
        self.assertEqual(48, calls["snapshot"]["recorded_kwh"])
        self.assertEqual(["sensor.good"], [r["entity_id"] for r in calls["snapshot"]["devices"]])
        self.assertEqual(1, calls["snapshot"]["coverage"]["unavailable_sources"])

