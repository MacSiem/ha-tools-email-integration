"""Compare actual common source intervals, never declared record counts.

Fixtures use the Recorder provider's source_buckets/provenance contract. Losing
the intersection mask, trusting declared completeness, or summing DST as 24h
must change the numeric result or reject/allow the wrong comparison.
"""
import copy
from datetime import datetime, timedelta, timezone
import unittest

from tests.test_composer import composer

UTC = timezone.utc


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def window(start='2026-01-02T00:00Z', end='2026-01-02T10:00Z',
           previous_start='2026-01-01T00:00Z', previous_end='2026-01-01T10:00Z', zone='UTC'):
    return {'status': 'pending', 'reason': None, 'timezone': zone, 'calendar_kind': 'daily',
            'current_start': start, 'current_end': end,
            'previous_start': previous_start, 'previous_end': previous_end}


def readings(start, end, value=1, missing=(), sources=('sensor.a', 'sensor.b')):
    start, end = instant(start), instant(end)
    hours = int((end - start).total_seconds() / 3600)
    buckets = {}
    for sid in sources:
        buckets[sid] = [{'from': int((start + timedelta(hours=i)).timestamp() * 1000),
                         'to': int((start + timedelta(hours=i + 1)).timestamp() * 1000),
                         'kwh': None if (sid, i) in missing else value} for i in range(hours)]
    complete = all(row['kwh'] is not None for rows in buckets.values() for row in rows)
    return {'status': 'ready' if complete else 'partial', 'source_ids': list(sources),
            'source_buckets': buckets,
            'devices': [{'entity_id': sid, 'name': sid,
                         'kwh': sum(row['kwh'] for row in buckets[sid] if row['kwh'] is not None)}
                        for sid in sources],
            'measurement_provenance': {sid: {'method': 'recorder_hourly_change_v1', 'unit': 'kWh',
                'source_unit': 'kWh', 'unit_class': 'energy', 'has_sum': True} for sid in sources},
            # Counts are intentionally not evidence: the comparison derives them.
            'coverage': {'complete': True, 'expected_source_hours': 1, 'measured_source_hours': 9999}}


class WeightedComparisonTest(unittest.TestCase):
    def pair(self, missing_current=(), missing_previous=(), current_value=1, previous_value=2):
        period = window()
        current = readings(period['current_start'], period['current_end'], current_value, missing_current)
        previous = readings(period['previous_start'], period['previous_end'], previous_value, missing_previous)
        return current, previous, period

    def test_ninety_percent_common_readings_compare_without_extrapolation(self):
        missing = (('sensor.b', 8), ('sensor.b', 9))
        current, previous, period = self.pair(missing, missing)
        before = copy.deepcopy((current, previous, period))
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('ready', result['status'])
        self.assertEqual('common_measurements', result['comparison_scope'])
        self.assertEqual((18, 36, -18, -50), tuple(result[k] for k in
                         ('current_kwh', 'previous_kwh', 'delta_kwh', 'percent')))
        self.assertIsNone(result['full_current_kwh'])
        self.assertEqual(.9, result['coverage']['current'])
        self.assertEqual(.9, result['coverage']['common_current'])
        self.assertEqual(20 * 3600, result['coverage']['expected_current_seconds'])
        self.assertEqual(before, (current, previous, period))

    def test_disjoint_missing_cells_do_not_make_ninety_percent_common_coverage(self):
        current, previous, period = self.pair(
            (('sensor.b', 8), ('sensor.b', 9)), (('sensor.a', 0), ('sensor.a', 1)))
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('unknown', result['status'])
        self.assertIn('coverage', result, 'Coverage must be derived from actual source intervals')
        self.assertEqual(.9, result['coverage']['current'])
        self.assertEqual(.9, result['coverage']['previous'])
        self.assertEqual(.8, result['coverage']['common_current'])
        self.assertIsNone(result['percent'])

    def test_duplicate_cannot_inflate_coverage_or_preserve_ready_declaration(self):
        current, previous, period = self.pair()
        current['source_buckets']['sensor.a'].append(copy.deepcopy(current['source_buckets']['sensor.a'][0]))
        current['source_buckets']['sensor.a'][1]['kwh'] = None
        current['source_buckets']['sensor.a'][2]['kwh'] = None
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('unknown', result['status'])
        self.assertEqual(.85, result['coverage']['current'])
        self.assertIsNone(result['percent'])

    def test_below_threshold_is_not_compared_even_with_forged_complete_counts(self):
        missing = (('sensor.a', 0), ('sensor.a', 1), ('sensor.b', 0))
        current, previous, period = self.pair(missing, missing)
        current['status'] = previous['status'] = 'ready'
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('unknown', result['status'])
        self.assertEqual(.85, result['coverage']['current'])
        self.assertIsNone(result['delta_kwh'])

    def test_zero_baseline_has_difference_without_fictional_percentage(self):
        missing = (('sensor.b', 8), ('sensor.b', 9))
        result = composer._compare_energy_snapshots(*self.pair(missing, missing, previous_value=0))
        self.assertEqual('ready', result['status'])
        self.assertEqual(18, result['delta_kwh'])
        self.assertEqual(0, result['previous_kwh'])
        self.assertIsNone(result['percent'])

    def test_changed_sources_or_normalization_stay_uncomparable(self):
        current, previous, period = self.pair((('sensor.b', 8),), ())
        previous['source_ids'] = ['sensor.other']
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('source_set_changed', result['reason'])
        current, previous, period = self.pair()
        previous['measurement_provenance']['sensor.a']['source_unit'] = 'Wh'
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('measurement_provenance_unavailable_or_changed', result['reason'])

    def test_actual_twenty_five_hour_day_keeps_full_calendar_totals(self):
        period = window('2026-10-24T22:00Z', '2026-10-25T23:00Z',
                        '2026-10-23T22:00Z', '2026-10-24T22:00Z', 'Europe/Warsaw')
        current = readings(period['current_start'], period['current_end'], sources=('sensor.a',))
        previous = readings(period['previous_start'], period['previous_end'], sources=('sensor.a',))
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('ready', result['status'])
        self.assertIn('coverage', result)
        self.assertEqual(25 * 3600, result['coverage']['expected_current_seconds'])
        self.assertEqual(24 * 3600, result['coverage']['expected_previous_seconds'])
        self.assertEqual(1, result['delta_kwh'])
        self.assertAlmostEqual(100 / 24, result['percent'])

    def test_missing_repeated_dst_hour_does_not_turn_other_readings_into_zero(self):
        period = window('2026-10-24T22:00Z', '2026-10-25T23:00Z',
                        '2026-10-23T22:00Z', '2026-10-24T22:00Z', 'Europe/Warsaw')
        current = readings(period['current_start'], period['current_end'],
                           missing=(('sensor.a', 3),), sources=('sensor.a',))
        previous = readings(period['previous_start'], period['previous_end'], sources=('sensor.a',))
        result = composer._compare_energy_snapshots(current, previous, period)
        self.assertEqual('ready', result['status'])
        self.assertEqual(.96, result['coverage']['current'])
        self.assertEqual(24, result['current_kwh'])
        self.assertEqual(0, result['delta_kwh'])
        self.assertIsNone(result['full_current_kwh'])


if __name__ == '__main__':
    unittest.main()
