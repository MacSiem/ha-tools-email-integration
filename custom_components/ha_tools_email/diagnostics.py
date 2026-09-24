"""Diagnostics for HA Tools Email (secrets and addresses redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DATA_STORAGE, DOMAIN, VERSION

TO_REDACT = {
    "password",
    "username",
    "sender",
    "default_recipient",
    "recipients",
    "server",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for the config entry."""
    storage = hass.data.get(DOMAIN, {}).get(DATA_STORAGE)
    state: dict[str, Any] = await storage.async_get_state() if storage else {}
    smtp = dict(state.get("smtp_config") or {})
    password = str(smtp.get("password", ""))
    smtp["password_kind"] = (
        "secrets_yaml" if password.startswith("!secret ") else ("literal" if password else "missing")
    )
    return {
        "version": VERSION,
        "smtp_config": async_redact_data(smtp, TO_REDACT),
        "security": state.get("security", {}),
        "schedules": async_redact_data(state.get("schedules", []), TO_REDACT),
        "scheduler_state": state.get("scheduler_state", {}),
    }
