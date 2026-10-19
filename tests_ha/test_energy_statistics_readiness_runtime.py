"""Real short-term sensor statistics become an hour after the initial timer.

Break caught: runtime discards a source-not-ready hour after target unknown ACK.
No preimported 168-hour fixture and no substituted producer/SQL adapter.
"""
import asyncio
from datetime import datetime,timedelta,timezone
from functools import partial

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.tasks import StatisticsTask
from homeassistant.helpers import entity_registry as er,area_registry as ar
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.tariff_history import migrate_tariff_v1
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_energy_runtime import profile
from tests_ha.test_energy_entity_statistics import _async_recorder_barrier
from tests_ha.test_energy_statistics_publisher_runtime import command

pytestmark=pytest.mark.asyncio
UTC=timezone.utc
END=datetime(2026,10,19,12,tzinfo=UTC)


async def drain(runtime):
    while runtime._lane['task'] is not None:
        await runtime._lane['task']
        await asyncio.sleep(0)


@pytest.mark.parametrize('compile_delay',[10,3610])
@pytest.mark.freeze_time('2026-10-19T10:55:01+00:00')
async def test_real_hour_compile_after_boundary_or_next_hour_is_automatically_retried(
        hass,hass_storage,recorder_mock,hass_ws_client,hass_admin_user,freezer,compile_delay):
    await hass.config.async_set_time_zone('UTC')
    entry,storage=await _setup(hass)
    await storage.async_save_tariff_book(migrate_tariff_v1(profile(),legacy_combined=False),expected_revision=0)
    area=ar.async_get(hass).async_create('Readiness')
    registry=er.async_get(hass)
    grid=registry.async_get_or_create('sensor','test','readiness_grid',suggested_object_id='readiness_grid')
    consumer=registry.async_get_or_create('sensor','test','readiness_consumer',suggested_object_id='readiness_consumer')
    registry.async_update_entity(consumer.entity_id,area_id=area.id)
    attributes={'unit_of_measurement':'kWh','device_class':'energy','state_class':'total_increasing'}
    for entity in (grid,consumer):hass.states.async_set(entity.entity_id,0,attributes)
    await hass.async_block_till_done();await _async_recorder_barrier(hass,recorder_mock)
    # First genuine sensor sum establishes a previous-hour baseline.
    freezer.move_to(END-timedelta(hours=1)+timedelta(seconds=10))
    recorder_mock.queue_task(StatisticsTask(END-timedelta(hours=1,minutes=5),False))
    await _async_recorder_barrier(hass,recorder_mock)
    freezer.move_to(END-timedelta(minutes=4,seconds=59))
    hass.states.async_set(grid.entity_id,1,attributes)
    hass.states.async_set(consumer.entity_id,.25,attributes)
    await hass.async_block_till_done();await _async_recorder_barrier(hass,recorder_mock)
    manager=await async_get_manager(hass)
    manager.data={'energy_sources':[{'type':'grid','flow_from':[{'stat_energy_from':grid.entity_id}],'flow_to':[]}],
        'device_consumption':[{'stat_consumption':consumer.entity_id}]}
    freezer.move_to(END)
    # A fixture token minted before the >1h frozen-clock jump has expired.
    refresh_token=await hass.auth.async_create_refresh_token(hass_admin_user)
    client=await hass_ws_client(hass,hass.auth.async_create_access_token(refresh_token))
    prepared=await command(client,'prepare');assert prepared['success'],prepared
    enabled=await command(client,'enable',proposal=prepared['result']['proposal'],expected_revision=prepared['result']['revision'],
        acknowledge_global=True,acknowledge_current_membership=True)
    assert enabled['success'],enabled
    runtime=hass.data[DOMAIN]['energy_statistics_runtime']
    await drain(runtime)
    publisher=hass.data[DOMAIN]['energy_statistics']
    assert publisher._state['runtime']['next_end']==int(END.timestamp()*1000)
    assert publisher._state['runtime']['last_end'] is None
    assert not publisher._state['series']
    # Compile the real final short-term interval and native hourly summary late.
    freezer.move_to(END+timedelta(seconds=compile_delay))
    recorder_mock.queue_task(StatisticsTask(END-timedelta(minutes=5),False))
    await _async_recorder_barrier(hass,recorder_mock)
    source=await recorder_mock.async_add_executor_job(partial(statistics_during_period,
        hass,END-timedelta(hours=1),END,{grid.entity_id,consumer.entity_id},'hour',None,{'sum','change'}))
    assert source[grid.entity_id][-1]['change']==pytest.approx(1)
    assert source[consumer.entity_id][-1]['change']==pytest.approx(.25)
    freezer.move_to(END+timedelta(seconds=compile_delay+5))
    async_fire_time_changed(hass,datetime.now(UTC))
    await hass.async_block_till_done();await drain(runtime)
    await _async_recorder_barrier(hass,recorder_mock)
    # A public barrier can need one content-readback retry after its first commit.
    if publisher._state['pending'] is not None:
        freezer.move_to(datetime.now(UTC)+timedelta(seconds=15))
        async_fire_time_changed(hass,datetime.now(UTC))
        await hass.async_block_till_done();await drain(runtime)
    sid=next(sid for sid,row in publisher._state['series'].items() if row['identity']['kind']=='area')
    result=await recorder_mock.async_add_executor_job(partial(statistics_during_period,
        hass,END-timedelta(hours=1),END,{sid},'hour',None,{'sum','change'}))
    assert len(result[sid])==1
    assert result[sid][0]['sum']==pytest.approx(.3)
    assert publisher._state['runtime']['last_end']>=int(END.timestamp()*1000)
    await client.close()
    assert await hass.config_entries.async_unload(entry.entry_id)
