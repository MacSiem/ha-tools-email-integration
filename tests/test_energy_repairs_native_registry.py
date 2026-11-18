"""Regression against HA's real IssueRegistry save/load and active semantics.

Run in the HA Core image; ordinary host discovery skips when HA is absent.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import issue_registry as ir
    import homeassistant.components.recorder as recorder
    import homeassistant.components.recorder.statistics as statistics
except ImportError:
    HomeAssistant = None

package = types.ModuleType('_native_repairs_registry')
package.__path__ = [str(Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email')]
sys.modules[package.__name__] = package
repairs = importlib.import_module(package.__name__ + '.energy_repairs')

@unittest.skipIf(HomeAssistant is None, 'Requires Home Assistant Core')
class NativeRegistryRestart(unittest.IsolatedAsyncioTestCase):
    async def test_preserved_entries_reactivate_only_on_confirmed_fact_and_keep_ignore(self):
        hass = HomeAssistant('/config')
        registry = ir.async_get(hass)
        source_id = 'sensor.kitchen'
        hass.states.async_set(source_id, 'unavailable', {'friendly_name':'Kitchen'})
        hass.states.get(source_id).last_changed = datetime.now(timezone.utc) - timedelta(hours=30)
        storage = object()
        config = {'energy_sources':[{'type':'grid','flow_from':[{'stat_energy_from':source_id}]}],
                  'device_consumption':[{'stat_consumption':source_id}]}
        manager = types.SimpleNamespace(data=config)
        association = types.SimpleNamespace(_running=True,_manager=manager)
        coordinator = types.SimpleNamespace(hass=hass,storage=storage,running=True,provider=None)
        hass.data[repairs.DOMAIN] = {'storage':storage, 'energy_snapshot':coordinator,
                                    'energy_source_device':association}
        stamp = (datetime.now(timezone.utc)-timedelta(hours=32)).timestamp()
        rec = types.SimpleNamespace(async_add_executor_job=AsyncMock(side_effect=lambda fn:fn()))
        read = lambda hass, n, sid, convert, kinds: {sid:[{'start':stamp,'sum':0.0}]}
        with patch.object(recorder,'get_instance',return_value=rec), \
             patch.object(statistics,'get_last_statistics',side_effect=read), \
             patch.object(statistics,'get_last_short_term_statistics',return_value={}):
            await repairs.async_publish_stale_sources(coordinator)
            stale_id = repairs._stale_id(source_id)
            grid_id = repairs.GRID_DEVICES
            self.assertTrue(registry.async_get_issue(repairs.DOMAIN,stale_id).active)
            self.assertTrue(registry.async_get_issue(repairs.DOMAIN,grid_id).active)
            registry.async_ignore(repairs.DOMAIN,grid_id,True)
            saved = registry._data_to_save()
            old_created = registry.async_get_issue(repairs.DOMAIN,grid_id).created
            with patch.object(registry._store,'async_load',AsyncMock(return_value=saved)):
                await registry._async_load()
            self.assertFalse(registry.async_get_issue(repairs.DOMAIN,grid_id).active)
            self.assertFalse(registry.async_get_issue(repairs.DOMAIN,stale_id).active)
            # An unknown Recorder result cannot reactivate a stale-source issue.
            with patch.object(statistics,'get_last_statistics',return_value={}):
                await repairs.async_publish_stale_sources(coordinator)
            self.assertFalse(registry.async_get_issue(repairs.DOMAIN,stale_id).active)
            self.assertTrue(registry.async_get_issue(repairs.DOMAIN,grid_id).active)
            await repairs.async_publish_stale_sources(coordinator)
            grid = registry.async_get_issue(repairs.DOMAIN,grid_id)
            stale = registry.async_get_issue(repairs.DOMAIN,stale_id)
            self.assertTrue(grid.active); self.assertTrue(stale.active)
            self.assertIsNotNone(grid.dismissed_version)
            self.assertEqual(old_created,grid.created)
            self.assertEqual('Kitchen',stale.translation_placeholders['name'])
            await repairs.async_publish_stale_sources(coordinator)
            self.assertEqual(2, len(registry.issues))
            hass.states.async_set(source_id,'0',{'friendly_name':'Kitchen'})
            await repairs.async_publish_stale_sources(coordinator)
            self.assertIsNone(registry.async_get_issue(repairs.DOMAIN,stale_id))
            manager.data={'energy_sources':[], 'device_consumption':[]}
            await repairs.async_publish_stale_sources(coordinator)
            self.assertIsNone(registry.async_get_issue(repairs.DOMAIN,grid_id))
