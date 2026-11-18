"""Build1 source coverage follows Recorder lifecycle and actual observations."""
import unittest
from datetime import datetime, timezone

from tests.test_energy_analysis import analysis
from tests.test_energy_weighted_comparison import readings
from tests.test_build1_energy_model import model


class Build1SourceCoverageTests(unittest.TestCase):
    def project(self, ids, missing=(), first=None, value=1):
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        data = readings(start, end, sources=ids, missing=missing, value=value)
        if first is not None:
            data['source_first'] = first
        acc = analysis.EnergyAnalysisAccumulator(None,
            {'start': start, 'end': end, 'timezone': 'UTC', 'kind': 'day'}, {},
            generated_at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
            consumers=[], membership={}, include_report_hours=True)
        acc.add_chunk(data, start, end)
        result = acc.finish()
        projection = acc.report_projection()
        report = model.build_report({'period': {'kind': 'day', 'start': start, 'end': end,
            'timezone': 'UTC'}, 'projection': projection, 'consumers': [], 'membership': {}})
        return result, report

    def test_lifecycle_expected_and_seen_are_period_scoped(self):
        ids = ('sensor.a', 'sensor.b')
        hour = int(datetime(2026, 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        cases = [
            ({'sensor.a': 0, 'sensor.b': hour + 7200000}, [('sensor.b', 0), ('sensor.b', 1)], (1, 1)),
            ({'sensor.a': 0, 'sensor.b': None}, [('sensor.b', 0), ('sensor.b', 1)], (1, 1)),
            ({'sensor.a': 0, 'sensor.b': hour}, [('sensor.b', 0)], (2, 2)),
            ({'sensor.a': 0, 'sensor.b': hour}, [('sensor.b', 0), ('sensor.b', 1)], (2, 1)),
        ]
        for first, missing, expected in cases:
            with self.subTest(first=first, missing=missing):
                analysis_result, report = self.project(ids, missing, first)
                for coverage in (analysis_result['coverage'], report['coverage']):
                    self.assertEqual(expected, (coverage['expected_sources'], coverage['measured_sources']))

    def test_missing_and_zero_observations_remain_distinct(self):
        ids = tuple('sensor.s%d' % i for i in range(44))
        for missing, value, expected in [
            ([(ids[-1], i) for i in range(2)], 1, (44, 43)),
            ([(sid, i) for sid in ids for i in range(2)], 1, (44, 0)),
            ([], 0, (44, 44)),
        ]:
            with self.subTest(expected=expected):
                result, report = self.project(ids, missing, value=value)
                self.assertEqual(expected, (report['coverage']['expected_sources'], report['coverage']['measured_sources']))
                self.assertEqual(result['coverage']['measured_sources'], report['coverage']['measured_sources'])
                self.assertEqual(0 if value == 0 else None if expected[1] == 0 else 86,
                                 report['totals']['import_kwh'])
