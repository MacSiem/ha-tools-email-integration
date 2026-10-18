"""Detailed analysis through actual HA auth, Energy manager, Recorder SQL and WS."""
import csv
from io import StringIO
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import models
from homeassistant.components.recorder.statistics import async_add_external_statistics

from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.tariff_history import migrate_tariff_v1
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_energy_runtime import profile

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(('missing_source', 'missing_consumer_hour', 'comparison', 'shared_gap'), [
    (False, False, 'previous', False), (True, False, 'previous', False),
    (False, True, 'previous', False), (False, False, 'mean7', False),
    (False, True, 'previous', True)])
@pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')
async def test_native_analysis_selected_comparison_chart_and_policy(hass, hass_storage,
        recorder_mock, freezer, hass_ws_client, hass_read_only_access_token, missing_source, missing_consumer_hour, comparison, shared_gap):
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
    start = datetime(2026, 10, 11 if comparison == 'mean7' else 17, tzinfo=timezone.utc)
    sample_count = 181 if comparison == 'mean7' else 37
    async_add_external_statistics(hass, metadata,
        [{'start': start+timedelta(hours=i-1), 'sum': float(i)} for i in range(sample_count)
         if not shared_gap or i != 4])
    async_add_external_statistics(hass, {**metadata, 'statistic_id': 'analysis:device', 'name': 'Synthetic device'},
        [{'start': start+timedelta(hours=i-1), 'sum': i*.25} for i in range(sample_count)
         if not missing_consumer_hour or i != 4])
    await recorder_mock.async_block_till_done()
    entry, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    # Detailed pricing reads the additive schema2 book. A legacy profile is
    # deliberately not auto-migrated without an explicit semantics decision.
    await storage.async_save_tariff_book(migrate_tariff_v1(profile(), legacy_combined=False), expected_revision=1)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    msg = {'type': f'{DOMAIN}/energy_analysis', 'period': 'day',
           'anchor': '2026-10-18', 'comparison': comparison}
    await client.send_json_auto_id(msg)
    response = await client.receive_json()
    assert response['success'], response
    result = response['result']; compared = result['comparisons'][comparison]
    assert result['period']['hours'] == 12
    assert result['context']['comparison_kind'] == comparison
    assert result['chart']['grain'] == 'hour'
    assert len(result['chart']['bins']) == 12
    if missing_source:
        assert result['coverage']['ratio'] == .5
        assert result['totals']['kwh'] is None
        assert compared['status'] == 'unknown' and compared['delta_kwh'] is None
    else:
        assert Decimal(result['totals']['kwh']) == 12
        assert compared['status'] == 'ready'
        if shared_gap:
            assert (compared['current_kwh'], compared['baseline_kwh'], compared['delta_kwh']) == (11, 12, -1)
        else:
            assert compared['current_kwh'] == compared['baseline_kwh'] == 12
            assert compared['delta_kwh'] == 0
        assert sum(Decimal(row['kwh']) for row in result['chart']['bins']) == 12
        assert sum(Decimal(row['usage_cost']) for row in result['chart']['bins']) == Decimal(result['totals']['usage_cost'])
        if comparison == 'mean7':
            costs = compared['costs']
            assert costs['status'] == 'ready' and costs['delta_cost'] == '0.00'
            assert len(costs['baselines']) == 7
            assert costs['current']['usage_cost'] == costs['baseline']['usage_cost'] == result['totals']['usage_cost']
            groups = compared['groups']
            assert groups['reconciles_household_delta']
            assert groups['reconciles_household_cost_delta']
            assert sum((Decimal(row['delta_usage_cost']) for row in groups['devices'].values()),
                Decimal(groups['unmeasured']['delta_usage_cost'])) == Decimal(costs['delta_cost'])
            group = groups['unassigned']
            assert tuple(Decimal(group[key]) for key in ('current_kwh', 'baseline_kwh', 'delta_kwh')) == (Decimal(3), Decimal(3), Decimal(0))
            assert group['delta_usage_cost'] == '0.00'
            assert group['costs']['baseline']['period_count'] == 7
            assert groups['membership_as_of'] == result['groups']['captured_at']
            assert 'fixed_cost' not in group['costs']['current']
        else:
            group = compared['groups']['unassigned']
        if missing_consumer_hour:
            # SQL has no hour03 statistic; hour04 carries the two-hour meter
            # increment. Match only actual present rows, without interpolation.
            assert compared['baseline_analysis']['groups']['unassigned']['kwh'] is None
            assert group['status'] == 'ready' and group['comparison_scope'] == 'common_measurements'
            assert Decimal(group['current_kwh']) == Decimal('2.75')
            assert Decimal(group['baseline_kwh']) == Decimal('3.00')
            assert Decimal(group['delta_kwh']) == Decimal('-.25')
            assert group['coverage']['common_group_seconds'] == 11*3600
            assert compared['groups']['reconciles_household_delta'] is shared_gap
            assert compared['groups']['reconciles_household_cost_delta'] is shared_gap
            if shared_gap:
                assert Decimal(group['delta_usage_cost']) + Decimal(compared['groups']['unmeasured']['delta_usage_cost']) == Decimal(compared['costs']['delta_cost'])
            assert 'fixed_cost' not in group['costs']['current']
        elif comparison == 'previous':
            assert Decimal(compared['baseline_analysis']['groups']['unassigned']['kwh']) == 3
            assert Decimal(group['delta_kwh']) == 0
            assert compared['groups']['reconciles_household_delta']
        if comparison == 'previous':
            assert group['delta_fixed_cost'] is None
    # Same authenticated household session and cached request after admin change.
    await storage.async_save_feature_access({'schema': 1, 'features': {
        'energy_analysis': {'read': {'mode': 'admin'}}}}, expected_revision=0)
    await client.send_json_auto_id(msg)
    denied = await client.receive_json()
    assert not denied['success'] and denied['error']['code'] == 'unauthorized'
    assert 'result' not in denied
    if not missing_source and not missing_consumer_hour and comparison == 'previous':
        report_msg = {**msg, 'type': f'{DOMAIN}/energy_report', 'language': 'pl',
                      'timezone': 'America/Los_Angeles', 'hour_cycle': '12'}
        await client.send_json_auto_id(report_msg)
        report_denied = await client.receive_json()
        assert not report_denied['success'] and report_denied['error']['code'] == 'unauthorized'
        # Report permission is independent: analysis remains admin-only.
        await storage.async_save_feature_access({'schema': 1, 'features': {
            'energy_analysis': {'read': {'mode': 'admin'}},
            'energy_reports': {'read': {'mode': 'household'}}}}, expected_revision=1)
        await client.send_json_auto_id(report_msg)
        report_response = await client.receive_json()
        assert report_response['success'], report_response
        report = report_response['result']
        assert report['context']['access_purpose'] == 'report_read'
        assert report['display'] == {'language': 'pl', 'timezone': 'America/Los_Angeles', 'hour_cycle': '12'}
        rows = list(csv.DictReader(StringIO(report['csv'])))
        assert all(row['snapshot_id'] == report['snapshot_id'] for row in rows)
        usage = next(row for row in rows if row['field'] == 'totals.kwh')
        assert Decimal(usage['value'].replace(',', '.')) == Decimal(result['totals']['kwh'])
        assert '<table' in report['html'] and '<svg' not in report['html']
        assert report['snapshot_id'] in report['html'] and report['snapshot_id'] in report['text']
        # Display date preference is independent of the Polish labels/numbers.
        await client.send_json_auto_id({**report_msg, 'date_language': 'en'})
        english_dates_response = await client.receive_json()
        assert english_dates_response['success'], english_dates_response
        english_dates = english_dates_response['result']
        assert english_dates['snapshot_id'] == report['snapshot_id']
        assert english_dates['display'] == {**report['display'], 'date_language': 'en'}
        english_rows = {row['field']: row for row in csv.DictReader(StringIO(english_dates['csv']))}
        polish_rows = {row['field']: row for row in rows}
        assert 'Oct' in english_rows['period.start']['value']
        assert 'paź' in polish_rows['period.start']['value']
        assert english_rows['totals.kwh']['value'] == polish_rows['totals.kwh']['value']
        await client.send_json_auto_id({**report_msg, 'date_language': 'de'})
        invalid_dates = await client.receive_json()
        assert not invalid_dates['success'] and invalid_dates['error']['code'] == 'invalid_format'
        assert 'result' not in invalid_dates
        await storage.async_save_feature_access({'schema': 1, 'features': {
            'energy_reports': {'read': {'mode': 'admin'}}}}, expected_revision=2)
        await client.send_json_auto_id(report_msg)
        denied_report = await client.receive_json()
        assert not denied_report['success'] and denied_report['error']['code'] == 'unauthorized'
        assert 'result' not in denied_report
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_native_household_dedicated_updates_are_empty_and_general_events_denied(
        hass, hass_storage, recorder_mock, hass_ws_client, hass_read_only_access_token):
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [{'type': 'grid', 'flow_from': [{'stat_energy_from': 'analysis:grid'}], 'flow_to': []}], 'device_consumption': []}
    entry, storage = await _setup(hass)
    service = hass.data[DOMAIN]['energy_analysis']
    client = await hass_ws_client(hass, hass_read_only_access_token)
    events = (f'{DOMAIN}_energy_analysis_updated', f'{DOMAIN}_feature_access_updated')
    for event in events:
        await client.send_json_auto_id({'type': 'subscribe_events', 'event_type': event})
        denied = await client.receive_json()
        assert not denied['success'] and denied['error']['code'] == 'unauthorized'
    await client.send_json_auto_id({'type': f'{DOMAIN}/subscribe_analysis_updates'})
    ack = await client.receive_json()
    assert ack['success'], ack
    assert len(service._update_subscriptions) == 1
    # Revocation does not suppress this control signal; actual reads still deny.
    await storage.async_save_feature_access({'schema': 1, 'features': {
        'energy_analysis': {'read': {'mode': 'admin'}}}}, expected_revision=0)
    for event in events:
        hass.bus.async_fire(event, {'private': {'user_id': 'secret', 'policy': 'private'}, 'entity_id': 'sensor.secret'})
        delivered = await client.receive_json()
        assert delivered == {'id': ack['id'], 'type': 'event', 'event': {}}
    await client.send_json_auto_id({'type': f'{DOMAIN}/energy_analysis', 'period': 'day'})
    denied = await client.receive_json()
    assert not denied['success'] and denied['error']['code'] == 'unauthorized'
    await client.send_json_auto_id({'type': 'unsubscribe_events', 'subscription': ack['id']})
    assert (await client.receive_json())['success']
    assert not service._update_subscriptions
    await client.send_json_auto_id({'type': f'{DOMAIN}/subscribe_analysis_updates'})
    assert (await client.receive_json())['success']
    assert len(service._update_subscriptions) == 1
    await client.close()
    await hass.async_block_till_done()
    assert not service._update_subscriptions
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({'type': f'{DOMAIN}/subscribe_analysis_updates'})
    assert (await client.receive_json())['success']
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not service._update_subscriptions
    # Core can subsequently unsubscribe/close an already stopped listener.
    await client.close()
    await hass.async_block_till_done()
    assert not service._update_subscriptions


async def test_native_dedicated_updates_recheck_initial_active_user(
        hass, hass_storage, recorder_mock, hass_ws_client, hass_read_only_access_token, hass_read_only_user):
    entry, _ = await _setup(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    try:
        # Keep the connection's earlier active identity while the fresh lookup
        # returns an inactive user; exercise the command's own fresh-user gate.
        inactive = SimpleNamespace(id=hass_read_only_user.id, is_active=False)
        with patch.object(hass.auth, 'async_get_user', AsyncMock(return_value=inactive)) as lookup:
            await client.send_json_auto_id({'type': f'{DOMAIN}/subscribe_analysis_updates'})
            denied = await client.receive_json()
            assert not denied['success'] and denied['error']['code'] == 'unauthorized'
            lookup.assert_awaited_once_with(hass_read_only_user.id)
        assert not hass.data[DOMAIN]['energy_analysis']._update_subscriptions
    finally:
        await client.close()
        await client.client.close()
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
