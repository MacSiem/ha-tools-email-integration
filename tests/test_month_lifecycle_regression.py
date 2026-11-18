"""Lifecycle proof survives monthly comparison and calendar mail production."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from tests import test_report70_comparison as fixtures
from tests.test_group_comparison_stream import stream

UTC = timezone.utc

class MonthLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_month_comparison_and_calendar_preview(self):
        fixture = fixtures.Report70ComparisonTest()
        h, _ = fixture.fixture(now='2026-11-18T12:34:00+00:00')
        h.user.is_admin = True
        h.manager.data['energy_sources'].append({'type': 'grid', 'stat_energy_from': 'sensor.new'})
        h.metadata['sensor.new'] = deepcopy(h.metadata['sensor.grid'])
        first = int(datetime(2026, 9, 10, tzinfo=UTC).timestamp()*1000)
        gap = int(datetime(2026, 9, 15, tzinfo=UTC).timestamp()*1000)
        async def usage(hass, sensors, start, end, cadence, *, metadata):
            ids = tuple(row['entity_id'] for row in sensors)
            data = fixtures.fixtures.readings(start.isoformat(), end.isoformat(), value=1, sources=ids)
            if 'sensor.new' in ids:
                data['source_first'] = {'sensor.grid': 0, 'sensor.new': first}
                for row in data['source_buckets']['sensor.new']:
                    if row['from'] < first or row['from'] == gap:
                        row['kwh'] = None
            return data
        h.usage = usage
        with h.patches():
            analysis = await h.subject.async_get_analysis('month', 'real-admin', anchor='2026-10-01', comparison='previous')
            comparison = analysis['comparisons']['previous']
            self.assertEqual('common_measurements', comparison['comparison_scope'])
            self.assertFalse(comparison['groups']['reconciles_household_delta'])
            request = fixture.request(h, cadence='monthly')
            report = await fixture.prepare(h, request, purpose='report_read')
            self.assertTrue(report['html'])
            self.assertIn('previous', report['summary']['comparisons'])
            self.assertEqual('common_measurements', report['summary']['comparisons']['previous']['comparison_scope'])

    def test_forged_lifecycle_proof_rejected(self):
        from tests.test_analysis_group_projection import GroupProjectionTest
        from tests.test_energy_weighted_comparison import readings
        f = GroupProjectionTest()
        grid = readings(f.start, f.end, sources=('sensor.grid', 'sensor.new'), value=12)
        grid['source_first'] = {'sensor.grid': 0, 'sensor.new': int(datetime.fromisoformat(f.middle).timestamp()*1000)}
        grid['source_buckets']['sensor.new'][0]['kwh'] = None
        projection = f.builder().add_chunk(grid, f.start, f.end, consumer_snapshot=f.consumer(), include_group_hours=True)
        forged = deepcopy(projection)
        forged['hours'][1]['grid_source_expected'].remove('sensor.new')
        with self.assertRaisesRegex(ValueError, 'invalid_grid_source_expected'):
            stream.group_projection_evidence(forged, datetime.fromisoformat(f.start), datetime.fromisoformat(f.end))
        projection['hours'][1]['grid_source_presence'].remove('sensor.new')
        with self.assertRaisesRegex(ValueError, 'partial_grid_cannot_supply_household'):
            stream.group_projection_evidence(projection, datetime.fromisoformat(f.start), datetime.fromisoformat(f.end))
