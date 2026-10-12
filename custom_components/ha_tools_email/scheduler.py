"""Store-backed scheduler for HA Tools Email reports."""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
# Keep validated ZoneInfo instances alive beyond Python's small strong cache.
# Store validates in a worker thread before callbacks/list metadata use them.
_SCHEDULE_ZONES: dict[str, ZoneInfo] = {}


def _zone(key: str) -> ZoneInfo:
    if key not in _SCHEDULE_ZONES:
        _SCHEDULE_ZONES[key] = ZoneInfo(key)
    return _SCHEDULE_ZONES[key]

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


def normalize_schedule_calendar(schedule: dict[str, Any]) -> dict[str, Any]:
    """Validate new fields, accepting established legacy day aliases on read."""
    clean = dict(schedule)
    if "timezone" in clean:
        value = clean["timezone"]
        if (not isinstance(value, str) or not value or len(value) > 128
                or value in {"localtime", "posixrules"} or value.startswith(("posix/", "right/"))):
            raise ValueError("timezone must be an IANA timezone name")
        try:
            _zone(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("timezone must be an available IANA timezone name") from None
    for key, aliases, lower, upper in (
        ("weekday", ("weekly_day",), 0, 6),
        ("month_day", ("monthday", "day"), 1, 31),
    ):
        values = []
        for field in (key, *aliases):
            if field not in clean:
                continue
            value = clean[field]
            if key == "weekday" and isinstance(value, str):
                value = _WEEKDAY_ALIASES.get(value.strip().lower())
            elif field in aliases and isinstance(value, str) and value.isdecimal():
                value = int(value)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{key} must be between {lower} and {upper}")
            values.append(value)
        if values and len(set(values)) != 1:
            raise ValueError(f"conflicting {key} aliases")
        if values:
            clean[key] = values[0]
        elif (key == "weekday" and clean.get("cadence") == CADENCE_WEEKLY):
            clean[key] = 0
        elif (key == "month_day" and clean.get("cadence") == CADENCE_MONTHLY):
            clean[key] = 1
        for alias in aliases:
            clean.pop(alias, None)
    return clean


def effective_schedule_timezone(
    schedule: dict[str, Any], now: datetime, local_timezone: str | tzinfo | None = None,
) -> tzinfo:
    """Explicit IANA zone wins; otherwise HA local zone (input zone for pure callers)."""
    explicit = schedule.get("timezone")
    if explicit is not None:
        return _zone(explicit)
    if isinstance(local_timezone, str):
        return _zone(local_timezone)
    return local_timezone or _aware(now).tzinfo or timezone.utc


def _resolve_wall(day: date, hour: int, minute: int, zone: tzinfo) -> datetime:
    """First fold; gaps shift forward by the UTC roundtrip's offset difference."""
    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
    return candidate.astimezone(timezone.utc).astimezone(zone)


def _candidate_for_date(schedule: dict[str, Any], day: date, zone: tzinfo) -> datetime | None:
    cadence = schedule.get("cadence", CADENCE_DAILY)
    if cadence == CADENCE_WEEKLY and day.weekday() != _schedule_weekday(schedule):
        return None
    if cadence == CADENCE_MONTHLY:
        wanted = min(_schedule_month_day(schedule), calendar.monthrange(day.year, day.month)[1])
        if day.day != wanted:
            return None
    hour, minute = parse_schedule_time(str(schedule.get("time") or "07:30"))
    return _resolve_wall(day, hour, minute, zone)


def calculate_next_fire(
    schedule: dict[str, Any], now: datetime, *, local_timezone: str | tzinfo | None = None,
) -> datetime:
    """Strictly next instant; same candidate resolver as actual minute callbacks."""
    schedule = normalize_schedule_calendar(schedule)
    now = _aware(now)
    zone = effective_schedule_timezone(schedule, now, local_timezone)
    day = now.astimezone(zone).date()
    # At most 31 days to a monthly candidate; margin covers skipped civil dates.
    for offset in range(64):
        candidate = _candidate_for_date(schedule, day + timedelta(days=offset), zone)
        if candidate is not None and candidate.astimezone(timezone.utc) > now.astimezone(timezone.utc):
            return candidate
    raise ValueError("schedule has no next calendar occurrence")


def calendar_report_window(
    schedule: dict[str, Any], now: datetime, *, local_timezone: str | tzinfo | None = None,
) -> tuple[datetime, datetime]:
    """Previous completed local day, ISO week or month, with UTC query bounds."""
    zone = effective_schedule_timezone(schedule, now, local_timezone)
    day = _aware(now).astimezone(zone).date()
    cadence = schedule.get("cadence", CADENCE_DAILY)
    if cadence == CADENCE_MONTHLY:
        end_day = day.replace(day=1)
        start_day = (end_day - timedelta(days=1)).replace(day=1)
    elif cadence == CADENCE_WEEKLY:
        end_day = day - timedelta(days=day.weekday())
        start_day = end_day - timedelta(days=7)
    else:
        end_day = day
        start_day = day - timedelta(days=1)
    return (_resolve_wall(start_day, 0, 0, zone).astimezone(timezone.utc),
            _resolve_wall(end_day, 0, 0, zone).astimezone(timezone.utc))


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


def should_fire_schedule(
    schedule: dict[str, Any], now: datetime, *, local_timezone: str | tzinfo | None = None,
) -> bool:
    """Match the resolved calendar instant, never the second autumn fold."""
    if not schedule.get("enabled", True):
        return False
    schedule = normalize_schedule_calendar(schedule)
    now = _aware(now)
    zone = effective_schedule_timezone(schedule, now, local_timezone)
    candidate = _candidate_for_date(schedule, now.astimezone(zone).date(), zone)
    return candidate is not None and (
        candidate.astimezone(timezone.utc) == now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    )


async def async_start_scheduler(
    hass: Any,
    storage: Any,
    load_config: Callable[[], Awaitable[dict[str, Any]]],
    send_email: Callable[..., None],
) -> None:
    """Start or reload scheduler tracking."""
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket[DATA_LOAD_CONFIG] = load_config
    bucket[DATA_SEND_EMAIL] = send_email
    await async_reload_schedules(hass)


async def async_reload_schedules(hass: Any) -> None:
    """One bounded HA minute tracker per active schedule; re-read before dispatch."""
    try:
        from homeassistant.helpers.event import async_track_time_change
    except Exception:
        _LOGGER.debug("Home Assistant event helper unavailable; scheduler not armed")
        return

    bucket = hass.data.setdefault(DOMAIN, {})
    fire_lock = bucket.setdefault("schedule_fire_lock", asyncio.Lock())
    generation = bucket["schedule_generation"] = bucket.get("schedule_generation", 0) + 1
    consumed = bucket.setdefault("schedule_consumed_periods", {})
    for unsub in bucket.get(DATA_SCHEDULER_UNSUBS, []):
        unsub()
    bucket[DATA_SCHEDULER_UNSUBS] = []

    storage = bucket.get("storage")
    if storage is None:
        return

    schedules = await storage.async_list_schedules()
    live_ids = {schedule["id"] for schedule in schedules}
    for removed_id in set(consumed) - live_ids:
        consumed.pop(removed_id, None)
    for schedule in schedules:
        if not schedule.get("enabled", True):
            continue
        try:
            normalize_schedule_calendar(schedule)
            parse_schedule_time(schedule["time"])
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Skipping invalid email schedule (id=%s): %s", schedule.get("id", "unknown"), err)
            continue

        async def _fire(now: datetime, schedule_id: str = schedule["id"]) -> None:
            async with fire_lock:
                if bucket.get("schedule_generation") != generation:
                    return
                current = await storage.async_get_schedule(schedule_id)
                if not current:
                    return
                local_zone = getattr(getattr(hass, "config", None), "time_zone", None)
                try:
                    if not should_fire_schedule(current, now, local_timezone=local_zone):
                        return
                    zone = effective_schedule_timezone(current, now, local_zone)
                    local_now = _aware(now).astimezone(zone)
                    period_key = fire_period_key(current, now, local_timezone=local_zone)
                    if consumed.get(schedule_id) == period_key or await storage.async_get_last_fired(schedule_id) == period_key:
                        return
                    if bucket.get("schedule_generation") != generation:
                        return
                    # Consume before SMTP: failures/unknown handoffs never retry.
                    # Save failure inhibits dispatch; in-memory fence avoids a
                    # duplicate if persistence succeeds but raises on its return.
                    consumed[schedule_id] = period_key
                    await storage.async_set_last_fired(schedule_id, period_key, local_now.isoformat())
                    # Persistence awaits may allow an edit/delete. Check again
                    # before the only dispatch path; a changed payload is skipped.
                    if bucket.get("schedule_generation") != generation or await storage.async_get_schedule(schedule_id) != current:
                        return
                    await async_send_schedule(hass, current, now=local_now)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Scheduled HA Tools Email report failed (%s)", type(err).__name__)
                    return

        # Match every local minute, then compare UTC instants in the selected
        # zone. The helper's returned unsubscribe retains the existing lifecycle.
        unsub = async_track_time_change(hass, _fire, second=0)
        bucket[DATA_SCHEDULER_UNSUBS].append(unsub)


def async_stop_scheduler(hass: Any) -> None:
    """Cancel callbacks and invalidate jobs already queued by HA."""
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["schedule_generation"] = bucket.get("schedule_generation", 0) + 1
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
        schedule_id=schedule.get("id"),
        expected_schedule=schedule,
        report_window=calendar_report_window(schedule, now or datetime.now(timezone.utc),
            local_timezone=getattr(getattr(hass, "config", None), "time_zone", None)),
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
    schedule_id: str | None = None,
    report_window: tuple[datetime, datetime] | None = None,
    expected_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose and send one report through the existing SMTP send path."""
    from .composer import async_build_report_payload
    from .history import async_dispatch_attempt

    now = _aware(now or datetime.now(timezone.utc))
    bucket = hass.data[DOMAIN]
    payload = {}
    to = ""
    async def prepare():
        nonlocal payload, to
        cfg = await bucket[DATA_LOAD_CONFIG]()
        to = ", ".join(recipients) if recipients else cfg.get("default_recipient", "")
        payload = await async_build_report_payload(
            hass, storage, kind=kind, cadence=cadence, now=now,
            **({"window_start": report_window[0], "window_end": report_window[1]} if report_window else {}),
        )
        # Recorder/config awaits can allow an administrator's edit/delete.
        # Re-read immediately before the shared dispatcher crosses into SMTP.
        if expected_schedule is not None and await storage.async_get_schedule(schedule_id) != expected_schedule:
            raise ValueError("schedule_changed_before_dispatch")
        return cfg, to, payload["subject"], payload["body"], payload["html"]
    dispatch = await async_dispatch_attempt(
        hass, storage, bucket[DATA_SEND_EMAIL], prepare,
        source="schedule" if schedule_id is not None else "manual",
        kind=kind, cadence=cadence,
        period=fire_period_key({"cadence": cadence}, now),
        schedule_id=schedule_id, now=now,
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
        **dispatch,
        "kind": kind,
        "cadence": cadence,
        "subject": payload["subject"],
        "recipient_count": len([item for item in to.split(",") if item.strip()]),
    }


def fire_period_key(
    schedule: dict[str, Any], now: datetime, *, local_timezone: str | tzinfo | None = None,
) -> str:
    """Logical cadence period in the same effective zone as its fire."""
    now = _aware(now).astimezone(effective_schedule_timezone(schedule, now, local_timezone))
    cadence = str(schedule.get("cadence") or CADENCE_DAILY)
    if cadence == CADENCE_WEEKLY:
        year, week, _ = now.isocalendar()
        return f"{cadence}:{year}-W{week:02d}"
    if cadence == CADENCE_MONTHLY:
        return f"{cadence}:{now.year}-{now.month:02d}"
    return f"{cadence}:{now.date().isoformat()}"


def _schedule_weekday(schedule: dict[str, Any]) -> int:
    return normalize_schedule_calendar(schedule).get("weekday", 0)


def _schedule_month_day(schedule: dict[str, Any]) -> int:
    return normalize_schedule_calendar(schedule).get("month_day", 1)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
