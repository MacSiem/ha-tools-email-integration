"""Rolling patterns through real HA Energy configuration, Recorder SQL and WS."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import models
from homeassistant.components.recorder.statistics import async_add_external_statistics

from custom_components.ha_tools_email import analysis_provider
from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.tariff_history import migrate_tariff_v1
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_energy_runtime import profile

pytestmark = pytest.mark.asyncio


@pytest.mark.freeze_time('2026-05-06T00:30:00+00:00')
async def test_native_patterns_grid_projection_and_fresh_policy(hass, hass_storage,
        recorder_mock, freezer, hass_ws_client, hass_read_only_access_token):
    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [{'type': 'grid',
        'flow_from': [{'stat_energy_from': 'patterns:grid'}], 'flow_to': []}],
        'device_consumption': [{'stat_consumption': 'patterns:device', 'name': 'Synthetic device'}]}
    metadata = {'source': 'patterns', 'statistic_id': 'patterns:grid',
        'name': 'Synthetic patterns', 'has_mean': False, 'has_sum': True,
        'unit_of_measurement': 'kWh'}
    if hasattr(models, 'StatisticMeanType'):
        metadata.update(mean_type=models.StatisticMeanType.NONE, unit_class='energy')
    start = datetime(2026, 4, 29, tzinfo=timezone.utc)
    rows = [{'start': start+timedelta(hours=i-1), 'sum': float(i)} for i in range(169)]
    async_add_external_statistics(hass, metadata, rows)
    async_add_external_statistics(hass, {**metadata, 'statistic_id': 'patterns:device'}, rows)
    await recorder_mock.async_block_till_done()
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    await storage.async_save_tariff_book(migrate_tariff_v1(profile(), legacy_combined=False), expected_revision=1)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    request = {'type': f'{DOMAIN}/energy_patterns', 'days': 7}
    with patch.object(analysis_provider, 'async_get_energy_usage',
                      AsyncMock(wraps=analysis_provider.async_get_energy_usage)) as read:
        await client.send_json_auto_id(request)
        response = await client.receive_json()
        assert response['success'], response
        result = response['result']
        assert result['kind'] == 'patterns'
        assert result['context']['request_kind'] == 'patterns'
        assert result['context']['access_purpose'] == 'analysis'
        assert result['window']['first_date'] == '2026-04-29'
        assert result['window']['last_date'] == '2026-05-05'
        assert len(result['patterns']['hours']) == 168
        assert len(result['patterns']['days']) == 7
        assert result['patterns']['complete_days'] == 7
        assert Decimal(result['totals']['kwh']) == 168
        assert result['totals']['energy_cost'] == '168.00'
        assert result['totals']['variable_cost'] == '33.60'
        assert all(Decimal(day['kwh']) == 24 for day in result['patterns']['days'])
        assert sum(Decimal(row['energy_cost']) for row in result['patterns']['hours']) == Decimal('168.00')
        assert 'groups' not in result and 'buckets' not in result
        assert read.await_count == 1
        assert [sensor['entity_id'] for sensor in read.await_args.args[1]] == ['patterns:grid']
        await client.send_json_auto_id({**request, 'days': 7.0})
        invalid = await client.receive_json()
        assert not invalid['success'] and invalid['error']['code'] == 'invalid_patterns_selection'
        await client.send_json_auto_id({**request, 'purpose': 'report_execute'})
        forged = await client.receive_json()
        assert not forged['success'] and forged['error']['code'] == 'invalid_format'
        assert read.await_count == 1
        await storage.async_save_feature_access({'schema': 1, 'features': {
            'energy_analysis': {'read': {'mode': 'admin'}}}}, expected_revision=0)
        await client.send_json_auto_id(request)
        denied = await client.receive_json()
        assert not denied['success'] and denied['error']['code'] == 'unauthorized'
        assert 'result' not in denied and read.await_count == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
