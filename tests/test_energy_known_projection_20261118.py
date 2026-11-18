"""Partial source totals are real observations, never a household balance."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from tests.test_energy_analysis import analysis
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version
from tests.test_energy_partial_sources_20261104 import model, mail, sample


class KnownProjectionTest(unittest.TestCase):
    def test_43_of_44_through_analysis_projection_and_mail(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        ids = tuple('sensor.s%d' % i for i in range(44))
        data = readings(start, end, sources=ids, missing=[(ids[-1], 0), (ids[-1], 1)])
        acc = analysis.EnergyAnalysisAccumulator(book([version(rate=1, monthly=31)]),
            {'start': start, 'end': end, 'timezone': 'UTC', 'kind': 'day'}, {},
            generated_at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
            consumers=[], membership={}, include_report_hours=True)
        acc.add_chunk(data, start, end)
        result = acc.finish()
        self.assertFalse(result['coverage']['complete'])
        self.assertIsNone(result['totals']['kwh'])
        self.assertEqual(Decimal('86'), Decimal(result['known_totals']['kwh']))
        self.assertEqual('86.00', result['known_totals']['usage_cost'])
        self.assertEqual('86.08', result['known_totals']['total_cost'])
        self.assertEqual('43.0', result['known_chart']['bins'][0]['kwh'])
        self.assertFalse(result['known_chart']['bins'][0]['complete'])
        projection = acc.report_projection()
        projection['hours'][0]['recorded_cost'] = '43'
        projection['hours'][1]['recorded_cost'] = '43'
        report = model.build_report({'period': {'kind': 'day', 'start': start, 'end': end, 'timezone': 'UTC'},
            'projection': projection, 'consumers': [], 'membership': {}})
        self.assertEqual(86, report['totals']['import_kwh'])
        self.assertEqual(86, report['totals']['usage_cost'])
        self.assertEqual(86.08, report['totals']['total_with_fixed'])
        self.assertIsNone(report['unmeasured']['kwh'])
        self.assertEqual(86, report['zones'][0]['kwh'])
        for lang in ('pl', 'en'):
            out = mail.render(report, language=lang, timezone_name='UTC', hour_cycle='24')
            self.assertIn('86', out['text'])
            self.assertNotIn(' ·  · ', out['text'])

    def test_no_observations_and_measured_zero_are_distinct(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        for value in (None, 0):
            acc = analysis.EnergyAnalysisAccumulator(None,
                {'start': start, 'end': end, 'timezone': 'UTC', 'kind': 'day'}, {},
                generated_at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
            acc.add_chunk(readings(start, end, value=value), start, end)
            result = acc.finish()
            self.assertEqual(None if value is None else Decimal(0),
                None if result['known_totals']['kwh'] is None else Decimal(result['known_totals']['kwh']))
            self.assertIsNone(result['known_totals']['usage_cost'])

    def test_incomplete_residual_never_claims_complete_balance(self):
        report = model.build_report(sample(mode='intermittent'))
        self.assertEqual(36, report['totals']['import_kwh'])
        self.assertIsNone(report['unmeasured']['kwh'])
        self.assertIsNone(report['unmeasured']['share'])
