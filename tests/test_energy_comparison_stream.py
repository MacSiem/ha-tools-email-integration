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
        bad = copy.deepcopy(snapshot); bad['source_buckets']['sensor.a'] = 'invalid_intervals'
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

    def test_two_hour_rollback_uses_actual_twenty_six_hours_and_partial_common_frames(self):
        current = ('2026-10-24T22:00Z', '2026-10-26T00:00Z')
        baseline = ('2026-10-23T22:00Z', '2026-10-24T22:00Z')
        for split in (1, 2, 3, 24, 168):
            with self.subTest(split=split):
                result, _, _ = self.run_pair(current, baseline, zone='Antarctica/Troll', split=split)
                self.assertEqual((26, 24), (result['current_kwh'], result['baseline_kwh']))
                self.assertEqual('calendar_totals', result['comparison_scope'])
                result, _, _ = self.run_pair(current, baseline, zone='Antarctica/Troll', split=split,
                                           missing={'current': [('sensor.a', 15)]})
                # Common23/26 <90%, despite own25/26 and baseline24/24.
                self.assertEqual('unknown', result['status'])
                self.assertEqual(23/26, result['coverage']['common_current'])

    def test_month_chunks_keep_whole_period_origin_for_partial_intersection(self):
        result, _, _ = self.run_pair(('2026-01-01T00:00Z', '2026-02-01T00:00Z'),
            ('2026-04-01T00:00Z', '2026-05-01T00:00Z'), kind='monthly', split=73,
            missing={'current': [('sensor.a', 8*24)], 'baseline': [('sensor.a', 9*24)]})
        self.assertEqual('ready', result['status'])
        self.assertEqual((718, 718), (result['current_kwh'], result['baseline_kwh']))


class ProvenComparisonChartTest(unittest.TestCase):
    model = ComparisonStreamTest.model
    run_pair = ComparisonStreamTest.run_pair

    def test_day_values_and_zero_have_actual_bounds_without_tariff(self):
        current = ('2026-01-02T00:00Z', '2026-01-03T00:00Z')
        baseline = ('2026-01-01T00:00Z', '2026-01-02T00:00Z')
        result, model, _ = self.run_pair(current, baseline, values=(1, 0), split=7)
        chart = result['chart']; rows = chart['markers']
        self.assertEqual(('ready', 'hour', 'calendar_totals'),
                         (chart['status'], chart['grain'], chart['comparison_scope']))
        self.assertEqual('selected_household_comparison_mask', chart['coverage_kind'])
        self.assertEqual('household_period_only', chart['fixed_fee_scope'])
        self.assertEqual(24, len(rows))
        self.assertEqual((24, 0, 24), tuple(result[k] for k in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
        self.assertEqual((1767312000000, 1767315600000, 1767225600000, 1767229200000),
                         tuple(rows[0][k] for k in ('current_from', 'current_to', 'baseline_from', 'baseline_to')))
        self.assertEqual(('1', '0'), (rows[0]['current_kwh'], rows[0]['baseline_kwh']))
        self.assertTrue(chart['reconciles_comparison_energy'])
        self.assertTrue(all('costs' not in row and 'source_ids' not in row for row in rows))
        rows[0]['baseline_kwh'] = '999'
        self.assertEqual('0', model.finish()['chart']['markers'][0]['baseline_kwh'])

    def test_fold_extra_is_unknown_and_following_hour_never_shifts(self):
        current = ('2026-10-24T22:00Z', '2026-10-25T23:00Z')
        baseline = ('2026-10-23T22:00Z', '2026-10-24T22:00Z')
        for periods in ((current, baseline), (baseline, current)):
            result, _, _ = self.run_pair(*periods, zone='Europe/Warsaw', values=(1, 2), split=2)
            chart = result['chart']
            self.assertEqual('ready', result['status'])
            self.assertFalse(chart['reconciles_comparison_energy'])
            self.assertEqual('partial', chart['status'])
            if periods[0] == current:
                rows = chart['markers']
                self.assertEqual(25, len(rows))
                self.assertEqual('unknown', rows[3]['status'])
                self.assertIsNone(rows[3]['baseline_kwh'])
                self.assertIsNone(rows[3]['baseline_from'])
                self.assertEqual('ready', rows[4]['status'])
                self.assertEqual(int(instant('2026-10-24T01:00Z').timestamp()*1000), rows[4]['baseline_from'])
                self.assertEqual('2', rows[4]['baseline_kwh'])

    def test_partial_source_intersection_is_not_full_household_hour(self):
        missing = [('sensor.b', 8), ('sensor.b', 9)]
        result, _, _ = self.run_pair(('2026-01-02T00:00Z', '2026-01-02T10:00Z'),
            ('2026-01-01T00:00Z', '2026-01-01T10:00Z'), sources=('sensor.a', 'sensor.b'),
            missing={'current': missing, 'baseline': missing}, values=(1, 2), split=3)
        chart = result['chart']; rows = chart['markers']
        self.assertEqual((18, 36), (result['current_kwh'], result['baseline_kwh']))
        self.assertEqual('common_measurements', chart['comparison_scope'])
        self.assertEqual(('1', '2'), (rows[8]['current_kwh'], rows[8]['baseline_kwh']))
        self.assertEqual(7200, rows[8]['expected_current_source_seconds'])
        self.assertEqual(3600, rows[8]['selected_current_source_seconds'])
        self.assertTrue(chart['reconciles_comparison_energy'])
        from tests.test_energy_analysis import analysis
        acc = analysis.EnergyAnalysisAccumulator(None, {'start': '2026-01-02T00:00Z',
            'end': '2026-01-02T10:00Z', 'timezone': 'UTC', 'kind': 'day'}, {},
            generated_at='2026-01-03T00:00Z')
        acc.add_chunk(readings('2026-01-02T00:00Z', '2026-01-02T10:00Z',
            sources=('sensor.a', 'sensor.b'), missing=missing),
            '2026-01-02T00:00Z', '2026-01-02T10:00Z')
        self.assertIsNone(acc.finish()['chart']['bins'][8]['kwh'])

    def test_disjoint_masks_below_threshold_do_not_publish_equal_zero_totals(self):
        result, _, _ = self.run_pair(('2026-01-02T00:00Z', '2026-01-02T10:00Z'),
            ('2026-01-01T00:00Z', '2026-01-01T10:00Z'), sources=('sensor.a', 'sensor.b'),
            missing={'current': [('sensor.b', 8), ('sensor.b', 9)],
                     'baseline': [('sensor.a', 0), ('sensor.a', 1)]}, values=(0, 0))
        self.assertEqual('unknown', result['status'])
        self.assertEqual('unknown', result['chart']['status'])
        self.assertFalse(result['chart']['reconciles_comparison_energy'])
        self.assertTrue(all(row['current_kwh'] is None and row['baseline_kwh'] is None
                            for row in result['chart']['markers']))

    def test_leap_zero_coincidence_does_not_prove_month_bijection(self):
        result, model, calls = self.run_pair(('2024-01-01T00:00Z', '2025-01-01T00:00Z'),
            ('2023-01-01T00:00Z', '2024-01-01T00:00Z'), kind='yearly', values=(0, 0))
        rows = result['chart']['markers']
        self.assertEqual((0, 0), (result['current_kwh'], result['baseline_kwh']))
        self.assertEqual(12, len(rows)); self.assertEqual(106, calls)
        self.assertEqual('unknown', rows[1]['status'])
        self.assertEqual('ready', rows[2]['status'])
        self.assertEqual(('0', '0'), (rows[2]['current_kwh'], rows[2]['baseline_kwh']))
        self.assertEqual(int(instant('2023-03-01T00:00Z').timestamp()*1000), rows[2]['baseline_from'])
        self.assertFalse(result['chart']['reconciles_comparison_energy'])
        self.assertLessEqual(model.peak_retained_cells, 2*168)
        self.assertEqual(0, model.retained_cells)

    def test_month_day31_and_partial_cutoff_keep_exact_offset_days(self):
        result, _, _ = self.run_pair(('2026-01-01T00:00Z', '2026-02-01T00:00Z'),
            ('2026-04-01T00:00Z', '2026-05-01T00:00Z'), kind='monthly', values=(1, 2), split=73)
        rows = result['chart']['markers']
        self.assertEqual(31, len(rows))
        self.assertEqual(('24', '48'), (rows[29]['current_kwh'], rows[29]['baseline_kwh']))
        self.assertEqual('unknown', rows[30]['status'])
        self.assertFalse(result['chart']['reconciles_comparison_energy'])
        short, _, _ = self.run_pair(('2026-01-01T00:00Z', '2026-01-03T12:00Z'),
            ('2025-12-01T00:00Z', '2025-12-03T12:00Z'), kind='monthly', values=(1, 2), split=7)
        self.assertEqual(('12', '24'), tuple(short['chart']['markers'][2][k]
                                           for k in ('current_kwh', 'baseline_kwh')))
        self.assertTrue(short['chart']['reconciles_comparison_energy'])

    def test_week_day_with_unequal_dst_hours_is_not_full_bin_match(self):
        result, _, _ = self.run_pair(('2026-03-22T23:00Z', '2026-03-29T22:00Z'),
            ('2026-03-15T23:00Z', '2026-03-22T23:00Z'), kind='weekly', zone='Europe/Warsaw')
        self.assertEqual((167, 168), (result['current_kwh'], result['baseline_kwh']))
        self.assertEqual(7, len(result['chart']['markers']))
        self.assertEqual('unknown', result['chart']['markers'][6]['status'])
        self.assertFalse(result['chart']['reconciles_comparison_energy'])

    def test_rejected_chunk_cannot_poison_marker_state(self):
        current = ('2026-01-02T00:00Z', '2026-01-02T01:00Z')
        baseline = ('2026-01-01T00:00Z', '2026-01-01T01:00Z')
        model = self.model(current, baseline)
        bad = readings(*current); bad['source_buckets']['sensor.a'] = 'invalid'
        with self.assertRaises(ValueError): model.push('current', bad, *current)
        model.push('current', readings(*current, sources=('sensor.a',)), *current)
        model.push('baseline', readings(*baseline, sources=('sensor.a',), value=2), *baseline)
        row = model.finish()['chart']['markers'][0]
        self.assertEqual(('1', '2'), (row['current_kwh'], row['baseline_kwh']))


class ProvenMean7ChartTest(unittest.TestCase):
    def compare(self, *, missing=None, values=None, hours=10, sources=('sensor.a', 'sensor.b')):
        module = importlib.import_module(service.__package__ + '.energy_comparisons')
        periods, snapshots = [], []
        for i in range(8):
            start = datetime(2026, 1, 8-i, tzinfo=timezone.utc); end = start+timedelta(hours=hours)
            periods.append({'start': start.isoformat(), 'end': end.isoformat()})
            snapshots.append(readings(start.isoformat(), end.isoformat(),
                sources=sources, value=(values or [1, 1, 2, 3, 4, 5, 6, 7])[i],
                missing=(missing or {}).get(i, ())))
        return module.compare_daily_mean7(snapshots[0], snapshots[1:], periods[0], periods[1:], timezone_name='UTC')

    def test_mean_uses_all_seven_raw_values_and_real_bounds(self):
        result = self.compare(values=[.1, .1, .2, .3, .4, .5, .6, .7], hours=24, sources=('sensor.a',))
        chart = result['chart']; row = chart['markers'][0]
        self.assertEqual('mean7', chart['baseline_aggregation'])
        self.assertEqual(7, len(chart['baseline_periods']))
        self.assertEqual(7, len(row['baseline_ranges']))
        self.assertIsNone(row['baseline_from']); self.assertIsNone(row['baseline_to'])
        self.assertEqual(('0.1', '0.4'), (row['current_kwh'], row['baseline_kwh']))
        self.assertEqual(int(instant('2026-01-07T00:00Z').timestamp()*1000), row['baseline_ranges'][0]['from'])
        self.assertEqual(int(instant('2026-01-01T00:00Z').timestamp()*1000), row['baseline_ranges'][6]['from'])
        self.assertTrue(chart['reconciles_comparison_energy'])

    def test_partial_mean_intersects_all_eight_sources_not_seven_independent_pairs(self):
        missing = {i: [('sensor.b', 9)] for i in range(8)}
        result = self.compare(missing=missing)
        row = result['chart']['markers'][9]
        self.assertEqual('common_measurements', result['comparison_scope'])
        self.assertEqual((19, 76), (result['current_kwh'], result['baseline_kwh']))
        self.assertEqual(('1', '4'), (row['current_kwh'], row['baseline_kwh']))
        self.assertEqual(3600, row['selected_current_source_seconds'])
        self.assertEqual([3600]*7, row['selected_baseline_source_seconds'])
        self.assertTrue(result['chart']['reconciles_comparison_energy'])

    def test_seven_different_missing_patterns_fail_global_gate_without_zero_series(self):
        result = self.compare(missing={i: [('sensor.b', i)] for i in range(8)})
        self.assertEqual('unknown', result['status'])
        self.assertEqual(.6, result['coverage']['common_current'])
        self.assertEqual('unknown', result['chart']['status'])
        self.assertFalse(result['chart']['reconciles_comparison_energy'])
        self.assertTrue(all(row['baseline_kwh'] is None for row in result['chart']['markers']))

    def test_distributed_missing_patterns_still_publish_only_all_eight_intersection(self):
        # 48 source cells/day; union of four absent cells still passes 90%.
        result = self.compare(hours=24, missing={i: [('sensor.b', i%4)] for i in range(8)})
        self.assertEqual('ready', result['status'])
        self.assertEqual((44, 176), (result['current_kwh'], result['baseline_kwh']))
        rows = result['chart']['markers']
        self.assertEqual(['1']*4+['2']*20, [r['current_kwh'] for r in rows])
        self.assertEqual(['4']*4+['8']*20, [r['baseline_kwh'] for r in rows])
        self.assertTrue(result['chart']['reconciles_comparison_energy'])

    def test_zero_is_measured_but_empty_common_frame_is_unknown(self):
        zeros = self.compare(values=[0]*8, sources=('sensor.a',))
        self.assertEqual(('0', '0'), tuple(zeros['chart']['markers'][0][k]
                                         for k in ('current_kwh', 'baseline_kwh')))
        result = self.compare(missing={i: [('sensor.a', 9), ('sensor.b', 9)] for i in range(8)})
        self.assertEqual('ready', result['status'])
        self.assertEqual('unknown', result['chart']['markers'][9]['status'])
        self.assertIsNone(result['chart']['markers'][9]['baseline_kwh'])
        self.assertTrue(result['chart']['reconciles_comparison_energy'])


class ComparisonChartMoneyContractTest(unittest.TestCase):
    def test_energy_chart_addition_preserves_literal_costs_and_costless_contract(self):
        from tests.test_comparison_costs import ComparisonCostsTest, tariff
        fixture = ComparisonCostsTest()
        priced, _ = fixture.run_pair(tariff(), split=1)
        costs = priced['costs']
        self.assertEqual(('1.40', '1.00', '0.40', '40.0'),
                         (costs['current']['usage_cost'], costs['baseline']['usage_cost'],
                          costs['delta_cost'], costs['percent']))
        self.assertEqual(('1.00', '0.40', '1.40'),
                         tuple(costs['current'][key] for key in ('energy_cost', 'variable_cost', 'total_cost')))
        bare, _ = fixture.run_pair(None, split=1)
        self.assertEqual(bare, {key: value for key, value in priced.items() if key != 'costs'})
        self.assertEqual([('1', '1')]*2,
                         [(row['current_kwh'], row['baseline_kwh']) for row in priced['chart']['markers']])

    def test_zero_equal_masks_with_no_common_hour_stays_unknown_point(self):
        fixture = ProvenComparisonChartTest()
        result, _, _ = fixture.run_pair(('2026-01-02T00:00Z', '2026-01-02T10:00Z'),
            ('2026-01-01T00:00Z', '2026-01-01T10:00Z'), values=(0, 0),
            missing={'current': [('sensor.a', 9)], 'baseline': [('sensor.a', 9)]})
        self.assertEqual('ready', result['status'])
        self.assertEqual('unknown', result['chart']['markers'][9]['status'])
        self.assertIsNone(result['chart']['markers'][9]['current_kwh'])
        self.assertTrue(result['chart']['reconciles_comparison_energy'])


class Mean7UnknownProofChartTest(unittest.TestCase):
    def test_missing_baselines_and_changed_provenance_have_explicit_unknown_chart(self):
        module = importlib.import_module(service.__package__ + '.energy_comparisons')
        period = {'start': '2026-01-08T00:00Z', 'end': '2026-01-08T10:00Z'}
        current = readings(period['start'], period['end'], sources=('sensor.a',))
        unknown = module.compare_daily_mean7(current, [], period, [], timezone_name='UTC')
        self.assertEqual(('unknown', 'seven_baselines_required'),
                         (unknown['chart']['status'], unknown['chart']['reason']))
        periods, baselines = [], []
        for day in range(7, 0, -1):
            a = f'2026-01-{day:02d}T00:00Z'; b = f'2026-01-{day:02d}T10:00Z'
            periods.append({'start': a, 'end': b})
            baselines.append(readings(a, b, sources=('sensor.a',)))
        baselines[6]['measurement_provenance']['sensor.a']['source_unit'] = 'Wh'
        unknown = module.compare_daily_mean7(current, baselines, period, periods, timezone_name='UTC')
        self.assertEqual('measurement_provenance_unavailable_or_changed', unknown['chart']['reason'])
        self.assertEqual([], unknown['chart']['markers'])
