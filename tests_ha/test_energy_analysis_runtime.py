"""Detailed analysis through actual HA auth, Energy manager, Recorder SQL and WS."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import models
from homeassistant.components.recorder.statistics import async_add_external_statistics

from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.tariff_history import migrate_tariff_v1
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_energy_runtime import profile

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('missing_source', [False, True])
@pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')
async def test_native_analysis_selected_comparison_chart_and_policy(hass, hass_storage,
        recorder_mock, freezer, hass_ws_client, hass_read_only_access_token, missing_source):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    grid = [{'stat_energy_from': 'analysis:grid'}]
    if missing_source:
        grid.append({'stat_energy_from': 'analysis:missing'})
    manager.data = {'energy_sources': [{'type': 'grid', 'flow_from': grid, 'flow_to': []}],
                    'device_consumption': [{'stat_consumption': 'analysis:device', 'name': 'Synthetic device'}]}
    metadata = {'source': 'analysis', 'statistic_id': 'analysis:grid', 'name': 'Synthetic analysis',
                'has_mean': False, 'has_sum': True, 'unit_of_measurement': 'kWh'}
    if hasattr(models, 'StatisticMeanType'):
        metadata.update(mean_type=models.StatisticMeanType.NONE, unit_class='energy')
    start = datetime(2026, 10, 17, tzinfo=timezone.utc)
    async_add_external_statistics(hass, metadata,
        [{'start': start+timedelta(hours=i-1), 'sum': float(i)} for i in range(37)])
    async_add_external_statistics(hass, {**metadata, 'statistic_id': 'analysis:device', 'name': 'Synthetic device'},
        [{'start': start+timedelta(hours=i-1), 'sum': i*.25} for i in range(37)])
    await recorder_mock.async_block_till_done()
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    # Detailed pricing reads the additive schema2 book. A legacy profile is
    # deliberately not auto-migrated without an explicit semantics decision.
    await storage.async_save_tariff_book(migrate_tariff_v1(profile(), legacy_combined=False), expected_revision=1)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    msg = {'type': f'{DOMAIN}/energy_analysis', 'period': 'day',
           'anchor': '2026-10-18', 'comparison': 'previous'}
    await client.send_json_auto_id(msg)
    response = await client.receive_json()
    assert response['success'], response
    result = response['result']; compared = result['comparisons']['previous']
    assert result['period']['hours'] == 12
    assert result['context']['comparison_kind'] == 'previous'
    assert result['chart']['grain'] == 'hour'
    assert len(result['chart']['bins']) == 12
    if missing_source:
        assert result['coverage']['ratio'] == .5
        assert result['totals']['kwh'] is None
        assert compared['status'] == 'unknown' and compared['delta_kwh'] is None
    else:
        assert Decimal(result['totals']['kwh']) == 12
        assert compared['status'] == 'ready'
        assert compared['current_kwh'] == compared['baseline_kwh'] == 12
        assert compared['delta_kwh'] == 0
        assert sum(Decimal(row['kwh']) for row in result['chart']['bins']) == 12
        assert sum(Decimal(row['usage_cost']) for row in result['chart']['bins']) == Decimal(result['totals']['usage_cost'])
        assert Decimal(compared['baseline_analysis']['groups']['unassigned']['kwh']) == 3
        assert Decimal(compared['groups']['unassigned']['delta_kwh']) == 0
        assert compared['groups']['reconciles_household_delta']
        assert compared['groups']['unassigned']['delta_fixed_cost'] is None
    # Same authenticated household session and cached request after admin change.
    await storage.async_save_feature_access({'schema': 1, 'features': {
        'energy_analysis': {'read': {'mode': 'admin'}}}}, expected_revision=0)
    await client.send_json_auto_id(msg)
    denied = await client.receive_json()
    assert not denied['success'] and denied['error']['code'] == 'unauthorized'
    assert 'result' not in denied
    assert await hass.config_entries.async_unload(entry.entry_id)
