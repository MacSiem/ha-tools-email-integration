"""Native Energy facts use the real service and its authorization/cache fences."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_energy_analysis_service import Harness, service


class NativeHarness(Harness):
    def __init__(self):
        super().__init__()
        self.book = None
        self.hass.config.currency = 'EUR'
        self.manager.data['energy_sources'][0]['stat_cost'] = 'sensor.grid_cost'
        self.metadata['sensor.grid_cost'] = {'has_sum': True, 'unit_of_measurement': 'EUR', 'unit_class': None}
        self.native_queries = []
        self.on_native = None
        self.state_reads = []
        self.current_state = SimpleNamespace(state='0.25', attributes={'unit_of_measurement': 'EUR/kWh'}, last_updated=self.now)
        def state_get(sid):
            self.state_reads.append(sid)
            return self.current_state
        self.hass.states = SimpleNamespace(get=state_get)

    @contextmanager
    def native_patches(self):
        async def read(hass, ids, start, end, *, guard):
            await guard()
            self.native_queries.append(tuple(ids))
            if self.on_native:
                self.on_native()
            rows = {sid: [{'start': start+timedelta(hours=i), 'change': 2.4375}
                          for i in range(int((end-start).total_seconds()/3600))] for sid in ids}
            await guard()
            return rows
        with self.patches(), patch.object(service, 'async_read_native_cost_chunk', read, create=True):
            yield


class NativePricingServiceTest(unittest.TestCase):
    def test_recorded_cost_preserves_null_book_fields_and_authoritative_hash(self):
        async def run():
            h = NativeHarness()
            with h.native_patches():
                result = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual('4.875', result['native_pricing']['period_cost']['amount'])
                self.assertIsNone(result['totals']['total_cost'])
                self.assertIsNone(result['totals']['usage_cost'])
                self.assertEqual('unknown', result['native_pricing']['period_cost']['fixed_fees_status'])
                unhashed = deepcopy(result); unhashed.pop('snapshot_id')
                self.assertEqual(service._digest(unhashed), result['snapshot_id'])
                before = len(h.native_queries)
                self.assertEqual(result, await h.subject.async_get_analysis('day', 'user'))
                self.assertEqual(before, len(h.native_queries))
                self.assertTrue(await h.subject.async_validate_result(result, 'user'))
        asyncio.run(run())

    def test_denied_native_cost_retains_energy_and_performs_no_native_io(self):
        async def run():
            h = NativeHarness()
            h.permissions.access_all_entities = lambda _: False
            h.permissions.check_entity = lambda sid, _: sid in ('sensor.grid','sensor.device')
            with h.native_patches():
                result = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual(Decimal(20), Decimal(result['totals']['kwh']))
                self.assertNotIn('native_pricing', result)
                self.assertNotIn('sensor.grid_cost', str(result))
                self.assertEqual([], h.native_queries)
                self.assertEqual([], h.state_reads)
        asyncio.run(run())

    def test_revoke_during_native_io_never_returns_or_caches_cost(self):
        async def run():
            h = NativeHarness()
            def revoke():
                h.permissions.access_all_entities = lambda _: False
                h.permissions.check_entity = lambda sid, _: sid in ('sensor.grid','sensor.device')
            h.on_native = revoke
            with h.native_patches():
                with self.assertRaises(RuntimeError):
                    await h.subject.async_get_analysis('day', 'user')
                self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_binding_currency_metadata_or_unload_change_during_native_io(self):
        async def run():
            for mode in ('binding','currency','unit','unload'):
                h = NativeHarness()
                def mutate():
                    if mode == 'binding': h.manager.data['energy_sources'][0]['stat_cost'] = 'sensor.other_cost'
                    elif mode == 'currency': h.hass.config.currency = 'USD'
                    elif mode == 'unit': h.metadata['sensor.grid_cost']['unit_of_measurement'] = 'USD'
                    else: h.subject.async_stop()
                h.on_native = mutate
                with self.subTest(mode=mode), h.native_patches():
                    with self.assertRaises(RuntimeError):
                        await h.subject.async_get_analysis('day', 'user')
                    self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_current_price_change_invalidates_cached_fact_without_repricing_history(self):
        async def run():
            h = NativeHarness()
            h.manager.data['energy_sources'][0]['entity_energy_price'] = 'sensor.price'
            with h.native_patches():
                first = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual('0.25', first['native_pricing']['current_prices'][0]['price'])
                h.current_state.state = '-0.5'
                second = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual('-0.5', second['native_pricing']['current_prices'][0]['price'])
                self.assertEqual(first['native_pricing']['period_cost'], second['native_pricing']['period_cost'])
                self.assertNotEqual(first['snapshot_id'], second['snapshot_id'])
                self.assertFalse(await h.subject.async_validate_result(first, 'user'))
        asyncio.run(run())

    def test_book_priority_skips_native_recorder_and_current_state_io(self):
        async def run():
            h = NativeHarness(); h.book = Harness().book
            h.manager.data['energy_sources'][0]['entity_energy_price'] = 'sensor.price'
            with h.native_patches():
                result = await h.subject.async_get_analysis('day', 'user')
                self.assertNotIn('native_pricing', result)
                self.assertEqual([], h.native_queries)
                self.assertEqual([], h.state_reads)
        asyncio.run(run())

    def test_explicit_core_cost_mapping_without_guessing_sensor_suffix(self):
        async def run():
            h = NativeHarness(); row = h.manager.data['energy_sources'][0]
            row.pop('stat_cost'); row['number_energy_price'] = 9
            h.hass.data['energy'] = {'cost_sensors': {'sensor.grid': 'sensor.grid_cost'}}
            with h.native_patches():
                result = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual('4.875', result['native_pricing']['period_cost']['amount'])
                self.assertEqual('9', result['native_pricing']['current_prices'][0]['price'])
        asyncio.run(run())
