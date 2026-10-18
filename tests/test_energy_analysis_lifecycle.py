"""Execute actual setup/unload functions with bounded HA service doubles.

Reuse the existing AST loading approach to exercise lifecycle without importing
SMTP/HA platform services. The executed functions are unmodified product code.
"""
import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'


class AnalysisLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, fail=None, unload=True):
        created = {'coordinator': [], 'registry': [], 'service': []}
        events = []
        class Coordinator:
            def __init__(self, hass, storage):
                self.storage, self.running = storage, True
                created['coordinator'].append(self)
            async def async_start(self):
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
        async def ensure(hass):
            pass
        storage = object()
        async def get_storage(hass):
            return storage
        async def forward(entry, platforms):
            if fail == 'platforms':
                raise RuntimeError('synthetic platform failure')
        async def unload_platforms(entry, platforms):
            return unload
        hass = types.SimpleNamespace(data={'ha_tools_email': {}},
            bus=types.SimpleNamespace(async_fire=lambda event, data: events.append((event, data))),
            config_entries=types.SimpleNamespace(async_forward_entry_setups=forward,
                                                 async_unload_platforms=unload_platforms))
        namespace = {'__package__': 'lifecycle_fixture', 'HomeAssistant': object, 'ConfigEntry': object,
                     'DOMAIN': 'ha_tools_email', '_async_ensure_setup': ensure,
                     '_async_get_storage': get_storage, 'async_stop_scheduler': lambda hass: None}
        tree = ast.parse((ROOT / '__init__.py').read_text())
        functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name in ('async_setup_entry', 'async_unload_entry')]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / '__init__.py'), 'exec'), namespace)
        mods = {}
        for filename, name, cls in [('energy', 'EnergySnapshotCoordinator', Coordinator),
                                    ('registry_adapter', 'RegistryMembership', Registry),
                                    ('analysis_service', 'EnergyAnalysisService', Service)]:
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
            registry.on_change(7)
            self.assertEqual([('ha_tools_email_energy_analysis_updated', {'registry_revision': 7})], events)
            self.assertTrue(await ns['async_unload_entry'](hass, entry))
            self.assertTrue(service.stopped)
            self.assertFalse(registry.active)
            self.assertFalse(made['coordinator'][0].running)
            self.assertNotIn('energy_analysis', bucket)
            self.assertNotIn('energy_membership', bucket)

    async def test_failed_start_or_platform_setup_leaves_no_live_analysis_or_listener(self):
        for boundary in ('start', 'platforms'):
            ns, hass, entry, made, events, mods = self.fixture(fail=boundary)
            with patch.dict(sys.modules, mods):
                with self.assertRaises(RuntimeError):
                    await ns['async_setup_entry'](hass, entry)
            self.assertEqual(1, len(made['service']))
            self.assertTrue(made['service'][0].stopped)
            self.assertFalse(made['registry'][0].active)
            self.assertFalse(made['coordinator'][0].running)
            self.assertNotIn('energy_analysis', hass.data['ha_tools_email'])
            self.assertNotIn('energy_membership', hass.data['ha_tools_email'])

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


if __name__ == '__main__':
    unittest.main()
