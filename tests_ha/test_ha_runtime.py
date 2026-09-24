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
    for command in ("get_config", "list_schedules"):
        await client.send_json_auto_id({"type": f"{DOMAIN}/{command}"})
        response = await client.receive_json()
        assert response["success"] is False
        assert response["error"]["code"] == "unauthorized"
    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id({"type": f"{DOMAIN}/get_config"})
    response = await admin.receive_json()
    assert response["success"] is True
    assert "password" not in response["result"]


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
        await hass.async_block_till_done()
        async_fire_time_changed(hass, fire_at + dt.timedelta(seconds=1))
        await hass.async_block_till_done()
    assert send.call_count == 1
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/set_schedule", "action": "upsert",
                                    "schedule": {"kind": "energy_report", "cadence": "weekly", "time": "08:00",
                                                 "recipients": ["family@example.com"], "enabled": True}})
    response = await client.receive_json()
    assert response["success"] is True, response
    assert len(response["result"]["schedules"]) == 2
