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
               for r in er.async_entries_for_config_entry(registry, entry.entry_id)
               if r.unique_id.startswith(f'{entry.entry_id}_energy_')}
    assert set(records) == {'zone', 'price', 'next_cheap', 'today_cost', 'today',
                            'cheap_share_today', 'cheap'}
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 8
    reports_id = registry.async_get_entity_id('binary_sensor', DOMAIN,
                                              f'{entry.entry_id}_reports_problem')
    assert reports_id is not None
    assert len({r.device_id for r in records.values()}) == 1
    device = dr.async_get(hass).async_get(records['price'].device_id)
    assert (DOMAIN, f'{entry.entry_id}_energy') in device.identifiers
    assert all(hass.states.get(r.entity_id).state == 'unavailable' for r in records.values())
    await storage.async_save_tariff(profile(), expected_revision=0)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    zone = hass.states.get(records['zone'].entity_id)
    assert zone.state == 'day'
    assert zone.attributes['classification'] == 'cheap'
    assert hass.states.get(records['cheap'].entity_id).state == 'on'
    price = hass.states.get(records['price'].entity_id)
    assert float(price.state) == 1.2
    assert price.attributes['state_class'] == 'measurement'
    assert price.attributes['unit_of_measurement'] == 'EUR/kWh'
    assert 'device_class' not in price.attributes
    cost = hass.states.get(records['today_cost'].entity_id)
    assert cost.state == 'unavailable'  # no configured Energy imports, no invented zero
    assert cost.attributes['device_class'] == 'monetary'
    assert cost.attributes['state_class'] == 'total'
    ids = {k: (r.entity_id, r.id, r.unique_id) for k, r in records.items()}
    assert len(coordinator._listeners) == 7
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    replacement = hass.data[DOMAIN]['energy_snapshot']
    assert replacement is not coordinator and not coordinator.running
    assert not coordinator._listeners and coordinator._unsub is None and coordinator._boundary_unsub is None
    assert len(replacement._listeners) == 7
    assert ids == {r.unique_id.removeprefix(f'{entry.entry_id}_energy_'):
                   (r.entity_id, r.id, r.unique_id)
                   for r in er.async_entries_for_config_entry(registry, entry.entry_id)
                   if r.unique_id.startswith(f'{entry.entry_id}_energy_')}
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 8
    assert registry.async_get_entity_id('binary_sensor', DOMAIN,
        f'{entry.entry_id}_reports_problem') == reports_id
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
@pytest.mark.parametrize('mutation', ['unload', 'reload', 'revision', 'revoke'])
async def test_native_ws_final_auth_await_rejects_stale_result(hass, hass_storage, hass_ws_client,
                                                             command, mutation):
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    client = await hass_ws_client(hass)
    original = hass.auth.async_get_user
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    method = 'async_snapshot' if command == 'energy_snapshot' else 'async_price_queries'
    calculate = getattr(coordinator, method)
    calculation_finished = False
    mutation_applied = False

    async def calculate_then_arm(*args, **kwargs):
        nonlocal calculation_finished
        result = await calculate(*args, **kwargs)
        calculation_finished = True
        return result

    async def get_user(uid):
        nonlocal mutation_applied
        user = await original(uid)
        # Source guards now reauthorize during IO too. Arm the mutation only
        # after the real calculation returns, at the final authenticated await.
        if calculation_finished and not mutation_applied:
            mutation_applied = True
            if mutation == 'unload':
                assert await hass.config_entries.async_unload(entry.entry_id)
            elif mutation == 'reload':
                assert await hass.config_entries.async_reload(entry.entry_id)
            elif mutation == 'revision':
                changed = deepcopy(profile())
                changed['zones'][0]['energyRate'] = 2
                await storage.async_save_tariff(changed, expected_revision=1)
            else:
                return None
        return user

    with patch.object(coordinator, method, new=calculate_then_arm), patch.object(
            hass.auth, 'async_get_user', new=get_user):
        await client.send_json_auto_id(request(command))
        response = await client.receive_json()
    assert calculation_finished and mutation_applied
    assert not response['success'], response
    assert response['error']['code'] == ('unauthorized' if mutation == 'revoke' else 'energy_unavailable')


async def test_native_recorder_external_import_without_state_prices_daily_monetary(hass, hass_storage,
                                                                                 recorder_mock, freezer):
    """Actual Recorder SQL metadata/change query, actual Energy manager, no provider doubles."""
    from datetime import datetime, timedelta, timezone
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import models
    from homeassistant.components.recorder.statistics import async_add_external_statistics

    freezer.move_to('2026-10-18T12:00:00+00:00')
    await hass.config.async_set_time_zone('UTC')
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
    assert state.attributes['state_class'] == 'total'
    assert result['entity_facts']['coverage_complete'] is True
    for key, expected in (('today', hours), ('cheap_share_today', 100)):
        sid = registry.async_get_entity_id('sensor', DOMAIN, f'{entry.entry_id}_energy_{key}')
        assert float(hass.states.get(sid).state) == pytest.approx(expected)
    # Hourly external history cannot supply the current half-hour's real 5-min tail.
    # Legacy completed-hour totals remain available, but native selected-current
    # sensors must become unavailable rather than publishing a fabricated zero.
    freezer.move_to('2026-10-18T12:30:00+00:00')
    incomplete = await coordinator.async_refresh()
    assert incomplete['today']['measurement_complete'] is True
    assert incomplete['today']['usage_cost'] == pytest.approx(hours * 1.2)
    assert incomplete['entity_facts']['coverage_complete'] is False
    for key in ('today', 'today_cost', 'cheap_share_today'):
        sid = registry.async_get_entity_id('sensor', DOMAIN, f'{entry.entry_id}_energy_{key}')
        assert hass.states.get(sid).state == 'unavailable'


async def test_native_timestamp_and_cheap_transition_publish_aware_values(hass, hass_storage, freezer):
    freezer.move_to('2026-10-18T12:30:00+00:00')
    await hass.config.async_set_time_zone('UTC')
    entry, storage = await _setup(hass)
    tariff = profile()
    tariff['zones'][0]['classification'] = 'expensive'
    tariff['zones'].append({'id': 'night', 'energyRate': .4,
                            'variableRate': .1, 'classification': 'cheap'})
    tariff['rules'] = [{'zone': 'night', 'intervals': [{'from': '22:00', 'to': '06:00'}]}]
    await storage.async_save_tariff(tariff, expected_revision=0)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    registry = er.async_get(hass)

    def state(key, domain='sensor'):
        return hass.states.get(registry.async_get_entity_id(
            domain, DOMAIN, f'{entry.entry_id}_energy_{key}'))

    await coordinator.async_refresh()
    assert state('next_cheap').state == '2026-10-18T22:00:00+00:00'
    assert state('next_cheap').attributes['device_class'] == 'timestamp'
    assert state('cheap', 'binary_sensor').state == 'off'
    assert float(state('price').state) == 1.2
    freezer.move_to('2026-10-18T22:30:00+00:00')
    await coordinator.async_refresh()
    assert state('cheap', 'binary_sensor').state == 'on'
    assert float(state('price').state) == .5
    assert state('next_cheap').state == '2026-10-19T22:00:00+00:00'


async def test_native_enum_metadata_does_not_invalidate_statistics_during_transient_snapshot_loss(hass, hass_storage):
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    zone_id = registry.async_get_entity_id('sensor', DOMAIN, f'{entry.entry_id}_energy_zone')
    assert registry.async_get(zone_id).capabilities['options'] == ['day']
    published = coordinator.data
    membership = hass.data[DOMAIN]['energy_membership']
    revision = membership.revision
    coordinator._invalidate()
    await hass.async_block_till_done()
    assert hass.states.get(zone_id).state == 'unavailable'
    assert registry.async_get(zone_id).capabilities['options'] == ['day']
    assert membership.revision == revision
    coordinator.data = published
    for listener in tuple(coordinator._listeners):
        listener()
    await hass.async_block_till_done()
    assert membership.revision == revision
    await storage.async_save_tariff(None, expected_revision=1)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert registry.async_get(zone_id).capabilities['options'] == []
    assert hass.states.get(zone_id).state == 'unavailable'
