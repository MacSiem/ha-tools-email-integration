"""Partial cents reconcile only after proving one actual disjoint source mask."""
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from tests import test_energy_analysis_service as fixtures
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version


class CommonGroupCentsTest(unittest.TestCase):
    periods = [dict(start='2026-01-02T00:00Z', end='2026-01-02T10:00Z'),
               dict(start='2026-01-01T00:00Z', end='2026-01-01T10:00Z')]

    def compare(self, *, mismatch=False, dynamic=False):
        ids = ('sensor.a', 'sensor.b', 'sensor.c')
        members = fixtures.registry_model.resolve_membership(list(ids), {
            'entities': {sid: {'area_id': 'area'+str(i)} for i, sid in enumerate(ids)},
            'areas': {'area'+str(i): {'floor_id': 'upstairs'} for i in range(3)},
            'floors': {'upstairs': {'name': 'Upstairs'}}}, revision=2, captured_at='2026-01-03T00:00Z')
        v = version(rate=1)
        if dynamic:
            v['profile']['dynamic'] = {'intervals': [{'from': '2026-01-01T01:00Z',
                'to': '2026-01-02T10:00Z', 'energyRate': 1, 'variableRate': 0}]}
        b = book([v]); rates = b['tariffs'][0]
        house = fixtures.service.PairedComparisonAccumulator(*self.periods,
            timezone_name='UTC', calendar_kind='daily', tariff=rates)
        groups = fixtures.service.GroupComparisonAccumulator(*self.periods,
            calendar_plan=house.calendar_plan, tariff=rates)
        for index, (side, period) in enumerate(zip(('current', 'baseline'), self.periods)):
            grid = readings(period['start'], period['end'], sources=('sensor.grid',),
                value=.015 if index == 0 else 0, missing=(('sensor.grid', 0),))
            gaps = [(sid, 0) for sid in ids]
            if mismatch: gaps.append(('sensor.a', 1))
            consumer = readings(period['start'], period['end'], sources=ids,
                value=.005 if index == 0 else 0, missing=gaps)
            accumulator = fixtures.service.EnergyAnalysisAccumulator(b,
                {**period, 'kind': 'day', 'timezone': 'UTC'}, {'registry_revision': 2},
                generated_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                consumers=[{'stat_consumption': sid} for sid in ids], membership=members)
            projection = accumulator.add_chunk(grid, period['start'], period['end'],
                consumer_snapshot=consumer, include_group_hours=True)
            house.push(side, grid, period['start'], period['end'])
            groups.push(side, projection, period['start'], period['end'])
        household = house.finish()
        return groups.finish(household), household

    def test_component_cents_derive_all_alternative_partitions_from_one_device_allocation(self):
        result, household = self.compare()
        self.assertTrue(result['reconciles_household_delta'])
        self.assertTrue(result['reconciles_household_cost_delta'])
        self.assertEqual('0.14', household['costs']['delta_cost'])
        self.assertEqual(['0.05', '0.05', '0.04'],
            [result['devices'][sid]['costs']['current']['usage_cost'] for sid in ('sensor.a', 'sensor.b', 'sensor.c')])
        for level in ('devices', 'areas', 'floors'):
            total = sum((Decimal(row['delta_usage_cost']) for row in result[level].values()), Decimal(0))
            if level == 'areas': total += Decimal(result['unassigned']['delta_usage_cost'])
            total += Decimal(result['unmeasured']['delta_usage_cost'])
            self.assertEqual(Decimal(household['costs']['delta_cost']), total)
        self.assertIsNone(result['unmeasured']['delta_fixed_cost'])
        self.assertNotIn('fixed_cost', result['devices']['sensor.a']['costs']['current'])

    def test_different_masks_never_claim_cost_partition_from_equal_zero_baselines(self):
        result, _ = self.compare(mismatch=True)
        self.assertFalse(result['reconciles_household_delta'])
        self.assertFalse(result['reconciles_household_cost_delta'])

    def test_structural_zero_prices_only_household_mask_without_borrowing_unknown_outside_price(self):
        result, household = self.compare(dynamic=True)
        self.assertTrue(result['reconciles_household_cost_delta'])
        self.assertEqual('0.00', result['unassigned']['costs']['baseline']['usage_cost'])
        self.assertEqual(9*3600, result['unassigned']['costs']['baseline']['mask_source_seconds'])
        self.assertEqual(household['costs']['delta_cost'], result['floors']['upstairs']['delta_usage_cost'])
