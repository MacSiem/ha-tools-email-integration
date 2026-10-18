"""Seven matched days use one common source-hour mask, never seven different current totals."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest
from tests.test_energy_weighted_comparison import readings

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
spec = importlib.util.spec_from_file_location('mean7_package.energy_comparisons', ROOT / 'energy_comparisons.py')
import sys, types
package = types.ModuleType('mean7_package'); package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
comparison = importlib.util.module_from_spec(spec); spec.loader.exec_module(comparison)


def period(day):
    start = datetime(2026, 1, day, tzinfo=timezone.utc)
    return {'start': start.isoformat(), 'end': (start + timedelta(hours=10)).isoformat()}


class MeanSevenTest(unittest.TestCase):
    def fixture(self, current_missing=(), baseline_missing=None, sources=('sensor.a', 'sensor.b')):
        current_period, periods = period(9), [period(day) for day in range(8, 1, -1)]
        current = readings(current_period['start'], current_period['end'], 1, current_missing, sources)
        baselines = [readings(p['start'], p['end'], index + 2,
                     baseline_missing[index] if baseline_missing else (), sources)
                     for index, p in enumerate(periods)]
        return current, baselines, current_period, periods

    def test_complete_seven_day_mean_is_actual_mean_and_zero_has_no_percent(self):
        result = comparison.compare_daily_mean7(*self.fixture(), timezone_name='UTC')
        self.assertEqual('ready', result['status'])
        self.assertEqual((20, 100, -80, -80), tuple(result[k] for k in
                         ('current_kwh', 'baseline_kwh', 'delta_kwh', 'percent')))
        self.assertEqual('calendar_totals', result['comparison_scope'])
        fixture = self.fixture()
        for snapshot in fixture[1]:
            for rows in snapshot['source_buckets'].values():
                for row in rows:
                    row['kwh'] = 0
        result = comparison.compare_daily_mean7(*fixture, timezone_name='UTC')
        self.assertEqual(20, result['delta_kwh'])
        self.assertEqual(0, result['baseline_kwh'])
        self.assertIsNone(result['percent'])

    def test_all_seven_and_current_share_exact_same_partial_mask(self):
        sources = tuple(f'sensor.{n}' for n in range(5))
        missing = [(('sensor.0', 0),), (('sensor.0', 1),), (('sensor.0', 2),),
                   (('sensor.0', 3),), (), (), ()]
        fixture = self.fixture((('sensor.0', 0), ('sensor.0', 1)), missing, sources)
        result = comparison.compare_daily_mean7(*fixture, timezone_name='UTC')
        self.assertEqual('ready', result['status'])
        self.assertEqual('common_measurements', result['comparison_scope'])
        self.assertEqual((46, 230, -184, -80), tuple(result[k] for k in
                         ('current_kwh', 'baseline_kwh', 'delta_kwh', 'percent')))
        self.assertIsNone(result['full_current_kwh'])
        self.assertEqual(.92, result['coverage']['common_current'])
        self.assertEqual([.92] * 7, result['coverage']['common_baselines'])
        self.assertEqual(50 * 3600, result['coverage']['expected_current_seconds'])

    def test_separate_ninety_five_percent_days_do_not_hide_union_of_gaps(self):
        missing = [(('sensor.a', i),) for i in range(7)]
        result = comparison.compare_daily_mean7(*self.fixture(baseline_missing=missing), timezone_name='UTC')
        self.assertEqual('unknown', result['status'])
        self.assertEqual('incomplete_statistics', result['reason'])
        self.assertEqual(.65, result['coverage']['common_current'])
        self.assertIsNone(result['delta_kwh'])

    def test_no_seven_day_mean_without_all_seven_compatible_days(self):
        fixture = self.fixture()
        fixture[1].pop(); fixture[3].pop()
        self.assertEqual('seven_baselines_required', comparison.compare_daily_mean7(*fixture, timezone_name='UTC')['reason'])
        fixture = self.fixture()
        fixture[1][6]['measurement_provenance']['sensor.a']['source_unit'] = 'Wh'
        self.assertEqual('measurement_provenance_unavailable_or_changed',
                         comparison.compare_daily_mean7(*fixture, timezone_name='UTC')['reason'])

    def test_complete_dst_days_keep_actual_twenty_five_and_twenty_four_hour_totals(self):
        current_period = {'start': '2026-10-24T22:00Z', 'end': '2026-10-25T23:00Z'}
        current = readings(**dict(start=current_period['start'], end=current_period['end']), sources=('sensor.a',))
        periods = []
        for day in range(24, 17, -1):
            start = datetime(2026, 10, day - 1, 22, tzinfo=timezone.utc)
            periods.append({'start': start.isoformat(), 'end': (start + timedelta(hours=24)).isoformat()})
        baselines = [readings(p['start'], p['end'], sources=('sensor.a',)) for p in periods]
        result = comparison.compare_daily_mean7(current, baselines, current_period, periods, timezone_name='Europe/Warsaw')
        self.assertEqual('calendar_totals', result['comparison_scope'])
        self.assertEqual((25, 24, 1), tuple(result[k] for k in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
        self.assertAlmostEqual(100/24, result['percent'])


if __name__ == '__main__':
    unittest.main()
