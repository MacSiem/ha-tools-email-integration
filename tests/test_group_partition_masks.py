"""Actual producer evidence for partial energy partition; costs remain separate."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from decimal import Decimal

from tests.test_analysis_group_projection import analysis, registry_model
from tests.test_energy_weighted_comparison import readings
from tests import test_group_comparison_stream as group_fixtures


class GroupPartitionMasksTest(unittest.TestCase):
    bounds = (group_fixtures.GroupStreamTest.current, group_fixtures.GroupStreamTest.baseline)

    def projected(self, bounds, *, grid_missing=(0,), missing=None, grids=('sensor.grid',),
                  unassigned=False, child=False, zero=False, value=4, grid_value=12):
        start, end = bounds
        ids = ['sensor.parent', 'sensor.child']
        entities = {sid: {'area_id': 'area'+str(i)} for i, sid in enumerate(ids)
                    if not (unassigned and i == 1)}
        membership = registry_model.resolve_membership(ids, {'entities': entities,
            'areas': {'area'+str(i): {'floor_id': 'floor'+str(i)} for i in range(2)},
            'floors': {'floor'+str(i): {'name': str(i)} for i in range(2)}},
            captured_at='2026-01-01T00:00Z', revision=2)
        consumers = [{'stat_consumption': sid, 'included_in_stat':
                      'sensor.parent' if child and i == 1 else None} for i, sid in enumerate(ids)]
        builder = analysis.EnergyAnalysisAccumulator(None,
            {'start': start, 'end': end, 'kind': 'day', 'timezone': 'UTC'},
            {'registry_revision': 2}, generated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            consumers=consumers, membership=membership, consumer_domain='grid_import')
        grid = readings(start, end, sources=grids, value=grid_value,
            missing=tuple((grids[0], hour) for hour in grid_missing))
        missing = {(sid, hour) for sid in ids for hour in grid_missing} if missing is None else missing
        data = readings(start, end, sources=tuple(ids), value=value, missing=missing)
        if zero:
            for row in data['source_buckets']['sensor.child']:
                if row['kwh'] is not None: row['kwh'] = 0
        return builder.add_chunk(grid, start, end, consumer_snapshot=data, include_group_hours=True)

    def compare(self, projections, *, bounds=None, zone='UTC', scope='common_measurements'):
        bounds = bounds or self.bounds
        model = group_fixtures.GroupStreamTest().model(*bounds, zone=zone)
        for side, data, period in zip(('current', 'baseline'), projections, bounds):
            model.push(side, data, *period)
        return model.finish({'status': 'ready', 'comparison_scope': scope}), model

    def test_actual_shared_gap_proves_energy_and_structural_empty_unassigned(self):
        data = [self.projected(period, value=value, grid_value=grid)
                for period, value, grid in zip(self.bounds, (4, 2), (12, 8))]
        self.assertEqual([], data[0]['group_membership']['unassigned'])
        result, model = self.compare(data)
        self.assertTrue(result['reconciles_household_delta'])
        for level, key in (('devices', 'sensor.parent'), ('areas', 'area0'), ('floors', 'floor0')):
            self.assertEqual(23/24, result[level][key]['coverage']['common_current'])
        self.assertEqual('0', result['unassigned']['current_kwh'])
        for level in ('devices', 'areas', 'floors'):
            partition = sum((Decimal(row['delta_kwh']) for row in result[level].values()), Decimal(0))
            if level == 'areas': partition += Decimal(result['unassigned']['delta_kwh'])
            partition += Decimal(result['unmeasured']['delta_kwh'])
            self.assertEqual(Decimal(92), partition)
        self.assertLessEqual(model.peak_retained_cells, 2*24*(sum(len(result[level]) for level in ('devices', 'areas', 'floors')) + 2))
        self.assertEqual(0, model.retained_cells)

    def test_different_rooms_and_measured_zero_are_not_empty_proofs(self):
        for zero in (False, True):
            data = [self.projected(period, unassigned=zero, zero=zero,
                missing={('sensor.parent', 0), ('sensor.child', 1)}) for period in self.bounds]
            result, _ = self.compare(data)
            self.assertFalse(result['reconciles_household_delta'])
            if zero:
                self.assertEqual(['sensor.child'], data[0]['group_membership']['unassigned'])
                self.assertEqual('0', result['unassigned']['current_kwh'])

    def test_partial_two_grid_sources_cannot_prove_household_even_with_equal_sums(self):
        data = [self.projected(period, grids=('sensor.grid', 'sensor.other'), missing=())
                for period in self.bounds]
        result, _ = self.compare(data)
        self.assertEqual('0', result['areas']['area0']['delta_kwh'])
        self.assertFalse(result['reconciles_household_delta'])

    def test_missing_child_keeps_known_unmeasured_but_rejects_partition(self):
        data = [self.projected(period, grid_missing=(), child=True,
            missing={('sensor.child', 0)}) for period in self.bounds]
        self.assertEqual(Decimal(8), Decimal(data[0]['hours'][0]['unmeasured']))
        self.assertFalse(data[0]['hours'][0]['partition_complete'])
        self.assertFalse(self.compare(data)[0]['reconciles_household_delta'])

    def test_unmatched_fold_complete_group_does_not_use_household_common_mask(self):
        bounds = (('2026-10-24T22:00Z', '2026-10-25T23:00Z'),
                  ('2026-10-23T22:00Z', '2026-10-24T22:00Z'))
        data = [self.projected(period, grid_missing=(20,), missing=()) for period in bounds]
        result, _ = self.compare(data, bounds=bounds, zone='Europe/Warsaw')
        self.assertEqual('per_group_calendar_totals', result['areas']['area0']['comparison_scope'])
        self.assertFalse(result['reconciles_household_delta'])

    def test_old_projection_is_accepted_without_partial_false_positive_and_full_is_preserved(self):
        data = [self.projected(period) for period in self.bounds]
        for projection in data: projection.pop('group_membership', None)
        self.assertFalse(self.compare(data)[0]['reconciles_household_delta'])
        data = [self.projected(period, grid_missing=()) for period in self.bounds]
        for projection in data: projection.pop('group_membership', None)
        self.assertTrue(self.compare(data, scope='calendar_totals')[0]['reconciles_household_delta'])

    def test_chunk_boundaries_do_not_change_mask_proof(self):
        data = [self.projected(period) for period in self.bounds]
        from datetime import timedelta
        for width in (1, 7, 24):
            model = group_fixtures.GroupStreamTest().model()
            for offset in range(0, 24, width):
                for side, projection, bounds in zip(('current', 'baseline'), data, self.bounds):
                    start = datetime.fromisoformat(bounds[0]) + timedelta(hours=offset)
                    end = min(start+timedelta(hours=width), datetime.fromisoformat(bounds[1]))
                    chunk = deepcopy(projection)
                    chunk['hours'] = chunk['hours'][offset:offset+width]
                    model.push(side, chunk, start, end)
            result = model.finish({'status': 'ready', 'comparison_scope': 'common_measurements'})
            self.assertTrue(result['reconciles_household_delta'])
            self.assertLessEqual(model.peak_retained_cells, 2*width*(sum(len(result[level]) for level in ('devices', 'areas', 'floors')) + 2))

    def test_missing_metadata_cannot_borrow_prior_chunk_membership(self):
        data = [self.projected(period) for period in self.bounds]
        model = group_fixtures.GroupStreamTest().model()
        model.push('current', data[0], *self.bounds[0])
        old = deepcopy(data[1]); old.pop('group_membership', None)
        with self.assertRaisesRegex(ValueError, 'group_context_changed'):
            model.push('baseline', old, *self.bounds[1])
        model.push('baseline', data[1], *self.bounds[1])
        self.assertTrue(model.finish({'status': 'ready',
            'comparison_scope': 'common_measurements'})['reconciles_household_delta'])

    def test_invalid_membership_rejection_is_atomic_and_identity_is_stable(self):
        data = [self.projected(period) for period in self.bounds]
        model = group_fixtures.GroupStreamTest().model()
        mutations = (
            lambda p: p['group_membership']['areas']['area0'].append('sensor.child'),
            lambda p: p['group_membership']['devices'].update({'sensor.parent': ['sensor.child']}),
            lambda p: p['group_membership']['floors'].pop('floor0'),
            lambda p: p['group_membership'].update(unassigned=['sensor.parent']),
            lambda p: p['group_membership']['areas'].update(extra=[]))
        for mutate in mutations:
            bad = deepcopy(data[0]); mutate(bad)
            with self.assertRaises(ValueError): model.push('current', bad, *self.bounds[0])
            self.assertEqual(0, model.retained_cells)
            self.assertEqual('current', model.needs_side())
        model.push('current', data[0], *self.bounds[0])
        bad = deepcopy(data[1])
        bad['group_membership']['areas']['area0'], bad['group_membership']['areas']['area1'] = (
            bad['group_membership']['areas']['area1'], bad['group_membership']['areas']['area0'])
        with self.assertRaisesRegex(ValueError, 'group_context_changed'):
            model.push('baseline', bad, *self.bounds[1])
        model.push('baseline', data[1], *self.bounds[1])
        self.assertTrue(model.finish({'status': 'ready',
            'comparison_scope': 'common_measurements'})['reconciles_household_delta'])
