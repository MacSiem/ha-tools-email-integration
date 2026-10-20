"""Only owned server ticks emit control refreshes; request APIs remain silent."""
import asyncio
import copy
import sys
import types
import unittest
from unittest.mock import patch

from tests.test_energy_snapshot import energy, at, classified, usage


class EnergyUpdateEventTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        events = []
        state = {'profile': classified(), 'revision': 1}
        class Storage:
            current_tariff_revision = 1
            async def async_get_tariff(self):
                return copy.deepcopy(state)
        storage = Storage()
        hass = types.SimpleNamespace(data={}, config=types.SimpleNamespace(time_zone='UTC'),
            bus=types.SimpleNamespace(async_fire=lambda *args: events.append(args)))
        async def provider(*args):
            return usage()
        coordinator = energy.EnergySnapshotCoordinator(hass, storage, provider=provider)
        hass.data[energy.DOMAIN].update(energy_snapshot=coordinator, storage=storage)
        return coordinator, events

    def result(self, instant='2026-10-18T12:00:00Z', **changes):
        result = {'profile_source': 'shared', 'generated_at': instant, 'tariff_revision': 1,
            'currency': 'PLN', 'price_basis': 'gross', 'measurement_timezone': 'UTC',
            'tariff_timezone': 'UTC', 'current': {'available': True, 'zone_id': 'peak',
                'zone_class': 'expensive', 'energy_rate': .6, 'variable_rate': .3, 'usage_rate': .9}}
        result.update(changes)
        return result

    async def tick(self, coordinator, result=None, *, failure=None):
        async def refresh():
            if failure is not None:
                raise failure
            coordinator.data = copy.deepcopy(result)
            return copy.deepcopy(result)
        with patch.object(coordinator, 'async_refresh', side_effect=refresh), \
             patch.object(coordinator, 'tariff_context_is_current', return_value=True):
            await coordinator._async_tick(None)

    async def test_bucket_once_real_tariff_changes_immediate_and_payload_is_empty(self):
        c, events = self.fixture()
        await self.tick(c, self.result())
        await self.tick(c, self.result('2026-10-18T12:00:30Z'))
        await self.tick(c, self.result('2026-10-18T12:04:59Z'))
        self.assertEqual(1, len(events))
        await self.tick(c, self.result('2026-10-18T12:05:00Z'))
        self.assertEqual(2, len(events))
        changed = self.result('2026-10-18T12:05:30Z')
        changed['current'].update(zone_id='offpeak', usage_rate=.5)
        await self.tick(c, changed)
        self.assertEqual(3, len(events))
        changed.update(currency='EUR')
        await self.tick(c, changed)
        self.assertEqual(4, len(events))
        changed.update(tariff_revision=2)
        await self.tick(c, changed)
        self.assertEqual(5, len(events))
        changed['today'] = {'PRIVATE_SOURCE': 'must_not_leak'}
        await self.tick(c, changed)
        self.assertEqual(5, len(events))
        self.assertEqual([(energy.DOMAIN + '_energy_updated', {})] * 5, events)

    async def test_one_purge_across_failures_then_restore_even_in_same_bucket(self):
        c, events = self.fixture()
        await self.tick(c, self.result())
        await self.tick(c, failure=RuntimeError('PRIVATE_SOURCE'))
        self.assertIsNone(c.data)
        self.assertTrue(c._energy_update_purged)
        for _ in range(3):
            await self.tick(c, failure=RuntimeError('PRIVATE_POLICY'))
        self.assertEqual(2, len(events))
        await self.tick(c, self.result())
        self.assertEqual(3, len(events))
        self.assertFalse(c._energy_update_invalidated)
        self.assertFalse(c._energy_update_purged)
        await self.tick(c, self.result())
        self.assertEqual(3, len(events))
        self.assertEqual([(energy.DOMAIN + '_energy_updated', {})] * 3, events)

    async def test_initial_failed_start_can_purge_once_without_any_cached_snapshot(self):
        c, events = self.fixture()
        for _ in range(3):
            await self.tick(c, failure=RuntimeError('source_unavailable'))
        self.assertEqual([(energy.DOMAIN + '_energy_updated', {})], events)

    async def test_lower_api_invalidation_is_pending_and_success_coalesces_restore(self):
        c, events = self.fixture()
        await self.tick(c, self.result())
        c._invalidate()
        c._invalidate()
        self.assertEqual(1, len(events))
        self.assertTrue(c._energy_update_invalidated)
        await self.tick(c, self.result())
        self.assertEqual(2, len(events))
        self.assertFalse(c._energy_update_invalidated)
        self.assertFalse(c._energy_update_purged)

    async def test_ownership_storage_replacement_and_stopped_block_emission(self):
        for replace in ('coordinator', 'storage', 'stop'):
            c, events = self.fixture()
            if replace == 'coordinator':
                c.hass.data[energy.DOMAIN]['energy_snapshot'] = object()
            elif replace == 'storage':
                c.hass.data[energy.DOMAIN]['storage'] = object()
            else:
                c.async_stop()
            await self.tick(c, self.result())
            self.assertEqual([], events)
            self.assertIsNone(c._energy_update_key)

    async def test_final_context_guard_rejects_accepted_but_overtaken_result(self):
        c, events = self.fixture()
        async def refresh():
            c.data = self.result()
            return copy.deepcopy(c.data)
        with patch.object(c, 'async_refresh', side_effect=refresh), \
             patch.object(c, 'tariff_context_is_current', return_value=False):
            await c._async_tick(None)
        self.assertIsNone(c.data)
        self.assertEqual([(energy.DOMAIN + '_energy_updated', {})], events)
        self.assertTrue(c._energy_update_purged)

    async def test_request_refresh_compare_override_and_manual_price_query_are_silent(self):
        c, events = self.fixture()
        with patch.object(energy, 'async_grid_sensors', return_value=[]):
            await c.async_refresh(now=at('2026-10-18T12:34Z'))
            await c.async_snapshot(profile=classified(), now=at('2026-10-18T12:34Z'))
            await c.async_price_queries([{'buckets': [{'from': '2026-10-18T12:00Z', 'to': '2026-10-18T13:00Z', 'kwh': 1}]}])
        self.assertEqual([], events)
        self.assertIsNone(c._energy_update_key)

    async def test_no_second_task_when_tick_is_already_running(self):
        c, events = self.fixture()
        c._tick_running = True
        with patch.object(c, 'async_refresh') as refresh:
            await c._async_tick(None)
        refresh.assert_not_called()
        self.assertEqual([], events)

    async def test_stop_during_initial_refresh_leaves_no_timer_or_pending_event(self):
        c, events = self.fixture()
        entered, release, scheduled = asyncio.Event(), asyncio.Event(), []
        async def refresh():
            entered.set()
            await release.wait()
            c._check_running()
        module = types.ModuleType('homeassistant.helpers.event')
        module.async_track_time_interval = lambda *args: scheduled.append('interval')
        with patch.dict(sys.modules, {module.__name__: module}), \
             patch.object(c, 'async_refresh', side_effect=refresh):
            task = asyncio.create_task(c.async_start())
            await entered.wait()
            c.async_stop()
            release.set()
            await task
        self.assertEqual([], scheduled)
        self.assertEqual([], events)
        self.assertIsNone(c._unsub)
        self.assertFalse(c._tick_running)
        self.assertFalse(c._energy_update_invalidated)
        self.assertFalse(c._energy_update_purged)

    async def test_stop_removes_existing_callbacks_and_prevents_late_publication(self):
        c, events = self.fixture()
        removed = []
        c._unsub = lambda: removed.append('interval')
        c._boundary_unsub = lambda: removed.append('boundary')
        c._energy_update_invalidated = True
        c.async_stop()
        c.async_stop()
        await c._async_tick(None)
        self.assertEqual(['interval', 'boundary'], removed)
        self.assertEqual([], events)
        self.assertFalse(c._listeners)
        self.assertIsNone(c._energy_update_key)
