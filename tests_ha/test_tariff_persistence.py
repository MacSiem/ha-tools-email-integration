"""Optional native own-Store IO; owner must select an SSD --basetemp."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store as HAStore

from custom_components.ha_tools_email.const import STORAGE_KEY, STORAGE_VERSION
from custom_components.ha_tools_email.storage import EmailStorage, StorageWriteError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def hass_storage():
    """Keep auth/registry mocked; enable real IO solely for this component key."""
    from pytest_homeassistant_custom_component.common import mock_storage
    actual_write, actual_load = HAStore._async_write_data, HAStore._async_load
    with mock_storage() as data:
        mocked_write, mocked_load = HAStore._async_write_data, HAStore._async_load

        async def selected_write(store, *args, **kwargs):
            method = actual_write if store.key == STORAGE_KEY else mocked_write
            await method(store, *args, **kwargs)

        async def selected_load(store):
            method = actual_load if store.key == STORAGE_KEY else mocked_load
            return await method(store)

        with patch.object(HAStore, '_async_write_data', new=selected_write), \
             patch.object(HAStore, '_async_load', new=selected_load):
            yield data


def configure_ssd(hass, tmp_path):
    if not tmp_path.resolve().is_relative_to(Path('/Volumes/Samsung_SSD')):
        pytest.skip('Native tariff storage requires explicit SSD --basetemp')
    hass.config.config_dir = str(tmp_path)


def synthetic_profile():
    return {'schema': 1, 'currency': 'EUR', 'timezone': 'UTC', 'clock': {'mode': 'civil'},
            'priceBasis': 'gross', 'validFrom': '2026-01-01', 'validTo': '2026-12-31',
            'defaultZone': 'day', 'zones': [{'id': 'day', 'energyRate': 1, 'variableRate': 0}],
            'rules': [], 'fixedMonthly': 7}


async def test_native_completed_tariff_write_fresh_instance_and_side_effect_free_read(hass, tmp_path):
    configure_ssd(hass, tmp_path)
    foreign = {'future': {'keep': [True]}, 'smtp_config': {'password': '!secret synthetic'},
               'schedules': [{'future': 'untouched'}],
               'attempt_history': {'schema': 99, 'opaque': ['untouched']}}
    await HAStore(hass, STORAGE_VERSION, STORAGE_KEY).async_save(foreign)
    storage = EmailStorage(hass)
    result = await storage.async_save_tariff(synthetic_profile(), expected_revision=0)
    assert result['revision'] == 1
    assert result['profile'] == synthetic_profile()
    path = tmp_path / '.storage' / STORAGE_KEY
    envelope = json.loads(path.read_text())
    for key, value in foreign.items():
        assert envelope['data'][key] == value
    assert envelope['data']['tariff_updated_at'] == result['updated_at']
    fresh = EmailStorage(hass)
    assert await fresh.async_get_tariff() == result
    before = path.read_bytes()
    with patch.object(fresh, '_async_migrate_legacy_smtp_config_locked',
                      side_effect=AssertionError('tariff read must not migrate')):
        assert await fresh.async_get_tariff() == result
    assert path.read_bytes() == before


async def test_native_failed_write_and_stopping_keep_tariff_until_explicit_retry(hass, tmp_path):
    from homeassistant.util.file import WriteError
    configure_ssd(hass, tmp_path)
    storage = EmailStorage(hass)
    old = await storage.async_save_tariff(synthetic_profile(), expected_revision=0)
    path = tmp_path / '.storage' / STORAGE_KEY
    before = path.read_bytes()
    lower_write = HAStore._async_write_data

    async def failing_own_write(store, *args, **kwargs):
        if store.key == STORAGE_KEY:
            raise WriteError('synthetic')
        await lower_write(store, *args, **kwargs)

    with patch.object(HAStore, '_async_write_data', new=failing_own_write):
        with pytest.raises(StorageWriteError):
            await storage.async_save_tariff(None, expected_revision=1)
    assert path.read_bytes() == before
    assert await storage.async_get_tariff() == old
    fresh = EmailStorage(hass)
    assert await fresh.async_get_tariff() == old
    state = hass.state
    try:
        hass.state = CoreState.stopping
        with pytest.raises(StorageWriteError):
            await storage.async_save_tariff(None, expected_revision=1)
    finally:
        hass.state = state
    assert path.read_bytes() == before
    assert await storage.async_get_tariff() == old
    new = await storage.async_save_tariff(None, expected_revision=1)
    assert new['revision'] == 2 and new['profile'] is None
    assert await EmailStorage(hass).async_get_tariff() == new
