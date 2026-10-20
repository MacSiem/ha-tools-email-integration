"""Native producer reads actual five-minute cells, never a user report DTO."""
import asyncio
import copy
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from tests.test_energy_snapshot import energy, at, classified, usage

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
SPEC = importlib.util.spec_from_file_location(energy.__package__ + '.energy_native_short', ROOT/'energy_native_short.py')
short = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(short)
SID = 'sensor.native_grid'
META = {SID: ('id', {'unit_of_measurement': 'Wh', 'has_sum': True, 'unit_class': 'energy'})}


def raw_at(start, changes=(100,), *, kind='datetime'):
    first = at(start)
    rows = []
    for index, value in enumerate(changes):
        stamp = first + timedelta(minutes=5 * index)
        rows.append({'start': stamp if kind == 'datetime' else stamp.isoformat() if kind == 'iso' else stamp.timestamp(),
                     'change': value})
    return {SID: rows}


class NativeShortPureTests(unittest.TestCase):
    def test_typed_real_changes_and_units_use_existing_normalizer(self):
        for kind in ('datetime', 'iso', 'epoch'):
            for unit, value, expected in [('Wh', 100, '.1'), ('kWh', .1, '.1'), ('MWh', .0001, '.1')]:
                metadata = {SID: {'unit_of_measurement': unit, 'has_sum': True, 'unit_class': 'energy'}}
                rows = short.normalized_cells(raw_at('2026-10-18T19:00Z', (value, value), kind=kind), metadata,
                    [SID], at('2026-10-18T19:00Z'), at('2026-10-18T19:10Z'))
                self.assertEqual(2, len(rows))
                self.assertAlmostEqual(float(expected), float(rows[0]['kwh']))
                self.assertAlmostEqual(float(expected), float(rows[1]['kwh']))
                self.assertEqual('2026-10-18T19:05:00+00:00', rows[1]['from'])

    def test_missing_duplicate_sum_only_bad_unit_and_naive_starts_are_unknown(self):
        cases = []
        cases.append(({SID: []}, META))
        cases.append((raw_at('2026-10-18T19:00Z', (100,)), {SID: {'unit_of_measurement': 'W', 'has_sum': True}}))
        cases.append(({SID: [{'start': at('2026-10-18T19:00Z'), 'sum': 100}]}, META))
        cases.append(({SID: [{'start': datetime(2026,10,18,19), 'change': 100}]}, META))
        duplicate = raw_at('2026-10-18T19:00Z'); duplicate[SID].append(copy.deepcopy(duplicate[SID][0])); cases.append((duplicate, META))
        for raw, metadata in cases:
            rows = short.normalized_cells(raw, metadata, [SID], at('2026-10-18T19:00Z'), at('2026-10-18T19:05Z'))
            self.assertIsNone(rows[0]['kwh'])

    def test_incremental_merge_does_not_double_count_old_or_outside_cells(self):
        previous = raw_at('2026-10-18T19:00Z')
        fresh = raw_at('2026-10-18T19:05Z', (200, 999))
        merged = short.merge_raw(previous, fresh, [SID], at('2026-10-18T19:05Z'), at('2026-10-18T19:10Z'))
        self.assertEqual(2, len(merged[SID]))
        rows = short.normalized_cells(merged, META, [SID], at('2026-10-18T19:00Z'), at('2026-10-18T19:10Z'))
        self.assertAlmostEqual(.3, sum(float(row['kwh']) for row in rows))
        self.assertEqual(1, len(previous[SID]))


class NativeShortCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, changes=(100,), unit='Wh'):
        calls = []
        state = {'profile': classified(), 'revision': 1}
        class Storage:
            current_tariff_revision = 1
            async def async_get_tariff(self): return copy.deepcopy(state)
        metadata = {SID: {'unit_of_measurement': unit, 'has_sum': True, 'unit_class': 'energy'}}
        async def provider(hass, start, end):
            rows = usage(start.isoformat(), int((end - start).total_seconds() / 3600))
            return rows
        async def tail(hass, start, end, ids):
            calls.append((start, end, list(ids)))
            return raw_at(start.isoformat(), changes[:int((end - start).total_seconds()/300)])
        hass = types.SimpleNamespace(data={}, config=types.SimpleNamespace(time_zone='UTC'))
        c = energy.EnergySnapshotCoordinator(hass, Storage(), provider=provider, short_provider=tail)
        hass.data[energy.DOMAIN].update(energy_snapshot=c, storage=c.storage)
        return c, calls, metadata, state

    async def refresh(self, c, metadata, instant):
        with patch.object(energy, 'async_grid_sensors', return_value=[{'entity_id': SID, 'name': 'Grid'}]), \
             patch.object(energy, 'async_get_energy_metadata', return_value=metadata):
            return await c.async_refresh(now=at(instant))

    async def test_1907_exactly_1905_nonoverlap_and_one_incremental_read_per_cutoff(self):
        c, calls, metadata, state = self.fixture(changes=(100,))
        first = await self.refresh(c, metadata, '2026-10-18T19:07Z')
        facts = first['entity_facts']
        self.assertEqual('2026-10-18T19:05:00+00:00', facts['measurement_through'])
        self.assertAlmostEqual(19.1, facts['import_kwh'])
        self.assertEqual(1, len(c._short_measurement))
        self.assertEqual(1, len(calls))
        await self.refresh(c, metadata, '2026-10-18T19:09Z')
        self.assertEqual(1, len(calls))
        later = await self.refresh(c, metadata, '2026-10-18T19:12Z')
        self.assertEqual(at('2026-10-18T19:05Z'), calls[-1][0])
        self.assertEqual(at('2026-10-18T19:10Z'), calls[-1][1])
        self.assertAlmostEqual(19.2, later['entity_facts']['import_kwh'])
        self.assertEqual(2, len(c._short_measurement))
        state['revision'] = c.storage.current_tariff_revision = 2
        await self.refresh(c, metadata, '2026-10-18T19:13Z')
        self.assertEqual(2, len(calls))

    async def test_before_first_completed_cell_unavailable_then_real_zero(self):
        c, calls, metadata, _ = self.fixture(changes=(0,))
        first = await self.refresh(c, metadata, '2026-10-18T00:04Z')
        self.assertIsNone(first['entity_facts']['import_kwh'])
        self.assertEqual('no_completed_interval', first['entity_facts']['reason'])
        self.assertEqual([], calls)
        second = await self.refresh(c, metadata, '2026-10-18T00:07Z')
        self.assertEqual(0, second['entity_facts']['import_kwh'])
        self.assertTrue(second['entity_facts']['coverage_complete'])
        self.assertIsNone(second['entity_facts']['cheap_share_pct'])
        self.assertEqual('2026-10-18T00:05:00+00:00', second['entity_facts']['measurement_through'])

    async def test_missing_real_tail_is_unknown_and_same_cutoff_rechecks_are_throttled(self):
        c, calls, metadata, _ = self.fixture(changes=())
        result = await self.refresh(c, metadata, '2026-10-18T19:07Z')
        self.assertIsNone(result['entity_facts']['import_kwh'])
        self.assertIsNone(result['entity_facts']['usage_cost'])
        self.assertFalse(result['entity_facts']['coverage_complete'])
        await self.refresh(c, metadata, '2026-10-18T19:07:29Z')
        self.assertEqual(1, len(calls))
        same = await self.refresh(c, metadata, '2026-10-18T19:07:30Z')
        self.assertEqual(2, len(calls))
        self.assertIsNone(same['entity_facts']['import_kwh'])
        self.assertIsNone(same['entity_facts']['usage_cost'])
        await self.refresh(c, metadata, '2026-10-18T19:07:31Z')
        self.assertEqual(2, len(calls))
        await self.refresh(c, metadata, '2026-10-18T19:12Z')
        self.assertEqual(at('2026-10-18T19:00Z'), calls[-1][0])
        self.assertEqual(3, len(calls))

    async def test_source_metadata_changes_during_tail_io_cannot_publish(self):
        c, _, metadata, _ = self.fixture()
        async def changing(hass, start, end, ids):
            metadata[SID]['unit_of_measurement'] = 'MWh'
            return raw_at(start.isoformat())
        c._short_provider = changing
        with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_changed'):
            await self.refresh(c, metadata, '2026-10-18T19:07Z')
        self.assertIsNone(c.data)
        self.assertIsNone(c._short_state)

    async def test_unload_during_real_tail_io_fails_closed(self):
        c, _, metadata, _ = self.fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        async def pending(hass, start, end, ids):
            entered.set(); await release.wait()
            return raw_at(start.isoformat())
        c._short_provider = pending
        task = asyncio.create_task(self.refresh(c, metadata, '2026-10-18T19:07Z'))
        await entered.wait(); c.async_stop(); release.set()
        with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_unloaded'): await task
        self.assertIsNone(c.data)
        self.assertIsNone(c._short_state)
        self.assertEqual([], c._short_measurement)

    async def test_fall_dst_uses_absolute_tail_boundaries_and_local_day(self):
        c, calls, metadata, _ = self.fixture()
        c.hass.config.time_zone = 'Europe/Warsaw'
        result = await self.refresh(c, metadata, '2026-10-25T02:07+01:00')
        self.assertEqual(at('2026-10-25T01:00Z'), calls[-1][0])
        self.assertEqual(at('2026-10-25T01:05Z'), calls[-1][1])
        self.assertEqual('2026-10-24T22:00:00+00:00', result['entity_facts']['measurement_from'])
        self.assertAlmostEqual(3.1, result['entity_facts']['import_kwh'])

    async def test_stale_five_minute_context_is_rejected_even_with_same_hour(self):
        c, _, metadata, _ = self.fixture()
        result = await self.refresh(c, metadata, '2026-10-18T19:07Z')
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return at('2026-10-18T19:10Z')
        with patch.object(energy, 'datetime', Clock):
            self.assertFalse(c.tariff_context_is_current(result))

    async def test_real_default_recorder_adapter_requests_five_minute_change_only(self):
        calls = []
        expected = raw_at('2026-10-18T19:00Z')
        def statistics(*args):
            calls.append(args)
            return expected
        class Recorder:
            async def async_add_executor_job(self, function, *args): return function(*args)
        statistics_module = types.ModuleType('homeassistant.components.recorder.statistics')
        statistics_module.statistics_during_period = statistics
        util_module = types.ModuleType('homeassistant.components.recorder.util')
        recorder_module = types.ModuleType('homeassistant.components.recorder')
        recorder_module.get_instance = lambda hass: Recorder()
        hass = object()
        with patch.dict(sys.modules, {statistics_module.__name__: statistics_module, util_module.__name__: util_module, recorder_module.__name__: recorder_module}):
            result = await short.async_read_short_statistics(hass, at('2026-10-18T19:00Z'), at('2026-10-18T19:05Z'), [SID])
        self.assertIs(expected, result)
        self.assertEqual((hass, at('2026-10-18T19:00Z'), at('2026-10-18T19:05Z'), {SID}, '5minute', {}, {'change'}), calls[0])

    async def test_final_tariff_revision_race_and_source_id_race_reject_results(self):
        c, _, metadata, state = self.fixture()
        original = c._async_build_snapshot
        async def overtaken(*args, **kwargs):
            result = await original(*args, **kwargs)
            state['revision'] = c.storage.current_tariff_revision = 2
            return result
        with patch.object(c, '_async_build_snapshot', side_effect=overtaken):
            with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_changed'):
                await self.refresh(c, metadata, '2026-10-18T19:07Z')
        self.assertIsNone(c.data)
        self.assertIsNone(c._short_state)
        c, _, metadata, _ = self.fixture()
        sources = [{'entity_id': SID, 'name': 'Grid'}]
        async def changed(hass, start, end, ids):
            sources[:] = [{'entity_id': 'sensor.replacement', 'name': 'Replacement'}]
            return raw_at(start.isoformat())
        c._short_provider = changed
        with patch.object(energy, 'async_grid_sensors', side_effect=lambda hass: list(sources)), \
             patch.object(energy, 'async_get_energy_metadata', return_value=metadata):
            with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_changed'):
                await c.async_refresh(now=at('2026-10-18T19:07Z'))
        self.assertIsNone(c.data)
        self.assertIsNone(c._short_state)

    async def test_native_cost_and_share_include_actual_tail_energy(self):
        c, _, metadata, _ = self.fixture(changes=(200,))
        result = await self.refresh(c, metadata, '2026-10-18T00:07Z')
        facts = result['entity_facts']
        self.assertAlmostEqual(.2, facts['import_kwh'])
        self.assertAlmostEqual(.2, facts['cheap_kwh'])
        self.assertEqual(100, facts['cheap_share_pct'])
        self.assertAlmostEqual(.08, facts['usage_cost'])
        self.assertTrue(facts['coverage_complete'])

    async def test_five_minute_rollover_during_realtime_refresh_rejects_without_publication(self):
        c, _, metadata, _ = self.fixture()
        clock = [at('2026-10-18T19:04Z')]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return clock[0]
        original = c._async_native_short
        async def rollover(*args, **kwargs):
            await original(*args, **kwargs)
            clock[0] = at('2026-10-18T19:05Z')
        with patch.object(energy, 'datetime', Clock), \
             patch.object(c, '_async_native_short', side_effect=rollover), \
             patch.object(energy, 'async_grid_sensors', return_value=[{'entity_id': SID, 'name': 'Grid'}]), \
             patch.object(energy, 'async_get_energy_metadata', return_value=metadata):
            with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_changed'):
                await c.async_refresh()
        self.assertIsNone(c.data)
        self.assertIsNone(c._short_state)

    async def test_hour_rollover_drops_tail_without_adding_it_to_full_hour_twice(self):
        c, calls, metadata, _ = self.fixture()
        await self.refresh(c, metadata, '2026-10-18T19:07Z')
        result = await self.refresh(c, metadata, '2026-10-18T20:00Z')
        self.assertEqual(20, result['entity_facts']['import_kwh'])
        self.assertEqual([], c._short_measurement)
        self.assertIsNone(c._short_state)
        self.assertEqual(1, len(calls))

    async def test_coordinator_replacement_during_tail_read_cannot_publish(self):
        c, _, metadata, _ = self.fixture()
        async def replaced(hass, start, end, ids):
            hass.data[energy.DOMAIN]['energy_snapshot'] = object()
            return raw_at(start.isoformat())
        c._short_provider = replaced
        with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_unloaded'):
            await self.refresh(c, metadata, '2026-10-18T19:07Z')
        self.assertIsNone(c.data)
        self.assertFalse(c.tariff_context_is_current({'profile_source': 'shared', 'tariff_revision': 1}))

    async def test_real_tick_recovers_delayed_first_midnight_commit_and_updates_three_native_entities(self):
        from tests.test_energy_snapshot import entity_modules
        sensor, _ = entity_modules()
        c, _, metadata, state = self.fixture(unit='kWh')
        c.hass.config.time_zone = 'Europe/Warsaw'
        state['profile']['zones'][1].update(energyRate=.5, variableRate=.1)
        clock = [at('2026-10-20T22:05:00Z')]
        calls, published = [], []
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return clock[0]
        async def delayed(hass, start, end, ids):
            calls.append((start, end))
            return {SID: []} if len(calls) == 1 else raw_at(start.isoformat(), (5,))
        c._short_provider = delayed
        entities = [sensor.EnergySensor(c, types.SimpleNamespace(entry_id='stable'), *args) for args in (
            ('today', 'entity_facts', 'import_kwh'), ('today_cost', 'today', 'usage_cost'),
            ('cheap_share_today', 'entity_facts', 'cheap_share_pct'))]
        for entity in entities:
            entity.async_write_ha_state = lambda e=entity: published.append((e.key, e.available, e.native_value))
            await entity.async_added_to_hass()
        with patch.object(energy, 'datetime', Clock), \
             patch.object(energy, 'async_grid_sensors', return_value=[{'entity_id': SID, 'name': 'Grid'}]), \
             patch.object(energy, 'async_get_energy_metadata', return_value=metadata):
            await c._async_tick(None)  # The natural boundary outruns the Recorder commit.
            self.assertEqual(1, len(calls))
            self.assertFalse(any(e.available for e in entities))
            clock[0] = at('2026-10-20T22:05:29Z')
            await c._async_tick(None)
            self.assertEqual(1, len(calls))
            clock[0] = at('2026-10-20T22:05:30Z')
            await c._async_tick(None)  # Existing cadence, same cutoff, now committed.
            self.assertEqual(2, len(calls))
            self.assertEqual([5, 3, 100], [e.native_value for e in entities])
            self.assertTrue(all(e.available for e in entities))
            self.assertCountEqual([('today', True, 5), ('today_cost', True, 3),
                              ('cheap_share_today', True, 100)], published[-3:])
            self.assertEqual('2026-10-20T22:05:00+00:00', c.data['entity_facts']['measurement_through'])
            clock[0] = at('2026-10-20T22:07:00Z')
            await c._async_tick(None)
            self.assertEqual(2, len(calls), 'accepted same-cutoff cells stay cached')
        for entity in entities:
            entity.remove()
        self.assertFalse(c._listeners)

    async def test_next_boundary_miss_retries_only_suffix_and_keeps_accepted_prefix_without_double_count(self):
        c, _, metadata, _ = self.fixture(unit='kWh')
        calls = []
        async def delayed(hass, start, end, ids):
            calls.append((start, end))
            return {SID: []} if len(calls) == 2 else raw_at(start.isoformat(), (5,))
        c._short_provider = delayed
        first = await self.refresh(c, metadata, '2026-10-18T00:05:30Z')
        self.assertEqual(5, first['entity_facts']['import_kwh'])
        missing = await self.refresh(c, metadata, '2026-10-18T00:10:00Z')
        self.assertIsNone(missing['entity_facts']['import_kwh'])
        self.assertEqual(at('2026-10-18T00:05Z'), c._short_state['through'])
        self.assertEqual(1, len(c._short_state['raw'][SID]))
        await self.refresh(c, metadata, '2026-10-18T00:10:29Z')
        self.assertEqual(2, len(calls))
        complete = await self.refresh(c, metadata, '2026-10-18T00:10:30Z')
        self.assertEqual(10, complete['entity_facts']['import_kwh'])
        self.assertEqual(2, len(c._short_state['raw'][SID]))
        self.assertEqual([(at('2026-10-18T00:00Z'), at('2026-10-18T00:05Z')),
            (at('2026-10-18T00:05Z'), at('2026-10-18T00:10Z')),
            (at('2026-10-18T00:05Z'), at('2026-10-18T00:10Z'))], calls)
        await self.refresh(c, metadata, '2026-10-18T00:14:59Z')
        self.assertEqual(3, len(calls))

    async def test_pending_retry_is_singleflight_and_waiting_refresh_reuses_new_complete_prefix(self):
        c, _, metadata, _ = self.fixture(unit='kWh')
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def delayed(hass, start, end, ids):
            calls.append((start, end))
            if len(calls) == 1:
                return {SID: []}
            entered.set()
            await release.wait()
            return raw_at(start.isoformat(), (5,))
        c._short_provider = delayed
        await self.refresh(c, metadata, '2026-10-18T00:05Z')
        with patch.object(energy, 'async_grid_sensors', return_value=[{'entity_id': SID, 'name': 'Grid'}]), \
             patch.object(energy, 'async_get_energy_metadata', return_value=metadata):
            first = asyncio.create_task(c.async_refresh(now=at('2026-10-18T00:05:30Z')))
            await asyncio.wait_for(entered.wait(), timeout=1)
            second = asyncio.create_task(c.async_refresh(now=at('2026-10-18T00:05:31Z')))
            release.set()
            a, b = await asyncio.gather(first, second)
        self.assertEqual(2, len(calls))
        self.assertEqual(5, a['entity_facts']['import_kwh'])
        self.assertEqual(5, b['entity_facts']['import_kwh'])

    async def test_duplicate_tail_is_still_unknown_and_only_clean_replacement_can_recover(self):
        c, _, metadata, _ = self.fixture(unit='kWh')
        calls = []
        async def corrected(hass, start, end, ids):
            calls.append((start, end))
            raw = raw_at(start.isoformat(), (5,))
            if len(calls) == 1:
                raw[SID].append(copy.deepcopy(raw[SID][0]))
            return raw
        c._short_provider = corrected
        first = await self.refresh(c, metadata, '2026-10-18T00:05Z')
        self.assertIsNone(first['entity_facts']['import_kwh'])
        self.assertEqual({}, c._short_state['raw'])
        final = await self.refresh(c, metadata, '2026-10-18T00:05:30Z')
        self.assertEqual(5, final['entity_facts']['import_kwh'])
        self.assertTrue(final['entity_facts']['coverage_complete'])
        self.assertEqual(2, len(calls))
