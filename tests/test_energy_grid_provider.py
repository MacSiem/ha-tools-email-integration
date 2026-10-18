"""Regress actual composer with controlled Energy/Recorder boundary responses."""
import asyncio
import copy
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests.helper_composer_schema2 import ComposerFixture
from decimal import Decimal


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
        def supplied(a,b,ids):
            return {sid:[row for row in values if a.timestamp()<=row['start']<b.timestamp()]
                    for sid,values in points.items()}
        fx=ComposerFixture(end,preferences=prefs,rows=rows,points=supplied)
        with fx.patches():
            result=asyncio.run(fx.compose(cadence,rolling=True))
        calls={'metadata_ids':fx.metadata_reads[-1],
               'statistics_ids':fx.statistics_calls[-1][2],
               'start':fx.statistics_calls[0][0],'end':fx.statistics_calls[-1][1],
               'snapshot':fx.adapter_reads[0]['result'],'producer':fx.snapshot}
        return result['summary'],calls

    def assert_ready(self, summary, calls, ids, total, cadence="daily"):
        measurement=calls['snapshot']
        self.assertEqual('ready',measurement['status'])
        self.assertTrue(summary['coverage']['complete'])
        self.assertIsInstance(summary['total_kwh'],str)
        self.assertEqual(Decimal(str(total)),Decimal(summary['total_kwh']))
        self.assertEqual(set(ids), {r['entity_id'] for r in measurement['devices']})
        self.assertEqual(len(ids),len(measurement['devices']))
        self.assertEqual(set(ids),calls['metadata_ids'])
        self.assertEqual(set(ids),calls['statistics_ids'])
        self.assertEqual(168 if cadence=='weekly' else 24,
                         int((calls['end']-calls['start']).total_seconds()/3600))
        self.assertEqual(cadence,summary['period']['cadence'])
        self.assertIsNone(summary['totals']['usage_cost'])
        self.assertIsNone(summary['total_cost'])

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
                self.assertEqual(["sensor.a", "sensor.b"], calls["snapshot"]["source_ids"])

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
                values = {r["entity_id"]: r["kwh"] for r in calls["snapshot"]["devices"]}
                self.assertAlmostEqual(hours, values["sensor.wh"])
                self.assertAlmostEqual(hours * 3, values["sensor.kwh"])
                self.assertAlmostEqual(hours * 2, values["sensor.mwh"])

    def test_gap_withholds_full_total_and_retains_measured_mixed_unit_sources(self):
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
                self.assertEqual(2, len(calls["snapshot"]["devices"]))
                self.assertEqual((24 if cadence == 'daily' else 168) * 2 - 1,
                                 calls['snapshot']['coverage']['measured_source_hours'])
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
                summary, calls = self.compose(prefs, {"sensor.grid": invalid})
                self.assertEqual("unsupported", calls["snapshot"]["status"])
                self.assertFalse(summary["coverage"]["complete"])
                self.assertIsNone(summary["total_kwh"])
                self.assertEqual([], calls["snapshot"]["devices"])

    def test_duplicate_negative_or_nonfinite_bucket_is_truthful_partial(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.grid"}]}
        for kind in ["duplicate", "negative", "nan"]:
            with self.subTest(kind=kind):
                def change(points):
                    if kind == "duplicate":
                        points["sensor.grid"].append(dict(points["sensor.grid"][0]))
                    else:
                        points["sensor.grid"][0]["change"] = -1 if kind == "negative" else float("nan")
                summary, calls = self.compose(prefs, {"sensor.grid": {"unit": "MWh"}}, mutation=change)
                self.assertEqual("partial", summary["status"])
                self.assertIsNone(summary["total_kwh"])
                self.assertEqual(23000, calls["snapshot"]["devices"][0]['kwh'])
                self.assertEqual(23, calls["snapshot"]["devices"][0]['measured_hours'])

    def test_no_grid_import_remains_no_data_not_measured_zero(self):
        fx=ComposerFixture(datetime(2026,9,30,12,tzinfo=timezone.utc),preferences={
            'energy_sources':[{'type':'solar','stat_energy_from':'sensor.solar'}]},
            rows={'sensor.solar':{}})
        with fx.patches():
            with self.assertRaisesRegex(ValueError,'energy_analysis_unavailable'):
                asyncio.run(fx.compose())
            from tests.helper_composer_schema2 import composer as actual_composer
            end=fx.h.now;start=end-timedelta(hours=24)
            raw=asyncio.run(actual_composer.async_get_energy_usage(fx.h.hass,[],start,end,'hour'))
            self.assertEqual('no_data',raw['status'])
            self.assertEqual([],raw['devices']);self.assertEqual([],raw['source_ids'])
            self.assertIsNone(raw.get('total_kwh'))
        self.assertIsNone(fx.snapshot)
        self.assertEqual([],fx.statistics_calls)
        self.assertEqual([],fx.metadata_reads)
        self.assertEqual([],fx.h.registry_ids)
        # No grid remains unavailable, never a fabricated zero-valued report.

    def test_finite_mwh_buckets_overflow_normalization_with_valid_second_provider(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": sid}
                                    for sid in ["sensor.mwh", "sensor.valid"]]}
        rows = {"sensor.mwh": {"unit": "MWh", "change": 1e304},
                "sensor.valid": {"unit": "kWh", "change": 1}}
        for cadence in ["daily", "weekly"]:
            with self.subTest(cadence=cadence):
                summary, calls = self.compose(prefs, rows, cadence=cadence)
                self.assertEqual("partial", summary["status"])
                self.assertEqual(['sensor.valid'], [r['entity_id'] for r in calls['snapshot']['devices']])
                self.assertEqual(24 if cadence == 'daily' else 168, calls['snapshot']['devices'][0]['kwh'])
                self.assertIsNone(summary['totals']['recorded_household_kwh'])
                self.assertIsNone(summary["total_kwh"])
                self.assertIsNone(summary["total_cost"])
                self.assertEqual(list(rows), calls["snapshot"]["source_ids"])
                self.assertEqual(set(rows), calls["statistics_ids"])

    def test_finite_provider_totals_overflow_aggregate_withholds_entire_period(self):
        prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": sid}
                                    for sid in ["sensor.a", "sensor.b"]]}
        # Each daily total is 9.6e307; each weekly total is 1.008e308.
        # Every bucket and individual provider total is finite, but two totals overflow.
        for cadence, change in [("daily", 4e306), ("weekly", 6e305)]:
            with self.subTest(cadence=cadence):
                rows = {"sensor.a": {"unit": "kWh", "change": change},
                        "sensor.b": {"unit": "kWh", "change": change}}
                summary, calls = self.compose(prefs, rows, cadence=cadence)
                self.assertEqual("partial", summary["status"])
                self.assertEqual([], calls["snapshot"]["devices"])
                self.assertIsNone(summary["total_kwh"])
                self.assertIsNone(summary["total_cost"])
                self.assertEqual(list(rows), calls["snapshot"]["source_ids"])
                self.assertEqual(set(rows), calls["statistics_ids"])

    def test_actual_report_rejects_missing_service_or_authenticated_actor_before_io(self):
        from tests.helper_composer_schema2 import composer as actual_composer
        for mode in ('service','actor','inactive'):
            fx=ComposerFixture(datetime(2026,9,30,12,tzinfo=timezone.utc))
            if mode=='service':fx.h.hass.data['ha_tools_email']['energy_analysis']=None
            if mode=='inactive':fx.h.user.is_active=False
            actor='forged-unbound-user' if mode=='actor' else fx.h.user.id
            with self.subTest(mode=mode),fx.patches(),self.assertRaises(
                    RuntimeError if mode=='service' else PermissionError):
                asyncio.run(actual_composer.async_build_energy_report_payload(fx.h.hass,cadence='daily',
                    now=fx.h.now,actor=actor,report_preferences=fx.prefs))
            self.assertEqual([],fx.statistics_calls)
            self.assertEqual([],fx.metadata_reads)
            self.assertEqual([],fx.h.registry_ids)
            self.assertIsNone(fx.snapshot)
