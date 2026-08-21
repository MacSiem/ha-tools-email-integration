"""WebSocket API for HA Tools Email."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN, EVENT_SCHEDULES_CHANGED, VALID_CADENCES, VALID_KINDS
from .scheduler import async_reload_schedules, async_send_now
from .storage import EmailStorage


def _bucket(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data[DOMAIN]


def _storage(hass: HomeAssistant) -> EmailStorage:
    return _bucket(hass)["storage"]


async def _safe_smtp_config(hass: HomeAssistant) -> dict[str, Any]:
    cfg = await _bucket(hass)["load_config"]()
    raw_password = cfg.get("password", "")
    return {
        "server": cfg.get("server", ""),
        "port": cfg.get("port", 587),
        "username": cfg.get("username", ""),
        "sender": cfg.get("sender", ""),
        "encryption": cfg.get("encryption", "starttls"),
        "default_recipient": cfg.get("default_recipient", ""),
        "uses_secret": str(raw_password).startswith("!secret "),
        "smtp_configured": bool(
            cfg.get("server") and cfg.get("username") and raw_password
        ),
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_config"})
@websocket_api.async_response
async def _ws_get_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return non-secret SMTP state and schedules."""
    config = await _safe_smtp_config(hass)
    config["schedules"] = await _storage(hass).async_list_schedules()
    connection.send_result(msg["id"], config)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list_schedules"})
@websocket_api.async_response
async def _ws_list_schedules(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return configured schedules."""
    connection.send_result(
        msg["id"], {"schedules": await _storage(hass).async_list_schedules()}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_schedule",
        vol.Optional("action", default="upsert"): vol.In(["create", "update", "upsert", "delete"]),
        vol.Optional("schedule"): dict,
        vol.Optional("schedule_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_set_schedule(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create, update, or delete a schedule."""
    storage = _storage(hass)
    action = msg.get("action", "upsert")
    try:
        if action == "delete":
            schedule_id = msg.get("schedule_id") or (msg.get("schedule") or {}).get("id")
            if not schedule_id:
                raise ValueError("schedule_id is required for delete")
            removed = await storage.async_delete_schedule(schedule_id)
            result = {"deleted": removed, "schedule_id": schedule_id}
        else:
            schedule = msg.get("schedule")
            if not isinstance(schedule, dict):
                raise ValueError("schedule object is required")
            saved = await storage.async_upsert_schedule(schedule)
            result = {"schedule": saved}
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_payload", str(err))
        return

    await async_reload_schedules(hass)
    schedules = await storage.async_list_schedules()
    hass.bus.async_fire(EVENT_SCHEDULES_CHANGED, {"schedules": schedules})
    result["schedules"] = schedules
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/send_now",
        vol.Required("kind"): vol.In(sorted(VALID_KINDS)),
        vol.Required("cadence"): vol.In(sorted(VALID_CADENCES)),
        vol.Optional("recipients", default=[]): vol.Any([str], str),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_send_now(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Compose and send a report immediately."""
    recipients = msg.get("recipients") or []
    if isinstance(recipients, str):
        recipients = [item.strip() for item in recipients.split(",") if item.strip()]
    try:
        result = await async_send_now(
            hass,
            kind=msg["kind"],
            cadence=msg["cadence"],
            recipients=recipients,
        )
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "send_failed", str(err))
        return
    connection.send_result(msg["id"], result)


def async_register_commands(hass: HomeAssistant) -> None:
    """Register all websocket commands."""
    for handler in (
        _ws_get_config,
        _ws_list_schedules,
        _ws_set_schedule,
        _ws_send_now,
    ):
        websocket_api.async_register_command(hass, handler)
