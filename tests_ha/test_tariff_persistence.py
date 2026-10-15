"""Optional native own-Store IO; owner must select an SSD --basetemp."""
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
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
    if sys.platform == 'darwin' and not tmp_path.resolve().is_relative_to(Path('/Volumes/Samsung_SSD')):
        pytest.fail('macOS native storage fixture requires explicit SSD --basetemp')
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


async def test_native_history_schedule_and_period_failure_preserve_file(hass, tmp_path):
    from homeassistant.util.file import WriteError
    configure_ssd(hass, tmp_path)
    storage = EmailStorage(hass)
    schedule = await storage.async_upsert_schedule(dict(
        id='qa-native', kind='log_digest', cadence='daily', time='07:30', enabled=False))
    path = tmp_path / '.storage' / STORAGE_KEY
    before = path.read_bytes()
    snapshot = deepcopy(storage._data)
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    attempt = dict(id='00000000-0000-4000-8000-000000000001', timestamp=now.isoformat(),
                   source='manual', kind='test', cadence=None, period=None,
                   schedule_ref=None, recipient_count=1, outcome='accepted_by_smtp')
    lower = HAStore._async_write_data

    async def failing_write(store, *args, **kwargs):
        if store.key == STORAGE_KEY:
            raise WriteError('synthetic')
        await lower(store, *args, **kwargs)

    with patch.object(HAStore, '_async_write_data', new=failing_write):
        for operation in (
            lambda: storage.async_append_attempt(attempt, now=now),
            lambda: storage.async_upsert_schedule({**schedule, 'time': '08:30'}),
            lambda: storage.async_delete_schedule(schedule['id']),
            lambda: storage.async_set_digest_state('log_digest', 'daily', {'count': 1}),
            lambda: storage.async_set_last_fired(schedule['id'], 'daily:2026-10-15', now.isoformat()),
        ):
            with pytest.raises(StorageWriteError):
                await operation()
            assert path.read_bytes() == before
            assert storage._data == snapshot
    # One explicit successful operation must not carry a failed mutation along.
    await storage.async_append_attempt(attempt, now=now)
    fresh = EmailStorage(hass)
    assert len((await fresh.async_get_attempt_history(now=now))['attempts']) == 1
    assert await fresh.async_get_schedule(schedule['id']) == schedule
    assert await fresh.async_get_last_fired(schedule['id']) is None
    assert await fresh.async_get_digest_state('log_digest', 'daily') == {}


async def test_native_failed_smtp_migration_preserves_legacy_until_completed_write(hass, tmp_path):
    from homeassistant.util.file import WriteError
    configure_ssd(hass, tmp_path)
    legacy = tmp_path / 'ha-tools' / 'smtp-config.json'
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({'server': 'synthetic', 'password': '!secret synthetic'}))
    before = legacy.read_bytes()
    with patch.object(HAStore, '_async_write_data', side_effect=WriteError('synthetic')):
        with pytest.raises(StorageWriteError):
            await EmailStorage(hass).async_load()
    assert legacy.read_bytes() == before
    restored = EmailStorage(hass)
    assert (await restored.async_get_smtp_config())['server'] == 'synthetic'
    assert not legacy.exists()
    envelope = json.loads((tmp_path / '.storage' / STORAGE_KEY).read_text())
    assert envelope['data']['smtp_config']['password'] == '!secret synthetic'
