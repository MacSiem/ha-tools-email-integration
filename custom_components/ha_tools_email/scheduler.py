"""Store-backed scheduler for HA Tools Email reports."""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from .const import (
        CADENCE_DAILY,
        CADENCE_MONTHLY,
        CADENCE_WEEKLY,
        DATA_LOAD_CONFIG,
        DATA_SCHEDULER_UNSUBS,
        DATA_SEND_EMAIL,
        DOMAIN,
        KIND_LOG_DIGEST,
    )
except ImportError:  # Allows pure tests to load this file without a package import.
    CADENCE_DAILY = "daily"
    CADENCE_MONTHLY = "monthly"
    CADENCE_WEEKLY = "weekly"
    DATA_LOAD_CONFIG = "load_config"
    DATA_SCHEDULER_UNSUBS = "scheduler_unsubs"
    DATA_SEND_EMAIL = "send_email"
    DOMAIN = "ha_tools_email"
    KIND_LOG_DIGEST = "log_digest"

_LOGGER = logging.getLogger(__name__)
_WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def calculate_next_fire(schedule: dict[str, Any], now: datetime) -> datetime:
    """Return the next datetime when a schedule should fire."""
    now = _aware(now)
    hour, minute = parse_schedule_time(str(schedule.get("time") or "07:30"))
    cadence = str(schedule.get("cadence") or CADENCE_DAILY)

    if cadence == CADENCE_WEEKLY:
        weekday = _schedule_weekday(schedule)
        days_ahead = (weekday - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if cadence == CADENCE_MONTHLY:
        day = _schedule_month_day(schedule)
        candidate = _monthly_candidate(now.year, now.month, day, hour, minute, now.tzinfo)
        if candidate <= now:
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            candidate = _monthly_candidate(year, month, day, hour, minute, now.tzinfo)
        return candidate

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def parse_schedule_time(value: str) -> tuple[int, int]:
    """Parse HH:MM schedule time."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError("schedule time must use HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("schedule time must use HH:MM in 24-hour format")
    return hour, minute


def should_fire_schedule(schedule: dict[str, Any], now: datetime) -> bool:
    """Return true if a tracked time event should fire for cadence."""
    if not schedule.get("enabled", True):
        return False
    now = _aware(now)
    cadence = str(schedule.get("cadence") or CADENCE_DAILY)
    if cadence == CADENCE_WEEKLY:
        return now.weekday() == _schedule_weekday(schedule)
    if cadence == CADENCE_MONTHLY:
        return now.day == _schedule_month_day(schedule)
    return True


async def async_start_scheduler(
    hass: Any,
    storage: Any,
    load_config: Callable[..., dict[str, Any]],
    send_email: Callable[..., None],
) -> None:
    """Start or reload scheduler tracking."""
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket[DATA_LOAD_CONFIG] = load_config
    bucket[DATA_SEND_EMAIL] = send_email
    await async_reload_schedules(hass)


async def async_reload_schedules(hass: Any) -> None:
    """Re-arm all async_track_time_change callbacks from Store schedules."""
    try:
        from homeassistant.helpers.event import async_track_time_change
    except Exception:
        _LOGGER.debug("Home Assistant event helper unavailable; scheduler not armed")
        return

    bucket = hass.data.setdefault(DOMAIN, {})
    for unsub in bucket.get(DATA_SCHEDULER_UNSUBS, []):
        unsub()
    bucket[DATA_SCHEDULER_UNSUBS] = []

    storage = bucket.get("storage")
    if storage is None:
        return

    schedules = await storage.async_list_schedules()
    for schedule in schedules:
        if not schedule.get("enabled", True):
            continue
        try:
            hour, minute = parse_schedule_time(schedule["time"])
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Skipping invalid email schedule %s: %s", schedule, err)
            continue

        async def _fire(now: datetime, schedule_id: str = schedule["id"]) -> None:
            current = await storage.async_get_schedule(schedule_id)
            if not current or not should_fire_schedule(current, now):
                return
            period_key = fire_period_key(current, now)
            if await storage.async_get_last_fired(schedule_id) == period_key:
                return
            try:
                await async_send_schedule(hass, current, now=now)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Scheduled HA Tools Email report failed: %s", err)
                return
            await storage.async_set_last_fired(
                schedule_id, period_key, _aware(now).isoformat()
            )

        unsub = async_track_time_change(
            hass, _fire, hour=hour, minute=minute, second=0, local=True
        )
        bucket[DATA_SCHEDULER_UNSUBS].append(unsub)


def async_stop_scheduler(hass: Any) -> None:
    """Cancel all scheduler callbacks."""
    bucket = hass.data.setdefault(DOMAIN, {})
    for unsub in bucket.get(DATA_SCHEDULER_UNSUBS, []):
        unsub()
    bucket[DATA_SCHEDULER_UNSUBS] = []


async def async_send_schedule(
    hass: Any,
    schedule: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose and send one configured schedule."""
    bucket = hass.data[DOMAIN]
    return await async_send_report(
        hass,
        bucket["storage"],
        kind=schedule["kind"],
        cadence=schedule["cadence"],
        recipients=schedule.get("recipients") or [],
        now=now,
    )


async def async_send_now(
    hass: Any,
    *,
    kind: str,
    cadence: str,
    recipients: list[str] | None = None,
) -> dict[str, Any]:
    """Compose and send a report immediately."""
    bucket = hass.data[DOMAIN]
    return await async_send_report(
        hass,
        bucket["storage"],
        kind=kind,
        cadence=cadence,
        recipients=recipients or [],
        now=datetime.now(timezone.utc),
    )


async def async_send_report(
    hass: Any,
    storage: Any,
    *,
    kind: str,
    cadence: str,
    recipients: list[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose and send one report through the existing SMTP send path."""
    from .composer import async_build_report_payload

    now = _aware(now or datetime.now(timezone.utc))
    bucket = hass.data[DOMAIN]
    cfg = await hass.async_add_executor_job(bucket[DATA_LOAD_CONFIG], hass)
    to = ", ".join(recipients) if recipients else cfg.get("default_recipient", "")
    payload = await async_build_report_payload(
        hass, storage, kind=kind, cadence=cadence, now=now
    )
    await hass.async_add_executor_job(
        bucket[DATA_SEND_EMAIL],
        hass,
        cfg,
        to,
        payload["subject"],
        payload["body"],
        payload["html"],
    )
    if kind == KIND_LOG_DIGEST:
        await storage.async_set_digest_state(
            kind,
            cadence,
            {
                "last_digest": now.isoformat(),
                "seen_keys": payload.get("dedup_keys", [])[-500:],
            },
        )
    return {
        "ok": True,
        "kind": kind,
        "cadence": cadence,
        "subject": payload["subject"],
        "recipient_count": len([item for item in to.split(",") if item.strip()]),
    }


def fire_period_key(schedule: dict[str, Any], now: datetime) -> str:
    """Return the logical cadence period key for deduping fires."""
    now = _aware(now)
    cadence = str(schedule.get("cadence") or CADENCE_DAILY)
    if cadence == CADENCE_WEEKLY:
        year, week, _ = now.isocalendar()
        return f"{cadence}:{year}-W{week:02d}"
    if cadence == CADENCE_MONTHLY:
        return f"{cadence}:{now.year}-{now.month:02d}"
    return f"{cadence}:{now.date().isoformat()}"


def _schedule_weekday(schedule: dict[str, Any]) -> int:
    value = schedule.get("weekday", schedule.get("weekly_day", 0))
    if isinstance(value, int):
        return max(0, min(6, value))
    return _WEEKDAY_ALIASES.get(str(value).strip().lower(), 0)


def _schedule_month_day(schedule: dict[str, Any]) -> int:
    try:
        value = int(schedule.get("day", schedule.get("month_day", 1)))
    except (TypeError, ValueError):
        return 1
    return max(1, min(31, value))


def _monthly_candidate(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    tzinfo: timezone | None,
) -> datetime:
    last_day = calendar.monthrange(year, month)[1]
    return datetime(
        year,
        month,
        min(day, last_day),
        hour,
        minute,
        tzinfo=tzinfo,
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
