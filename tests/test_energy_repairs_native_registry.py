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
    async def test_cleanup_including_ignored_preserves_other_issues(self):
        hass = HomeAssistant('/config')
        registry = ir.async_get(hass)
        keys = [*repairs.ISSUES, repairs._stale_id('sensor.kitchen')]
        for key in [*keys, 'other_feature']:
            ir.async_create_issue(hass, repairs.DOMAIN, key, is_fixable=False,
                                  severity=ir.IssueSeverity.WARNING, translation_key=key)
        registry.async_ignore(repairs.DOMAIN, keys[0], True)
        repairs.clear_legacy_issues(hass)
        repairs.clear_legacy_issues(hass)
        for key in keys:
            self.assertIsNone(registry.async_get_issue(repairs.DOMAIN, key))
        self.assertIsNotNone(registry.async_get_issue(repairs.DOMAIN, 'other_feature'))
