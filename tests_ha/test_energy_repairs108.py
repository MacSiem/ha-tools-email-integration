"""Retired energy Repairs are removed on setup and remain absent."""
import pytest
from homeassistant.helpers import issue_registry as ir
from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.energy_repairs import ISSUES, _stale_id
from tests_ha.test_ha_runtime import _setup

pytestmark = pytest.mark.asyncio

async def test_setup_removes_legacy_including_ignored_and_reload_is_idempotent(hass, hass_storage, recorder_mock):
    registry = ir.async_get(hass)
    keys = [*ISSUES, _stale_id('sensor.retired')]
    for key in [*keys, 'other_feature']:
        ir.async_create_issue(hass, DOMAIN, key, is_fixable=False,
                              severity=ir.IssueSeverity.WARNING, translation_key=key)
    registry.async_ignore(DOMAIN, keys[0], True)
    entry, _ = await _setup(hass)
    for key in keys:
        assert registry.async_get_issue(DOMAIN, key) is None
    assert registry.async_get_issue(DOMAIN, 'other_feature') is not None
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    await coordinator._async_tick(None)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.data[DOMAIN]['energy_snapshot']._async_tick(None)
    for key in keys:
        assert registry.async_get_issue(DOMAIN, key) is None
    assert registry.async_get_issue(DOMAIN, 'other_feature') is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
