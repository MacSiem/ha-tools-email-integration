"""Literal calendar totals and common source-hour coverage across chunk splits."""
import copy
from datetime import datetime, timedelta, timezone
import importlib
import unittest
from tests.test_energy_analysis_service import service
from tests.test_energy_weighted_comparison import readings


def instant(value):
    return datetime.fromisoformat(value)


class ComparisonStreamTest(unittest.TestCase):
    def model(self, current, baseline, kind='daily', zone='UTC'):
        module = importlib.import_module(service.__package__ + '.energy_comparison_stream')
        return module.PairedComparisonAccumulator(
            dict(zip(('start', 'end'), current)), dict(zip(('start', 'end'), baseline)),
            timezone_name=zone, calendar_kind=kind)

    def run_pair(self, current, baseline, *, kind='daily', zone='UTC', split=168,
                 sources=('sensor.a',), missing=None, values=(1, 1)):
        model = self.model(current, baseline, kind, zone)
        cursors = [instant(current[0]), instant(baseline[0])]
        starts, ends = cursors[:], [instant(current[1]), instant(baseline[1])]
        calls = 0
        while (side := model.needs_side()) is not None:
            index = 0 if side == 'current' else 1
            start = cursors[index]; end = min(start + timedelta(hours=split), ends[index])
            offset = int((start-starts[index]).total_seconds()/3600)
            absent = [(sid, hour-offset) for sid, hour in (missing or {}).get(side, ())
                      if offset <= hour < offset + (end-start).total_seconds()/3600]
            data = readings(start.isoformat(), end.isoformat(), sources=sources,
                            value=values[index], missing=absent)
            model.push(side, data, start, end)
            cursors[index] = end; calls += 1
        return model.finish(), model, calls

    def test_full_dst_actual_totals_and_zero_baseline(self):
        current = ('2026-10-24T22:00Z', '2026-10-25T23:00Z')
        baseline = ('2026-10-23T22:00Z', '2026-10-24T22:00Z')
        for split in (1, 2, 7, 24, 168):
            with self.subTest(split=split):
                result, _, _ = self.run_pair(current, baseline, zone='Europe/Warsaw', split=split)
                self.assertEqual('ready', result['status'])
                self.assertEqual('calendar_totals', result['comparison_scope'])
                self.assertEqual((25, 24, 1), tuple(result[k] for k in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
                self.assertAlmostEqual(100/24, result['percent'])
        result, _, _ = self.run_pair(current, baseline, zone='Europe/Warsaw', values=(1, 0))
        self.assertEqual(25, result['delta_kwh']); self.assertIsNone(result['percent'])

    def test_partial_uses_identical_frames_and_all_four_coverage_ratios(self):
        bounds = [('2026-01-01T00:00Z', '2026-01-01T10:00Z'),
                  ('2025-12-31T00:00Z', '2025-12-31T10:00Z')]
        for same, expected in ((True, 'ready'), (False, 'unknown')):
            absent = [('sensor.a', 0), ('sensor.b', 0)]
            missing = {'current': absent, 'baseline': absent if same else [('sensor.a', 1), ('sensor.b', 1)]}
            result, _, _ = self.run_pair(*bounds, sources=('sensor.a', 'sensor.b'), missing=missing, split=3)
            self.assertEqual(expected, result['status'])
            self.assertEqual(.9, result['coverage']['current'])
            self.assertEqual(.9 if same else .8, result['coverage']['common_current'])
            if same:
                self.assertEqual((18, 18), (result['current_kwh'], result['baseline_kwh']))
                self.assertEqual('common_measurements', result['comparison_scope'])
            else:
                self.assertIsNone(result['delta_kwh'])

    def test_unmatched_fold_counts_own_only_without_lowering_threshold(self):
        current = ('2026-10-24T22:00Z', '2026-10-25T23:00Z')
        baseline = ('2026-10-23T22:00Z', '2026-10-24T22:00Z')
        result, _, _ = self.run_pair(current, baseline, zone='Europe/Warsaw', split=2,
                                   missing={'current': [('sensor.a', 20)]})
        self.assertEqual('ready', result['status'])
        self.assertEqual((23, 23), (result['current_kwh'], result['baseline_kwh']))
        self.assertEqual(24/25, result['coverage']['current'])
        self.assertEqual(23/25, result['coverage']['common_current'])

    def test_year_leap_and_different_month_lengths_never_zip_shift(self):
        result, _, calls = self.run_pair(('2024-01-01T00:00Z', '2025-01-01T00:00Z'),
            ('2023-01-01T00:00Z', '2024-01-01T00:00Z'), kind='yearly')
        self.assertEqual((8784, 8760, 24), tuple(result[k] for k in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
        self.assertEqual(106, calls)
        # A missing March1 cell must not be paired to February29 after the leap.
        missing = {'current': [('sensor.a', 60*24)], 'baseline': [('sensor.a', 59*24)]}
        result, _, _ = self.run_pair(('2024-01-01T00:00Z', '2025-01-01T00:00Z'),
            ('2023-01-01T00:00Z', '2024-01-01T00:00Z'), kind='yearly', missing=missing)
        self.assertEqual((8759, 8759), (result['current_kwh'], result['baseline_kwh']))
        result, _, _ = self.run_pair(('2026-01-01T00:00Z', '2026-02-01T00:00Z'),
            ('2026-02-01T00:00Z', '2026-03-01T00:00Z'), kind='monthly', split=73)
        self.assertEqual((744, 672), (result['current_kwh'], result['baseline_kwh']))

    def test_rejected_push_does_not_advance_or_poison_retry_and_inputs_detached(self):
        current = ('2026-01-01T00:00Z', '2026-01-01T01:00Z')
        baseline = ('2025-12-31T00:00Z', '2025-12-31T01:00Z')
        model = self.model(current, baseline)
        snapshot = readings(*current, sources=('sensor.a',))
        bad = copy.deepcopy(snapshot); bad['measurement_provenance']['sensor.a']['unit'] = 'Wh'
        with self.assertRaises(ValueError): model.push('current', bad, *current)
        self.assertEqual('current', model.needs_side())
        model.push('current', snapshot, *current)
        snapshot['source_buckets']['sensor.a'][0]['kwh'] = 5000
        with self.assertRaises(ValueError): model.push('current', snapshot, *current)
        model.push('baseline', readings(*baseline, sources=('sensor.a',)), *baseline)
        result = model.finish(); self.assertEqual(1, result['current_kwh'])
        result['current_kwh'] = 99
        self.assertEqual(1, model.finish()['current_kwh'])

    def test_duplicate_invalidates_cell_and_source_changes_fail(self):
        current = ('2026-01-01T00:00Z', '2026-01-01T10:00Z')
        baseline = ('2025-12-31T00:00Z', '2025-12-31T10:00Z')
        model = self.model(current, baseline)
        snapshot = readings(*current, sources=('sensor.a',))
        snapshot['source_buckets']['sensor.a'].append(copy.deepcopy(snapshot['source_buckets']['sensor.a'][0]))
        model.push('current', snapshot, *current)
        with self.assertRaises(ValueError): model.push('baseline', readings(*baseline, sources=('sensor.b',)), *baseline)
        model.push('baseline', readings(*baseline, sources=('sensor.a',)), *baseline)
        result = model.finish()
        self.assertEqual(.9, result['coverage']['current'])
        self.assertEqual(9, result['current_kwh'])

    def test_year_at_max_sources_retains_only_two_week_chunks(self):
        result, model, calls = self.run_pair(('2024-01-01T00:00Z', '2025-01-01T00:00Z'),
            ('2023-01-01T00:00Z', '2024-01-01T00:00Z'), kind='yearly',
            sources=tuple(f'sensor.s{i}' for i in range(512)))
        self.assertEqual(8784*512, result['current_kwh'])
        self.assertEqual(8760*512, result['baseline_kwh'])
        self.assertEqual(106, calls)
        self.assertLessEqual(model.peak_retained_cells, 2*168*512)
        self.assertGreater(model.peak_retained_cells, 168*512)
        self.assertEqual(0, model.retained_cells)
