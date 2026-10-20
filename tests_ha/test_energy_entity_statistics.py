"""W28/D-T through real HA entity platforms, registries and Recorder statistics."""
import asyncio
from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.components.energy.data import async_get_manager
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ha_tools_email.const import DOMAIN
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_energy_runtime import profile

pytestmark = pytest.mark.asyncio


async def _async_recorder_barrier(hass, instance):
    """Unconditional FIFO commit marker, including a task already dequeued.

    async_block_till_done's empty-queue fast path can return while Recorder is
    executing a StatisticsTask. Wait behind that task without checking emptiness.
    """
    from homeassistant.components.recorder.tasks import SynchronizeTask

    names = {field.name for field in fields(SynchronizeTask)}
    if 'future' in names:  # Core 2026.10
        marker = hass.loop.create_future()
        instance.queue_task(SynchronizeTask(marker))
        await marker
    elif 'event' in names:  # Core 2024.7
        marker = asyncio.Event()
        instance.queue_task(SynchronizeTask(marker))
        await marker.wait()
    else:
        raise AssertionError('Unsupported Recorder SynchronizeTask contract')


def records(hass, entry):
    registry = er.async_get(hass)
    return {key: registry.async_get(registry.async_get_entity_id(domain, DOMAIN,
            f'{entry.entry_id}_energy_{key}')) for domain, key in
            (('sensor', 'zone'), ('sensor', 'price'), ('sensor', 'next_cheap'),
             ('sensor', 'today_cost'), ('sensor', 'today'),
             ('sensor', 'cheap_share_today'), ('binary_sensor', 'cheap'))}


def preferences(*ids, flat=False):
    return {'energy_sources': ([{'type': 'grid', 'stat_energy_from': sid} for sid in ids]
            if flat else [{'type': 'grid', 'flow_from': [{'stat_energy_from': sid}
                           for sid in ids], 'flow_to': []}]), 'device_consumption': []}


def source(hass, suffix):
    entry = MockConfigEntry(domain='test', title=f'Meter {suffix}', unique_id=suffix)
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={('test', suffix)}, name=f'Meter {suffix}', manufacturer='Meter Inc',
        model='Grid')
    device = dr.async_get(hass).async_update_device(device.id, name_by_user=f'User {suffix}')
    entity = er.async_get(hass).async_get_or_create('sensor', 'test', suffix,
        config_entry=entry, device_id=device.id, suggested_object_id=f'grid_{suffix}')
    return entry, device, entity


def custom_fields(record):
    return tuple(getattr(record, field) for field in
        ('entity_id', 'unique_id', 'id', 'name', 'area_id', 'disabled_by',
         'hidden_by', 'options', 'icon'))


def source_fields(device):
    return tuple(getattr(device, field) for field in
        ('identifiers', 'connections', 'name', 'name_by_user', 'area_id', 'manufacturer', 'model'))


@pytest.mark.parametrize('flat', [False, True])
async def test_native_rebind_preserves_all_seven_ids_customizations_and_foreign_device(hass, hass_storage, flat):
    manager = await async_get_manager(hass)
    manager.data = preferences()
    a_entry, a, ae = source(hass, 'a')
    b_entry, b, be = source(hass, 'b')
    entry, _ = await _setup(hass)
    registry, devices = er.async_get(hass), dr.async_get(hass)
    association = hass.data[DOMAIN]['energy_source_device']
    original = records(hass, entry)
    fallback = original['price'].device_id
    area = __import__('homeassistant.helpers.area_registry', fromlist=['async_get']).async_get(hass).async_create('User room')
    price = registry.async_update_entity(original['price'].entity_id,
        new_entity_id='sensor.my_tariff', name='My tariff', area_id=area.id, icon='mdi:cash')
    registry.async_update_entity_options(price.entity_id, 'sensor', {'display_precision': 3})
    registry.async_update_entity(original['zone'].entity_id, disabled_by=er.RegistryEntryDisabler.USER,
                                 hidden_by=er.RegistryEntryHider.USER)
    await hass.async_block_till_done()
    custom = {key: custom_fields(row) for key, row in records(hass, entry).items()}
    before_a, before_b = source_fields(a), source_fields(b)
    for ids, expected in (((ae.entity_id,), a.id), ((be.entity_id,), b.id),
                          ((ae.entity_id, be.entity_id), fallback),
                          (('external:grid',), fallback), (('sensor.missing',), fallback),
                          ((), fallback), ((ae.entity_id,), a.id)):
        manager.data = preferences(*ids, flat=flat)
        association.async_reconcile()
        await hass.async_block_till_done()
        current = records(hass, entry)
        assert {row.device_id for row in current.values()} == {expected}
        assert custom == {key: custom_fields(row) for key, row in current.items()}
        assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 8
        report_id = registry.async_get_entity_id('binary_sensor', DOMAIN,
                                                 f'{entry.entry_id}_reports_problem')
        assert registry.async_get(report_id).device_id == fallback
        assert source_fields(devices.async_get(a.id)) == before_a
        assert source_fields(devices.async_get(b.id)) == before_b
        for foreign_entry, original_device in ((a_entry, a), (b_entry, b)):
            device = devices.async_get(original_device.id)
            if hasattr(device, 'config_entry_id'):
                assert device.config_entry_id == foreign_entry.entry_id
            else:
                assert foreign_entry.entry_id in device.config_entries
    if not hasattr(devices.async_get(a.id), 'config_entry_id'):
        assert entry.entry_id in devices.async_get(a.id).config_entries
        assert entry.entry_id not in devices.async_get(b.id).config_entries
    # A real source update must not make Core drop our entities for a missing link.
    devices.async_update_device(a.id, sw_version='2')
    await hass.async_block_till_done()
    assert custom == {key: custom_fields(row) for key, row in records(hass, entry).items()}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not association._running and not association._unsubscribers
    replacement = hass.data[DOMAIN]['energy_source_device']
    assert replacement is not association
    assert {row.device_id for row in records(hass, entry).values()} == {a.id}
    assert custom == {key: custom_fields(row) for key, row in records(hass, entry).items()}
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not replacement._running and not replacement._unsubscribers
    replacement.async_reconcile()  # no post-unload write
    assert 'energy_source_device' not in hass.data[DOMAIN]


async def test_native_registry_source_change_and_preferences_poll_without_manager_listener_leak(hass, hass_storage, freezer):
    freezer.move_to('2026-10-18T12:00:00Z')
    manager = await async_get_manager(hass)
    a_entry, a, ae = source(hass, 'a')
    _, b, _ = source(hass, 'b')
    manager.data = preferences(ae.entity_id)
    listener_count = len(manager._update_listeners)
    entry, _ = await _setup(hass)
    association = hass.data[DOMAIN]['energy_source_device']
    assert {row.device_id for row in records(hass, entry).values()} == {a.id}
    if not hasattr(b, 'config_entry_id'):
        dr.async_get(hass).async_update_device(b.id, add_config_entry_id=a_entry.entry_id)
    er.async_get(hass).async_update_entity(ae.entity_id, device_id=b.id)
    await hass.async_block_till_done()
    assert {row.device_id for row in records(hass, entry).values()} == {b.id}
    manager.data = preferences('external:import')
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass, datetime.now(timezone.utc))
    await hass.async_block_till_done()
    fallbacks = [device for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
                 if (DOMAIN, f'{entry.entry_id}_energy') in device.identifiers]
    assert len(fallbacks) == 1
    fallback = fallbacks[0]
    assert {row.device_id for row in records(hass, entry).values()} == {fallback.id}
    assert len(manager._update_listeners) == listener_count
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not association._unsubscribers


@pytest.mark.parametrize('start,following', [
    ('2026-03-28T23:00:00+00:00', '2026-03-29T22:00:00+00:00'),
    ('2026-10-24T22:00:00+00:00', '2026-10-25T23:00:00+00:00'),
])
async def test_native_daily_total_statistics_metadata_reset_and_signed_sum(hass, hass_storage, recorder_mock, freezer, start, following):
    from homeassistant.components.sensor.recorder import list_statistic_ids
    from homeassistant.components.recorder.tasks import StatisticsTask
    from homeassistant.components.recorder.statistics import statistics_during_period
    from custom_components.ha_tools_email.energy import measurement_window
    from custom_components.ha_tools_email.energy_native_short import cutoff_at

    beginning = datetime.fromisoformat(start)
    next_day = datetime.fromisoformat(following)
    freezer.move_to(beginning + timedelta(minutes=1))
    await hass.config.async_set_time_zone('Europe/Warsaw')
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    # This case supplies actual published snapshots to real entities/Recorder.
    # Stop the independent source timer so freezer jumps cannot race synthetic
    # publication with a new no-source refresh. Runtime timer behavior is covered
    # separately in test_energy_runtime.py.
    for handle in ('_unsub', '_boundary_unsub'):
        unsubscribe = getattr(coordinator, handle)
        if unsubscribe is not None:
            unsubscribe()
            setattr(coordinator, handle, None)
    ids = records(hass, entry)
    cost_id, price_id = ids['today_cost'].entity_id, ids['price'].entity_id
    baseline = deepcopy(coordinator.data)
    instance = recorder_mock

    async def publish(instant, value, reset):
        freezer.move_to(instant)
        snapshot = deepcopy(baseline)
        # Publish the current producer contract, not only its legacy hourly alias.
        # Core must see an available TOTAL before it can expose last_reset and
        # compile signed sums. All original reset/sum/unavailable checks remain.
        start, end, aligned = measurement_window(instant, hass.config.time_zone)
        assert aligned and start == reset
        snapshot['generated_at'] = instant.isoformat()
        snapshot['today'].update(usage_cost=value, **{'from': start.isoformat(), 'to': end.isoformat()})
        snapshot['entity_facts'].update(usage_cost=value,
            measurement_from=start.isoformat(), measurement_through=cutoff_at(instant).isoformat(),
            coverage_complete=value is not None,
            reason=None if value is not None else 'measurement_incomplete')
        coordinator.data = snapshot
        for listener in tuple(coordinator._listeners):
            listener()
        await hass.async_block_till_done()
        await _async_recorder_barrier(hass, instance)
        return hass.states.get(cost_id)

    first = await publish(beginning + timedelta(minutes=1), 10, beginning)
    assert first.attributes['state_class'] == 'total'
    assert first.attributes['last_reset'] == beginning.isoformat()
    assert first.attributes['device_class'] == 'monetary'
    assert first.attributes['unit_of_measurement'] == 'EUR'
    price = hass.states.get(price_id)
    assert price.attributes['state_class'] == 'measurement'
    assert price.attributes['unit_of_measurement'] == 'EUR/kWh'
    assert 'device_class' not in price.attributes and 'last_reset' not in price.attributes
    metadata = await instance.async_add_executor_job(list_statistic_ids, hass)
    assert metadata[cost_id]['has_sum'] is True
    assert metadata[cost_id]['unit_of_measurement'] == 'EUR'
    assert metadata[price_id]['has_sum'] is False
    # Compile actual Recorder 5-minute rows, including a signed decline and a
    # new cycle starting below zero. Unknown must not invent another reset.
    for index, (instant, value, reset) in enumerate([
        (beginning + timedelta(minutes=1), 10, beginning),
        (beginning + timedelta(minutes=6), -2, beginning),
        (next_day + timedelta(minutes=1), -1, next_day),
    ]):
        state = await publish(instant, value, reset)
        assert float(state.state) == value
        assert state.attributes['last_reset'] == reset.isoformat()
        period = instant.replace(minute=instant.minute - instant.minute % 5, second=0, microsecond=0)
        freezer.move_to(period + timedelta(minutes=5, seconds=1))
        instance.queue_task(StatisticsTask(period, False))
        await _async_recorder_barrier(hass, instance)
    rows = await instance.async_add_executor_job(partial(statistics_during_period,
        hass, beginning, next_day + timedelta(minutes=5), {cost_id}, '5minute', None,
        {'sum', 'state', 'last_reset'}))
    assert [row['sum'] for row in rows[cost_id]] == pytest.approx([0, -12, -13])
    assert [row['start'] for row in rows[cost_id]] == [beginning.timestamp(),
        (beginning + timedelta(minutes=5)).timestamp(), next_day.timestamp()]
    unknown = await publish(next_day + timedelta(minutes=6), None, next_day)
    assert unknown.state == 'unavailable'
    zero = await publish(next_day + timedelta(minutes=7), 0, next_day)
    assert zero.state == '0'
    assert zero.attributes['last_reset'] == next_day.isoformat()
