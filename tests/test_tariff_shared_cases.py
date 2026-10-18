"""The exact shared JSON expectations also execute in the frontend model."""
import importlib.util
import json
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('shared_case_tariff', ROOT / 'custom_components/ha_tools_email/tariff.py')
tariff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tariff)
CASES = json.loads((ROOT / 'tests/fixtures/tariff-parity.json').read_text())['cases']
class SharedTariffCases(unittest.TestCase):
    def test_shared_expected_results(self):
        for case in CASES:
            with self.subTest(name=case['name']):
                before = json.dumps(case, sort_keys=True)
                model = tariff.EnergyTariff(case['profile'])
                self.assertTrue(model.valid)
                result = model.price_buckets(case['rows'], case.get('coverage'))
                for key, value in case['expected'].items(): self.assertEqual(result[key], value, key)
                if 'issueReasons' in case: self.assertEqual([v['reason'] for v in result['issues']], case['issueReasons'])
                for i, expected in enumerate(case.get('rowExpected', [])):
                    for key, value in expected.items(): self.assertEqual(result['rows'][i][key], value, key)
                self.assertEqual(json.dumps(case, sort_keys=True), before)

class SharedZoneReportTest(unittest.IsolatedAsyncioTestCase):
    async def test_report_keeps_multiple_zones_without_apportioning_reading(self):
        from decimal import Decimal
        from tests import test_tariff_profile as fixtures
        case=next(c for c in CASES if c['name'].startswith('same price'))
        payload=await fixtures.TariffComposerTest.compose(self,case['profile'])
        snapshot=self.last_fixture.snapshot
        self.assertTrue(snapshot['totals']['pricing_complete'])
        self.assertEqual(Decimal('43'),Decimal(snapshot['totals']['energy_cost']))
        mixed=next(row for row in snapshot['chart']['bins'] if
            'multiple_zones_in_measurement' in row['zone_breakdown']['unattributed']['reasons'])
        self.assertEqual(Decimal('1'),Decimal(mixed['zone_breakdown']['unattributed']['recorded_kwh']))
        self.assertEqual([],mixed['zone_breakdown']['zones'])
        self.assertEqual('1.00',mixed['usage_cost'])
        self.assertTrue(mixed['zone_breakdown']['reconciles_known_costs'])
        # Original selector proof retains the exact multiple-zone identities;
        # the authoritative period refuses to allocate the hour to either zone.
        original=tariff.EnergyTariff(case['profile']).price_buckets(case['rows'])
        self.assertIsNone(original['rows'][0]['zone'])
        self.assertEqual(['day','night'],original['rows'][0]['zones'])
        self.assertEqual('multiple',original['rows'][0]['zoneAttribution'])
        self.assertEqual(payload['snapshot_id'],snapshot['snapshot_id'])
