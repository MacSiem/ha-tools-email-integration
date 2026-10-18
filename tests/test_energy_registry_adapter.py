"""Exercise registry lookup/event boundaries; no full-registry export."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'


def load_adapter():
    package = types.ModuleType('registry_adapter_test_package'); package.__path__ = [str(ROOT)]
    core = types.ModuleType('homeassistant.core'); core.callback = lambda fn: fn
    with patch.dict(sys.modules, {package.__name__: package, core.__name__: core}):
        spec = importlib.util.spec_from_file_location(package.__name__ + '.registry_adapter', ROOT / 'registry_adapter.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class RegistryAdapterTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self, floor=True):
        module = load_adapter()
        listeners, changes = {}, []
        class Bus:
            def async_listen(self, event, callback):
                listeners.setdefault(event, []).append(callback)
                return lambda: listeners[event].remove(callback)
        entities = {'sensor.a': types.SimpleNamespace(device_id='dev', area_id=None, name='Lamp', original_name=None),
                    'sensor.foreign': types.SimpleNamespace(device_id='other', area_id='foreign')}
        devices = {'dev': types.SimpleNamespace(area_id='room')}
        areas = {'room': types.SimpleNamespace(name='Room', floor_id='up'),
                 'office': types.SimpleNamespace(name='Office', floor_id=None)}
        floors = {'up': types.SimpleNamespace(name='Up')}
        er = types.SimpleNamespace(async_get=lambda hass: types.SimpleNamespace(async_get=entities.get),
                                   EVENT_ENTITY_REGISTRY_UPDATED='entity_registry_updated')
        dr = types.SimpleNamespace(async_get=lambda hass: types.SimpleNamespace(async_get=devices.get),
                                   EVENT_DEVICE_REGISTRY_UPDATED='device_registry_updated')
        ar = types.SimpleNamespace(async_get=lambda hass: types.SimpleNamespace(async_get_area=areas.get),
                                   EVENT_AREA_REGISTRY_UPDATED='area_registry_updated')
        fr = types.SimpleNamespace(async_get=lambda hass: types.SimpleNamespace(async_get_floor=floors.get),
                                   EVENT_FLOOR_REGISTRY_UPDATED='floor_registry_updated') if floor else None
        hass = types.SimpleNamespace(data={}, bus=Bus())
        adapter = module.RegistryMembership(hass, changes.append, registry_modules=(er, dr, ar, fr))
        adapter.start()
        return adapter, hass, listeners, changes, entities, areas, floors

    async def test_capture_requested_sources_only_and_override_move_without_reload(self):
        adapter, hass, listeners, changes, entities, areas, floors = self.fixture()
        old = adapter.capture(['sensor.a', 'external:meter'])
        self.assertEqual({'sensor.a', 'external:meter'}, set(old['sources']))
        self.assertEqual('room', old['sources']['sensor.a']['area_id'])
        entities['sensor.a'].area_id = 'office'
        listeners['entity_registry_updated'][0](types.SimpleNamespace(data={'entity_id': 'sensor.a'}))
        current = adapter.capture(['sensor.a'])
        self.assertEqual('office', current['sources']['sensor.a']['area_id'])
        self.assertEqual('room', old['sources']['sensor.a']['area_id'])
        self.assertGreater(current['revision'], old['revision'])
        self.assertEqual([], changes)
        await asyncio.sleep(0)
        self.assertEqual([current['revision']], changes)
        adapter.stop()

    async def test_rename_and_floor_delete_affect_next_capture_and_coalesce(self):
        adapter, hass, listeners, changes, entities, areas, floors = self.fixture()
        old = adapter.capture(['sensor.a'])
        areas['room'].name = 'Renamed'; floors.clear(); areas['room'].floor_id = None
        for event in ('area_registry_updated', 'floor_registry_updated', 'device_registry_updated'):
            listeners[event][0](types.SimpleNamespace(data={}))
        result = adapter.capture(['sensor.a'])
        self.assertEqual('Renamed', result['sources']['sensor.a']['area_name'])
        self.assertIsNone(result['sources']['sensor.a']['floor_id'])
        self.assertEqual('Up', old['sources']['sensor.a']['floor_name'])
        await asyncio.sleep(0)
        self.assertEqual([result['revision']], changes)
        adapter.stop()

    async def test_floor_capability_fallback_keeps_rooms(self):
        adapter, hass, listeners, changes, entities, areas, floors = self.fixture(floor=False)
        result = adapter.capture(['sensor.a'])
        self.assertFalse(result['floor_supported'])
        self.assertEqual('room', result['sources']['sensor.a']['area_id'])
        self.assertNotIn('floor_registry_updated', listeners)
        adapter.stop()

    async def test_stop_unsubscribes_and_discards_pending_notification(self):
        adapter, hass, listeners, changes, entities, areas, floors = self.fixture()
        old_callbacks = [fn for values in listeners.values() for fn in values]
        old_callbacks[0](types.SimpleNamespace(data={}))
        adapter.stop(); adapter.stop()
        for fn in old_callbacks:
            fn(types.SimpleNamespace(data={}))
        await asyncio.sleep(0)
        self.assertEqual([], changes)
        self.assertTrue(all(not values for values in listeners.values()))
        with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_unloaded'):
            adapter.capture(['sensor.a'])
        prior = adapter.revision
        adapter.start()
        self.assertGreater(adapter.revision, prior)
        adapter.start()
        self.assertEqual(4, sum(len(values) for values in listeners.values()))
        adapter.stop()
