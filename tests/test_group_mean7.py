"""Daily group means select one intersection across all eight real projections."""
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import unittest
from tests import test_energy_analysis_service as fixtures
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import tariff, version

mean = importlib.import_module(fixtures.service.__package__ + '.energy_group_mean7')


class GroupMeanSevenTest(unittest.TestCase):
    def fixture(self, *, hours=24, holes=None, values=None, domain='grid_import'):
        values = values or [4, *([2]*7)]
        periods = [{'start': f'2026-01-{day:02d}T00:00Z',
                    'end': f'2026-01-{day+1:02d}T00:00Z' if hours == 24 else f'2026-01-{day:02d}T{hours:02d}:00Z'}
                   for day in [8, *range(1,8)]]
        membership = fixtures.registry_model.resolve_membership(['sensor.a', 'sensor.b'], {
            'entities': {'sensor.a': {'area_id': 'office'}, 'sensor.b': {'area_id': 'kitchen'}},
            'areas': {'office': {'floor_id': 'up'}, 'kitchen': {'floor_id': 'down'}},
            'floors': {'up': {'name': 'Up'}, 'down': {'name': 'Down'}}},
            revision=2, captured_at='2026-01-10T00:00Z')
        projected = []
        for i, (period, value) in enumerate(zip(periods, values)):
            builder = fixtures.service.EnergyAnalysisAccumulator(None,
                {**period, 'kind': 'day', 'timezone': 'UTC'}, {'registry_revision': 2},
                generated_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                consumers=[{'stat_consumption': sid} for sid in ('sensor.a','sensor.b')],
                membership=membership, consumer_domain=domain)
            grid = readings(period['start'], period['end'], sources=('sensor.grid',), value=20)
            consumer = readings(period['start'], period['end'], sources=('sensor.a','sensor.b'),
                value=value, missing=tuple(('sensor.a', hour) for hour in (holes or {}).get(i, ())))
            projected.append(builder.add_chunk(grid, period['start'], period['end'],
                consumer_snapshot=consumer, include_group_hours=True))
        old, new = version(rate=.4, version_id='old', monthly=31), version(
            start='2026-01-08', rate=.5, version_id='new', monthly=31)
        old['profile']['zones'][0]['variableRate'] = .1
        new['profile']['zones'][0]['variableRate'] = .2
        return projected, periods, tariff([old,new])

    def compare(self, projections, periods, rates=None):
        return mean.compare_daily_group_mean7(projections, periods, timezone_name='UTC', tariff=rates)

    def test_full_energy_mean_and_history_costs_never_allocate_fixed_fees(self):
        projections, periods, rates = self.fixture()
        result = self.compare(projections, periods, rates)
        row = result['areas']['office']
        self.assertEqual((Decimal(96),Decimal(48),Decimal(48)),
            tuple(Decimal(row[k]) for k in ('current_kwh','baseline_kwh','delta_kwh')))
        self.assertEqual('43.20', row['delta_usage_cost'])
        self.assertEqual(7, row['costs']['baseline']['period_count'])
        self.assertNotIn('fixed_cost', row['costs']['current'])
        self.assertIsNone(row['delta_fixed_cost'])
        self.assertFalse(result['reconciles_household_delta'])

    def test_same_missing_hour_all_eight_windows_uses_twenty_three_hours(self):
        projections, periods, rates = self.fixture(holes={i:{0} for i in range(8)})
        row = self.compare(projections, periods, rates)['areas']['office']
        self.assertEqual('common_measurements', row['comparison_scope'])
        self.assertEqual((Decimal(92),Decimal(46),Decimal(46)),
            tuple(Decimal(row[k]) for k in ('current_kwh','baseline_kwh','delta_kwh')))
        self.assertEqual('41.40', row['delta_usage_cost'])
        self.assertEqual([23/24]*7, row['coverage']['common_baselines'])

    def test_different_missing_hours_do_not_average_seven_different_pair_masks(self):
        projections, periods, rates = self.fixture(hours=10, holes={1:{0},2:{1}})
        result = self.compare(projections, periods, rates)
        row = result['areas']['office']
        self.assertEqual('unknown', row['status'])
        self.assertEqual(.8, row['coverage']['common_current'])
        self.assertIsNone(row['delta_kwh']); self.assertIsNone(row['delta_usage_cost'])
        self.assertEqual('ready', result['areas']['kitchen']['status'])
        self.assertEqual(Decimal(40), Decimal(result['areas']['kitchen']['current_kwh']))

    def test_zero_baseline_is_known_and_percent_is_absent(self):
        projections, periods, rates = self.fixture(values=[4,0,0,0,0,0,0,0])
        row = self.compare(projections, periods, rates)['areas']['office']
        self.assertEqual(Decimal(0), Decimal(row['baseline_kwh']))
        self.assertIsNone(row['percent'])
        self.assertEqual('67.20', row['delta_usage_cost'])

    def test_site_consumption_keeps_energy_without_grid_costs(self):
        projections, periods, rates = self.fixture(domain='site_consumption')
        result = self.compare(projections, periods, rates)
        self.assertEqual('ready', result['areas']['office']['status'])
        self.assertIsNone(result['areas']['office']['delta_usage_cost'])
        self.assertIsNone(result['currency'])

    def test_projection_context_mismatch_is_rejected(self):
        projections, periods, rates = self.fixture()
        projections[-1]['registry_revision'] += 1
        with self.assertRaises(ValueError): self.compare(projections, periods, rates)
