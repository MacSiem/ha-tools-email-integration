"""Optional chunk projection reuses actual disjoint hourly group calculations."""
import copy
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from tests.test_energy_analysis import analysis
from tests.test_energy_analysis_service import registry_model
from tests.test_energy_weighted_comparison import readings


class GroupProjectionTest(unittest.TestCase):
    start, middle, end = '2026-01-01T00:00Z', '2026-01-01T01:00Z', '2026-01-01T02:00Z'

    def builder(self, *, domain='grid_import', count=None):
        ids = ['sensor.parent', 'sensor.child'] if count is None else [f'sensor.s{i}' for i in range(count)]
        consumers = [{'stat_consumption': sid, 'included_in_stat': 'sensor.parent'
                      if sid == 'sensor.child' else None} for sid in ids]
        entities = {sid: {'area_id': 'area'+str(i)} for i, sid in enumerate(ids)}
        areas = {'area'+str(i): {'name': str(i), 'floor_id': 'floor'+str(i)} for i in range(len(ids))}
        floors = {'floor'+str(i): {'name': str(i)} for i in range(len(ids))}
        membership = registry_model.resolve_membership(ids, {'entities': entities, 'areas': areas,
            'floors': floors}, captured_at='2026-01-01T03:00Z', revision=2)
        return analysis.EnergyAnalysisAccumulator(None,
            {'start': self.start, 'end': self.end, 'kind': 'day', 'timezone': 'UTC'},
            {'registry_revision': 2}, generated_at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
            consumers=consumers, membership=membership, consumer_domain=domain)

    def consumer(self, start=None, end=None):
        data = readings(start or self.start, end or self.end, sources=('sensor.parent', 'sensor.child'))
        for sid, value in (('sensor.parent', 10), ('sensor.child', 4)):
            for row in data['source_buckets'][sid]: row['kwh'] = value
        return data

    def test_hierarchy_projection_and_default_result_are_identical_and_detached(self):
        grid = readings(self.start, self.end, sources=('sensor.grid',), value=12)
        consumer = self.consumer(); original = copy.deepcopy((grid, consumer))
        projected, plain = self.builder(), self.builder()
        hours = projected.add_chunk(grid, self.start, self.end, consumer_snapshot=consumer, include_group_hours=True)
        self.assertIsNone(plain.add_chunk(grid, self.start, self.end, consumer_snapshot=consumer))
        self.assertEqual(1, hours['schema']); self.assertEqual(2, len(hours['hours']))
        self.assertEqual('current_snapshot', hours['membership_mode'])
        self.assertEqual(2, hours['registry_revision'])
        for hour in hours['hours']:
            self.assertTrue(hour['partition_complete'])
            self.assertEqual(['sensor.grid'], hour['grid_source_presence'])
            self.assertEqual(Decimal(6), Decimal(hour['devices']['sensor.parent']))
            self.assertEqual(Decimal(4), Decimal(hour['devices']['sensor.child']))
            self.assertEqual(Decimal(2), Decimal(hour['unmeasured']))
            self.assertEqual(Decimal(0), Decimal(hour['unassigned']))
            self.assertEqual(Decimal(6), Decimal(hour['areas']['area0']))
            self.assertEqual(Decimal(4), Decimal(hour['floors']['floor1']))
        hours['hours'][0]['devices']['sensor.parent'] = '9999'
        self.assertEqual(plain.finish(), projected.finish())
        self.assertEqual(original, (grid, consumer))

    def test_missing_grid_never_projects_partial_import_as_household_or_residual(self):
        model = self.builder()
        grid = readings(self.start, self.end, sources=('sensor.grid', 'sensor.missing'), value=12)
        for row in grid['source_buckets']['sensor.missing']: row['kwh'] = None
        projection = model.add_chunk(grid, self.start, self.end, consumer_snapshot=self.consumer(), include_group_hours=True)
        self.assertEqual(['sensor.grid', 'sensor.missing'], projection['grid_source_ids'])
        for hour in projection['hours']:
            self.assertIsNone(hour['household_kwh']); self.assertIsNone(hour['unmeasured'])
            self.assertFalse(hour['partition_complete'])
            self.assertEqual(['sensor.grid'], hour['grid_source_presence'])
            self.assertEqual(Decimal(6), Decimal(hour['devices']['sensor.parent']))

    def test_missing_child_does_not_turn_parent_into_full_meter(self):
        consumer = self.consumer(); consumer['source_buckets']['sensor.child'][0]['kwh'] = None
        projection = self.builder().add_chunk(readings(self.start, self.end, sources=('sensor.grid',), value=12),
            self.start, self.end, consumer_snapshot=consumer, include_group_hours=True)
        first, second = projection['hours']
        self.assertIsNone(first['devices']['sensor.parent'])
        self.assertIsNone(first['devices']['sensor.child'])
        self.assertIsNone(first['unmeasured'])
        self.assertEqual(Decimal(6), Decimal(second['devices']['sensor.parent']))

    def test_site_domain_preserves_room_energy_without_grid_remainder(self):
        projection = self.builder(domain='site_consumption').add_chunk(
            readings(self.start, self.end, sources=('sensor.grid',), value=12), self.start, self.end,
            consumer_snapshot=self.consumer(), include_group_hours=True)
        self.assertEqual('site_consumption', projection['energy_domain'])
        for hour in projection['hours']:
            self.assertIsNone(hour['unmeasured']); self.assertFalse(hour['partition_complete'])
            self.assertEqual(Decimal(6), Decimal(hour['areas']['area0']))

    def test_rejected_chunk_can_be_retried_without_advancing_or_counting_twice(self):
        model = self.builder()
        model.add_chunk(readings(self.start, self.middle, sources=('sensor.grid',), value=12),
            self.start, self.middle, consumer_snapshot=self.consumer(self.start, self.middle), include_group_hours=True)
        bad = self.consumer(self.middle, self.end)
        bad['measurement_provenance']['sensor.child']['source_unit'] = 'Wh'
        with self.assertRaises(ValueError):
            model.add_chunk(readings(self.middle, self.end, sources=('sensor.grid',), value=12),
                self.middle, self.end, consumer_snapshot=bad, include_group_hours=True)
        retry = model.add_chunk(readings(self.middle, self.end, sources=('sensor.grid',), value=12),
            self.middle, self.end, consumer_snapshot=self.consumer(self.middle, self.end), include_group_hours=True)
        self.assertEqual(1, len(retry['hours']))
        self.assertEqual(Decimal(12), Decimal(model.finish()['groups']['devices']['sensor.parent']['kwh']))

    def test_all_group_levels_may_exceed_recorder_source_limit_without_year_storage(self):
        model = self.builder(count=512)
        data = readings(self.start, self.end, sources=tuple(f'sensor.s{i}' for i in range(512)))
        projection = model.add_chunk(readings(self.start, self.end, sources=('sensor.grid',), value=600),
            self.start, self.end, consumer_snapshot=data, include_group_hours=True)
        hour = projection['hours'][0]
        self.assertEqual((512, 512, 512), tuple(len(hour[key]) for key in ('devices', 'areas', 'floors')))
        self.assertEqual(Decimal(88), Decimal(hour['unmeasured']))
        self.assertEqual(2, len(projection['hours']))
