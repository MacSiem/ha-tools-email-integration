"""W20 through real HA platform forwarding, registries and authenticated WS."""
from copy import deepcopy
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr

from custom_components.ha_tools_email.const import DOMAIN
from tests_ha.test_ha_runtime import _setup

pytestmark = pytest.mark.asyncio


def profile():
    return {'schema': 1, 'currency': 'EUR', 'timezone': 'UTC', 'clock': {'mode': 'civil'},
            'priceBasis': 'gross', 'validFrom': '2026-01-01', 'validTo': '2026-12-31',
            'defaultZone': 'day', 'zones': [{'id': 'day', 'energyRate': 1,
            'variableRate': .2, 'classification': 'cheap'}], 'rules': [], 'fixedMonthly': 7}


def request(command):
    data = {'type': f'{DOMAIN}/{command}'}
    if command == 'energy_price_queries':
        data['queries'] = [{'buckets': [{'from': '2026-10-18T12:00Z',
                           'to': '2026-10-18T13:00Z', 'kwh': 2}]}]
    return data


async def test_native_forwarded_entities_availability_metadata_unload_reload(hass, hass_storage):
    entry, storage = await _setup(hass)
    registry = er.async_get(hass)
    records = {r.unique_id.removeprefix(f'{entry.entry_id}_energy_'): r
               for r in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert set(records) == {'zone', 'price', 'next_cheap', 'today_cost', 'cheap'}
    assert len({r.device_id for r in records.values()}) == 1
    device = dr.async_get(hass).async_get(records['price'].device_id)
    assert (DOMAIN, f'{entry.entry_id}_energy') in device.identifiers
    assert all(hass.states.get(r.entity_id).state == 'unavailable' for r in records.values())
    await storage.async_save_tariff(profile(), expected_revision=0)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(records['zone'].entity_id).state == 'cheap'
    assert hass.states.get(records['cheap'].entity_id).state == 'on'
    price = hass.states.get(records['price'].entity_id)
    assert float(price.state) == 1.2
    assert price.attributes['state_class'] == 'measurement'
    assert price.attributes['unit_of_measurement'] == 'EUR/kWh'
    assert 'device_class' not in price.attributes
    cost = hass.states.get(records['today_cost'].entity_id)
    assert cost.state == 'unavailable'  # no configured Energy imports, no invented zero
    assert cost.attributes['device_class'] == 'monetary'
    assert 'state_class' not in cost.attributes
    ids = {k: (r.entity_id, r.id, r.unique_id) for k, r in records.items()}
    assert len(coordinator._listeners) == 5
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    replacement = hass.data[DOMAIN]['energy_snapshot']
    assert replacement is not coordinator and not coordinator.running
    assert not coordinator._listeners and coordinator._unsub is None and coordinator._boundary_unsub is None
    assert len(replacement._listeners) == 5
    assert ids == {r.unique_id.removeprefix(f'{entry.entry_id}_energy_'):
                   (r.entity_id, r.id, r.unique_id)
                   for r in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert float(hass.states.get(records['price'].entity_id).state) == 1.2
    await storage.async_save_tariff(None, expected_revision=1)
    await replacement.async_refresh()
    assert all(hass.states.get(r.entity_id).state == 'unavailable' for r in records.values())
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not replacement.running and not replacement._listeners
    assert 'energy_snapshot' not in hass.data[DOMAIN]


@pytest.mark.parametrize('command', ['energy_snapshot', 'energy_price_queries'])
async def test_native_household_ws_shared_override_and_unloaded(hass, hass_storage, hass_ws_client,
                                                               hass_read_only_access_token, command):
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    before = await storage.async_get_tariff()
    for override in (False, True):
        msg = request(command)
        if override:
            msg['profile'] = None
        await client.send_json_auto_id(msg)
        response = await client.receive_json()
        assert response['success'], response
        result = response['result']
        assert result['profile_source'] == ('override' if override else 'shared')
        assert result['tariff_revision'] == (None if override else 1)
        if command == 'energy_snapshot':
            assert result['current']['usage_rate'] == (None if override else 1.2)
            assert result['today']['usage_cost'] is None
        else:
            assert result['results'][0]['usageCost'] == (None if override else 2.4)
        assert await storage.async_get_tariff() == before
    await hass.config_entries.async_unload(entry.entry_id)
    await client.send_json_auto_id(request(command))
    response = await client.receive_json()
    assert not response['success'] and response['error']['code'] == 'energy_unavailable'


@pytest.mark.parametrize('command', ['energy_snapshot', 'energy_price_queries'])
@pytest.mark.parametrize('mutation', ['unload', 'revision', 'revoke'])
async def test_native_ws_final_auth_await_rejects_stale_result(hass, hass_storage, hass_ws_client,
                                                             command, mutation):
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    client = await hass_ws_client(hass)
    original = hass.auth.async_get_user
    calls = 0

    async def get_user(uid):
        nonlocal calls
        user = await original(uid)
        calls += 1
        if calls == 2:
            if mutation == 'unload':
                assert await hass.config_entries.async_unload(entry.entry_id)
            elif mutation == 'revision':
                changed = deepcopy(profile())
                changed['zones'][0]['energyRate'] = 2
                await storage.async_save_tariff(changed, expected_revision=1)
            else:
                return None
        return user

    with patch.object(hass.auth, 'async_get_user', new=get_user):
        await client.send_json_auto_id(request(command))
        response = await client.receive_json()
    assert calls == 2
    assert not response['success'], response
    assert response['error']['code'] == ('unauthorized' if mutation == 'revoke' else 'energy_unavailable')


async def test_native_recorder_external_import_without_state_prices_daily_monetary(hass, hass_storage,
                                                                                 recorder_mock):
    """Actual Recorder SQL metadata/change query, actual Energy manager, no provider doubles."""
    from datetime import datetime, timedelta, timezone
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import models
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    hass.config.set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [{'type': 'grid', 'flow_from': [
        {'stat_energy_from': 'w20:grid'}], 'flow_to': []}], 'device_consumption': []}
    assert hass.states.get('w20:grid') is None
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours = int((now.replace(minute=0, second=0, microsecond=0) - start).total_seconds() / 3600)
    assert hours > 0, 'Run after at least one completed UTC hour'
    metadata = {'source': 'w20', 'statistic_id': 'w20:grid', 'name': 'Synthetic grid',
                'has_mean': False, 'has_sum': True, 'unit_of_measurement': 'kWh'}
    if hasattr(models, 'StatisticMeanType'):
        metadata.update(mean_type=models.StatisticMeanType.NONE, unit_class='energy')
    rows = [{'start': start + timedelta(hours=i-1), 'sum': float(i)}
            for i in range(hours + 1)]
    async_add_external_statistics(hass, metadata, rows)
    await recorder_mock.async_block_till_done()
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    result = await coordinator.async_refresh()
    assert result['today']['measurement_complete'], result
    assert result['today']['usage_cost'] == pytest.approx(hours * 1.2)
    assert result['today']['fixed_monthly'] == 7
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id('sensor', DOMAIN, f'{entry.entry_id}_energy_today_cost')
    state = hass.states.get(entity_id)
    assert float(state.state) == pytest.approx(hours * 1.2)
    assert state.attributes['device_class'] == 'monetary'
    assert state.attributes['unit_of_measurement'] == 'EUR'
    assert 'state_class' not in state.attributes
