"""Recorder grace remains stable across hour and local-day boundaries."""
import unittest
from datetime import datetime, timezone
from tests.test_build1_energy_native_short import energy, short

cutoff_at = short.cutoff_at
measurement_window = energy.measurement_window


class RecorderGraceTest(unittest.TestCase):
    def test_hour_boundary_and_local_midnight(self):
        for now, cutoff, end in (
            ('2026-01-01T11:02:00+00:00', '2026-01-01T10:55:00+00:00', '2026-01-01T10:00:00+00:00'),
            ('2026-01-01T11:05:00+00:00', '2026-01-01T11:00:00+00:00', '2026-01-01T11:00:00+00:00'),
            ('2026-01-01T00:02:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
        ):
            with self.subTest(now=now):
                now = datetime.fromisoformat(now)
                self.assertEqual(cutoff_at(now, 'UTC').isoformat(), cutoff)
                self.assertEqual(measurement_window(now, 'UTC')[1].isoformat(), end)

    def test_dst_fold_preserves_absolute_hour(self):
        now = datetime.fromisoformat('2026-10-25T01:02:00+00:00')
        self.assertEqual(cutoff_at(now, 'Europe/Warsaw').isoformat(), '2026-10-25T00:55:00+00:00')
        self.assertEqual(measurement_window(now, 'Europe/Warsaw')[1].isoformat(), '2026-10-25T00:00:00+00:00')


class EntityPrivacyTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_source_ids_are_not_in_snapshot_or_entity_attributes(self):
        import importlib
        from tests.test_energy_snapshot import entity_modules
        from tests.test_build1_energy_entities import NativeSensorTests
        projection = importlib.import_module(energy.__package__ + '.energy_entity_projection')
        start = datetime.fromisoformat('2026-01-01T00:00:00+00:00')
        end = datetime.fromisoformat('2026-01-01T00:05:00+00:00')
        result = {'today': {'from': start.isoformat(), 'to': start.isoformat(),
                            'boundary_complete': True}}
        measurement = {'source_ids': ['sensor.private_a', 'sensor.private_b'], 'hourly_buckets': []}
        cells = [{'from': start.isoformat(), 'to': end.isoformat(), 'kwh': 1,
                  'missing_sources': ['sensor.private_b']}]
        facts = projection._today(result, measurement, None, cells, end)
        self.assertEqual(1, facts['missing_sources_count'])
        self.assertNotIn('sensor.private', repr(facts))
        helper = NativeSensorTests()
        entities, coordinator = await helper.sensors()
        coordinator.data['entity_facts'] = facts
        self.assertNotIn('sensor.private', repr(entities['today'].extra_state_attributes))
        self.assertEqual(1, entities['today'].extra_state_attributes['missing_sources_count'])
