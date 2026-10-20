"""Executable R12 examples from the supplied Build1 golden; raw facts stay exact."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
GOLDEN=json.loads((ROOT/'tests/fixtures/build1/golden-energy-2026-10-17.json').read_text())
SPEC=importlib.util.spec_from_file_location('build1_energy_format',ROOT/'custom_components/ha_tools_email/energy_format.py')
FORMAT=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FORMAT)

class Build1FormatTests(unittest.TestCase):
    def test_supplied_golden_numbers(self):
        for value,places,expected in GOLDEN['format_cases']:
            with self.subTest(value=value):self.assertEqual(FORMAT.number(value,places,'pl'),expected)

    def test_supplied_polish_plurals(self):
        for value,expected in GOLDEN['plural_cases']:
            with self.subTest(value=value):self.assertEqual(FORMAT.plural(value,'błąd','błędy','błędów','pl'),expected)

    def test_explicit_display_roles(self):
        for language,decimal in [('pl',','),('en','.')]:
            self.assertEqual(FORMAT.energy('18.77',language=language),'18'+decimal+'8 kWh')
            self.assertEqual(FORMAT.energy('18.77',role='table',period='day',language=language),'18'+decimal+'77 kWh')
            self.assertEqual(FORMAT.energy('18.77',role='table',period='week',language=language),'18'+decimal+'8 kWh')
            self.assertEqual(FORMAT.money('15.6566','PLN',language=language),'15'+decimal+'66 '+('zł' if language=='pl' else 'PLN'))
            self.assertEqual(FORMAT.rate('0.83413425679','PLN',language=language),'0'+decimal+'83 '+('zł' if language=='pl' else 'PLN')+'/kWh')
            self.assertEqual(FORMAT.percent('33.0847',role='share',language=language),'33%')
            self.assertEqual(FORMAT.percent('6.6477',role='change',language=language),'+'+'6'+decimal+'6%')
            self.assertEqual(FORMAT.percent('-6.6477',role='change',language=language),'−6'+decimal+'6%')

    def test_display_does_not_mutate_raw_facts(self):
        model=GOLDEN['model'];before=json.dumps(model,sort_keys=True)
        FORMAT.money(model['totals']['usage_cost'],'PLN')
        FORMAT.energy(model['totals']['import_kwh'],role='table',period='day')
        self.assertEqual(json.dumps(model,sort_keys=True),before)
        self.assertEqual(FORMAT.number('49.999999999999545',2,'pl'),'50,00')
        self.assertEqual(FORMAT.number('-7.225',2,'en'),'−7.23')
