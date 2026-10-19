"""Report status through native HA websocket schema/auth and actual Store."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from custom_components.ha_tools_email.const import DOMAIN
from tests_ha.test_ha_runtime import _setup

pytestmark = pytest.mark.asyncio


async def test_native_status_registration_no_smtp_and_strict_schema(hass, hass_storage, hass_ws_client):
    entry, storage = await _setup(hass)
    client = await hass_ws_client(hass)
    before = await storage.async_get_state()
    with patch.object(storage, 'async_get_smtp_config', side_effect=AssertionError('No SMTP read')):
        for purpose in ('energy_report', 'log_digest'):
            await client.send_json_auto_id({'type': f'{DOMAIN}/get_report_status', 'purpose': purpose})
            response = await client.receive_json()
            assert response['success'], response
            result = response['result']
            assert set(result) == {'schema', 'purpose', 'generated_at', 'schedule_state',
                                   'next_report_at', 'history_state', 'last_attempt'}
            assert result['purpose'] == purpose
            assert result['schedule_state'] == 'unconfigured'
            assert result['next_report_at'] is None and result['last_attempt'] is None
    assert await storage.async_get_state() == before
    await client.send_json_auto_id({'type': f'{DOMAIN}/get_report_status', 'purpose': 'manual'})
    assert not (await client.receive_json())['success']


async def test_native_household_status_requires_per_purpose_delegation(hass, hass_storage, hass_ws_client,
                                                                    hass_read_only_access_token):
    entry, storage = await _setup(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({'type': f'{DOMAIN}/get_report_status', 'purpose': 'energy_report'})
    denied = await client.receive_json()
    assert not denied['success'] and denied['error']['code'] == 'unauthorized'
    await storage.async_save_feature_access({'schema': 1, 'features': {
        'energy_reports': {'read': {'mode': 'household'}}}}, expected_revision=0)
    await client.send_json_auto_id({'type': f'{DOMAIN}/get_report_status', 'purpose': 'energy_report'})
    assert (await client.receive_json())['success']
    await client.send_json_auto_id({'type': f'{DOMAIN}/get_report_status', 'purpose': 'log_digest'})
    assert not (await client.receive_json())['success']


@pytest.mark.parametrize('mutation', ['unload', 'policy', 'schedule'])
async def test_native_status_rejects_changes_during_final_authorization(hass, hass_storage, hass_ws_client, mutation):
    entry, storage = await _setup(hass)
    client = await hass_ws_client(hass)
    original_read, original_auth = storage.async_get_report_status, hass.auth.async_get_user
    armed = False
    async def read(*args, **kwargs):
        nonlocal armed
        result = await original_read(*args, **kwargs)
        armed = True
        return result
    async def user(uid):
        nonlocal armed
        result = await original_auth(uid)
        if armed:
            armed = False
            if mutation == 'unload':
                assert await hass.config_entries.async_unload(entry.entry_id)
            elif mutation == 'policy':
                await storage.async_save_feature_access({'schema': 1, 'features': {}}, expected_revision=0)
            else:
                await storage.async_upsert_schedule({'kind': 'energy_report', 'cadence': 'daily',
                    'time': '07:00', 'enabled': False}, author_user_id=uid)
        return result
    with patch.object(storage, 'async_get_report_status', new=read), patch.object(hass.auth, 'async_get_user', new=user):
        await client.send_json_auto_id({'type': f'{DOMAIN}/get_report_status', 'purpose': 'energy_report'})
        response = await client.receive_json()
    assert not response['success'], response
    assert response['error']['code'] in ('unauthorized', 'report_status_unavailable')
