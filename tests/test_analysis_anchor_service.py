"""Selected periods reach actual bounded analysis and final context validation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch
from tests import test_energy_analysis_service as fixtures
from tests import test_energy_analysis_websocket as ws_fixtures


class AnalysisAnchorServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_historical_leap_year_reads_actual_53_bounded_pairs(self):
        h = fixtures.Harness(); h.book = None
        h.now = datetime(2026, 10, 18, 12, 34, tzinfo=timezone.utc)
        with h.patches():
            result = await h.subject.async_get_analysis('year', 'user', anchor='2024-06-15')
            self.assertEqual('2024-01-01T00:00:00+00:00', result['period']['start'])
            self.assertEqual('2025-01-01T00:00:00+00:00', result['period']['end'])
            self.assertEqual('2024-01-01', result['context']['period_anchor'])
            self.assertFalse(result['period']['partial'])
            self.assertEqual(8784, result['period']['hours'])
            self.assertEqual(Decimal(87840), Decimal(result['totals']['kwh']))
            windows = [(a, b) for ids, a, b in h.queries if ids == ('sensor.grid',)]
            self.assertEqual(53, len(windows))
            self.assertEqual(106, len(h.queries))
            self.assertTrue(all(0 < (b-a).total_seconds() <= 7*86400 for a, b in windows))
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))

    async def test_historical_context_cache_survives_hour_rollover_but_selection_changes_it(self):
        h = fixtures.Harness(); h.book = None
        h.now = datetime(2026, 10, 18, 12, 34, tzinfo=timezone.utc)
        with h.patches():
            earlier = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10')
            self.assertEqual(24, earlier['period']['hours'])
            self.assertEqual(2, len(h.queries))
            h.now += timedelta(hours=1)
            self.assertTrue(h.subject.context_is_current(earlier))
            self.assertTrue(await h.subject.async_validate_result(earlier, 'user'))
            cached = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10')
            self.assertEqual(earlier, cached)
            self.assertEqual(2, len(h.queries))
            next_day = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-11')
            self.assertNotEqual(earlier['snapshot_id'], next_day['snapshot_id'])
            self.assertEqual(4, len(h.queries))
            current = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-18')
            h.now += timedelta(hours=1)
            self.assertFalse(h.subject.context_is_current(current))
            self.assertTrue(h.subject.context_is_current(earlier))

    async def test_equivalent_selected_month_dates_use_one_canonical_cache_context(self):
        h = fixtures.Harness(); h.book = None
        h.now = datetime(2026, 10, 18, 12, 34, tzinfo=timezone.utc)
        with h.patches():
            first = await h.subject.async_get_analysis('month', 'user', anchor='2026-10-04')
            queries = len(h.queries)
            second = await h.subject.async_get_analysis('month', 'user', anchor='2026-10-07')
            self.assertEqual('2026-10-01', first['context']['period_anchor'])
            self.assertEqual(first, second)
            self.assertEqual(queries, len(h.queries))

    async def test_invalid_or_future_selection_never_reads_private_statistics(self):
        for anchor in ('2026-02-30', '20261017', [], True, '2026-10-19'):
            h = fixtures.Harness()
            h.now = datetime(2026, 10, 18, 12, 34, tzinfo=timezone.utc)
            with self.subTest(anchor=anchor), h.patches(), patch.object(
                    fixtures.service, 'async_get_energy_metadata', AsyncMock()) as metadata:
                with self.assertRaises(ValueError):
                    await h.subject.async_get_analysis('day', 'user', anchor=anchor)
                metadata.assert_not_awaited()
                self.assertEqual([], h.queries)
                self.assertEqual([], h.registry_ids)

    async def test_ws_forwards_anchor_without_accepting_client_identity_or_clock(self):
        ws, hass, service, conn, results, errors, calls = ws_fixtures.AnalysisWebsocketTest.fixture(self)
        received = []
        async def get_analysis(kind, user_id, *, anchor=None):
            received.append((kind, user_id, anchor))
            return {'schema': 2, 'snapshot_id': 'selected', 'totals': {'kwh': '3'}}
        service.async_get_analysis = get_analysis
        await ws._ws_energy_analysis(hass, conn, {'id': 1, 'period': 'month', 'anchor': '2024-02-29',
                                               'user_id': 'untrusted', 'now': '9999-01-01'})
        self.assertEqual([], errors)
        self.assertEqual([('month', 'synthetic', '2024-02-29')], received)
        self.assertEqual('selected', results[-1][1]['snapshot_id'])
