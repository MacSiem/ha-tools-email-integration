"""Literal per-group same-frame arithmetic; apply before implementation for RED.

Pure model tests. No HA runtime, store write or Recorder. The fixture package
loads the production modules without loading the integration entry point.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import importlib
import unittest
from unittest.mock import patch

from tests.test_energy_analysis_service import service
from tests import test_analysis_group_projection as projection_fixtures
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import tariff, version

stream = importlib.import_module(service.__package__ + '.energy_group_stream')
core = importlib.import_module(service.__package__ + '.energy_comparison_core')
PROVENANCE = {'method': 'recorder_hourly_change_v1', 'unit': 'kWh',
              'source_unit': 'kWh', 'has_sum': True, 'unit_class': 'energy'}


def instant(value):
    return datetime.fromisoformat(value)


def projection(start, end, *, count=1, value='1', missing=(), offset=0, domain='grid_import'):
    ids = ['sensor.s'+str(i) for i in range(count)]
    result = {'schema': 1, 'consumer_source_ids': ids,
        'consumer_provenance': {sid: deepcopy(PROVENANCE) for sid in ids},
        'grid_source_ids': ['sensor.grid'], 'grid_provenance': {'sensor.grid': deepcopy(PROVENANCE)},
        'registry_revision': 7, 'membership_mode': 'current_snapshot', 'energy_domain': domain, 'hours': []}
    cursor = instant(start) if isinstance(start, str) else start
    end = instant(end) if isinstance(end, str) else end
    while cursor < end:
        stamp = int(cursor.timestamp()*1000)
        cells = {sid: None if (i, offset) in missing else str(value) for i, sid in enumerate(ids)}
        result['hours'].append({'from': stamp, 'to': stamp+3600000,
            'devices': cells, 'areas': {'area'+str(i): cells[sid] for i, sid in enumerate(ids)},
            'floors': {'floor'+str(i): cells[sid] for i, sid in enumerate(ids)},
            'unassigned': '0', 'unmeasured': '0' if domain == 'grid_import' else None,
            'household_kwh': str(Decimal(str(value))*count), 'partition_complete': False,
            'grid_source_presence': ['sensor.grid']})
        cursor += timedelta(hours=1)
        offset += 1
    return result


class GroupStreamTest(unittest.TestCase):
    current = ('2026-01-02T00:00Z', '2026-01-03T00:00Z')
    baseline = ('2026-01-01T00:00Z', '2026-01-02T00:00Z')

    def model(self, current=None, baseline=None, *, kind='daily', zone='UTC', rates=None):
        current, baseline = current or self.current, baseline or self.baseline
        periods = [dict(zip(('start', 'end'), bounds)) for bounds in (current, baseline)]
        plan = core.CalendarComparisonPlan(*periods, timezone_name=zone, calendar_kind=kind)
        model = stream.GroupComparisonAccumulator(*periods, calendar_plan=plan, tariff=rates)
        self.assertIs(plan, model.calendar_plan)
        return model

    def run_pair(self, current=None, baseline=None, *, missing=None, values=('4', '2'),
                 split=168, count=1, kind='daily', zone='UTC', rates=None, domain='grid_import'):
        bounds = [current or self.current, baseline or self.baseline]
        model = self.model(*bounds, kind=kind, zone=zone, rates=rates)
        starts = [instant(bound[0]) for bound in bounds]
        cursors, ends = starts[:], [instant(bound[1]) for bound in bounds]
        while (side := model.needs_side()) is not None:
            index = 0 if side == 'current' else 1
            start = cursors[index]
            end = min(start+timedelta(hours=split), ends[index])
            offset = int((start-starts[index]).total_seconds()/3600)
            data = projection(start, end, count=count, value=values[index],
                missing=(missing or {}).get(side, ()), offset=offset, domain=domain)
            model.push(side, data, start, end)
            cursors[index] = end
        return model.finish(), model

    def test_partial_room_retains_own_full_and_uses_only_twenty_three_common_hours(self):
        for split in (1, 2, 7, 73, 168):
            result, _ = self.run_pair(missing={'baseline': {(0, 0)}}, split=split)
            row = result['areas']['area0']
            self.assertEqual('common_measurements', row['comparison_scope'])
            self.assertEqual(('92', '46', '46'), tuple(row[key] for key in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
            self.assertEqual('96', row['full_current_kwh'])
            self.assertEqual('96', row['own_current_kwh'])
            self.assertIsNone(row['full_baseline_kwh'])
            self.assertEqual(23/24, row['coverage']['common_current'])
            self.assertEqual(100, row['percent'])
            self.assertFalse(result['reconciles_household_delta'])

    def test_four_ratios_reject_different_masks_and_ignore_unrelated_missing_room(self):
        current = (self.current[0], '2026-01-02T10:00Z')
        baseline = (self.baseline[0], '2026-01-01T10:00Z')
        for same, expected in ((True, 'ready'), (False, 'unknown')):
            result, _ = self.run_pair(current, baseline, count=2, split=3,
                missing={'current': {(0, 0)}, 'baseline': {(0, 0 if same else 1)}})
            row = result['areas']['area0']
            self.assertEqual(expected, row['status'])
            self.assertEqual(.9, row['coverage']['current'])
            self.assertEqual(.9 if same else .8, row['coverage']['common_baseline'])
            self.assertEqual('ready', result['areas']['area1']['status'])
            self.assertEqual('40', result['areas']['area1']['current_kwh'])
            if not same:
                self.assertIsNone(row['current_kwh'])
                self.assertIsNone(row['delta_kwh'])

    def test_zero_unknown_and_nonfinite_percent(self):
        result, _ = self.run_pair(values=('1', '0'))
        self.assertEqual('24', result['devices']['sensor.s0']['delta_kwh'])
        self.assertIsNone(result['devices']['sensor.s0']['percent'])
        result, _ = self.run_pair(values=('1e300', '1e-300'))
        self.assertEqual('ready', result['devices']['sensor.s0']['status'])
        self.assertIsNone(result['devices']['sensor.s0']['percent'])
        result, _ = self.run_pair(missing={'current': {(0, i) for i in range(24)}})
        self.assertEqual('unknown', result['devices']['sensor.s0']['status'])
        self.assertEqual(0, result['devices']['sensor.s0']['coverage']['current'])
        self.assertEqual('ready', result['unassigned']['status'])
        self.assertEqual('0', result['unassigned']['current_kwh'])

    def test_full_actual_calendar_dst_leap_and_month_lengths(self):
        cases = [
            (('2026-10-24T22:00Z', '2026-10-25T23:00Z'), ('2026-10-23T22:00Z', '2026-10-24T22:00Z'),
             'daily', 'Europe/Warsaw', (25, 24)),
            (('2026-10-24T22:00Z', '2026-10-26T00:00Z'), ('2026-10-23T22:00Z', '2026-10-24T22:00Z'),
             'daily', 'Antarctica/Troll', (26, 24)),
            (('2024-01-01T00:00Z', '2025-01-01T00:00Z'), ('2023-01-01T00:00Z', '2024-01-01T00:00Z'),
             'yearly', 'UTC', (8784, 8760)),
            (('2026-01-01T00:00Z', '2026-02-01T00:00Z'), ('2026-02-01T00:00Z', '2026-03-01T00:00Z'),
             'monthly', 'UTC', (744, 672))]
        for current, baseline, kind, zone, expected in cases:
            for split in (1, 73, 168):
                result, _ = self.run_pair(current, baseline, kind=kind, zone=zone, split=split, values=('1', '1'))
                row = result['areas']['area0']
                self.assertEqual('per_group_calendar_totals', row['comparison_scope'])
                self.assertEqual(expected, tuple(Decimal(row[key]) for key in ('current_kwh', 'baseline_kwh')))

    def test_hierarchy_from_real_projection_and_missing_child_preserves_known_remainder(self):
        fixtures = projection_fixtures.GroupProjectionTest()
        model = self.model((fixtures.start, fixtures.end), (fixtures.start, fixtures.end))
        data = fixtures.consumer()
        data['source_buckets']['sensor.child'][0]['kwh'] = None
        projected = fixtures.builder().add_chunk(
            readings(fixtures.start, fixtures.end, sources=('sensor.grid',), value=12),
            fixtures.start, fixtures.end, consumer_snapshot=data, include_group_hours=True)
        for side in ('current', 'baseline'):
            model.push(side, projected, fixtures.start, fixtures.end)
        result = model.finish()
        self.assertEqual('unknown', result['devices']['sensor.parent']['status'])
        self.assertEqual('6', result['devices']['sensor.parent']['own_current_kwh'])
        self.assertEqual(Decimal(4), Decimal(result['unmeasured']['current_kwh']))
        self.assertFalse(result['reconciles_household_delta'])
        complete = fixtures.builder().add_chunk(
            readings(fixtures.start, fixtures.end, sources=('sensor.grid',), value=12),
            fixtures.start, fixtures.end, consumer_snapshot=fixtures.consumer(), include_group_hours=True)
        model = self.model((fixtures.start, fixtures.end), (fixtures.start, fixtures.end))
        for side in ('current', 'baseline'):
            model.push(side, complete, fixtures.start, fixtures.end)
        result = model.finish({'status': 'ready', 'comparison_scope': 'calendar_totals'})
        self.assertEqual((Decimal(12), Decimal(8), Decimal(4)), tuple(Decimal(result['devices'][sid]['current_kwh'])
            for sid in ('sensor.parent', 'sensor.child')) + (Decimal(result['unmeasured']['current_kwh']),))
        self.assertTrue(result['reconciles_household_delta'])

    def test_history_costs_follow_group_mask_and_never_allocate_fixed_fees(self):
        old = version(start='2025-12-31', rate=.4, version_id='old', monthly=31)
        new = version(start='2026-01-02', rate=.5, version_id='new', monthly=31)
        old['profile']['zones'][0]['variableRate'] = .1
        new['profile']['zones'][0]['variableRate'] = .2
        current = ('2026-01-01T23:00Z', '2026-01-02T23:00Z')
        baseline = ('2025-12-31T23:00Z', '2026-01-01T23:00Z')
        for split in (1, 7, 168):
            result, _ = self.run_pair(current, baseline, rates=tariff([old, new]), split=split,
                missing={'current': {(0, 0)}, 'baseline': {(0, 1)}})
            row = result['areas']['area0']
            self.assertEqual(('88', '44'), (row['current_kwh'], row['baseline_kwh']))
            self.assertEqual('61.60', row['costs']['current']['usage_cost'])
            self.assertEqual('22.00', row['costs']['baseline']['usage_cost'])
            self.assertEqual('39.60', row['delta_usage_cost'])
            self.assertEqual('26.40', row['delta_energy_cost'])
            self.assertEqual('13.20', row['delta_variable_cost'])
            self.assertEqual(22*3600, row['costs']['current']['mask_source_seconds'])
            self.assertIsNone(row['delta_fixed_cost'])
            self.assertNotIn('fixed_cost', row['costs']['current'])
            self.assertNotIn('fees', row['costs']['baseline'])
        result, _ = self.run_pair(rates=tariff([version()]), domain='site_consumption')
        self.assertEqual('ready', result['areas']['area0']['status'])
        self.assertIsNone(result['areas']['area0']['delta_usage_cost'])

    def test_context_shape_and_numeric_failures_leave_retry_unmodified(self):
        model = self.model()
        data = projection(*self.current)
        for change in (
            lambda p: p.update(membership_mode='historical'),
            lambda p: p['hours'][-1]['devices'].update({'sensor.s0': 'NaN'}),
            lambda p: p['hours'][-1]['areas'].update({'area0': '-1'}),
            lambda p: p['hours'][-1].update(partition_complete=True, household_kwh='999'),
            lambda p: p['hours'][-1].update(grid_source_presence=[]),
            lambda p: p['hours'][-1].update(to=0),
            lambda p: p['hours'].pop()):
            bad = deepcopy(data)
            change(bad)
            with self.assertRaises(ValueError): model.push('current', bad, *self.current)
            self.assertEqual('current', model.needs_side())
            self.assertEqual(0, model.retained_cells)
        model.push('current', data, *self.current)
        data['hours'][0]['devices']['sensor.s0'] = '999'
        baseline = projection(*self.baseline)
        for key, value in (('registry_revision', 8), ('energy_domain', 'site_consumption'),
                           ('consumer_provenance', {'sensor.s0': {**PROVENANCE, 'source_unit': 'Wh'}})):
            bad = deepcopy(baseline)
            bad[key] = value
            with self.assertRaises(ValueError): model.push('baseline', bad, *self.baseline)
        model.push('baseline', baseline, *self.baseline)
        result = model.finish()
        self.assertEqual('24', result['devices']['sensor.s0']['current_kwh'])
        result['devices'].clear()
        self.assertIn('sensor.s0', model.finish()['devices'])

    def test_maximum_groups_two_slots_and_chunk_bound(self):
        current = ('2026-01-02T00:00Z', '2026-01-10T00:00Z')
        baseline = ('2025-01-02T00:00Z', '2025-01-10T00:00Z')
        model = self.model(current, baseline, kind='weekly')
        with self.assertRaises(ValueError): model.push('current', {}, *current)
        self.assertEqual('current', model.needs_side())
        result, model = self.run_pair(current, baseline, count=512, kind='weekly')
        groups = sum(len(result[level]) for level in ('devices', 'areas', 'floors')) + 2
        self.assertEqual(1538, groups)
        self.assertGreater(model.peak_retained_cells, 168*groups)
        self.assertLessEqual(model.peak_retained_cells, 2*168*groups)
        self.assertEqual(0, model.retained_cells)
        self.assertEqual('768', result['areas']['area511']['current_kwh'])

    def test_partial_dst_fold_and_whole_month_origin(self):
        result, _ = self.run_pair(('2026-10-24T22:00Z', '2026-10-25T23:00Z'),
            ('2026-10-23T22:00Z', '2026-10-24T22:00Z'), zone='Europe/Warsaw',
            split=2, values=('1', '1'), missing={'current': {(0, 20)}})
        row = result['areas']['area0']
        self.assertEqual(('23', '23'), (row['current_kwh'], row['baseline_kwh']))
        self.assertEqual(24/25, row['coverage']['current'])
        self.assertEqual(23/25, row['coverage']['common_current'])
        result, _ = self.run_pair(('2026-01-01T00:00Z', '2026-02-01T00:00Z'),
            ('2026-04-01T00:00Z', '2026-05-01T00:00Z'), kind='monthly', split=73,
            values=('1', '1'), missing={'current': {(0, 8*24)}, 'baseline': {(0, 9*24)}})
        self.assertEqual(('718', '718'), (result['areas']['area0']['current_kwh'], result['areas']['area0']['baseline_kwh']))

    def test_legacy_and_missing_historical_component_do_not_block_energy(self):
        old = version(start='2026-01-01', rate=.005, version_id='old', rate_semantics='legacy_combined')
        new = version(start='2026-01-02', rate=.005, version_id='new')
        new['profile']['zones'][0]['variableRate'] = .005
        result, _ = self.run_pair(rates=tariff([old, new]), values=('1', '1'))
        row = result['areas']['area0']
        self.assertEqual(('0.24', '0.12'), (row['costs']['current']['usage_cost'], row['costs']['baseline']['usage_cost']))
        self.assertEqual('0.12', row['delta_usage_cost'])
        self.assertIsNone(row['delta_energy_cost'])
        self.assertEqual('-0.12', row['delta_legacy_combined_cost'])
        result, _ = self.run_pair(rates=tariff([new]))
        row = result['areas']['area0']
        self.assertEqual('ready', row['status'])
        self.assertIsNone(row['delta_usage_cost'])
        self.assertIn('unknown_historical_version', row['costs']['baseline']['issues'])

    def test_structurally_empty_and_no_floor_groups(self):
        result, _ = self.run_pair(count=0)
        self.assertEqual({}, result['devices'])
        self.assertEqual({}, result['floors'])
        self.assertEqual('0', result['unassigned']['current_kwh'])
        model = self.model()
        for side, bounds in (('current', self.current), ('baseline', self.baseline)):
            data = projection(*bounds)
            for hour in data['hours']:
                hour['floors'] = {'__no_floor__': '1'}
            model.push(side, data, *bounds)
        self.assertEqual('24', model.finish()['floors']['__no_floor__']['current_kwh'])

    def test_disjoint_grid_presence_retains_valid_room_but_not_residual(self):
        model = self.model()
        for side, bounds in (('current', self.current), ('baseline', self.baseline)):
            data = projection(*bounds)
            data['grid_source_ids'].append('sensor.other')
            data['grid_provenance']['sensor.other'] = deepcopy(PROVENANCE)
            for index, hour in enumerate(data['hours']):
                hour['grid_source_presence'] = ['sensor.grid' if index % 2 else 'sensor.other']
                hour['household_kwh'] = hour['unmeasured'] = None
            model.push(side, data, *bounds)
        result = model.finish({'status': 'ready', 'comparison_scope': 'common_measurements'})
        self.assertEqual('24', result['areas']['area0']['current_kwh'])
        self.assertEqual('unknown', result['unmeasured']['status'])
        self.assertFalse(result['reconciles_household_delta'])

    def test_one_price_lookup_per_actual_hour_for_all_groups_and_both_masks(self):
        seen = []
        original = core.HistoricalTariffRates.resolve_interval
        def resolve(rates, start, end):
            seen.append((start, end))
            return original(rates, start, end)
        with patch.object(core.HistoricalTariffRates, 'resolve_interval', resolve):
            result, _ = self.run_pair(count=3, split=7, rates=tariff([version()]))
        self.assertEqual('ready', result['areas']['area0']['status'])
        self.assertEqual(48, len(seen))
        self.assertEqual(48, len(set(seen)))

    def test_decimal_residual_price_stays_below_half_cent_before_period_rounding(self):
        v = version(rate=.05); v['profile']['zones'][0]['variableRate'] = 0
        result, _ = self.run_pair((self.current[0], '2026-01-02T01:00Z'),
            (self.baseline[0], '2026-01-01T01:00Z'), rates=tariff([v]),
            values=('0.09999999999999999999999999999999999999', '0'))
        row = result['areas']['area0']
        self.assertEqual('0.00', row['costs']['current']['usage_cost'])
        self.assertEqual('0.00', row['delta_usage_cost'])
