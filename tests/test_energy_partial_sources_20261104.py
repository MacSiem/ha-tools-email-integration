"""Partial source data must remain visible without inventing a complete balance."""
import copy
import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

pkg = types.ModuleType('_partial_sources_test')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email')]
sys.modules[pkg.__name__] = pkg
model = importlib.import_module(pkg.__name__ + '.energy_model')
mail = importlib.import_module(pkg.__name__ + '.mail_energy')

def sample(count=24, mode='partial'):
    start = datetime(2026, 11, 3, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        a = start + timedelta(hours=i)
        full = mode == 'full' or mode == 'intermittent' and i % 2 == 0
        present = mode != 'none'
        recorded = '0' if mode == 'zero' else '2' if full else '1' if present else None
        rows.append({'from': a.isoformat(), 'to': (a + timedelta(hours=1)).isoformat(),
            'kwh': recorded if full else None, 'recorded_kwh': recorded,
            'measured_sources': 2 if full else 1 if present else 0, 'expected_sources': 2,
            'missing_sources': [] if full else ['b'] if present else ['a', 'b'],
            'recorded_cost': None if recorded is None else str(float(recorded) * .5),
            'currency': 'PLN', 'price_basis': 'gross', 'costs': {},
            'devices': {'a': {'kwh': '0' if mode == 'zero' else '1' if present else None}, 'b': {'kwh': '1' if full else None}}})
    return {'period': {'kind': 'day', 'start': start.isoformat(), 'end': (start + timedelta(hours=count)).isoformat(), 'timezone': 'UTC'},
        'projection': {'hours': rows, 'currency': 'PLN', 'price_basis': 'gross', 'fixed_share': None},
        'consumers': [{'stat_consumption': 'a'}, {'stat_consumption': 'b'}],
        'membership': {'sources': {'a': {'name': 'A'}, 'b': {'name': '<script>B</script>'}}},
        'colors': {'as_of': '2026-11-03', 'window_days': 30, 'devices': {}}}

class PartialSources(unittest.TestCase):
    def test_status_subtotal_and_missing_ranges(self):
        for mode, status, complete_hours, subtotal in [('full','complete',24,48),('partial','partial',0,24),('none','no_data',0,None),('zero','partial',0,0),('intermittent','partial',12,36)]:
            with self.subTest(mode=mode):
                result = model.build_report(sample(mode=mode))
                self.assertEqual(result['coverage']['status'],status)
                self.assertEqual(result['coverage']['measured_hours'],complete_hours)
                self.assertEqual(result['totals']['recorded_import_kwh'],subtotal)
                self.assertEqual(result['coverage']['expected_source_hours'],48)
                if mode == 'partial':
                    self.assertIsNone(result['totals']['import_kwh'])
                    self.assertEqual(result['totals']['recorded_usage_cost'],12)
                    self.assertEqual(len(result['coverage']['missing_sources'][0]['ranges']),1)
                if mode == 'intermittent': self.assertEqual(len(result['coverage']['missing_sources'][0]['ranges']),12)
    def test_mail_both_languages_escapes_and_draws(self):
        result = model.build_report(sample())
        for lang in ('pl','en'):
            rendered = mail.render(result, language=lang, timezone_name='UTC', hour_cycle='24')
            self.assertIn('24',rendered['subject'])
            self.assertIn('&lt;script&gt;B&lt;/script&gt;',rendered['html'])
            self.assertNotIn('<script>B</script>',rendered['html'])
            self.assertIn('<script>B</script>',rendered['text'])
            self.assertIn('data-energy-bar="0"',rendered['html'])
            self.assertIn('1.00 kWh' if lang == 'en' else '1,00 kWh',rendered['text'])
    def test_missing_price_is_not_zero(self):
        data = sample()
        data['projection']['hours'][5]['recorded_cost'] = None
        self.assertIsNone(model.build_report(data)['totals']['recorded_usage_cost'])
    def test_dst_day_lengths(self):
        for start, end, n in [('2026-03-29T00:00:00+01:00','2026-03-30T00:00:00+02:00',23),('2026-10-25T00:00:00+02:00','2026-10-26T00:00:00+01:00',25)]:
            data=sample(n)
            first=datetime.fromisoformat(start).astimezone(timezone.utc)
            for i,row in enumerate(data['projection']['hours']):
                row['from']=(first+timedelta(hours=i)).isoformat();row['to']=(first+timedelta(hours=i+1)).isoformat()
            data['period'].update(start=start,end=end,timezone='Europe/Warsaw')
            result=model.build_report(data)
            self.assertEqual(result['coverage']['expected_source_hours'],2*n)
            self.assertEqual(result['coverage']['measured_source_hours'],n)
            self.assertEqual(result['coverage']['status'],'partial')
