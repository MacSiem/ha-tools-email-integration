"""Native Recorder/WS proof on each supported Core environment, no fake DB."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import models
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics, async_import_statistics, statistics_during_period)
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.tariff_history import migrate_tariff_v1
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_energy_runtime import profile
from tests_ha.test_energy_entity_statistics import _async_recorder_barrier

pytestmark = pytest.mark.asyncio
UTC = timezone.utc
START = datetime(2026, 10, 11, 12, tzinfo=UTC)
END = START + timedelta(hours=168)


async def command(client, suffix, **fields):
    await client.send_json_auto_id({'type': f'{DOMAIN}/energy_statistics_{suffix}', **fields})
    return await client.receive_json()


async def setup_dataset(hass, recorder_mock, hass_ws_client):
    await hass.config.async_set_time_zone('UTC')
    area = ar.async_get(hass).async_create('Publisher area')
    entity = er.async_get(hass).async_get_or_create('sensor', 'test', 'publisher_device',
        suggested_object_id='publisher_device')
    er.async_get(hass).async_update_entity(entity.entity_id, area_id=area.id)
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [{'type': 'grid', 'flow_from': [
        {'stat_energy_from': 'pstats:grid'}], 'flow_to': []}],
        'device_consumption': [{'stat_consumption': entity.entity_id}]}
    def metadata(source, sid):
        result = {'source': source, 'statistic_id': sid, 'name': 'Publisher fixture',
                  'has_mean': False, 'has_sum': True, 'unit_of_measurement': 'kWh'}
        fields = models.StatisticMetaData.__annotations__
        if 'mean_type' in fields:
            result.pop('has_mean'); result['mean_type'] = models.StatisticMeanType.NONE
        if 'unit_class' in fields:
            result['unit_class'] = 'energy'
        return result
    async_add_external_statistics(hass, metadata('pstats', 'pstats:grid'), [
        {'start': START+timedelta(hours=i-1), 'sum': float(i)} for i in range(169)])
    async_import_statistics(hass, metadata('recorder', entity.entity_id), [
        {'start': START+timedelta(hours=i-1), 'sum': i*.25} for i in range(169)])
    await _async_recorder_barrier(hass, recorder_mock)
    entry, storage = await _setup(hass)
    await storage.async_save_tariff_book(migrate_tariff_v1(profile(), legacy_combined=False), expected_revision=0)
    client = await hass_ws_client(hass)
    prepared = await command(client, 'prepare'); assert prepared['success'], prepared
    consent = await command(client, 'enable', proposal=prepared['result']['proposal'],
        expected_revision=prepared['result']['revision'], acknowledge_global=True,
        acknowledge_current_membership=True)
    assert consent['success'], consent
    return entry, storage, client, area.id


async def publish(client, hass, recorder_mock):
    # Explicit enable now starts the real one-hour runtime. Its retained task
    # must settle before this separate manual 168-hour gesture; a Recorder
    # barrier alone cannot settle an active producer/Store coroutine.
    runtime = hass.data[DOMAIN]['energy_statistics_runtime']
    while runtime._lane['task'] is not None:
        await runtime._lane['task']
        await asyncio.sleep(0)  # Let the real task done callback release its lane.
    result = await command(client, 'refresh', backfill=True, acknowledge_current_membership=True)
    if not result['success']:
        # A supported Recorder barrier may return before an already-dequeued
        # import commits. Pending intent stays durable; repeat after native FIFO.
        await _async_recorder_barrier(hass, recorder_mock)
        result = await command(client, 'refresh', backfill=True, acknowledge_current_membership=True)
    assert result['success'], result
    return result['result']


@pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')
async def test_native_168_points_metadata_change_ws_global_read_and_graph_catalog(
        hass, hass_storage, recorder_mock, hass_ws_client, hass_read_only_access_token):
    entry, storage, client, area_id = await setup_dataset(hass, recorder_mock, hass_ws_client)
    status = await publish(client, hass, recorder_mock)
    subject = hass.data[DOMAIN]['energy_statistics']
    sid = next(sid for sid, row in subject._state['series'].items()
               if row['identity']['kind'] == 'area' and row['identity']['key'] == area_id)
    assert area_id not in sid
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    await reader.send_json_auto_id({'type': 'recorder/statistics_during_period',
        'start_time': START.isoformat(), 'end_time': END.isoformat(), 'statistic_ids': [sid],
        'period': 'hour', 'types': ['sum', 'change']})
    data = await reader.receive_json(); assert data['success'], data
    rows = data['result'][sid]
    assert len(rows) == 168
    assert len({row['start'] for row in rows}) == 168
    assert all(row['change'] == pytest.approx(.3) for row in rows)
    assert rows[-1]['sum'] == pytest.approx(50.4)
    meta = (await subject.recorder.async_metadata([sid]))[sid]
    assert meta['source'] == DOMAIN
    assert meta.get('statistics_unit_of_measurement', meta.get('unit_of_measurement')) == 'EUR'
    assert meta['has_sum']
    if 'mean_type' in models.StatisticMetaData.__annotations__:
        assert meta['mean_type'] == models.StatisticMeanType.NONE
    if 'unit_class' in models.StatisticMetaData.__annotations__:
        assert meta['unit_class'] is None
    denied = await command(reader, 'status'); assert not denied['success']
    # This is the real global-read limitation: backend policy cannot hide these
    # rows from the native reader. The explicit enable warning must disclose it.
    assert status['reader_scope'] == 'all_authenticated_ha_users'
    graph = {'type': 'statistics-graph', 'days_to_show': 7, 'period': 'hour',
        'chart_type': 'bar', 'stat_types': ['change'], 'entities': [sid]}
    assert graph['entities'][0] in {item['statistic_id'] for item in status['catalog']}
    await publish(client, hass, recorder_mock)
    again = await subject.recorder.instance.async_add_executor_job(partial(statistics_during_period,
        hass, START, END, {sid}, 'hour', None, {'sum'}))
    assert len(again[sid]) == 168
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(RuntimeError): subject._check()


@pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')
async def test_native_known_to_unknown_rebuild_and_historical_revision(
        hass, hass_storage, recorder_mock, hass_ws_client):
    entry, storage, client, area_id = await setup_dataset(hass, recorder_mock, hass_ws_client)
    await publish(client, hass, recorder_mock)
    subject = hass.data[DOMAIN]['energy_statistics']
    sid = next(sid for sid, row in subject._state['series'].items() if row['identity']['kind'] == 'area')
    state = await storage.async_get_tariff_book(); changed = deepcopy(state['book'])
    changed['tariffs'][0]['versions'][0]['profile']['zones'][0]['energyRate'] = 2
    await storage.async_save_tariff_book(changed, expected_revision=state['revision'], historical_edit_ack=True)
    await publish(client, hass, recorder_mock)
    rows = await subject.recorder.instance.async_add_executor_job(partial(statistics_during_period,
        hass, START, END, {sid}, 'hour', None, {'sum', 'change'}))
    assert len(rows[sid]) == 168
    assert all(row['change'] == pytest.approx(.55) for row in rows[sid])
    state = await storage.async_get_tariff_book(); changed = deepcopy(state['book'])
    version = changed['tariffs'][0]['versions'][0]
    version['valid_from'] = '2026-10-18'
    # Keep the embedded schema1 validity consistent with the changed version.
    version['profile']['validFrom'] = '2026-10-18'
    await storage.async_save_tariff_book(changed, expected_revision=state['revision'], historical_edit_ack=True)
    await publish(client, hass, recorder_mock)
    rows = await subject.recorder.instance.async_add_executor_job(partial(statistics_during_period,
        hass, START, END, {sid}, 'hour', None, {'sum', 'change'}))
    assert len(rows[sid]) == 12
    assert all(row['change'] == pytest.approx(.55) for row in rows[sid])
    assert rows[sid][-1]['sum'] == pytest.approx(6.6)


@pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')
async def test_native_disabled_restart_and_author_revocation_do_not_enqueue(
        hass, hass_storage, recorder_mock, hass_ws_client):
    entry, storage, client, area_id = await setup_dataset(hass, recorder_mock, hass_ws_client)
    status = await publish(client, hass, recorder_mock)
    subject = hass.data[DOMAIN]['energy_statistics']; before = deepcopy(subject._state['series'])
    result = await command(client, 'disable', expected_revision=status['revision'])
    assert result['success']; assert not result['result']['enabled']
    # Restore a fresh kernel from confirmed HA Store, not in-memory consent.
    from custom_components.ha_tools_email.energy_statistics_publisher import EnergyStatisticsPublisher
    replacement = EnergyStatisticsPublisher(hass, subject.service)
    share_author = subject._state['share']['author']
    assert not (await replacement.async_refresh(share_author))['enabled']
    assert replacement.recorder is None
    assert replacement._state['series'] == before
    user = await hass.auth.async_get_user(share_author)
    await hass.auth.async_update_user(user, is_active=False)
    with pytest.raises(PermissionError): await replacement.async_refresh(share_author)
    assert replacement.recorder is None
