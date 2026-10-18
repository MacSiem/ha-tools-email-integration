"""Requested comparisons use actual guarded provider reads and one DTO/hash."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import unittest
from tests import test_energy_analysis_service as fixtures
from tests import test_analysis_periods as calendar_fixtures
from tests import test_energy_analysis_websocket as ws_fixtures


class AnalysisComparisonsTest(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        h = fixtures.Harness(); h.book = None
        h.now = datetime(2026, 10, 18, 12, 34, tzinfo=timezone.utc)
        return h

    async def test_historical_year_pair_streams_each_grid_once_and_consumers_only_current(self):
        h = self.harness()
        with h.patches():
            result = await h.subject.async_get_analysis('year', 'user', anchor='2024-06-15', comparison='previous')
            compared = result['comparisons']['previous']
            self.assertEqual('ready', compared['status'])
            self.assertEqual((87840, 87600, 240), tuple(compared[k] for k in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
            self.assertEqual(159, len(h.queries))
            self.assertEqual(53, sum(ids == ('sensor.device',) for ids, _, _ in h.queries))
            self.assertTrue(all(0 < (end-start).total_seconds() <= 168*3600 for _, start, end in h.queries))
            self.assertEqual('previous', result['context']['comparison_kind'])
            payload = copy.deepcopy(result); fingerprint = payload.pop('snapshot_id')
            self.assertEqual(fingerprint, hashlib.sha256(json.dumps(payload, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode()).hexdigest())
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))

    async def test_cache_separates_requested_baseline_and_retains_final_reauthorization(self):
        h = self.harness()
        with h.patches():
            first = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            count = len(h.queries)
            again = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            self.assertEqual(first, again); self.assertEqual(count, len(h.queries))
            other = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='same_weekday')
            self.assertNotEqual(first['snapshot_id'], other['snapshot_id'])
            self.assertNotEqual(first['context']['comparison_signature'], other['context']['comparison_signature'])
            h.user.is_active = False
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='same_weekday')
            self.assertIsNone(h.subject._cache)

    async def test_revoke_during_baseline_io_never_returns_or_caches_comparison(self):
        for mode in ('role', 'policy', 'registry', 'unit'):
            h = self.harness()
            async def mutate(ids, start, end):
                if start.date().isoformat() == '2026-10-09':
                    if mode == 'role': h.user.is_active = False
                    elif mode == 'policy': h.storage.current_feature_access_revision += 1
                    elif mode == 'registry': h.registry.revision += 1
                    else: h.metadata['sensor.grid']['unit_of_measurement'] = 'Wh'
            h.on_read = mutate
            with self.subTest(mode=mode), h.patches():
                with self.assertRaises((PermissionError, RuntimeError)):
                    await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
                self.assertIsNone(h.subject._cache)
                self.assertEqual(1, sum(ids == ('sensor.device',) for ids, _, _ in h.queries))

    async def test_daily_mean_reads_all_seven_and_uses_one_eight_window_intersection(self):
        h = self.harness(); original = h.usage
        async def usage(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            sensors, start = args[1], args[2]
            if sensors[0]['entity_id'] == 'sensor.grid':
                snapshot['source_buckets']['sensor.grid'][start.day-3]['kwh'] = None
            return snapshot
        h.usage = usage
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='mean7')
            compared = result['comparisons']['mean7']
            self.assertEqual('unknown', compared['status'])
            self.assertAlmostEqual(16/24, compared['coverage']['common_current'])
            self.assertIsNone(compared['delta_kwh'])
            self.assertEqual(9, len(h.queries))

    async def test_invalid_comparison_before_private_io_and_ws_only_passes_supported_fields(self):
        h = self.harness()
        with h.patches():
            with self.assertRaises(ValueError):
                await h.subject.async_get_analysis('year', 'user', comparison='mean7')
            self.assertEqual([], h.queries); self.assertEqual([], h.registry_ids)
        ws, hass, service, conn, results, errors, _ = ws_fixtures.AnalysisWebsocketTest.fixture(self)
        received = []
        async def get(kind, user_id, **kwargs):
            received.append((kind, user_id, kwargs)); return {'schema': 2}
        service.async_get_analysis = get
        await ws._ws_energy_analysis(hass, conn, {'id': 1, 'period': 'day', 'comparison': 'previous',
            'anchor': '2026-10-10', 'user_id': 'forged', 'now': '9999-01-01'})
        self.assertEqual([], errors)
        self.assertEqual([('day', 'synthetic', {'anchor': '2026-10-10', 'comparison': 'previous'})], received)


class WeeklyYearAgoTest(unittest.TestCase):
    def test_same_iso_week_crosses_gregorian_year_and_preserves_partial_cutoff(self):
        periods = calendar_fixtures.periods.resolve_analysis_periods('week',
            datetime(2021, 1, 6, 12, 34, tzinfo=timezone.utc), 'UTC')
        baseline = periods['comparisons']['year_ago']['baseline_periods'][0]
        self.assertEqual('2019-12-30T00:00:00+00:00', baseline['start'])
        self.assertEqual('2020-01-01T12:00:00+00:00', baseline['end'])
        self.assertEqual(60, baseline['hours'])

    def test_nonexistent_iso_week53_is_unknown_without_substitution(self):
        periods = calendar_fixtures.periods.resolve_analysis_periods('week',
            datetime(2026, 10, 18, 12, tzinfo=timezone.utc), 'UTC', anchor='2020-12-28')
        self.assertEqual('ready', periods['status'])
        self.assertEqual({'status': 'unknown', 'reason': 'iso_week_unavailable', 'baseline_periods': []},
                         periods['comparisons']['year_ago'])
