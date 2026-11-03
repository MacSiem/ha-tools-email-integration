"""Exercise composition with HA's Recorder and Energy manager boundaries."""
import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from decimal import Decimal


class EnergyRecorderTest(unittest.TestCase):
    def compose(self, *, gap=False, unit="kWh", unit_class="energy"):
        from tests.helper_composer_schema2 import ComposerFixture
        end=datetime(2026,9,30,12,tzinfo=timezone.utc)
        start=end-timedelta(days=1)
        points=[{'start':(start+timedelta(hours=i)).timestamp(),
                 'end':(start+timedelta(hours=i+1)).timestamp(),'change':1}
                for i in range(24) if not (gap and i==8)]
        def recorded(a,b,ids):
            return {'sensor.grid':points,'sensor.unrelated':[dict(row,change=100) for row in points]}
        fx=ComposerFixture(end,preferences={'energy_sources':[
            {'type':'grid','stat_energy_from':'sensor.grid'},
            {'type':'grid','stat_energy_from':'sensor.grid'}]},
            rows={'sensor.grid':{'unit':unit,'unit_class':unit_class}},points=recorded)
        with fx.patches():payload=asyncio.run(fx.compose())
        self.assertEqual(1,len(fx.statistics_calls))
        self.assertEqual({'sensor.grid'},fx.statistics_calls[0][2])
        self.assertEqual(1,len(fx.manager_reads))
        payload['adapter']=fx.adapter_reads[0]['result']
        return payload

    def test_configured_grid_roots_are_unique_and_unrelated_meters_are_excluded(self):
        result = self.compose()
        self.assertEqual(Decimal('24'),Decimal(result['summary']['total_kwh']))
        self.assertEqual(["sensor.grid"], [r["entity_id"] for r in result["adapter"]["devices"]])
        self.assertIsNone(result["summary"]["total_cost"])

    def test_missing_recorder_hour_withholds_period_total(self):
        result = self.compose(gap=True)
        self.assertIsNone(result["summary"]["total_kwh"])
        self.assertFalse(result["summary"]["coverage"]["complete"])
        self.assertIn("Measured energy: Unknown kWh", result["body"])

    def test_recorder_metadata_controls_wh_conversion(self):
        self.assertEqual(Decimal('0.024'),Decimal(self.compose(unit='Wh')['summary']['total_kwh']))

    def test_non_energy_metadata_cannot_be_counted_as_kwh(self):
        self.assertIsNone(self.compose(unit="kW", unit_class="power")["summary"]["total_kwh"])

class EnergyP1WholeSourceTest(unittest.TestCase):
    def test_recorder_omitting_entire_configured_source_keeps_two_expected_sources(self):
        from tests.helper_composer_schema2 import ComposerFixture
        from tests.test_energy_measurements import measurements
        end = datetime(2026,9,30,12,tzinfo=timezone.utc); start=end-timedelta(days=1)
        points=[{'start':(start+timedelta(hours=i)).timestamp(),'change':1} for i in range(24)]
        def recorded(a,b,ids): return {'sensor.a':points}
        fx=ComposerFixture(end, preferences={'energy_sources':[{'type':'grid','stat_energy_from':'sensor.a'},
            {'type':'grid','stat_energy_from':'sensor.b'}]},
            rows={'sensor.a':{'unit':'kWh','unit_class':'energy'},'sensor.b':{'unit':'kWh','unit_class':'energy'}},points=recorded)
        with fx.patches(): payload=asyncio.run(fx.compose())
        raw=fx.adapter_reads[0]['result']
        evidence=measurements.measurement_evidence(raw,start,end)
        self.assertEqual({'sensor.a','sensor.b'},set(raw['source_ids']))
        self.assertEqual(48,evidence['coverage']['expected_source_seconds']/3600)
        self.assertEqual(24,evidence['coverage']['measured_source_seconds']/3600)
        self.assertIsNone(evidence['kwh'])
        self.assertEqual(24,evidence['observed_source_kwh'])
        self.assertTrue(all(row['expected_sources']==2 and row['kwh'] is None for row in evidence['hourly_buckets']))
