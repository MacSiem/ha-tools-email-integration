"""Recorder evidence is recomputed from source intervals, never declarations."""
import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest
from tests.test_energy_weighted_comparison import readings

spec = importlib.util.spec_from_file_location('energy_measurements', Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email/energy_measurements.py')
measurements = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measurements)


class MeasurementEvidenceTest(unittest.TestCase):
    def test_absent_readings_never_become_observed_zero(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T01:00Z'
        result = measurements.measurement_evidence(readings(start, end, value=None), start, end)
        self.assertEqual('no_data', result['status'])
        self.assertIsNone(result['kwh'])
        self.assertIsNone(result['recorded_household_kwh'])
        self.assertIsNone(result['observed_source_kwh'])
        self.assertEqual(0, result['coverage']['measured_source_seconds'])

    def test_missing_source_hour_keeps_known_cells_and_withholds_household_total(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T10:00Z'
        snapshot = readings(start, end, missing=(('sensor.b', 9),))
        original = copy.deepcopy(snapshot)
        result = measurements.measurement_evidence(snapshot, start, end)
        self.assertEqual('partial', result['status'])
        self.assertEqual(.95, result['coverage']['ratio'])
        self.assertEqual(20 * 3600, result['coverage']['expected_source_seconds'])
        self.assertEqual(19 * 3600, result['coverage']['measured_source_seconds'])
        self.assertIsNone(result['kwh'])
        self.assertEqual(18, result['recorded_household_kwh'])
        self.assertEqual([2] * 9 + [None], [row['kwh'] for row in result['hourly_buckets']])
        self.assertEqual(10, len(result['per_source']['sensor.a']))
        self.assertEqual(original, snapshot)

    def test_duplicate_invalidates_cell_even_with_declared_complete(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T10:00Z'
        snapshot = readings(start, end)
        snapshot['source_buckets']['sensor.a'].append(copy.deepcopy(snapshot['source_buckets']['sensor.a'][0]))
        result = measurements.measurement_evidence(snapshot, start, end)
        self.assertEqual(.95, result['coverage']['ratio'])
        self.assertIsNone(result['hourly_buckets'][0]['kwh'])
        self.assertFalse(result['coverage']['complete'])

    def test_unsupported_source_is_unknown_without_losing_other_readings(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T01:00Z'
        snapshot = readings(start, end)
        snapshot['measurement_provenance']['sensor.b']['unit'] = 'Wh'
        result = measurements.measurement_evidence(snapshot, start, end)
        self.assertEqual(.5, result['coverage']['ratio'])
        self.assertEqual(1, len(result['per_source']['sensor.a']))
        self.assertEqual({}, result['per_source']['sensor.b'])
        self.assertIsNone(result['kwh'])

    def test_exact_twenty_five_hour_complete_grid_and_measured_zero(self):
        start, end = '2026-10-24T22:00Z', '2026-10-25T23:00Z'
        snapshot = readings(start, end, value=0, sources=('sensor.a',))
        result = measurements.measurement_evidence(snapshot, start, end)
        self.assertEqual('ready', result['status'])
        self.assertEqual(25, len(result['hourly_buckets']))
        self.assertEqual(25 * 3600, result['coverage']['expected_source_seconds'])
        self.assertEqual(0, result['kwh'])

    def test_boundaries_and_workload_are_bounded_without_rounded_intervals(self):
        snapshot = readings('2026-01-01T00:00Z', '2026-01-01T01:00Z')
        for start, end in [('2026-01-01T00:30Z', '2026-01-01T01:00Z'),
                           ('2026-01-01T00:00', '2026-01-01T01:00'),
                           ('2026-01-01T00:00Z', '2026-01-01T00:00Z'),
                           ('2026-01-01T00:00Z', '2028-01-01T00:00Z')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                measurements.measurement_evidence(snapshot, start, end)
