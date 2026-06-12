"""Server-side report composition for HA Tools Email."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Iterable

try:
    from .const import (
        CADENCE_DAILY,
        CADENCE_MONTHLY,
        CADENCE_WEEKLY,
        DEFAULT_CURRENCY,
        DEFAULT_PRICE_PER_KWH,
        KIND_ENERGY_REPORT,
        KIND_LOG_DIGEST,
    )
except ImportError:  # Allows pure tests to load this file without a package import.
    CADENCE_DAILY = "daily"
    CADENCE_MONTHLY = "monthly"
    CADENCE_WEEKLY = "weekly"
    DEFAULT_CURRENCY = "PLN"
    DEFAULT_PRICE_PER_KWH = 0.0
    KIND_ENERGY_REPORT = "energy_report"
    KIND_LOG_DIGEST = "log_digest"


def build_log_digest_payload(
    entries: Iterable[dict[str, Any]],
    *,
    cadence: str,
    now: datetime | None = None,
    since: datetime | None = None,
    seen_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a log digest email payload from system_log-like entries."""
    now = _aware(now or datetime.now(timezone.utc))
    summary = summarize_log_entries(entries, since=since, seen_keys=seen_keys)
    title = f"HA Log Digest - {_cadence_label(cadence)}"
    subject = f"{title} - {now.date().isoformat()}"
    body = _render_log_plain(title, summary)
    html = render_log_digest_html(title, summary, now)
    return {
        "subject": subject,
        "body": body,
        "html": html,
        "summary": summary,
        "dedup_keys": summary["dedup_keys"],
    }


def summarize_log_entries(
    entries: Iterable[dict[str, Any]],
    *,
    since: datetime | None = None,
    seen_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Aggregate system log entries by level/logger with optional dedup state."""
    seen = set(seen_keys or [])
    accepted: list[dict[str, Any]] = []
    dedup_keys = set(seen)
    since = _aware(since) if since else None

    for raw in entries:
        if not isinstance(raw, dict):
            continue
        when = _entry_time(raw)
        if since and when and when <= since:
            continue
        key = _entry_key(raw)
        if key in seen and (since is None or when is None):
            continue
        dedup_keys.add(key)
        entry = dict(raw)
        entry["_message"] = _entry_message(raw)
        entry["_timestamp_dt"] = when
        accepted.append(entry)

    level_counts: dict[str, int] = {}
    logger_counts: dict[str, int] = {}
    for entry in accepted:
        level = str(entry.get("level") or "UNKNOWN").upper()
        logger = str(entry.get("name") or "unknown")
        count = int(entry.get("count") or 1)
        level_counts[level] = level_counts.get(level, 0) + count
        logger_counts[logger] = logger_counts.get(logger, 0) + count

    recent_errors = [
        entry
        for entry in accepted
        if str(entry.get("level") or "").upper() in {"ERROR", "CRITICAL"}
    ]
    recent_warnings = [
        entry
        for entry in accepted
        if str(entry.get("level") or "").upper() == "WARNING"
    ]
    recent_errors.sort(
        key=lambda item: item.get("_timestamp_dt") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    recent_warnings.sort(
        key=lambda item: item.get("_timestamp_dt") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    return {
        "total": sum(level_counts.values()),
        "level_counts": level_counts,
        "logger_counts": dict(
            sorted(logger_counts.items(), key=lambda item: item[1], reverse=True)
        ),
        "recent_errors": recent_errors[:10],
        "recent_warnings": recent_warnings[:10],
        "entries": accepted,
        "dedup_keys": sorted(dedup_keys)[-500:],
    }


def render_log_digest_html(
    title: str, summary: dict[str, Any], now: datetime | None = None
) -> str:
    """Render compact HTML for a log digest."""
    now = _aware(now or datetime.now(timezone.utc))
    level_rows = "".join(
        "<tr>"
        f"<td>{escape(level)}</td>"
        f"<td style=\"text-align:right;font-weight:700\">{count}</td>"
        "</tr>"
        for level, count in sorted(summary["level_counts"].items())
    )
    if not level_rows:
        level_rows = '<tr><td colspan="2">No warnings or errors found.</td></tr>'

    logger_rows = "".join(
        "<tr>"
        f"<td>{escape(logger)}</td>"
        f"<td style=\"text-align:right\">{count}</td>"
        "</tr>"
        for logger, count in list(summary["logger_counts"].items())[:10]
    )
    if not logger_rows:
        logger_rows = '<tr><td colspan="2">No logger activity in this digest.</td></tr>'

    error_items = "".join(
        "<li>"
        f"<strong>{escape(str(entry.get('level') or ''))}</strong> "
        f"{escape(str(entry.get('name') or 'unknown'))}: "
        f"{escape(entry.get('_message') or '')} "
        f"<span style=\"color:#64748b\">x{int(entry.get('count') or 1)}</span>"
        "</li>"
        for entry in summary["recent_errors"]
    )
    if not error_items:
        error_items = "<li>No recent errors.</li>"

    warning_items = "".join(
        "<li>"
        f"{escape(str(entry.get('name') or 'unknown'))}: "
        f"{escape(entry.get('_message') or '')} "
        f"<span style=\"color:#64748b\">x{int(entry.get('count') or 1)}</span>"
        "</li>"
        for entry in summary["recent_warnings"]
    )
    if not warning_items:
        warning_items = "<li>No recent warnings.</li>"

    return f"""<!doctype html>
<html><body style="margin:0;background:#f8fafc;color:#0f172a;font-family:Arial,sans-serif">
  <div style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0">
    <div style="padding:18px 22px;background:#111827;color:#ffffff">
      <h1 style="font-size:20px;margin:0">{escape(title)}</h1>
      <p style="margin:6px 0 0;color:#cbd5e1;font-size:13px">{escape(now.isoformat())}</p>
    </div>
    <div style="padding:18px 22px">
      <p style="font-size:15px;margin:0 0 14px">Total counted warnings/errors: <strong>{summary["total"]}</strong></p>
      <h2 style="font-size:16px;margin:18px 0 8px">Counts by level</h2>
      <table style="width:100%;border-collapse:collapse">{level_rows}</table>
      <h2 style="font-size:16px;margin:18px 0 8px">Top loggers</h2>
      <table style="width:100%;border-collapse:collapse">{logger_rows}</table>
      <h2 style="font-size:16px;margin:18px 0 8px">Top 10 recent errors</h2>
      <ul style="padding-left:18px;line-height:1.5">{error_items}</ul>
      <h2 style="font-size:16px;margin:18px 0 8px">Top 10 recent warnings</h2>
      <ul style="padding-left:18px;line-height:1.5">{warning_items}</ul>
      <p style="margin:20px 0 0;font-size:11px;color:#94a3b8">Generated by HA Tools Email</p>
    </div>
  </div>
</body></html>"""


def build_energy_report_payload(
    devices: Iterable[dict[str, Any]],
    *,
    cadence: str,
    now: datetime | None = None,
    currency: str = DEFAULT_CURRENCY,
    price_per_kwh: float = DEFAULT_PRICE_PER_KWH,
) -> dict[str, Any]:
    """Build an energy report email payload from device usage rows."""
    now = _aware(now or datetime.now(timezone.utc))
    rows = _clean_energy_devices(devices, price_per_kwh)
    total_kwh = sum(row["kwh"] for row in rows)
    total_cost = total_kwh * price_per_kwh
    title = f"{_cadence_label(cadence)} Energy Report"
    subject = f"{title} - {now.date().isoformat()}"
    body_lines = [
        f"{title} - {now.date().isoformat()}",
        f"Total: {total_kwh:.2f} kWh / {total_cost:.2f} {currency}",
        "",
        "Top consumers:",
    ]
    body_lines.extend(
        f"{row['name']}: {row['kwh']:.2f} kWh / {row['cost']:.2f} {currency}"
        for row in rows[:10]
    )
    html = render_energy_report_html(
        title, rows, total_kwh, total_cost, currency, now
    )
    return {
        "subject": subject,
        "body": "\n".join(body_lines),
        "html": html,
        "summary": {
            "total_kwh": total_kwh,
            "total_cost": total_cost,
            "devices": rows,
        },
    }


def render_energy_report_html(
    title: str,
    devices: list[dict[str, Any]],
    total_kwh: float,
    total_cost: float,
    currency: str,
    now: datetime | None = None,
) -> str:
    """Render compact HTML for an energy report."""
    now = _aware(now or datetime.now(timezone.utc))
    rows = "".join(
        "<tr>"
        f"<td style=\"padding:9px 12px;border-bottom:1px solid #e2e8f0\">{escape(row['name'])}</td>"
        f"<td style=\"padding:9px 12px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:700\">{row['kwh']:.2f}</td>"
        f"<td style=\"padding:9px 12px;border-bottom:1px solid #e2e8f0;text-align:right\">{row['cost']:.2f} {escape(currency)}</td>"
        f"<td style=\"padding:9px 12px;border-bottom:1px solid #e2e8f0;text-align:right;color:#64748b\">{row['share']:.0f}%</td>"
        "</tr>"
        for row in devices[:25]
    )
    if not rows:
        rows = '<tr><td colspan="4" style="padding:12px">No energy statistics found.</td></tr>'

    return f"""<!doctype html>
<html><body style="margin:0;background:#f8fafc;color:#0f172a;font-family:Arial,sans-serif">
  <div style="max-width:720px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0">
    <div style="padding:18px 22px;background:#1d4ed8;color:#ffffff">
      <h1 style="font-size:20px;margin:0">{escape(title)}</h1>
      <p style="margin:6px 0 0;color:#dbeafe;font-size:13px">{escape(now.isoformat())}</p>
    </div>
    <div style="padding:18px 22px">
      <p style="font-size:15px;margin:0 0 14px">Total: <strong>{total_kwh:.2f} kWh</strong> / <strong>{total_cost:.2f} {escape(currency)}</strong></p>
      <table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0">
        <thead><tr style="background:#f1f5f9">
          <th style="padding:9px 12px;text-align:left">Consumer</th>
          <th style="padding:9px 12px;text-align:right">kWh</th>
          <th style="padding:9px 12px;text-align:right">Cost</th>
          <th style="padding:9px 12px;text-align:right">Share</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="margin:20px 0 0;font-size:11px;color:#94a3b8">Generated by HA Tools Email</p>
    </div>
  </div>
</body></html>"""


async def async_build_report_payload(
    hass: Any,
    storage: Any,
    *,
    kind: str,
    cadence: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose a report payload from Home Assistant runtime data."""
    if kind == KIND_LOG_DIGEST:
        return await async_build_log_digest_payload(hass, storage, cadence=cadence, now=now)
    if kind == KIND_ENERGY_REPORT:
        return await async_build_energy_report_payload(hass, cadence=cadence, now=now)
    raise ValueError(f"Unsupported report kind: {kind}")


async def async_build_log_digest_payload(
    hass: Any,
    storage: Any,
    *,
    cadence: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose a log digest from system_log records and Store dedup state."""
    now = _aware(now or datetime.now(timezone.utc))
    state = await storage.async_get_digest_state(KIND_LOG_DIGEST, cadence)
    since = _parse_datetime(state.get("last_digest")) or _period_start(cadence, now)
    entries = await async_get_system_log_entries(hass)
    return build_log_digest_payload(
        entries,
        cadence=cadence,
        now=now,
        since=since,
        seen_keys=state.get("seen_keys") or [],
    )


async def async_get_system_log_entries(hass: Any) -> list[dict[str, Any]]:
    """Read entries from Home Assistant's system_log handler."""
    handler = hass.data.get("system_log") if hasattr(hass, "data") else None
    records = getattr(handler, "records", None)
    if records is None:
        return []
    if hasattr(records, "to_list"):
        return list(records.to_list())
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    return []


async def async_build_energy_report_payload(
    hass: Any,
    *,
    cadence: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose an energy report from recorder statistics."""
    now = _aware(now or datetime.now(timezone.utc))
    start = _period_start(cadence, now)
    sensors = discover_energy_sensors(hass)
    devices = await async_get_energy_usage(hass, sensors, start, now, cadence)
    return build_energy_report_payload(
        devices,
        cadence=cadence,
        now=now,
        currency=DEFAULT_CURRENCY,
        price_per_kwh=DEFAULT_PRICE_PER_KWH,
    )


def discover_energy_sensors(hass: Any) -> list[dict[str, Any]]:
    """Auto-discover kWh energy sensors from HA state machine."""
    if not hasattr(hass, "states"):
        return []
    try:
        states = list(hass.states.async_all("sensor"))
    except TypeError:
        states = [
            state
            for state in hass.states.async_all()
            if getattr(state, "entity_id", "").startswith("sensor.")
        ]
    except Exception:
        return []

    sensors: list[dict[str, Any]] = []
    for state in states:
        entity_id = getattr(state, "entity_id", "")
        attrs = getattr(state, "attributes", {}) or {}
        unit = attrs.get("unit_of_measurement")
        state_class = attrs.get("state_class")
        device_class = attrs.get("device_class")
        if device_class != "energy" and not (
            unit in {"kWh", "Wh"} and state_class in {"total", "total_increasing", "measurement"}
        ):
            continue
        if str(getattr(state, "state", "")).lower() in {"unknown", "unavailable"}:
            continue
        sensors.append(
            {
                "entity_id": entity_id,
                "name": attrs.get("friendly_name")
                or entity_id.replace("sensor.", "").replace("_", " ").title(),
                "unit": unit or "kWh",
            }
        )
    return sensors


async def async_get_energy_usage(
    hass: Any,
    sensors: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    cadence: str,
) -> list[dict[str, Any]]:
    """Fetch recorder statistics for energy sensors."""
    if not sensors:
        return []

    try:
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.components.recorder.util import get_instance
    except Exception:
        return []

    statistic_ids = [sensor["entity_id"] for sensor in sensors]
    sensor_by_id = {sensor["entity_id"]: sensor for sensor in sensors}
    period = "hour" if cadence == CADENCE_DAILY else "day"

    try:
        result = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            set(statistic_ids),
            period,
            {},
            {"change"},
        )
    except Exception:
        return []

    devices: list[dict[str, Any]] = []
    for entity_id, points in (result or {}).items():
        sensor = sensor_by_id.get(entity_id)
        if not sensor or not points:
            continue
        total_change = sum(float(point.get("change") or 0) for point in points)
        if sensor.get("unit") == "Wh":
            total_change = total_change / 1000
        if total_change <= 0:
            continue
        devices.append(
            {
                "name": sensor["name"],
                "entity_id": entity_id,
                "kwh": total_change,
            }
        )
    return devices


def _clean_energy_devices(
    devices: Iterable[dict[str, Any]], price_per_kwh: float
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device in devices:
        try:
            kwh = float(device.get("kwh", device.get("month", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if kwh <= 0:
            continue
        rows.append(
            {
                "name": str(device.get("name") or device.get("entity_id") or "Unknown"),
                "entity_id": str(device.get("entity_id") or ""),
                "kwh": kwh,
                "cost": kwh * price_per_kwh,
                "share": 0.0,
            }
        )
    rows.sort(key=lambda row: row["kwh"], reverse=True)
    total = sum(row["kwh"] for row in rows)
    if total > 0:
        for row in rows:
            row["share"] = row["kwh"] / total * 100
    return rows


def _render_log_plain(title: str, summary: dict[str, Any]) -> str:
    lines = [
        title,
        f"Total counted warnings/errors: {summary['total']}",
        "",
        "Counts by level:",
    ]
    if summary["level_counts"]:
        lines.extend(
            f"- {level}: {count}"
            for level, count in sorted(summary["level_counts"].items())
        )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Top loggers:")
    if summary["logger_counts"]:
        lines.extend(
            f"- {logger}: {count}"
            for logger, count in list(summary["logger_counts"].items())[:10]
        )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Top 10 recent errors:")
    if summary["recent_errors"]:
        lines.extend(
            f"- {entry.get('level')} {entry.get('name')}: {entry.get('_message')} "
            f"x{int(entry.get('count') or 1)}"
            for entry in summary["recent_errors"]
        )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Top 10 recent warnings:")
    if summary["recent_warnings"]:
        lines.extend(
            f"- {entry.get('name')}: {entry.get('_message')} "
            f"x{int(entry.get('count') or 1)}"
            for entry in summary["recent_warnings"]
        )
    else:
        lines.append("- none")
    return "\n".join(lines)


def _entry_message(entry: dict[str, Any]) -> str:
    message = entry.get("message", "")
    if isinstance(message, list):
        return " ".join(str(item) for item in message)
    return str(message or "")


def _entry_key(entry: dict[str, Any]) -> str:
    return "|".join(
        (
            str(entry.get("level") or ""),
            str(entry.get("name") or ""),
            _entry_message(entry),
        )
    )


def _entry_time(entry: dict[str, Any]) -> datetime | None:
    raw = entry.get("timestamp") or entry.get("first_occurred")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(raw, str):
        return _parse_datetime(raw)
    if isinstance(raw, datetime):
        return _aware(raw)
    return None


def _period_start(cadence: str, now: datetime) -> datetime:
    if cadence == CADENCE_DAILY:
        return now - timedelta(days=1)
    if cadence == CADENCE_WEEKLY:
        return now - timedelta(days=7)
    if cadence == CADENCE_MONTHLY:
        return now - timedelta(days=30)
    return now - timedelta(days=1)


def _cadence_label(cadence: str) -> str:
    return {
        CADENCE_DAILY: "Daily",
        CADENCE_WEEKLY: "Weekly",
        CADENCE_MONTHLY: "Monthly",
    }.get(cadence, str(cadence).title())


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
