"""Execute actual setup/unload functions with bounded HA service doubles.

Reuse the existing AST loading approach to exercise lifecycle without importing
SMTP/HA platform services. The executed functions are unmodified product code.
"""
import ast
import asyncio
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'


class AnalysisLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, fail=None, unload=True):
        created = {'coordinator': [], 'registry': [], 'service': [], 'association': [], 'native': [], 'statistics_runtime': [],
                   'platforms': {name: False for name in ('sensor', 'binary_sensor', 'button', 'event', 'calendar')}, 'unload_calls': []}
        events = []
        class Coordinator:
            def __init__(self, hass, storage):
                self.storage, self.running = storage, True
                created['coordinator'].append(self)
            async def async_start(self):
                if fail == 'cancel':
                    raise asyncio.CancelledError
                if fail == 'start':
                    raise RuntimeError('synthetic start failure')
            def async_stop(self):
                self.running = False
        class Registry:
            def __init__(self, hass, on_change):
                self.on_change, self.active = on_change, False
                created['registry'].append(self)
            def start(self):
                self.active = True
            def stop(self):
                self.active = False
        class Service:
            def __init__(self, hass, entry_id, coordinator, registry):
                self.registry, self.stopped = registry, False
                created['service'].append(self)
            def async_stop(self):
                self.stopped = True
        class Association:
            def __init__(self, hass, entry):
                self.running, self.stop_calls, self.unsubscribe_calls = False, 0, 0
                self._unsubscribers = []
                created['association'].append(self)
            async def async_start(self):
                self.running = True
                def unsubscribe():
                    self.unsubscribe_calls += 1
                self._unsubscribers = [unsubscribe, unsubscribe, unsubscribe]
                if fail == 'association_start':
                    raise RuntimeError('synthetic association start failure')
                if fail == 'association_cancel':
                    raise asyncio.CancelledError
            def async_stop(self):
                self.running = False
                self.stop_calls += 1
                for unsubscribe in self._unsubscribers:
                    unsubscribe()
                self._unsubscribers.clear()
        class Native:
            def __init__(self, hass, entry, storage):
                self.running, self._unsubscribers = False, []
                created['native'].append(self)
            async def async_start(self):
                self.running = True
                self._unsubscribers = [object(), object()]
                if fail == 'native_start':
                    raise RuntimeError('synthetic native metadata start failure')
                if fail == 'native_cancel':
                    raise asyncio.CancelledError
            def async_stop(self):
                self.running = False
                self._unsubscribers.clear()
        class StatisticsRuntime:
            def __init__(self, hass, entry_id):
                self.running, self.stopped = False, False
                created['statistics_runtime'].append(self)
            def async_start(self):
                self.running = True
            def async_stop(self):
                self.running, self.stopped = False, True
        async def ensure(hass):
            pass
        async def reload_schedules(hass):
            events.append(('scheduler_reloaded', {}))
        storage = object()
        async def get_storage(hass):
            return storage
        async def forward(entry, platforms):
            if fail == 'platforms':
                raise RuntimeError('synthetic platform failure')
            for platform in platforms:
                created['platforms'][platform] = True
        async def unload_platforms(entry, platforms):
            created['unload_calls'].append(tuple(platforms))
            if unload:
                for platform in platforms:
                    created['platforms'][platform] = False
            return unload
        hass = types.SimpleNamespace(data={'ha_tools_email': {}},
            bus=types.SimpleNamespace(async_fire=lambda event, data: events.append((event, data))),
            config_entries=types.SimpleNamespace(async_forward_entry_setups=forward,
                                                 async_unload_platforms=unload_platforms))
        namespace = {'__package__': 'lifecycle_fixture', 'HomeAssistant': object, 'ConfigEntry': object,
                     'DOMAIN': 'ha_tools_email', '_async_ensure_setup': ensure,
                     '_async_get_storage': get_storage, 'async_stop_scheduler': lambda hass: None,
                     'async_reload_schedules': reload_schedules}
        tree = ast.parse((ROOT / '__init__.py').read_text())
        functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name in ('async_setup_entry', 'async_unload_entry')]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / '__init__.py'), 'exec'), namespace)
        mods = {}
        for filename, name, cls in [('energy', 'EnergySnapshotCoordinator', Coordinator),
                                    ('registry_adapter', 'RegistryMembership', Registry),
                                    ('analysis_service', 'EnergyAnalysisService', Service),
                                    ('energy_source_device', 'EnergySourceDeviceAssociation', Association),
                                    ('email_native', 'EmailNativeCoordinator', Native),
                                    ('energy_statistics_runtime', 'EnergyStatisticsRuntime', StatisticsRuntime)]:
            mod = types.ModuleType('lifecycle_fixture.' + filename); setattr(mod, name, cls); mods[mod.__name__] = mod
        return namespace, hass, types.SimpleNamespace(entry_id='synthetic'), created, events, mods

    async def test_setup_registry_event_and_unload_have_complete_resource_lifecycle(self):
        ns, hass, entry, made, events, mods = self.fixture()
        with patch.dict(sys.modules, mods):
            self.assertTrue(await ns['async_setup_entry'](hass, entry))
            bucket = hass.data['ha_tools_email']
            service = bucket['energy_analysis']; registry = bucket['energy_membership']
            self.assertIs(service.registry, registry)
            self.assertTrue(registry.active)
            association = bucket['energy_source_device']
            self.assertTrue(association.running)
            self.assertEqual(3, len(association._unsubscribers))
            self.assertTrue(all(made['platforms'].values()))
            self.assertEqual([('scheduler_reloaded', {})], events)
            events.clear()
            registry.on_change(7)
            self.assertEqual([('ha_tools_email_energy_analysis_updated', {'registry_revision': 7})], events)
            self.assertTrue(await ns['async_unload_entry'](hass, entry))
            self.assertTrue(service.stopped)
            self.assertTrue(made['statistics_runtime'][0].stopped)
            self.assertFalse(registry.active)
            self.assertFalse(made['coordinator'][0].running)
            self.assertNotIn('energy_analysis', bucket)
            self.assertNotIn('energy_membership', bucket)
            self.assertNotIn('energy_source_device', bucket)
            self.assertFalse(association.running)
            self.assertFalse(association._unsubscribers)
            self.assertEqual(3, association.unsubscribe_calls)
            self.assertEqual(1, association.stop_calls)
            self.assertFalse(any(made['platforms'].values()))

    async def test_failed_start_or_platform_setup_leaves_no_live_analysis_or_listener(self):
        for boundary in ('start', 'platforms', 'cancel'):
            ns, hass, entry, made, events, mods = self.fixture(fail=boundary)
            with patch.dict(sys.modules, mods):
                with self.assertRaises(asyncio.CancelledError if boundary == 'cancel' else RuntimeError):
                    await ns['async_setup_entry'](hass, entry)
            self.assertEqual(1, len(made['service']))
            self.assertTrue(made['service'][0].stopped)
            self.assertFalse(made['registry'][0].active)
            self.assertFalse(made['coordinator'][0].running)
            self.assertNotIn('energy_analysis', hass.data['ha_tools_email'])
            self.assertNotIn('energy_membership', hass.data['ha_tools_email'])
            self.assertEqual(1, len(made['association']))
            self.assertFalse(made['association'][0].running)
            self.assertFalse(made['association'][0]._unsubscribers)
            self.assertNotIn('energy_source_device', hass.data['ha_tools_email'])

    async def test_failed_platform_unload_preserves_running_service_and_registry(self):
        ns, hass, entry, made, events, mods = self.fixture(unload=False)
        with patch.dict(sys.modules, mods):
            await ns['async_setup_entry'](hass, entry)
            before = dict(hass.data['ha_tools_email'])
            self.assertFalse(await ns['async_unload_entry'](hass, entry))
        self.assertEqual(before, hass.data['ha_tools_email'])
        self.assertFalse(made['service'][0].stopped)
        self.assertTrue(made['registry'][0].active)
        self.assertTrue(made['coordinator'][0].running)
        self.assertTrue(made['native'][0].running)
        self.assertTrue(made['statistics_runtime'][0].running)
        self.assertEqual(2, len(made['native'][0]._unsubscribers))
        association = made['association'][0]
        self.assertTrue(association.running)
        self.assertEqual(3, len(association._unsubscribers))
        self.assertEqual(0, association.stop_calls)
        self.assertEqual(0, association.unsubscribe_calls)
        self.assertTrue(all(made['platforms'].values()))
        self.assertEqual([('sensor', 'binary_sensor', 'button', 'event', 'calendar')], made['unload_calls'])


    async def test_association_start_failure_or_cancel_releases_helpers_and_forwarded_platforms(self):
        for boundary in ('association_start', 'association_cancel'):
            with self.subTest(boundary=boundary):
                ns, hass, entry, made, events, mods = self.fixture(fail=boundary)
                with patch.dict(sys.modules, mods):
                    error = asyncio.CancelledError if boundary == 'association_cancel' else RuntimeError
                    with self.assertRaises(error):
                        await ns['async_setup_entry'](hass, entry)
                association = made['association'][0]
                self.assertFalse(association.running)
                self.assertFalse(association._unsubscribers)
                self.assertEqual(1, association.stop_calls)
                self.assertEqual(3, association.unsubscribe_calls)
                self.assertTrue(made['service'][0].stopped)
                self.assertFalse(made['registry'][0].active)
                self.assertFalse(made['coordinator'][0].running)
                self.assertFalse(any(made['platforms'].values()))
                self.assertEqual([('sensor', 'binary_sensor', 'button', 'event', 'calendar')], made['unload_calls'])
                self.assertFalse(any(key in hass.data['ha_tools_email'] for key in
                    ('energy_source_device', 'energy_analysis', 'energy_membership', 'energy_snapshot')))
                self.assertNotIn(('scheduler_reloaded', {}), events)


    async def test_native_metadata_start_failure_or_cancel_cleans_before_forwarding(self):
        for boundary in ('native_start', 'native_cancel'):
            ns, hass, entry, made, events, mods = self.fixture(fail=boundary)
            with patch.dict(sys.modules, mods):
                with self.assertRaises(asyncio.CancelledError if boundary == 'native_cancel' else RuntimeError):
                    await ns['async_setup_entry'](hass, entry)
            self.assertFalse(made['native'][0].running)
            self.assertFalse(made['native'][0]._unsubscribers)
            self.assertFalse(made['coordinator'][0].running)
            self.assertFalse(made['registry'][0].active)
            self.assertFalse(any(made['platforms'].values()))
            self.assertNotIn('email_native', hass.data['ha_tools_email'])


if __name__ == '__main__':
    unittest.main()
