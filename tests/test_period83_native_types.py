"""HA YAML wraps trusted timezone text in a str subclass (NodeStrClass)."""
import unittest
from decimal import Decimal
from tests import test_energy_analysis as fixture

class YamlText(str):
    """Same primitive contract as HA NodeStrClass; no value coercion."""

class PeriodNativeText83Test(unittest.TestCase):
    def test_yaml_text_period_is_public_builtin_text_without_changing_energy_or_cost(self):
        helper=fixture.EnergyAnalysisTest()
        b=helper.builder(period_extra={key:YamlText(value) for key,value in {
            'start':'2026-01-01T00:00Z','end':'2026-01-01T02:00Z',
            'timezone':'UTC','kind':'day'}.items()})
        helper.add(b,'2026-01-01T00:00Z','2026-01-01T01:00Z',1)
        helper.add(b,'2026-01-01T01:00Z','2026-01-01T02:00Z',2)
        dto=b.finish()
        for key in ['start','end','timezone','kind']:
            self.assertIs(type(dto['period'][key]),str,key)
        self.assertEqual(dto['period']['timezone'],'UTC')
        self.assertEqual(dto['period']['kind'],'day')
        self.assertEqual(Decimal(dto['totals']['kwh']),Decimal(3))
        self.assertEqual(dto['totals']['energy_cost'],'3.00')
        self.assertEqual(dto['totals']['fixed_cost'],'0.08')
        self.assertEqual(dto['totals']['total_cost'],'3.08')

    def test_nontext_objects_and_numeric_subclasses_are_still_rejected(self):
        class LooksLikeText:
            def __str__(self):return 'UTC'
        class WrappedInt(int):pass
        for value in [LooksLikeText(),object(),WrappedInt(1)]:
            with self.subTest(value=type(value).__name__), self.assertRaisesRegex(ValueError,'invalid_analysis_period'):
                fixture.analysis._public_period({'timezone':value})
