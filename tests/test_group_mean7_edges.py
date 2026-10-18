"""Independent edge cases for W24/W25/D-M; artifact only, never run here.

Keep v53-mean-group-tests46.py's six tests separately. No TestCase import or
inheritance from that module: collection of this module adds only new cases.
The owner should install this as tests/test_group_mean7_edges.py on a frozen
candidate containing energy_group_mean7 and group_projection_evidence.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from zoneinfo import ZoneInfo

from tests import test_energy_analysis_service as fixtures
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import tariff, version

mean = importlib.import_module(fixtures.service.__package__ + '.energy_group_mean7')
house = importlib.import_module(fixtures.service.__package__ + '.energy_comparisons')


class GroupMeanSevenEdgesTest(unittest.TestCase):
    def fixture(self, *, zone='UTC', anchor='2026-01-08', values=None,
                consumer_holes=None, grid_holes=None, rates=None):
        # Membership input is explicitly a list, not a tuple/set/generator.
        ids = ['sensor.a', 'sensor.b']
        members = fixtures.registry_model.resolve_membership(ids, {
            'entities': {'sensor.a': {'area_id': 'office'},
                         'sensor.b': {'area_id': 'kitchen'}},
            'areas': {'office': {'floor_id': 'up'}, 'kitchen': {'floor_id': 'down'}},
            'floors': {'up': {'name': 'Up'}, 'down': {'name': 'Down'}}},
            revision=2, captured_at='2026-11-01T00:00Z')
        current = date.fromisoformat(anchor)
        days = [current, *(current - timedelta(days=n) for n in range(7, 0, -1))]
        periods = []
        for day in days:
            start = datetime.combine(day, time(), ZoneInfo(zone))
            end = datetime.combine(day + timedelta(days=1), time(), ZoneInfo(zone))
            periods.append({'start': start.astimezone(timezone.utc).isoformat(),
                            'end': end.astimezone(timezone.utc).isoformat()})
        values = [4, 2, 2, 2, 2, 2, 2, 2] if values is None else values
        self.assertEqual(8, len(values))
        projections, grids = [], []
        for i, (period, value) in enumerate(zip(periods, values)):
            grid = readings(**period, sources=('sensor.grid',), value=20,
                missing=tuple(('sensor.grid', h) for h in (grid_holes or {}).get(i, ())))
            consumer = readings(**period, sources=tuple(ids), value=value,
                missing=tuple((sid, h) for sid, h in (consumer_holes or {}).get(i, ())))
            builder = fixtures.service.EnergyAnalysisAccumulator(None,
                {**period, 'kind': 'day', 'timezone': zone}, {'registry_revision': 2},
                generated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
                consumers=[{'stat_consumption': sid} for sid in ids], membership=members)
            projections.append(builder.add_chunk(grid, period['start'], period['end'],
                consumer_snapshot=consumer, include_group_hours=True))
            grids.append(grid)
        return projections, periods, grids, zone, tariff([version()]) if rates is None else rates

    def compare(self, data, *, proof=False):
        projections, periods, grids, zone, rates = data
        household = house.compare_daily_mean7(grids[0], grids[1:], periods[0], periods[1:],
            timezone_name=zone) if proof else None
        return mean.compare_daily_group_mean7(projections, periods,
            timezone_name=zone, tariff=rates, household_comparison=household)

    def amounts(self, row, current, baseline, delta):
        self.assertEqual('ready', row['status'])
        self.assertEqual(tuple(map(Decimal, (current, baseline, delta))),
            tuple(Decimal(row[key]) for key in ('current_kwh', 'baseline_kwh', 'delta_kwh')))

    def test_dst25_full_calendar_totals_keep_the_extra_actual_hour(self):
        # 25*4=100; each of seven baselines is 24*2=48; delta=52.
        result = self.compare(self.fixture(zone='Europe/Warsaw', anchor='2026-10-25'), proof=True)
        row = result['areas']['office']
        self.amounts(row, '100', '48', '52')
        self.assertEqual('per_group_calendar_totals', row['comparison_scope'])
        self.assertEqual(90000, row['coverage']['expected_current_seconds'])
        self.assertEqual([86400]*7, row['coverage']['expected_baseline_seconds'])
        self.assertEqual('52.00', row['delta_usage_cost'])
        self.assertTrue(result['reconciles_household_delta'])

    def test_dst26_full_calendar_totals_supported_by_troll(self):
        # The service already supports Troll's two-hour autumn transition.
        # 26*4=104, not 96 or 100; seven actual 24-hour baselines average 48.
        result = self.compare(self.fixture(zone='Antarctica/Troll', anchor='2026-10-25'), proof=True)
        row = result['areas']['office']
        self.amounts(row, '104', '48', '56')
        self.assertEqual(93600, row['coverage']['expected_current_seconds'])
        self.assertEqual([86400]*7, row['coverage']['expected_baseline_seconds'])
        self.assertEqual('56.00', row['delta_usage_cost'])
        self.assertTrue(result['reconciles_household_delta'])

    def test_missing_fold_on_dst25_selects_twenty_four_common_hours(self):
        # Warsaw UTC hour index3 is the second local02:00, fold1.
        data = self.fixture(zone='Europe/Warsaw', anchor='2026-10-25',
            consumer_holes={0: [('sensor.a', 3)]})
        row = self.compare(data)['areas']['office']
        self.amounts(row, '96', '48', '48')
        self.assertEqual('common_measurements', row['comparison_scope'])
        self.assertEqual(.96, row['coverage']['current'])
        self.assertEqual(.96, row['coverage']['common_current'])
        self.assertEqual([1.0]*7, row['coverage']['common_baselines'])
        self.assertIsNone(row['full_current_kwh'])

    def test_true_eight_window_mask_ready_with_two_different_baseline_holes(self):
        data = self.fixture(consumer_holes={1: [('sensor.a', 0)], 7: [('sensor.a', 1)]})
        row = self.compare(data)['areas']['office']
        # All eight windows must drop both00:00 and01:00: 22*4 vs22*2.
        self.amounts(row, '88', '44', '44')
        self.assertEqual('common_measurements', row['comparison_scope'])
        self.assertEqual(79200, row['coverage']['common_group_seconds'])
        self.assertAlmostEqual(22/24, row['coverage']['common_current'])
        self.assertEqual('44.00', row['delta_usage_cost'])
        self.assertEqual(Decimal('96'), Decimal(row['own_current_kwh']))

    def test_eight_individually_good_masks_have_only_sixteen_common_hours(self):
        data = self.fixture(consumer_holes={i: [('sensor.a', i)] for i in range(8)})
        row = self.compare(data)['areas']['office']
        self.assertEqual('unknown', row['status'])
        self.assertAlmostEqual(23/24, row['coverage']['current'])
        self.assertEqual([23/24]*7, row['coverage']['baselines'])
        self.assertEqual(57600, row['coverage']['common_group_seconds'])
        self.assertAlmostEqual(16/24, row['coverage']['common_current'])
        self.assertIsNone(row['delta_kwh'])
        self.assertIsNone(row['delta_usage_cost'])

    def test_all_eight_measured_zero_days_are_known_zero(self):
        result = self.compare(self.fixture(values=[0]*8), proof=True)
        row = result['areas']['office']
        self.amounts(row, '0', '0', '0')
        self.assertIsNone(row['percent'])
        self.assertEqual('0.00', row['costs']['baseline']['usage_cost'])
        self.assertEqual('0.00', row['delta_usage_cost'])
        self.assertTrue(result['reconciles_household_delta'])

    def test_one_unknown_baseline_is_not_a_six_day_mean(self):
        data = self.fixture(consumer_holes={7: [('sensor.a', h) for h in range(3)]})
        row = self.compare(data)['areas']['office']
        self.assertEqual('unknown', row['status'])
        self.assertEqual(.875, row['coverage']['baselines'][6])
        self.assertIsNone(row['baseline_kwh'])
        self.assertIsNone(row['full_baseline_kwh'])
        self.assertIsNone(row['delta_usage_cost'])

    def test_unknown_price_on_one_baseline_keeps_energy_and_unknown_cost(self):
        # First real baseline is Jan1, outside tariff history starting Jan2.
        data = self.fixture(rates=tariff([version(start='2026-01-02')]))
        row = self.compare(data)['areas']['office']
        self.amounts(row, '96', '48', '48')
        self.assertEqual('96.00', row['costs']['current']['usage_cost'])
        self.assertIsNone(row['costs']['baseline']['usage_cost'])
        self.assertEqual('unknown', row['costs']['status'])
        self.assertIsNone(row['delta_usage_cost'])
        self.assertEqual(7, row['costs']['baseline']['period_count'])

    def test_unknown_variable_component_does_not_erase_known_energy_cost(self):
        v = version()
        v['profile']['zones'][0].pop('variableRate')
        row = self.compare(self.fixture(rates=tariff([v])))['areas']['office']
        self.amounts(row, '96', '48', '48')
        self.assertEqual('48.00', row['delta_energy_cost'])
        self.assertIsNone(row['delta_variable_cost'])
        self.assertIsNone(row['delta_usage_cost'])

    def test_currency_or_basis_mismatch_does_not_erase_measured_energy(self):
        for field, replacement in [('currency', 'PLN'), ('priceBasis', 'net')]:
            with self.subTest(field=field):
                old, current = version(), version('2026-01-08', version_id='current')
                current['profile'][field] = replacement
                row = self.compare(self.fixture(rates=tariff([old, current])))['areas']['office']
                self.amounts(row, '96', '48', '48')
                self.assertEqual('unknown', row['costs']['status'])
                self.assertEqual('currency_or_basis_mismatch', row['costs']['reason'])
                self.assertIsNone(row['delta_energy_cost'])
                self.assertIsNone(row['delta_usage_cost'])

    def test_mixed_identity_in_single_baseline_is_not_dropped_from_seven(self):
        for field, replacement in [('currency', 'PLN'), ('priceBasis', 'net')]:
            with self.subTest(field=field):
                versions = [version(), version('2026-01-07', version_id='different'),
                            version('2026-01-08', version_id='current')]
                versions[1]['profile'][field] = replacement
                row = self.compare(self.fixture(rates=tariff(versions)))['areas']['office']
                self.amounts(row, '96', '48', '48')
                self.assertFalse(row['costs']['baseline']['compatible'])
                self.assertIsNone(row['costs']['baseline']['usage_cost'])
                self.assertIsNone(row['delta_usage_cost'])

    def test_mean_baseline_components_round_once_then_delta_uses_displayed_sides(self):
        # Six raw days are24*0.0002125=0.0051 and seventh is measured0.
        # Mean(raw)=0.0306/7=0.004371428... ->0.00.
        # Mean(round(days))=0.06/7=0.008571428... ->0.01 is wrong.
        # Current24*0.00605=0.14520 ->0.15; displayed delta0.15-0.00=0.15.
        data = self.fixture(values=[.00605, *([.0002125]*6), 0])
        row = self.compare(data)['areas']['office']
        self.assertEqual('0.15', row['costs']['current']['energy_cost'])
        self.assertEqual('0.00', row['costs']['baseline']['energy_cost'])
        self.assertEqual('0.15', row['delta_energy_cost'])
        self.assertEqual('0.15', row['delta_usage_cost'])

    def test_full_partition_energyproof_has_actual_conservation(self):
        result = self.compare(self.fixture(), proof=True)
        self.assertTrue(result['reconciles_household_delta'])
        self.amounts(result['areas']['office'], '96', '48', '48')
        self.amounts(result['unmeasured'], '288', '384', '-96')
        # Areas are48+48, unassigned0, unmeasured-96: household delta0.
        self.assertEqual(Decimal('0'), sum((Decimal(result['areas'][g]['delta_kwh'])
            for g in ('office', 'kitchen')), Decimal(result['unmeasured']['delta_kwh'])))
        self.assertFalse(result['reconciles_household_cost_delta'])

    def test_partial_partition_energyproof_uses_the_household_common_mask(self):
        holes = {i: [('sensor.a', 0), ('sensor.b', 0)] for i in range(8)}
        grid_holes = {i: [0] for i in range(8)}
        result = self.compare(self.fixture(consumer_holes=holes, grid_holes=grid_holes), proof=True)
        self.assertTrue(result['reconciles_household_delta'])
        self.amounts(result['areas']['office'], '92', '46', '46')
        self.amounts(result['unmeasured'], '276', '368', '-92')
        self.assertEqual(82800, result['areas']['office']['coverage']['common_group_seconds'])
        self.assertFalse(result['reconciles_household_cost_delta'])

    def test_equal_zero_deltas_on_different_masks_do_not_prove_partition(self):
        # All consumers are constant across days; numerical deltas alone are0.
        # Household selects23 hours, office selects22, kitchen selects23.
        holes = {i: [('sensor.a', 0), ('sensor.b', 0)] for i in range(8)}
        holes[7].append(('sensor.a', 1))
        result = self.compare(self.fixture(values=[2]*8, consumer_holes=holes,
            grid_holes={i: [0] for i in range(8)}), proof=True)
        self.amounts(result['areas']['office'], '44', '44', '0')
        self.amounts(result['areas']['kitchen'], '46', '46', '0')
        self.assertFalse(result['reconciles_household_delta'])
        self.assertFalse(result['reconciles_household_cost_delta'])

    def test_ready_household_claim_without_partitions_does_not_prove_energy(self):
        data = self.fixture()
        projections = deepcopy(data[0])
        for projection in projections:
            for hour in projection['hours']:
                hour['partition_complete'] = False
        result = self.compare((projections, *data[1:]), proof=True)
        self.assertEqual('ready', result['areas']['office']['status'])
        self.assertFalse(result['reconciles_household_delta'])
