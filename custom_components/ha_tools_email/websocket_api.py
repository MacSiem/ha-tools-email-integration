"""WebSocket API for HA Tools Email."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN, EVENT_SCHEDULES_CHANGED, VALID_CADENCES, VALID_KINDS
from .composer import (async_build_energy_report_payload, async_build_report_payload,
                       async_get_system_log_entries, build_log_digest_payload)
from .scheduler import (async_reload_schedules, async_send_now, calculate_next_fire,
                        calendar_report_window, effective_schedule_timezone, fire_period_key,
                        normalize_schedule_calendar, async_resolve_manual_period)
from .storage import EmailStorage, TariffRevisionConflict
from .tariff import TariffValidationError


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


async def _snapshot_authorized(hass, connection):
    """Recheck the authenticated active household user across asynchronous IO."""
    user = getattr(connection, 'user', None)
    if user is None or not getattr(user, 'is_active', False):
        return False
    current = await hass.auth.async_get_user(user.id)
    return current is not None and current.is_active


def _energy_context_current(hass, coordinator, result):
    """No await between this lifecycle/context guard and the result send."""
    if hass.data.get(DOMAIN, {}).get('energy_snapshot') is not coordinator:
        return False
    if not getattr(coordinator, 'running', not getattr(coordinator, '_stopped', False)):
        return False
    check = getattr(coordinator, 'tariff_context_is_current', None)
    return check(result) if check is not None else True


async def _book_authorized(hass, connection, *, admin=False):
    user = getattr(connection, 'user', None)
    if user is None or not getattr(user, 'is_active', False):
        return False
    current = await hass.auth.async_get_user(user.id)
    return (current is not None and current.is_active
            and (not admin or getattr(current, 'is_admin', False)))


def _book_context_current(hass, storage, coordinator, revision=None):
    bucket = hass.data.get(DOMAIN, {})
    return (bucket.get('storage') is storage and bucket.get('energy_snapshot') is coordinator
            and coordinator is not None and getattr(coordinator, 'running', False)
            and (revision is None or storage.current_tariff_revision == revision))


@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/get_tariff_book'})
@websocket_api.async_response
async def _ws_get_tariff_book(hass, connection, msg):
    if not await _book_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    bucket = hass.data.get(DOMAIN, {})
    storage, coordinator = bucket.get('storage'), bucket.get('energy_snapshot')
    try:
        result = await storage.async_get_tariff_book()
    except Exception:
        connection.send_error(msg['id'], 'tariff_book_read_failed', 'Tariff history could not be read.')
        return
    if not await _book_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_book_changed', 'Tariff history changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/preview_tariff_book',
    vol.Required('book'): dict,
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_preview_tariff_book(hass, connection, msg):
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    bucket = hass.data.get(DOMAIN, {})
    storage, coordinator = bucket.get('storage'), bucket.get('energy_snapshot')
    try:
        result = await storage.async_preview_tariff_book(msg['book'], expected_revision=msg['expected_revision'])
    except TariffRevisionConflict:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff history changed. Reload before continuing.')
        return
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_book', 'Tariff history or revision is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_book_preview_failed', 'Tariff history could not be validated.')
        return
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_book_changed', 'Tariff history changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/set_tariff_book',
    vol.Required('book'): dict,
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
    vol.Optional('historical_edit_ack', default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_set_tariff_book(hass, connection, msg):
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    bucket = hass.data.get(DOMAIN, {})
    storage, coordinator = bucket.get('storage'), bucket.get('energy_snapshot')

    async def before_save():
        if not await _book_authorized(hass, connection, admin=True):
            return False
        return _book_context_current(hass, storage, coordinator)

    try:
        result = await storage.async_save_tariff_book(msg['book'],
            expected_revision=msg['expected_revision'],
            historical_edit_ack=msg.get('historical_edit_ack', False), before_save=before_save)
    except TariffRevisionConflict:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff history changed. Reload before saving.')
        return
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_book', 'Tariff history or revision is invalid.')
        return
    except ValueError as error:
        if str(error) == 'historical_edit_ack_required':
            connection.send_error(msg['id'], 'historical_edit_ack_required',
                                  'This edit changes historical calculations. Review the affected dates and confirm explicitly.')
        else:
            connection.send_error(msg['id'], 'invalid_tariff_book', 'Tariff history or revision is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_book_save_failed', 'Tariff history could not be saved.')
        return
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_book_changed', 'Tariff history changed. Reload before continuing.')
        return
    hass.async_create_task(coordinator._async_tick(None))
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_analysis',
    vol.Optional('period', default='day'): vol.In(('day', 'week', 'month', 'year')),
})
@websocket_api.async_response
async def _ws_energy_analysis(hass, connection, msg):
    """Detailed source-aware analysis; identity comes only from the connection."""
    if not await _snapshot_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    service = hass.data.get(DOMAIN, {}).get('energy_analysis')
    if service is None:
        connection.send_error(msg['id'], 'energy_analysis_unavailable', 'Energy analysis is unavailable.')
        return
    user_id = connection.user.id
    try:
        result = await service.async_get_analysis(msg.get('period', 'day'), user_id)
        if not await _snapshot_authorized(hass, connection):
            raise PermissionError('energy_sources_unauthorized')
        # This last async validation includes the concrete current source access
        # policy. Do not insert another await before the final context/send.
        if await service.async_validate_result(result, user_id) is not True:
            raise RuntimeError('energy_analysis_changed')
        if (hass.data.get(DOMAIN, {}).get('energy_analysis') is not service
                or not service.context_is_current(result)):
            raise RuntimeError('energy_analysis_changed')
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Energy source access required.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_analysis_unavailable', 'Energy analysis is unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_snapshot',
    vol.Optional('profile'): vol.Any(dict, None),
})
@websocket_api.async_response
async def _ws_energy_snapshot(hass, connection, msg):
    if not await _snapshot_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    coordinator = hass.data.get(DOMAIN, {}).get('energy_snapshot')
    if coordinator is None:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy snapshot is unavailable.')
        return
    try:
        result = await coordinator.async_snapshot(**({'profile': msg['profile']} if 'profile' in msg else {}))
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy snapshot is unavailable.')
        return
    if not await _snapshot_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    if not _energy_context_current(hass, coordinator, result):
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy snapshot is unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_price_queries',
    vol.Required('queries'): list,
    vol.Optional('profile'): vol.Any(dict, None),
})
@websocket_api.async_response
async def _ws_energy_price_queries(hass, connection, msg):
    """Read-only pricing of supplied card buckets, never household measurement."""
    if not await _snapshot_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    coordinator = hass.data.get(DOMAIN, {}).get('energy_snapshot')
    if coordinator is None:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy pricing is unavailable.')
        return
    try:
        result = await coordinator.async_price_queries(msg['queries'],
            **({'profile': msg['profile']} if 'profile' in msg else {}))
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile is invalid.')
        return
    except ValueError:
        connection.send_error(msg['id'], 'invalid_energy_price_queries', 'Price queries exceed supported structure or limits.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy pricing is unavailable.')
        return
    if not await _snapshot_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    if not _energy_context_current(hass, coordinator, result):
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy pricing is unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_tariff"})
@websocket_api.async_response
async def _ws_get_tariff(hass, connection, msg) -> None:
    """Read tariff-only settings for an authenticated household connection."""
    try:
        result = await _storage(hass).async_get_tariff()
    except Exception:
        connection.send_error(msg["id"], "tariff_read_failed", "Tariff settings could not be read.")
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/set_tariff",
    vol.Required("profile"): vol.Any(dict, None),
    vol.Required("expected_revision"): vol.All(int, vol.Range(min=0)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_set_tariff(hass, connection, msg) -> None:
    """Admin save with optimistic revision and confirmed persisted readback."""
    try:
        result = await _storage(hass).async_save_tariff(
            msg["profile"], expected_revision=msg["expected_revision"])
    except TariffRevisionConflict:
        connection.send_error(msg["id"], "tariff_revision_conflict", "Tariff settings changed. Reload before saving.")
        return
    except TariffValidationError:
        connection.send_error(msg["id"], "invalid_tariff_profile", "Tariff profile or revision is invalid.")
        return
    except Exception:
        # Do not expose Store paths, SMTP state, or private exception messages.
        connection.send_error(msg["id"], "tariff_save_failed", "Tariff settings could not be saved.")
        return
    coordinator = _bucket(hass).get('energy_snapshot')
    if coordinator is not None:
        # The save has already been confirmed; a Recorder outage must not turn it
        # into an ambiguous save response. Refresh runs through the shared lock.
        hass.async_create_task(coordinator._async_tick(None))
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_attempt_history"})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_get_attempt_history(hass, connection, msg) -> None:
    """Read bounded metadata only; history is never a content export."""
    try:
        result = await _storage(hass).async_get_attempt_history()
    except Exception:
        connection.send_error(msg["id"], "history_unavailable", "Email attempt history is unavailable.")
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_config"})
@websocket_api.require_admin
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


def _utcnow() -> datetime:
    """Trusted server clock; client timestamps are not accepted."""
    return datetime.now(timezone.utc)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list_schedules"})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_list_schedules(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Legacy envelope plus computed metadata; never save or inspect SMTP."""
    storage = _storage(hass)
    rows = await storage.async_list_schedules()
    now = _utcnow()
    local_zone = getattr(getattr(hass, "config", None), "time_zone", None)
    result = []
    for row in rows:
        metadata = {"next_fire": None, "effective_timezone": None}
        try:
            zone = effective_schedule_timezone(row, now, local_zone)
            metadata["effective_timezone"] = getattr(zone, "key", str(zone))
            if row.get("enabled", True):
                next_fire = calculate_next_fire(row, now, local_timezone=local_zone)
                last = await storage.async_get_last_fired(row["id"])
                if fire_period_key(row, next_fire, local_timezone=local_zone) == last:
                    next_fire = calculate_next_fire(row, next_fire, local_timezone=local_zone)
                metadata["next_fire"] = next_fire.isoformat()
        except (ValueError, TypeError, KeyError):
            # No private parser errors or stored credentials in metadata.
            pass
        result.append({**row, **metadata})
    connection.send_result(msg["id"], {"schedules": result})


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
    except Exception:
        connection.send_error(msg["id"], "storage_write_failed", "Schedule state could not be saved.")
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
        vol.Optional("period_mode"): vol.In(["rolling", "calendar"]),
        vol.Optional("timezone"): str,
        vol.Optional("max_entries"): vol.All(int, vol.Range(min=1, max=1000)),
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
            **({"period_mode": msg["period_mode"]} if "period_mode" in msg else {}),
            **({"report_timezone": msg["timezone"]} if "timezone" in msg else {}),
            **({"max_entries": msg["max_entries"]} if "max_entries" in msg else {}),
        )
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "send_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/preview_energy_report",
        vol.Required("cadence"): vol.In(sorted(VALID_CADENCES)),
        vol.Optional("calendar_period"): bool,
        vol.Optional("format"): vol.In(["summary", "mail"]),
        vol.Optional("period_mode"): vol.In(["rolling", "calendar"]),
        vol.Optional("timezone"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_preview_energy_report(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Read Recorder without sending; completed calendar periods are explicit opt-in."""
    if msg.get("format") == "mail":
        await _async_preview_mail(hass, connection, msg, "energy_report")
        return
    if "period_mode" in msg:
        connection.send_error(msg["id"], "invalid_payload", "period_mode requires format=mail")
        return
    calendar_period = msg.get("calendar_period", False)
    if type(calendar_period) is not bool or "timezone" in msg and not calendar_period:
        connection.send_error(msg["id"], "invalid_payload", "Timezone requires calendar_period=true.")
        return
    if not calendar_period:
        # Keep the existing dashboard's current-period request and response intact.
        payload = await async_build_energy_report_payload(hass, cadence=msg["cadence"])
        connection.send_result(msg["id"], payload["summary"])
        return
    now = _utcnow()
    local_zone = getattr(getattr(hass, "config", None), "time_zone", None)
    context = {"cadence": msg["cadence"]}
    if "timezone" in msg:
        context["timezone"] = msg["timezone"]
    try:
        await asyncio.to_thread(normalize_schedule_calendar, context)
        zone = effective_schedule_timezone(context, now, local_zone)
        start, end = calendar_report_window(context, now, local_timezone=local_zone)
    except (ValueError, TypeError, KeyError):
        connection.send_error(msg["id"], "invalid_payload", "Invalid calendar timezone.")
        return
    report_zone = getattr(zone, "key", str(zone))
    payload = await async_build_energy_report_payload(hass, cadence=msg["cadence"], now=now,
        window_start=start, window_end=end, report_timezone=report_zone)
    summary = dict(payload["summary"])
    summary.update(cadence=msg["cadence"], effective_timezone=report_zone,
        period={**summary["period"], "semantics": "previous_completed_calendar_period"})
    connection.send_result(msg["id"], summary)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/preview_log_digest",
    vol.Optional("format"): vol.In(["summary", "mail"]),
    vol.Optional("period_mode"): vol.In(["rolling", "calendar"]),
    vol.Optional("max_entries"): vol.All(int, vol.Range(min=1, max=1000)),
    vol.Required("cadence"): vol.In(sorted(VALID_CADENCES)),
    vol.Optional("timezone"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_preview_log_digest(hass, connection, msg) -> None:
    """Readonly retained log aggregate preview for a completed HA calendar period."""
    if msg.get("format") == "mail":
        await _async_preview_mail(hass, connection, msg, "log_digest")
        return
    if "period_mode" in msg or "max_entries" in msg:
        connection.send_error(msg["id"], "invalid_payload", "period_mode/max_entries require format=mail")
        return
    now = _utcnow()
    cadence = msg["cadence"]
    local_zone = getattr(getattr(hass, "config", None), "time_zone", None)
    context = {"cadence": cadence}
    if "timezone" in msg:
        context["timezone"] = msg["timezone"]
    try:
        await asyncio.to_thread(normalize_schedule_calendar, context)
        zone = effective_schedule_timezone(context, now, local_zone)
        start, end = calendar_report_window(context, now, local_timezone=local_zone)
    except (ValueError, TypeError, KeyError):
        connection.send_error(msg["id"], "invalid_payload", "Invalid calendar timezone.")
        return
    entries = await async_get_system_log_entries(hass)
    # Reuse the existing pure composer, with a bounded timestamp filter. Missing
    # timestamps remain explicitly incomplete; counts are lifetime aggregates.
    from .composer import _entry_time
    entries = [row for row in entries if _entry_time(row) is None or _entry_time(row) < end]
    payload = build_log_digest_payload(entries, cadence=cadence, now=now, since=start - timedelta(microseconds=1))
    summary = payload["summary"]
    result = {key: summary[key] for key in
              ("total", "level_counts", "logger_counts", "recent_errors", "recent_warnings", "unique_problems", "counts_complete")}
    if not summary['unique_problems']:
        result.update(total=None, counts_complete=False, history_unavailable=True)
    # Internal datetime objects are not a WebSocket JSON contract.
    for key in ("recent_errors", "recent_warnings"):
        result[key] = [{"level": row.get("level"), "name": row.get("name"),
                        "message": row["_message"], "count": row.get("count"),
                        "timestamp": row["_timestamp_dt"].isoformat() if row["_timestamp_dt"] else None}
                       for row in result[key]]
    result.update(cadence=cadence, effective_timezone=getattr(zone, "key", str(zone)),
                  period={"from": start.isoformat(), "to": end.isoformat(),
                          "semantics": "previous_completed_calendar_period"},
                  source={"name": "system_log", "count_semantics": "retained_aggregates",
                          "complete": False, "retained_entries": len(entries),
                          "reason": "retained_last_observed_aggregates_not_full_event_history"})
    connection.send_result(msg["id"], result)


async def _async_preview_mail(hass, connection, msg, kind) -> None:
    """Shared server composer, no SMTP config, dispatch, Store writes or history."""
    now = _utcnow()
    # New mail contract defaults to rolling, matching explicit manual rolling send.
    # Existing summary contracts above preserve their historical default behavior.
    mode = msg.get("period_mode", "calendar" if msg.get("calendar_period") is True else "rolling")
    try:
        if "calendar_period" in msg and (type(msg["calendar_period"]) is not bool or
                msg["calendar_period"] != (mode == "calendar")):
            raise ValueError("calendar_period conflicts with period_mode")
        limit = msg.get("max_entries", 10)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("max_entries must be an integer between 1 and 1000")
        options = await async_resolve_manual_period(hass, kind=kind, cadence=msg["cadence"],
            now=now, period_mode=mode, report_timezone=msg.get("timezone"))
    except (ValueError, TypeError, KeyError) as err:
        connection.send_error(msg["id"], "invalid_payload", str(err))
        return
    start, end = options.pop("report_window")
    payload = await async_build_report_payload(hass, _bucket(hass).get("storage"),
        kind=kind, cadence=msg["cadence"], now=now,
        window_start=start, window_end=end, **options,
        **({"max_entries": limit} if kind == "log_digest" else {}))
    summary = payload["summary"]
    source = summary.get("source") or {
        "name": "recorder", "kind": "grid_import", "status": summary.get("status", "no_data"),
        "complete": summary.get("status") == "ready", "source_ids": summary.get("source_ids", [])}
    connection.send_result(msg["id"], {
        "subject": payload["subject"], "body": payload["body"], "html": payload["html"],
        "cadence": msg["cadence"], "period": summary["period"], "source": source,
        "generated_at": now.isoformat(), **({"effective_timezone": options["report_timezone"]}
            if "report_timezone" in options else {}),
    })


def async_register_commands(hass: HomeAssistant) -> None:
    """Register all websocket commands."""
    for handler in (
        _ws_get_tariff,
        _ws_get_tariff_book,
        _ws_preview_tariff_book,
        _ws_set_tariff_book,
        _ws_energy_snapshot,
        _ws_energy_analysis,
        _ws_energy_price_queries,
        _ws_set_tariff,
        _ws_get_config,
        _ws_get_attempt_history,
        _ws_list_schedules,
        _ws_set_schedule,
        _ws_send_now,
        _ws_preview_energy_report,
        _ws_preview_log_digest,
    ):
        websocket_api.async_register_command(hass, handler)
