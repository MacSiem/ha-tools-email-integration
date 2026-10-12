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
        import test_tariff_profile as fixtures
        case = next(c for c in CASES if c['name'].startswith('same price'))
        payload = await fixtures.TariffComposerTest.compose(self, case['profile'])
        summary = payload['summary']
        self.assertTrue(summary['tariff_complete'])
        self.assertEqual(summary['energyCost'], 43)
        mixed = next(z for z in summary['zoneBreakdown'] if z['zone'] is None)
        self.assertEqual(mixed['zones'], ['day', 'night'])
        self.assertEqual(mixed['zoneAttribution'], 'multiple')
        self.assertEqual(mixed['kwh'], 1)
