"""Energy Repairs through real HA registry, manager, SQL and native guards.

Owner runs after integration. Synthetic external-statistic fixtures use the
real Recorder queue and readback, never a fake successful source/provider.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import models
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.energy_repairs import MISSING_IMPORT, OVERLAP
from tests_ha.test_ha_runtime import _setup

pytestmark = [pytest.mark.asyncio, pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')]


def preferences(consumers=None):
    return {'energy_sources': [{'type': 'grid', 'flow_from': [
        {'stat_energy_from': 'repairs108:grid'}], 'flow_to': []}],
        'device_consumption': consumers or []}


def issue(hass, key):
    return ir.async_get(hass).async_get_issue(DOMAIN, key)


async def meters(hass, recorder, rates):
    start = datetime(2026, 10, 18, tzinfo=timezone.utc)
    for sid, rate in rates.items():
        metadata = {'source': 'repairs108', 'statistic_id': sid,
            'name': 'Synthetic meter', 'has_mean': False, 'has_sum': True,
            'unit_of_measurement': 'kWh'}
        if hasattr(models, 'StatisticMeanType'):
            metadata.update(mean_type=models.StatisticMeanType.NONE, unit_class='energy')
        async_add_external_statistics(hass, metadata, [
            {'start': start + timedelta(hours=i-1), 'sum': i*rate} for i in range(13)])
    await recorder.async_block_till_done()


async def request(client, **options):
    await client.send_json_auto_id({'type': f'{DOMAIN}/energy_analysis',
                                   'period': 'day', **options})
    response = await client.receive_json()
    assert response['success'], response
    return response['result']


async def test_native_missing_import_requires_background_and_confirmed_manager(
        hass, hass_storage, recorder_mock):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [], 'device_consumption': []}
    entry, _ = await _setup(hass)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    # Setup's first tick had no running association yet: no guessed absence.
    assert issue(hass, MISSING_IMPORT) is None
    await coordinator.async_snapshot()
    await coordinator.async_snapshot(profile=None)
    await coordinator.async_refresh(now=datetime.now(timezone.utc))
    assert issue(hass, MISSING_IMPORT) is None
    await coordinator._async_tick(None)
    first = issue(hass, MISSING_IMPORT)
    assert first is not None and first.translation_key == MISSING_IMPORT
    assert first.translation_placeholders is None
    await coordinator._async_tick(None)
    assert issue(hass, MISSING_IMPORT) is first
    # A configured import resolves this configuration issue, even if SQL has
    # no measurements. This does not claim a working meter or zero consumption.
    manager.data = preferences()
    await coordinator._async_tick(None)
    assert issue(hass, MISSING_IMPORT) is None
    assert coordinator.data['today']['measurement_complete'] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_empty_sensor_error_or_edit_after_final_await_is_not_absence(
        hass, hass_storage, recorder_mock):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = preferences()
    entry, storage = await _setup(hass)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    # Composer currently maps a resolution error to []. The actual manager
    # still has an import, so that failure must never create missing-import.
    with patch('custom_components.ha_tools_email.energy.async_grid_sensors',
               new=AsyncMock(return_value=[])):
        await coordinator._async_tick(None)
    assert issue(hass, MISSING_IMPORT) is None
    manager.data = {'energy_sources': [{'type': 'grid', 'flow_from': [{}]}],
                    'device_consumption': []}
    await coordinator._async_tick(None)
    assert issue(hass, MISSING_IMPORT) is None  # Malformed is not confirmed empty.
    manager.data = {'energy_sources': [], 'device_consumption': []}
    real_tariff, calls = storage.async_get_tariff_book, 0
    async def tariff_then_change_sources():
        nonlocal calls
        result = await real_tariff()
        calls += 1
        if calls == 2:  # Refresh's final existing storage await.
            manager.data = preferences()
        return result
    with patch.object(storage, 'async_get_tariff_book', new=tariff_then_change_sources):
        await coordinator._async_tick(None)
    assert calls == 2 and issue(hass, MISSING_IMPORT) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_unload_cleans_only_owned_issues_and_reload_recreates(
        hass, hass_storage, recorder_mock):
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [], 'device_consumption': []}
    entry, _ = await _setup(hass)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    await coordinator._async_tick(None)
    assert issue(hass, MISSING_IMPORT) is not None
    ir.async_create_issue(hass, DOMAIN, 'other_feature_issue', is_fixable=False,
                         severity=ir.IssueSeverity.WARNING, translation_key='other_feature_issue')
    assert await hass.config_entries.async_reload(entry.entry_id)
    replacement = hass.data[DOMAIN]['energy_snapshot']
    assert replacement is not coordinator
    await replacement._async_tick(None)
    assert issue(hass, MISSING_IMPORT) is not None
    # A stale stopped owner must not erase the new owner's issue.
    coordinator.async_stop()
    assert issue(hass, MISSING_IMPORT) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert issue(hass, MISSING_IMPORT) is None and issue(hass, OVERLAP) is None
    assert issue(hass, 'other_feature_issue') is not None


@pytest.mark.parametrize('reason', ['consumption_exceeds_grid_import', 'children_exceed_parent'])
async def test_native_actual_producer_overlap_unknown_retains_complete_partition_clears(
        hass, hass_storage, recorder_mock, hass_ws_client, reason):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    rows = [{'stat_consumption': 'repairs108:device'}]
    rates = {'repairs108:grid': 1, 'repairs108:device': 2}
    if reason == 'children_exceed_parent':
        rows.append({'stat_consumption': 'repairs108:child',
                     'included_in_stat': 'repairs108:device'})
        rates.update({'repairs108:device': .5, 'repairs108:child': 1})
    manager.data = preferences(rows)
    await meters(hass, recorder_mock, rates)
    entry, _ = await _setup(hass)
    client = await hass_ws_client(hass)
    result = await request(client)
    assert result['coverage']['complete'] is True
    assert reason in result['groups']['issues']
    assert result['groups']['partition_complete'] is False
    original = issue(hass, OVERLAP)
    assert original is not None and original.translation_key == OVERLAP
    assert original.translation_placeholders is None
    # Incomplete current measurement is actual missing SQL for an extra import.
    manager.data['energy_sources'][0]['flow_from'].append(
        {'stat_energy_from': 'repairs108:missing'})
    partial = await request(client)
    assert partial['coverage']['complete'] is False
    assert issue(hass, OVERLAP) is original
    # New current complete configuration, no consumers: actual partition proof.
    manager.data = preferences()
    complete = await request(client)
    assert complete['coverage']['complete'] is True
    assert complete['groups']['partition_complete'] is True
    assert complete['groups']['issues'] == []
    assert issue(hass, OVERLAP) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('options', [
    {'anchor': '2026-10-18'}, {'comparison': 'previous'},
    {'type': f'{DOMAIN}/energy_report', 'language': 'en', 'timezone': 'UTC', 'hour_cycle': '24'}])
async def test_native_anchored_comparison_or_report_cannot_publish_global_issue(
        hass, hass_storage, recorder_mock, hass_ws_client, options):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = preferences([{'stat_consumption': 'repairs108:device'}])
    await meters(hass, recorder_mock, {'repairs108:grid': 1, 'repairs108:device': 2})
    entry, _ = await _setup(hass)
    client = await hass_ws_client(hass)
    await request(client, **options)
    assert issue(hass, OVERLAP) is None
    current = await request(client)
    assert 'consumption_exceeds_grid_import' in current['groups']['issues']
    assert issue(hass, OVERLAP) is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('change', ['policy', 'sources', 'timezone'])
async def test_native_final_fresh_guard_blocks_publication_after_real_context_change(
        hass, hass_storage, recorder_mock, hass_ws_client, hass_read_only_access_token, change):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = preferences([{'stat_consumption': 'repairs108:device'}])
    await meters(hass, recorder_mock, {'repairs108:grid': 1, 'repairs108:device': 2})
    entry, storage = await _setup(hass)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    client = await hass_ws_client(hass, hass_read_only_access_token)
    real_cpu, changed = coordinator._async_run_cpu, False
    async def finish_then_change(job):
        nonlocal changed
        value = await real_cpu(job)
        if getattr(getattr(job, 'func', None), '__name__', None) == '_finish' and not changed:
            changed = True
            if change == 'policy':
                await storage.async_save_feature_access({'schema': 1, 'features': {
                    'energy_analysis': {'read': {'mode': 'selected_users', 'user_ids': []}}}},
                    expected_revision=storage.current_feature_access_revision)
            elif change == 'sources':
                manager.data = preferences()
            else:
                await hass.config.async_set_time_zone('Europe/Warsaw')
        return value
    with patch.object(coordinator, '_async_run_cpu', new=finish_then_change):
        await client.send_json_auto_id({'type': f'{DOMAIN}/energy_analysis', 'period': 'day'})
        response = await client.receive_json()
    assert changed and response['success'] is False, response
    assert 'result' not in response and issue(hass, OVERLAP) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_known_zero_partition_is_complete_not_unknown(
        hass, hass_storage, recorder_mock, hass_ws_client):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = preferences([{'stat_consumption': 'repairs108:device'}])
    await meters(hass, recorder_mock, {'repairs108:grid': 0, 'repairs108:device': 0})
    entry, _ = await _setup(hass)
    client = await hass_ws_client(hass)
    result = await request(client)
    assert result['coverage']['complete'] is True
    assert result['groups']['partition_complete'] is True
    assert result['groups']['devices']['repairs108:device']['measurement_complete'] is True
    assert issue(hass, OVERLAP) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_injected_clock_cannot_publish_despite_real_source_success(
        hass, hass_storage, recorder_mock, hass_ws_client):
    from custom_components.ha_tools_email.analysis_service import EnergyAnalysisService
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = preferences([{'stat_consumption': 'repairs108:device'}])
    await meters(hass, recorder_mock, {'repairs108:grid': 1, 'repairs108:device': 2})
    entry, _ = await _setup(hass)
    bucket = hass.data[DOMAIN]
    original = bucket['energy_analysis']
    subject = EnergyAnalysisService(hass, entry.entry_id, bucket['energy_snapshot'],
        bucket['energy_membership'], clock=lambda: datetime.now(timezone.utc))
    bucket['energy_analysis'] = subject
    try:
        client = await hass_ws_client(hass)
        result = await request(client)
        assert result['coverage']['complete'] is True
        assert 'consumption_exceeds_grid_import' in result['groups']['issues']
        assert issue(hass, OVERLAP) is None
    finally:
        subject.async_stop()
        bucket['energy_analysis'] = original
    assert await hass.config_entries.async_unload(entry.entry_id)
