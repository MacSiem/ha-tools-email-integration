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

    async def test_missing_real_tail_is_unknown_and_attempted_once_until_new_cutoff(self):
        c, calls, metadata, _ = self.fixture(changes=())
        result = await self.refresh(c, metadata, '2026-10-18T19:07Z')
        self.assertIsNone(result['entity_facts']['import_kwh'])
        self.assertIsNone(result['entity_facts']['usage_cost'])
        self.assertFalse(result['entity_facts']['coverage_complete'])
        await self.refresh(c, metadata, '2026-10-18T19:09Z')
        self.assertEqual(1, len(calls))
        await self.refresh(c, metadata, '2026-10-18T19:12Z')
        self.assertEqual(at('2026-10-18T19:00Z'), calls[-1][0])
        self.assertEqual(2, len(calls))

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
