"""Seven-day pricing uses one eight-window mask and averages before rounding."""
from copy import deepcopy
from decimal import Decimal
import importlib
import unittest

from tests.test_energy_analysis_service import service
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import tariff, version

comparison = importlib.import_module(service.__package__ + '.energy_comparisons')


class MeanSevenCostTest(unittest.TestCase):
    def fixture(self, *, hours=10, missing=None, quantities=None):
        days = [8, *range(1, 8)]
        periods = [{'start': f'2026-01-{day:02d}T00:00Z',
                    'end': f'2026-01-{day:02d}T{hours:02d}:00Z'} for day in days]
        quantities = quantities or [8, *range(1, 8)]
        snapshots = [readings(p['start'], p['end'], sources=('sensor.grid',), value=value,
            missing=tuple(('sensor.grid', h) for h in (missing or {}).get(i, ())))
            for i, (p, value) in enumerate(zip(periods, quantities))]
        old, new = version(rate=.4, version_id='old', monthly=31), version(
            start='2026-01-08', rate=.5, version_id='new', monthly=31)
        old['profile']['zones'][0]['variableRate'] = .1
        new['profile']['zones'][0]['variableRate'] = .2
        return snapshots, periods, tariff([old, new])

    def compare(self, snapshots, periods, book):
        return comparison.compare_daily_mean7(snapshots[0], snapshots[1:], periods[0], periods[1:],
            timezone_name='UTC', tariff=book)

    def test_full_mean_uses_all_seven_actual_historical_prices_and_period_fees(self):
        snapshots, periods, book = self.fixture()
        original = deepcopy((snapshots, periods, book))
        result = self.compare(snapshots, periods, book)
        self.assertEqual((80, 40, 40), tuple(result[k] for k in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
        costs = result['costs']
        self.assertEqual(('56.00', '20.00', '36.00'),
            (costs['current']['usage_cost'], costs['baseline']['usage_cost'], costs['delta_cost']))
        self.assertEqual(('0.42', '0.42'), (costs['current']['fixed_cost'], costs['baseline']['fixed_cost']))
        self.assertEqual(('56.42', '20.42'), (costs['current']['total_cost'], costs['baseline']['total_cost']))
        self.assertEqual(7, costs['baseline']['period_count'])
        self.assertEqual(7, len(costs['baselines']))
        self.assertEqual('arithmetic_mean_seven_unrounded_components', costs['baseline']['aggregation'])
        self.assertEqual(original, (snapshots, periods, book))

    def test_partial_prices_only_one_shared_mask_and_does_not_form_a_full_bill(self):
        snapshots, periods, book = self.fixture(missing={i: {0} for i in range(8)})
        result = self.compare(snapshots, periods, book)
        self.assertEqual('common_measurements', result['comparison_scope'])
        self.assertEqual((72, 36), (result['current_kwh'], result['baseline_kwh']))
        costs = result['costs']
        self.assertEqual(('50.40', '18.00', '32.40'),
            (costs['current']['usage_cost'], costs['baseline']['usage_cost'], costs['delta_cost']))
        self.assertTrue(all(row['mask_source_seconds'] == 9*3600 for row in costs['baselines']))
        for row in [costs['current'], costs['baseline']]:
            self.assertEqual('0.42', row['fixed_cost'])
            self.assertIsNone(row['total_cost']); self.assertIsNone(row['effective_price'])

    def test_different_missing_hours_reject_average_despite_each_day_ninety_percent(self):
        snapshots, periods, book = self.fixture(missing={1: {0}, 2: {1}})
        result = self.compare(snapshots, periods, book)
        self.assertEqual('unknown', result['status'])
        self.assertEqual(.8, result['coverage']['common_current'])
        self.assertEqual('unknown', result['costs']['status'])
        self.assertIsNone(result['costs']['current']['usage_cost'])
        self.assertIsNone(result['costs']['delta_cost'])

    def test_average_before_cent_rounding_includes_zero_days(self):
        snapshots, periods, _ = self.fixture(hours=1, quantities=[1, 1, 1, 1, 1, 0, 0, 0])
        v = version(rate=.005); v['profile']['zones'][0]['variableRate'] = .005
        result = self.compare(snapshots, periods, tariff([v]))
        costs = result['costs']
        self.assertAlmostEqual(4/7, result['baseline_kwh'])
        self.assertEqual(('0.00', '0.00', '0.00'), tuple(costs['baseline'][k]
            for k in ('energy_cost', 'variable_cost', 'usage_cost')))
        self.assertEqual('0.02', costs['current']['usage_cost'])
        self.assertEqual('0.02', costs['delta_cost'])

    def test_zero_baseline_missing_component_and_currency_never_fabricate_costs(self):
        snapshots, periods, book = self.fixture(quantities=[1, 0, 0, 0, 0, 0, 0, 0])
        result = self.compare(snapshots, periods, book)
        self.assertIsNone(result['costs']['percent'])
        book['versions'][0]['profile']['zones'][0]['variableRate'] = None
        result = self.compare(snapshots, periods, book)
        self.assertEqual('ready', result['status'])
        self.assertIsNone(result['costs']['baseline']['usage_cost'])
        self.assertIsNone(result['costs']['delta_cost'])
        snapshots, periods, book = self.fixture()
        book['versions'][0]['profile']['currency'] = 'PLN'
        result = self.compare(snapshots, periods, book)
        self.assertEqual('ready', result['status'])
        self.assertEqual('unknown', result['costs']['status'])
        self.assertIsNone(result['costs']['delta_cost'])

    def test_no_tariff_preserves_energy_only_contract(self):
        snapshots, periods, _ = self.fixture()
        old = comparison.compare_daily_mean7(snapshots[0], snapshots[1:], periods[0], periods[1:], timezone_name='UTC')
        result = self.compare(snapshots, periods, None)
        self.assertEqual(old, result)
        self.assertNotIn('costs', result)
