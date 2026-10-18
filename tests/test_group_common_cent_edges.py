"""Producer-to-DTO checks for partial-mask component cent allocation."""
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from tests import test_energy_analysis_service as fixtures
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version


class CommonGroupCentEdgesTest(unittest.TestCase):
    def compare(self, versions, periods=None):
        periods = periods or [dict(start='2026-01-02T00:00Z', end='2026-01-02T10:00Z'),
                   dict(start='2026-01-01T00:00Z', end='2026-01-01T10:00Z')]
        ids = ('sensor.a', 'sensor.b', 'sensor.c')
        members = fixtures.registry_model.resolve_membership(list(ids), {
            'entities': {sid: {'area_id': 'area'+str(i)} for i, sid in enumerate(ids)},
            'areas': {'area'+str(i): {'floor_id': 'upstairs'} for i in range(3)},
            'floors': {'upstairs': {'name': 'Upstairs'}}}, revision=2, captured_at='2026-01-03T00:00Z')
        tariff_book = book(versions)
        household_pair = fixtures.service.PairedComparisonAccumulator(*periods,
            timezone_name='UTC', calendar_kind='daily', tariff=tariff_book['tariffs'][0])
        groups = fixtures.service.GroupComparisonAccumulator(*periods,
            calendar_plan=household_pair.calendar_plan, tariff=tariff_book['tariffs'][0])
        accumulators = []
        for index, (side, period) in enumerate(zip(('current', 'baseline'), periods)):
            grid = readings(period['start'], period['end'], sources=('sensor.grid',),
                value=.015 if index == 0 else 0, missing=(('sensor.grid', 0),))
            consumers = readings(period['start'], period['end'], sources=ids,
                value=.005 if index == 0 else 0, missing=[(sid, 0) for sid in ids])
            accumulator = fixtures.service.EnergyAnalysisAccumulator(tariff_book,
                {**period, 'kind': 'day', 'timezone': 'UTC'}, {'registry_revision': 2},
                generated_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
                consumers=[{'stat_consumption': sid} for sid in ids], membership=members)
            projection = accumulator.add_chunk(grid, period['start'], period['end'],
                consumer_snapshot=consumers, include_group_hours=True)
            household_pair.push(side, grid, period['start'], period['end'])
            groups.push(side, projection, period['start'], period['end'])
            accumulators.append(accumulator)
        household = household_pair.finish()
        return groups.finish(household), household, accumulators, groups

    def assert_partitions(self, result, household):
        for level in ('devices', 'areas', 'floors'):
            rows = [*result[level].values(), result['unmeasured']]
            if level == 'areas':
                rows.append(result['unassigned'])
            self.assertEqual(Decimal(household['costs']['delta_cost']),
                sum((Decimal(row['delta_usage_cost']) for row in rows), Decimal(0)))

    def test_negative_prices_use_one_stable_signed_allocation(self):
        v = version(rate=-1)
        v['profile']['allowNegativeEnergy'] = True
        result, household, _, _ = self.compare([v])
        self.assertTrue(result['reconciles_household_cost_delta'])
        self.assertEqual(['-0.04', '-0.05', '-0.05'],
            [result['devices'][sid]['costs']['current']['usage_cost']
             for sid in ('sensor.a', 'sensor.b', 'sensor.c')])
        self.assert_partitions(result, household)

    def test_missing_component_cannot_claim_cost_partition_or_invent_zero(self):
        v = version()
        v['profile']['zones'][0]['variableRate'] = None
        result, _, _, _ = self.compare([v])
        self.assertTrue(result['reconciles_household_delta'])
        self.assertFalse(result['reconciles_household_cost_delta'])
        side = result['devices']['sensor.a']['costs']['current']
        self.assertIsNone(side['known_costs']['variable_cost'])
        self.assertIsNone(side['usage_cost'])

    def test_changed_currency_or_basis_retains_energy_proof_only(self):
        for field, value in (('currency', 'PLN'), ('priceBasis', 'net')):
            with self.subTest(field=field):
                v = version('2026-01-02', version_id='b')
                v['profile'][field] = value
                result, _, _, _ = self.compare([version(), v])
                self.assertTrue(result['reconciles_household_delta'])
                self.assertFalse(result['reconciles_household_cost_delta'])

    def test_mixed_legacy_keeps_full_unknown_and_allocates_known_parts(self):
        versions = [version(rate_semantics='legacy_combined'),
                    version('2026-01-02', version_id='b'),
                    version('2026-01-03', version_id='c', rate_semantics='legacy_combined'),
                    version('2026-01-04', version_id='d')]
        periods = [dict(start='2026-01-03T20:00Z', end='2026-01-04T06:00Z'),
                   dict(start='2026-01-01T20:00Z', end='2026-01-02T06:00Z')]
        result, household, _, _ = self.compare(versions, periods)
        self.assertTrue(result['reconciles_household_cost_delta'])
        self.assert_partitions(result, household)
        for row in result['devices'].values():
            side = row['costs']['current']
            self.assertIsNone(side['energy_cost'])
            self.assertIsNone(side['variable_cost'])
            self.assertIsNotNone(side['known_costs']['energy_cost'])
            self.assertEqual(Decimal(side['usage_cost']),
                sum((Decimal(value) for value in side['known_costs'].values()
                     if value is not None), Decimal(0)))

    def test_service_overlays_adjusted_structural_zero_and_no_household_fee(self):
        streamed, household, accumulators, pair = self.compare([version(monthly=31)])
        dto = fixtures.service._finish(accumulators[0], {'comparison_kind': 'previous'},
            'grid_import', {'previous': household}, accumulators[1], pair)
        result = dto['comparisons']['previous']['groups']
        self.assertTrue(result['reconciles_household_cost_delta'])
        self.assertEqual(streamed['unassigned'], result['unassigned'])
        self.assert_partitions(result, household)
        for row in [*result['devices'].values(), *result['areas'].values(),
                    *result['floors'].values(), result['unassigned'], result['unmeasured']]:
            self.assertIsNone(row['delta_fixed_cost'])
            for side in ('current', 'baseline'):
                self.assertNotIn('fixed_cost', row['costs'][side])
                self.assertNotIn('fees', row['costs'][side])
