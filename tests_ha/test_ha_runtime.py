"""HA Tools Email inside a real Home Assistant core (2.1.0 security and options)."""

from __future__ import annotations

import smtplib
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_tools_email.const import (
    DATA_STORAGE,
    DOMAIN,
    ISSUE_PASSWORD_IN_HISTORY,
    STORAGE_KEY,
)
from custom_components.ha_tools_email.diagnostics import (
    async_get_config_entry_diagnostics,
)

SMTP_FORM = {
    "server": "smtp.example.com",
    "port": 587,
    "encryption": "starttls",
    "username": "user@example.com",
    "sender": "user@example.com",
    "default_recipient": "family@example.com",
}


async def _setup(hass, stored=None, hass_storage=None):
    if stored is not None:
        hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": stored}
    entry = MockConfigEntry(domain=DOMAIN, title="HA Tools Email", unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, hass.data[DOMAIN][DATA_STORAGE]


async def _options(hass, entry, user_input):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    return result, await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def test_options_flow_stores_password_in_store_not_entry(hass, hass_storage):
    entry, storage = await _setup(hass)
    form, result = await _options(hass, entry, {**SMTP_FORM, "password": "CANARY-OPTIONS-1"})
    assert form["description_placeholders"]["password_state"] == "not set"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {}
    cfg = await storage.async_get_smtp_config()
    assert cfg["password"] == "CANARY-OPTIONS-1"
    assert cfg["server"] == "smtp.example.com"
    assert await storage.async_password_via_options() is True
    assert "CANARY-OPTIONS-1" not in str(entry.as_dict())


async def test_blank_password_keeps_existing_and_form_never_echoes_it(hass, hass_storage):
    entry, storage = await _setup(hass)
    await _options(hass, entry, {**SMTP_FORM, "password": "KEEP-ME"})
    form, result = await _options(hass, entry, {**SMTP_FORM, "server": "smtp2.example.com"})
    assert form["description_placeholders"]["password_state"] == "set"
    assert "KEEP-ME" not in str(form["data_schema"].schema)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    cfg = await storage.async_get_smtp_config()
    assert cfg["password"] == "KEEP-ME"
    assert cfg["server"] == "smtp2.example.com"


@pytest.mark.parametrize(('send_test', 'error'), [(False, 'save_failed'), (True, 'save_failed_after_test')])
async def test_options_write_failure_keeps_form_and_does_not_repeat_test(hass, hass_storage, send_test, error):
    from homeassistant.helpers.storage import Store
    from homeassistant.util.file import WriteError
    entry, storage = await _setup(hass)
    with patch.object(Store, '_async_write_data', side_effect=WriteError('synthetic')), \
         patch('custom_components.ha_tools_email._send_email') as send:
        _, result = await _options(hass, entry, {**SMTP_FORM, 'password': 'SYNTHETIC', 'send_test': send_test})
    assert result['type'] is FlowResultType.FORM
    assert result['errors'] == {'base': error}
    assert send.call_count == int(send_test)
    assert (await storage.async_get_smtp_config())['password'] == ''
    assert not await storage.async_password_via_options()
    assert entry.options == {}
    marker = next(k for k in result['data_schema'].schema if k.schema == 'send_test')
    assert marker.default() is False


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "invalid_auth"),
        (OSError("unreachable"), "cannot_connect"),
    ],
)
async def test_send_test_failure_is_a_form_error_and_saves_nothing(hass, hass_storage, side_effect, error):
    entry, storage = await _setup(hass)
    with patch("custom_components.ha_tools_email._send_email", side_effect=side_effect):
        _, result = await _options(hass, entry, {**SMTP_FORM, "password": "pw", "send_test": True})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert (await storage.async_get_smtp_config())["password"] == ""


async def test_send_test_success_sends_to_default_recipient(hass, hass_storage):
    entry, storage = await _setup(hass)
    with patch("custom_components.ha_tools_email._send_email") as send:
        _, result = await _options(hass, entry, {**SMTP_FORM, "password": "pw", "send_test": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert send.call_args.args[2] == "family@example.com"


async def test_invalid_sender_is_rejected(hass, hass_storage):
    entry, _ = await _setup(hass)
    _, result = await _options(hass, entry, {**SMTP_FORM, "sender": "not-an-address", "password": "pw"})
    assert result["errors"] == {"sender": "invalid_email"}


async def test_save_config_rejects_password_and_stores_nothing(hass, hass_storage):
    _, storage = await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "save_config", {**SMTP_FORM, "password": "CANARY-ACTION"}, blocking=True
        )
    assert (await storage.async_get_smtp_config())["password"] == ""


async def test_save_config_accepts_known_secret_and_keeps_password_otherwise(hass, hass_storage, tmp_path):
    _, storage = await _setup(hass)
    with open(hass.config.path("secrets.yaml"), "w", encoding="utf-8") as handle:
        handle.write("smtp_app_password: from-secrets\n")
    await hass.services.async_call(
        DOMAIN, "save_config", {**SMTP_FORM, "password_secret": "smtp_app_password"}, blocking=True
    )
    assert (await storage.async_get_smtp_config())["password"] == "!secret smtp_app_password"
    await hass.services.async_call(DOMAIN, "save_config", {**SMTP_FORM, "server": "s3.example.com"}, blocking=True)
    cfg = await storage.async_get_smtp_config()
    assert cfg["password"] == "!secret smtp_app_password"
    assert cfg["server"] == "s3.example.com"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "save_config", {**SMTP_FORM, "password_secret": "missing"}, blocking=True
        )


async def test_repair_for_literal_password_saved_before_2_1_0_and_cleared_after_options(hass, hass_storage):
    legacy = {"smtp_config": {**SMTP_FORM, "password": "OLD-LITERAL"}, "schedules": []}
    entry, _ = await _setup(hass, legacy, hass_storage)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, ISSUE_PASSWORD_IN_HISTORY) is not None
    await _options(hass, entry, {**SMTP_FORM, "password": "NEW-PASSWORD"})
    assert registry.async_get_issue(DOMAIN, ISSUE_PASSWORD_IN_HISTORY) is None


async def test_no_repair_for_secret_reference(hass, hass_storage):
    stored = {"smtp_config": {**SMTP_FORM, "password": "!secret smtp"}, "schedules": []}
    await _setup(hass, stored, hass_storage)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_PASSWORD_IN_HISTORY) is None


async def test_websocket_reads_require_admin(hass, hass_storage, hass_ws_client, hass_read_only_access_token):
    await _setup(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    for command in ("get_config", "list_schedules", "preview_energy_report"):
        payload = {"type": f"{DOMAIN}/{command}"}
        if command == "preview_energy_report":
            payload["cadence"] = "daily"
        await client.send_json_auto_id(payload)
        response = await client.receive_json()
        assert response["success"] is False
        assert response["error"]["code"] == "unauthorized"
    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id({"type": f"{DOMAIN}/get_config"})
    response = await admin.receive_json()
    assert response["success"] is True
    assert "password" not in response["result"]


@pytest.mark.freeze_time('2026-10-18T12:30:00+00:00')
async def test_admin_energy_preview_does_not_send_or_change_schedules(
        hass, hass_storage, hass_ws_client, recorder_mock):
    """Actual configured Energy manager and Recorder, real WS/auth/report path."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder import models
    from homeassistant.components.recorder.statistics import async_add_external_statistics
    from custom_components.ha_tools_email.tariff_history import migrate_tariff_v1
    from tests_ha.test_energy_runtime import profile

    await hass.config.async_set_time_zone('UTC')
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [{'type': 'grid',
        'flow_from': [{'stat_energy_from': 'preview72:grid'}], 'flow_to': []}],
        'device_consumption': []}
    metadata = {'source': 'preview72', 'statistic_id': 'preview72:grid',
        'name': 'Preview72 configured grid', 'has_mean': False, 'has_sum': True,
        'unit_of_measurement': 'kWh'}
    if hasattr(models, 'StatisticMeanType'):
        metadata.update(mean_type=models.StatisticMeanType.NONE, unit_class='energy')
    start = datetime(2026, 10, 17, 11, tzinfo=timezone.utc)
    async_add_external_statistics(hass, metadata,
        [{'start': start + timedelta(hours=i), 'sum': float(i)} for i in range(26)])
    await recorder_mock.async_block_till_done()
    _, storage = await _setup(hass)
    await storage.async_save_tariff(profile(), expected_revision=0)
    await storage.async_save_tariff_book(migrate_tariff_v1(profile(), legacy_combined=False), expected_revision=1)
    schedules = await storage.async_list_schedules()
    history = await storage.async_get_attempt_history()
    client = await hass_ws_client(hass)
    service = hass.data[DOMAIN]['energy_analysis']
    with patch('custom_components.ha_tools_email.scheduler.async_send_report') as send, \
         patch.object(storage, 'async_get_smtp_config', side_effect=AssertionError('preview SMTP config')) as smtp, \
         patch.object(service, 'async_prepare_report', wraps=service.async_prepare_report) as prepared:
        await client.send_json_auto_id({'type': f'{DOMAIN}/preview_energy_report', 'cadence': 'daily'})
        response = await client.receive_json()
    assert response['success'] is True
    result = response['result']
    assert result['status'] == 'ready'
    assert Decimal(result['total_kwh']) == Decimal('24')
    assert result['total_cost'] == result['totals']['total_cost']
    assert result['period']['cadence'] == 'daily'
    assert result['period']['hours'] == 24
    assert result['period']['semantics'] == 'rolling_completed_hours'
    assert result['snapshot_id']
    assert prepared.await_count == 1
    assert prepared.await_args.kwargs['purpose'] == 'report_read'
    assert await storage.async_list_schedules() == schedules
    assert await storage.async_get_attempt_history() == history
    send.assert_not_called()
    smtp.assert_not_called()


async def test_admin_unconfigured_preview_has_safe_error_without_private_io(
        hass, hass_storage, hass_ws_client):
    """No grid must fail closed before Recorder/SMTP, with a readable WS error.

    This also exposes the current missing safe-error mapping in legacy preview;
    keep it red until the production owner repairs the handler separately.
    """
    from homeassistant.components.energy.data import async_get_manager
    manager = await async_get_manager(hass)
    manager.data = {'energy_sources': [], 'device_consumption': []}
    _, storage = await _setup(hass)
    schedules = await storage.async_list_schedules()
    client = await hass_ws_client(hass)
    with patch('custom_components.ha_tools_email.analysis_service.async_get_energy_metadata',
               side_effect=AssertionError('unconfigured private metadata')) as metadata, \
         patch('custom_components.ha_tools_email.analysis_provider.async_get_energy_usage',
               side_effect=AssertionError('unconfigured Recorder')) as recorder, \
         patch.object(storage, 'async_get_smtp_config', side_effect=AssertionError('unconfigured SMTP')) as smtp, \
         patch('custom_components.ha_tools_email.scheduler.async_send_report') as send:
        await client.send_json_auto_id({'type': f'{DOMAIN}/preview_energy_report', 'cadence': 'daily'})
        response = await client.receive_json()
    assert response['success'] is False
    assert response['error']['code'] == 'energy_report_unavailable'
    assert 'energy_analysis_unavailable' not in response['error']['message']
    assert await storage.async_list_schedules() == schedules
    for boundary in (metadata, recorder, smtp, send):
        boundary.assert_not_called()


async def test_diagnostics_are_redacted(hass, hass_storage):
    stored = {
        "smtp_config": {**SMTP_FORM, "password": "DIAG-SECRET"},
        "schedules": [{"id": "a", "kind": "log_digest", "cadence": "daily", "time": "07:30", "recipients": ["x@example.com"], "enabled": True}],
    }
    entry, _ = await _setup(hass, stored, hass_storage)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in ("DIAG-SECRET", "user@example.com", "family@example.com", "x@example.com", "smtp.example.com"):
        assert secret not in text
    assert diag["smtp_config"]["password_kind"] == "literal"


async def test_single_config_entry_and_unload(hass, hass_storage):
    entry, _ = await _setup(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_stored_schedule_loads_fires_once_per_period(hass, hass_storage, hass_ws_client):
    """Regression: a stored schedule used to crash setup (async_track_time_change(local=...))."""
    import datetime as dt

    import homeassistant.util.dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    stored = {
        "smtp_config": {**SMTP_FORM, "password": "!secret smtp"},
        "schedules": [{"id": "daily-log", "kind": "log_digest", "cadence": "daily", "time": "07:30",
                        "recipients": ["family@example.com"], "enabled": True}],
    }
    with patch("custom_components.ha_tools_email.scheduler.async_send_schedule") as send:
        entry, storage = await _setup(hass, stored, hass_storage)
        now = dt_util.now()
        fire_at = now.replace(hour=7, minute=30, second=0, microsecond=0) + dt.timedelta(days=1)
        async_fire_time_changed(hass, fire_at)
        # Timer callbacks and their executor-backed storage reads can be HA
        # background tasks; the default wait excludes them on older Core.
        await hass.async_block_till_done(wait_background_tasks=True)
        async_fire_time_changed(hass, fire_at + dt.timedelta(seconds=1))
        await hass.async_block_till_done(wait_background_tasks=True)
    assert send.call_count == 1
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/set_schedule", "action": "upsert",
                                    "schedule": {"kind": "energy_report", "cadence": "weekly", "time": "08:00",
                                                 "recipients": ["family@example.com"], "enabled": True}})
    response = await client.receive_json()
    assert response["success"] is True, response
    assert len(response["result"]["schedules"]) == 2
