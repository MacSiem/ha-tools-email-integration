"""Server-side report composition for HA Tools Email."""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from html import escape
from math import fsum, isfinite
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from .energy_measurements import calendar_grid, source_cells, valid_provenance, validated_source_ids
except ImportError:  # Existing pure tests also load composer directly by filename.
    import importlib.util
    from pathlib import Path
    _measurements_spec = importlib.util.spec_from_file_location(
        'ha_tools_email_measurements', Path(__file__).with_name('energy_measurements.py'))
    _measurements = importlib.util.module_from_spec(_measurements_spec)
    _measurements_spec.loader.exec_module(_measurements)
    calendar_grid = _measurements.calendar_grid
    source_cells = _measurements.source_cells
    valid_provenance = _measurements.valid_provenance
    validated_source_ids = _measurements.validated_source_ids

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


try:
    from .mail_charts import (
        attach_mail_charts, append_mail_html, build_energy_chart_model, build_log_chart_model,
        render_charts_html, render_charts_plain, safe_energy_url,
    )
except ImportError:  # Keep existing direct-file pure-test loading supported.
    import importlib.util
    from pathlib import Path
    _charts_spec = importlib.util.spec_from_file_location(
        "ha_tools_email_static_charts", Path(__file__).with_name("mail_charts.py")
    )
    _charts = importlib.util.module_from_spec(_charts_spec)
    _charts_spec.loader.exec_module(_charts)
    attach_mail_charts = _charts.attach_mail_charts
    append_mail_html = _charts.append_mail_html
    build_energy_chart_model = _charts.build_energy_chart_model
    build_log_chart_model = _charts.build_log_chart_model
    render_charts_html = _charts.render_charts_html
    render_charts_plain = _charts.render_charts_plain
    safe_energy_url = _charts.safe_energy_url


def _mail_text(value: str) -> str:
    """Keep Unicode scalars/pairs, replacing unpaired surrogates for UTF-8 mail."""
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _presentation(language: str | None = None, display_timezone: str | None = None) -> dict[str, Any]:
    """Per-message presentation only; never changes the calculation calendar."""
    language = "pl" if str(language or "en").lower().replace("_", "-").split("-")[0] == "pl" else "en"
    try:
        zone = ZoneInfo(display_timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = timezone.utc
    return {"language": language, "zone": zone}


def _ha_presentation(hass: Any) -> dict[str, Any]:
    config = getattr(hass, "config", None)
    return _presentation(getattr(config, "language", None), getattr(config, "time_zone", None))


def _report_date(value: datetime, presentation: dict[str, Any]) -> str:
    local = _aware(value).astimezone(presentation["zone"])
    months = (("sty", "lut", "mar", "kwi", "maj", "cze", "lip", "sie", "wrz", "paź", "lis", "gru")
              if presentation["language"] == "pl" else
              ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))
    # Include offset: the repeated DST hour remains unambiguous.
    return f"{local.day:02d} {months[local.month-1]} {local.year}, {local:%H:%M %Z} ({local:%z})"


def _energy_chart_label(chart: dict[str, Any], row: dict[str, Any],
                        model: dict[str, Any], presentation: dict[str, Any]) -> str:
    """Render absolute bounds without changing the measured calendar-day identity."""
    granularity = chart.get("granularity")
    if (chart.get("id") not in {"energy-usage", "energy-cost", "variable-fees"}
            or granularity not in {"hour", "calendar_day"}):
        return row["label"]
    try:
        start = datetime.fromtimestamp(row["from"] / 1000, timezone.utc)
        end = datetime.fromtimestamp(row["to"] / 1000, timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return row["label"]
    # Daily totals belong to the report calendar, even when HA displays another day.
    local = (_presentation(presentation["language"], model.get("timezone"))
             if granularity == "calendar_day" else presentation)
    bounds = f"{_report_date(start, local)} → {_report_date(end, local)}"
    if granularity == "calendar_day":
        return f"{bounds} ({row['hours']}h; {model['timezone']})"
    return bounds


def _title(kind: str, cadence: str, presentation: dict[str, Any]) -> str:
    pl = presentation["language"] == "pl"
    label = ({"daily": "dzienny", "weekly": "tygodniowy", "monthly": "miesięczny"}.get(cadence, cadence)
             if pl else _cadence_label(cadence))
    if kind == KIND_LOG_DIGEST:
        return f"HA Log Digest – {label}"
    return f"Raport energii – {label}" if pl else f"{label} Energy Report"


def _date_line(value: datetime, presentation: dict[str, Any]) -> str:
    return ("Data raportu" if presentation["language"] == "pl" else "Report date") + " · " + _report_date(value, presentation)


def _subject(title: str, value: datetime, presentation: dict[str, Any]) -> str:
    return f"{title} – {_report_date(value, presentation)}"


def _finish_plain(payload: dict[str, Any], date: datetime, presentation: dict[str, Any]) -> None:
    footer = "Wygenerowano przez HA Tools Email" if presentation["language"] == "pl" else "Generated by HA Tools Email"
    technical = "Czas techniczny UTC" if presentation["language"] == "pl" else "Technical time UTC"
    ending = f"\n\n{technical} · {_aware(date).astimezone(timezone.utc).isoformat()}\n{footer}"
    body = payload['body'].rstrip()
    caveat = 'Occurrences in retained aggregates; not exact events or complete event history.'
    if payload['summary'].get('visualization', {}).get('context') == caveat:
        body += '\n\nHow to read these numbers ¹\n' + caveat
    payload['body'] = body + ending


def _mail_styles() -> str:
    # Explicit light excludes both the media rule and Outlook's dark selector.
    def rules(prefix: str, dark: bool) -> str:
        page, surface, card, text, muted = (("#101923", "#182533", "#223548", "#f1f5f9", "#cbd5e1") if dark else
                                          ("#edf2f7", "#ffffff", "#edf3ff", "#172b4d", "#475569"))
        return (f"{prefix} .mail-page{{background:{page}!important;color:{text}!important}}"
                f"{prefix} .mail-surface{{background:{surface}!important;color:{text}!important}}"
                f"{prefix} .mail-card{{background:{card}!important;color:{text}!important}}"
                f"{prefix} .mail-content p,{prefix} .mail-content td,{prefix} .mail-content span:not(.mail-severity),"
                f"{prefix} .mail-content caption{{color:{text}!important}}"
                f"{prefix} .mail-content .mail-muted{{color:{muted}!important}}"
                f"{prefix} .mail-content a{{color:{'#93c5fd' if dark else '#1d4ed8'}!important}}")
    return (":root{color-scheme:light dark;supported-color-schemes:light dark}"
            + "@media (prefers-color-scheme:dark){" + rules('html:not([data-mail-preview-theme="light"])', True) + "}"
            + rules('[data-ogsc] html:not([data-mail-preview-theme="light"])', True)
            + rules('html[data-ogsc]:not([data-mail-preview-theme="light"])', True)
            + rules('html:not([data-mail-preview-theme="light"]) [data-ogsc]', True)
            + rules('html[data-mail-preview-theme="dark"]', True)
            + rules('html[data-mail-preview-theme="light"]', False)
            + ".mail-header{background:#41627d!important;color:#ffffff!important}"
            + ".mail-header p,.mail-header h1{color:#ffffff!important}"
            + ".mail-content .mail-severity{color:#ffffff!important;display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:700}"
            + ".mail-content .mail-severity-warning{background:#854d0e!important}"
            + ".mail-content .mail-severity-error,.mail-content .mail-severity-critical{background:#b91c1c!important}"
            + ".mail-content .mail-severity-other{background:#475569!important}")


def _severity_badge(level: str) -> str:
    level = str(level or "UNKNOWN").upper()
    tone = level.lower() if level in {"WARNING", "ERROR", "CRITICAL"} else "other"
    color = {"warning": "#854d0e", "error": "#b91c1c", "critical": "#b91c1c"}.get(tone, "#475569")
    return (f'<span class="mail-severity mail-severity-{tone}" '
            f'style="display:inline-block;background:{color};color:#ffffff;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:700">{escape(level)}</span>')


def _report_frame(title: str, date: datetime, content: str, *, accent: str = "#0f766e",
                  presentation: dict[str, Any] | None = None) -> str:
    """Email-safe, self-contained layout shared by both report products."""
    presentation = presentation or _presentation()
    reading = ""
    marker = '<section class="mail-how-to-read">'
    if marker in content:
        before, _, after = content.partition(marker)
        section, close, rest = after.partition('</section>')
        if close:
            reading = marker + section + close
            content = before + rest
    footer = "Wygenerowano przez HA Tools Email" if presentation["language"] == "pl" else "Generated by HA Tools Email"
    technical = "Czas techniczny UTC" if presentation["language"] == "pl" else "Technical time UTC"
    return f'''<!doctype html>
<html lang="{presentation['language']}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="ha-tools-mail-presentation" content="1">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark"><style>{_mail_styles()}</style></head>
<body class="mail-page" style="margin:0;background:#edf2f7;color:#172b4d;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.55;word-break:break-word;overflow-wrap:anywhere">
<table class="mail-page" role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#edf2f7"><tr><td align="center" style="padding:24px 8px">
<table class="mail-surface" role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;background:#ffffff;border:1px solid #dce5ef;border-radius:16px;overflow:hidden">
<tr><td class="mail-header" bgcolor="#41627d" style="padding:28px 24px;border-top:6px solid {accent};color:#ffffff">
<p style="margin:0 0 12px;color:#ffffff;font-size:12px;font-weight:700;letter-spacing:2px">HOME ASSISTANT / HA TOOLS</p>
<h1 style="margin:0;font-size:27px;line-height:1.2;color:#ffffff">{escape(title)}</h1>
<p style="margin:10px 0 0;color:#ffffff;font-size:14px">{escape(_date_line(date, presentation))}</p>
</td></tr><tr><td class="mail-content" style="padding:24px 20px">{content}
<!-- HA_TOOLS_REPORT_SECTIONS -->
{reading}
<p class="mail-muted" style="border-top:1px solid #dce5ef;padding-top:18px;margin:24px 0 0;color:#52647b;font-size:12px">{technical} · {escape(_aware(date).astimezone(timezone.utc).isoformat())}</p>
<p class="mail-muted" style="color:#52647b;font-size:12px">{footer}</p>
</td></tr></table></td></tr></table></body></html>'''


def build_log_digest_payload(
    entries: Iterable[dict[str, Any]],
    *,
    cadence: str,
    now: datetime | None = None,
    since: datetime | None = None,
    seen_keys: Iterable[str] | None = None,
    max_entries: int = 10,
    presentation: dict[str, Any] | None = None,
    _finalize: bool = True,
) -> dict[str, Any]:
    """Build a log digest email payload from system_log-like entries."""
    now = _aware(now or datetime.now(timezone.utc))
    summary = summarize_log_entries(entries, since=since, seen_keys=seen_keys, until=now, max_entries=max_entries)
    presentation = presentation or _presentation()
    title = _title(KIND_LOG_DIGEST, cadence, presentation)
    subject = _subject(title, now, presentation)
    summary["visualization"] = build_log_chart_model(summary)
    body = _date_line(now, presentation) + "\n" + _render_log_plain(title, summary) + "\n\nStatic chart data\n" + render_charts_plain(summary["visualization"], include_context=False)
    html = render_log_digest_html(title, summary, now, presentation=presentation)
    payload = {
        "subject": _mail_text(subject),
        "body": _mail_text(body),
        "html": _mail_text(html),
        "summary": summary,
        "dedup_keys": summary["dedup_keys"],
    }
    if _finalize:
        _finish_plain(payload, now, presentation)
    return payload


def summarize_log_entries(
    entries: Iterable[dict[str, Any]],
    *,
    since: datetime | None = None,
    seen_keys: Iterable[str] | None = None,
    until: datetime | None = None,
    max_entries: int = 10,
) -> dict[str, Any]:
    """Aggregate system log entries by level/logger with optional dedup state."""
    if type(max_entries) is not int or not 1 <= max_entries <= 1000:
        raise ValueError("max_entries must be an integer between 1 and 1000")
    seen = set(seen_keys or [])
    accepted: list[dict[str, Any]] = []
    dedup_keys = set(seen)
    since = _aware(since) if since else None

    for raw in _group_retained_logs(entries, until=until):
        when = _entry_time(raw)
        if until and when and when > _aware(until):
            continue
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

    level_counts: dict[str, int | None] = {}
    logger_counts: dict[str, int | None] = {}
    for entry in accepted:
        level = str(entry.get("level") or "UNKNOWN").upper()
        logger = str(entry.get("name") or "unknown")
        count = entry["count"]
        level_counts[level] = _log_count_sum(level_counts.get(level, 0), count)
        logger_counts[logger] = _log_count_sum(logger_counts.get(logger, 0), count)

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
        "total": _log_count_total(level_counts.values()),
        "unique_problems": len(accepted),
        "max_entries": max_entries,
        "counts_complete": all(entry["count"] is not None for entry in accepted) and _log_count_total(level_counts.values()) is not None,
        "source": {"name": "system_log", "complete": False, "count_semantics": "retained_aggregates",
                   "unknown_counts": sum(entry["count"] is None for entry in accepted)},
        "level_counts": level_counts,
        "logger_counts": dict(
            sorted(logger_counts.items(), key=lambda item: (item[1] is not None, item[1] if item[1] is not None else 0), reverse=True)
        ),
        "recent_errors": recent_errors[:max_entries],
        "recent_warnings": recent_warnings[:max_entries],
        "entries": accepted,
        "dedup_keys": sorted(dedup_keys)[-500:],
    }


def render_log_digest_html(
    title: str, summary: dict[str, Any], now: datetime | None = None, *,
    presentation: dict[str, Any] | None = None,
) -> str:
    """Render compact HTML for a log digest."""
    now = _aware(now or datetime.now(timezone.utc))
    level_rows = "".join(
        "<tr>"
        f"<td>{_severity_badge(level)}</td>"
        f"<td style=\"text-align:right;font-weight:700\">{_log_count_text(count)}</td>"
        "</tr>"
        for level, count in sorted(summary["level_counts"].items())
    )
    if not level_rows:
        level_rows = '<tr><td colspan="2">No warnings or errors found.</td></tr>'

    logger_rows = "".join(
        "<tr>"
        f"<td>{escape(logger)}</td>"
        f"<td style=\"text-align:right\">{_log_count_text(count)}</td>"
        "</tr>"
        for logger, count in list(summary["logger_counts"].items())[:10]
    )
    if not logger_rows:
        logger_rows = '<tr><td colspan="2">No logger activity in this digest.</td></tr>'

    error_items = "".join(
        "<li>"
        f"{_severity_badge(entry.get('level') or 'UNKNOWN')} "
        f"{escape(str(entry.get('name') or 'unknown'))}: "
        f"{escape(entry.get('_message') or '')} "
        f"<span style=\"color:#64748b\">x{_log_count_text(entry.get('count'))}</span>"
        "</li>"
        for entry in summary["recent_errors"]
    )
    if not error_items:
        error_items = "<li>No recent errors.</li>"

    warning_items = "".join(
        "<li>"
        f"{_severity_badge(entry.get('level') or 'WARNING')} "
        f"{escape(str(entry.get('name') or 'unknown'))}: "
        f"{escape(entry.get('_message') or '')} "
        f"<span style=\"color:#64748b\">x{_log_count_text(entry.get('count'))}</span>"
        "</li>"
        for entry in summary["recent_warnings"]
    )
    if not warning_items:
        warning_items = "<li>No recent warnings.</li>"

    empty_history = summary.get('history_unavailable', False)
    metric = 'Unavailable' if empty_history else _log_count_text(summary['total'])
    label = 'Period history' if empty_history else 'Retained warning / error occurrences'
    notice = ('The Home Assistant buffer has no retained entries in this report period. '
              'A restart or buffer eviction can remove older entries; this is not evidence of zero errors.'
              if empty_history else
              'Counts describe retained aggregates, not exact events or complete history.')
    if empty_history:
        return _report_frame(title, now,
            '<div class="mail-card" style="padding:22px;background:#fff7e6;border:1px solid #e4be71;border-radius:12px">'
            '<p style="margin:0;color:#704500;font-size:13px;font-weight:700">PERIOD HISTORY</p>'
            '<p style="margin:6px 0;color:#704500;font-size:30px;font-weight:700">Unavailable</p>'
            f'<p style="margin:12px 0 0;color:#704500">{notice}</p></div>'
            + render_charts_html(build_log_chart_model(summary), embedded=True, context_only=True),
            accent='#3b6fd8', presentation=presentation)
    return _report_frame(title, now, f"""
      <div class="mail-card" style="padding:22px;background:#edf3ff;border:1px solid #c5d7f7;border-radius:12px">
      <p style="margin:0;color:#344f7c;font-size:13px;font-weight:700">{label.upper()}</p>
      <p style="margin:6px 0;color:#1e3a5f;font-size:36px;font-weight:700;line-height:1.1">{metric}</p>
      <p style="margin:12px 0 0;color:#344f7c">{notice}</p></div>
      <h2 style="font-size:16px;margin:18px 0 8px">Counts by level</h2>
      <table style="width:100%;table-layout:fixed;border-collapse:collapse">{level_rows}</table>
      <h2 style="font-size:16px;margin:18px 0 8px">Top loggers</h2>
      <table style="width:100%;table-layout:fixed;border-collapse:collapse">{logger_rows}</table>
      <h2 style="font-size:16px;margin:18px 0 8px">Top {summary.get("max_entries", 10)} recent errors</h2>
      <ul style="padding-left:18px;line-height:1.5">{error_items}</ul>
      <h2 style="font-size:16px;margin:18px 0 8px">Top {summary.get("max_entries", 10)} recent warnings</h2>
      <ul style="padding-left:18px;line-height:1.5">{warning_items}</ul>
      {render_charts_html(summary.get("visualization") or build_log_chart_model(summary), embedded=True)}
      """, accent="#3b6fd8", presentation=presentation)


def build_energy_report_payload(
    devices: Iterable[dict[str, Any]],
    *,
    cadence: str,
    now: datetime | None = None,
    currency: str = DEFAULT_CURRENCY,
    price_per_kwh: float | None = None,
    interactive_url: str | None = None,
    _include_charts: bool = True,
    source_kind: str = "consumers",
    coverage: dict[str, Any] | None = None,
    presentation: dict[str, Any] | None = None,
    _finalize: bool = True,
) -> dict[str, Any]:
    """Build an energy report email payload from device usage rows."""
    now = _aware(now or datetime.now(timezone.utc))
    rows = _clean_energy_devices(devices, price_per_kwh)
    recorded_kwh = sum(row["kwh"] for row in rows) if rows else None
    complete = coverage is None or coverage.get("complete") is True
    total_kwh = recorded_kwh if complete else None
    rate = _energy_rate(price_per_kwh)
    total_cost = total_kwh * rate if total_kwh is not None and rate is not None else None
    summary = {"total_kwh": total_kwh, "total_cost": total_cost, "devices": rows}
    if coverage is not None:
        summary.update(recorded_kwh=recorded_kwh, coverage=coverage)
    summary["visualization"] = build_energy_chart_model({**summary, "currency": currency}, interactive_url=interactive_url, source_kind=source_kind)
    presentation = presentation or _presentation()
    title = _title(KIND_ENERGY_REPORT, cadence, presentation)
    subject = _subject(title, now, presentation)
    body_lines = [
        title,
        _date_line(now, presentation),
        (f"Total: {total_kwh:.2f} kWh / {_energy_cost_text(total_cost, currency)}"
         if total_kwh is not None else
         f"Recorded usage: {recorded_kwh:.2f} kWh. Incomplete coverage; full-period total unavailable."
         if recorded_kwh is not None else "No energy statistics found for this period."),
        "",
        "Grid import sources:" if source_kind == "grid" else "Top consumers:",
    ]
    body_lines.extend(
        f"{row['name']}: {row['kwh']:.2f} kWh / {_energy_cost_text(row['cost'], currency)}"
        + (f" · {row['measured_hours']}/{row['expected_hours']} measured hours"
           if 'measured_hours' in row else '')
        for row in rows[:10]
    )
    if coverage is not None:
        body_lines.extend(['', f"Data coverage: {coverage['measured_source_hours']}/{coverage['expected_source_hours']} source-hours; "
                           f"{coverage['complete_sources']}/{coverage['sources']} complete sources."])
    html = render_energy_report_html(
        title, rows, total_kwh, total_cost, currency, now,
        visualization=summary["visualization"], include_charts=_include_charts, source_kind=source_kind,
        coverage=coverage, recorded_kwh=recorded_kwh, presentation=presentation,
    )
    payload = {
        "subject": subject,
        "body": "\n".join(body_lines) + ("\n\nStatic chart data\n" + render_charts_plain(summary["visualization"], row_label=lambda chart, row: _energy_chart_label(chart, row, summary["visualization"], presentation)) if _include_charts else ""),
        "html": html,
        "summary": summary,
    }
    if _finalize:
        _finish_plain(payload, now, presentation)
    return payload


def render_energy_report_html(
    title: str,
    devices: list[dict[str, Any]],
    total_kwh: float | None,
    total_cost: float | None,
    currency: str,
    now: datetime | None = None,
    *,
    visualization: dict[str, Any] | None = None,
    interactive_url: str | None = None,
    include_charts: bool = True,
    source_kind: str = "consumers",
    coverage: dict[str, Any] | None = None,
    recorded_kwh: float | None = None,
    presentation: dict[str, Any] | None = None,
) -> str:
    """Render compact HTML for an energy report."""
    now = _aware(now or datetime.now(timezone.utc))
    if visualization is None:
        visualization = build_energy_chart_model(
            {"devices": devices, "currency": currency}, interactive_url=interactive_url, source_kind=source_kind
        )
    has_costs = any(row.get('cost') is not None for row in devices)
    rows = "".join(
        "<tr>"
        f"<td style=\"padding:12px 8px;border-bottom:1px solid #e2e8f0\">{escape(row['name'])}"
        + (f'<br><span style="font-size:12px;color:#52647b">{row["measured_hours"]}/{row["expected_hours"]} measured hours</span>'
           if "measured_hours" in row else "") + "</td>"
        f"<td style=\"padding:9px 4px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:700;white-space:nowrap\">{row['kwh']:.2f}</td>"
        + (f'<td style="padding:9px 4px;border-bottom:1px solid #e2e8f0;text-align:right;white-space:nowrap">{escape(_energy_cost_text(row["cost"], currency))}</td>' if has_costs else '') +
        f"<td style=\"padding:9px 4px;border-bottom:1px solid #e2e8f0;text-align:right;color:#64748b;white-space:nowrap\">{row['share']:.0f}%</td>"
        "</tr>"
        for row in devices[:25]
    )
    if not rows:
        rows = '<tr><td colspan="3" style="padding:12px">No energy statistics found.</td></tr>'
    total_text = (f"Total: {total_kwh:.2f} kWh / {_energy_cost_text(total_cost, currency)}"
                  if total_kwh is not None else
                  f"Recorded usage: {recorded_kwh:.2f} kWh. Incomplete coverage; full-period total unavailable."
                  if recorded_kwh is not None else "No energy statistics found for this period.")
    value = total_kwh if total_kwh is not None else recorded_kwh
    metric = f'{value:,.2f}' if value is not None else 'Unavailable'
    label = 'Recorded usage' if total_kwh is None and recorded_kwh is not None else 'Measured energy'
    coverage_html = ''
    if coverage is not None:
        measured, expected = coverage['measured_source_hours'], coverage['expected_source_hours']
        percent = measured / expected * 100 if expected else 0
        color = '#0f766e' if coverage['complete'] else '#a15c00'
        coverage_html = (f'<p style="margin:16px 0 6px;font-size:14px;color:{color}"><strong>Data coverage · {percent:.1f}%</strong>'
                         f' · {measured}/{expected} source-hours · {coverage["complete_sources"]}/{coverage["sources"]} complete sources</p>'
                         f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td width="{percent:.5g}%" bgcolor="{color}" height="6"></td>'
                         f'<td width="{100-percent:.5g}%" bgcolor="#e2e8f0" height="6"></td></tr></table>')
    return _report_frame(title, now, f"""
      <div class="mail-card" style="padding:22px;background:#eaf7f4;border:1px solid #b6ded6;border-radius:12px">
      <p style="margin:0;color:#172b4d;font-size:13px;font-weight:700">{label.upper()}</p>
      <p style="margin:6px 0;color:#115e59;font-size:38px;font-weight:700;line-height:1.1">{metric} <span style="font-size:18px">kWh</span></p>
      <p style="margin:10px 0 0;color:#172b4d">{escape(total_text)}</p></div>
      {coverage_html}
      <h2 style="font-size:18px;margin:26px 0 12px">{"Grid import sources" if source_kind == "grid" else "Top consumers"}</h2>
      <p style="color:#52647b;font-size:13px">Share of recorded usage. Missing readings are never filled with zero.</p>
      <table style="width:100%;table-layout:auto;border-collapse:collapse;border:1px solid #e2e8f0">
        <thead><tr class="mail-card" style="background:#f1f5f9">
          <th style="padding:9px 8px;text-align:left;width:50%">{"Grid import source" if source_kind == "grid" else "Consumer"}</th>
          <th style="padding:9px 4px;text-align:right">kWh</th>
          {'<th style="padding:9px 4px;text-align:right">Cost</th>' if has_costs else ''}
          <th style="padding:9px 4px;text-align:right">Share</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      {render_charts_html(visualization, embedded=True, row_label=lambda chart, row: _energy_chart_label(chart, row, visualization, presentation or _presentation())) if include_charts else ""}
      """, presentation=presentation)


async def async_build_report_payload(
    hass: Any,
    storage: Any,
    *,
    kind: str,
    cadence: str,
    now: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    report_timezone: str | None = None,
    period_semantics: str | None = None,
    max_entries: int | None = None,
    actor=None,
    purpose: str = "report_read",
    report_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose a report payload from Home Assistant runtime data."""
    if kind == KIND_ENERGY_REPORT:
        return await async_build_energy_report_payload(hass,cadence=cadence,now=now,
            window_start=window_start,window_end=window_end,report_timezone=report_timezone,
            actor=actor,purpose=purpose,report_preferences=report_preferences,
            period_semantics=period_semantics)
    presentation = _ha_presentation(hass)
    now = _aware(now or datetime.now(timezone.utc))
    if kind == KIND_LOG_DIGEST:
        payload = await async_build_log_digest_payload(hass, storage, cadence=cadence, now=now, presentation=presentation, _finalize=False,
            window_start=window_start, window_end=window_end,
            **({"manual_window": True} if period_semantics is not None else {}),
            **({"max_entries": max_entries} if max_entries is not None else {}))
    elif kind == KIND_ENERGY_REPORT:
        payload = await async_build_energy_report_payload(hass, cadence=cadence, now=now, presentation=presentation, _finalize=False,
            window_start=window_start, window_end=window_end,
            **({"compare_calendar": False} if period_semantics == "rolling_completed_hours" else {}),
            **({"report_timezone": report_timezone} if report_timezone is not None else {}))
    else:
        raise ValueError(f"Unsupported report kind: {kind}")
    if period_semantics is not None:
        period = {"from": window_start.isoformat(), "to": window_end.isoformat(),
                  "semantics": period_semantics}
        payload["summary"]["period"] = period
        # Keep body and HTML period semantics identical on preview and dispatch.
        label = ({"daily": "Previous completed day", "weekly": "Previous completed ISO week",
                  "monthly": "Previous completed month"}[cadence]
                 if period_semantics == "previous_completed_calendar_period" else
                 {"daily": "Last 24 hours", "weekly": "Last 7 days", "monthly": "Last 30 days"}[cadence])
        if presentation["language"] == "pl":
            label = ({"daily": "Poprzedni pełny dzień", "weekly": "Poprzedni pełny tydzień ISO",
                      "monthly": "Poprzedni pełny miesiąc"}[cadence]
                     if period_semantics == "previous_completed_calendar_period" else
                     {"daily": "Ostatnie 24 godziny", "weekly": "Ostatnie 7 dni", "monthly": "Ostatnie 30 dni"}[cadence])
        prefix, suffix = (("Okres raportu", "Koniec jest wyłączony.") if presentation["language"] == "pl" else
                          ("Report period", "The end is exclusive."))
        context = f"{prefix}: {label}. {_report_date(window_start, presentation)} → {_report_date(window_end, presentation)}. {suffix}"
        payload["body"] += "\n\n" + context
        append_mail_html(payload, f"<p>{escape(context)}</p>")
    _finish_plain(payload, _aware(now or datetime.now(timezone.utc)), presentation)
    # Preview and dispatch expose the same final text, including Energy source
    # names and appended sections. Keep raw summary/source identity untouched.
    for field in ("subject", "body", "html"):
        payload[field] = _mail_text(payload[field])
    return payload


async def async_build_log_digest_payload(
    hass: Any,
    storage: Any,
    *,
    cadence: str,
    now: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    max_entries: int = 10,
    manual_window: bool = False,
    presentation: dict[str, Any] | None = None,
    _finalize: bool = True,
) -> dict[str, Any]:
    """Compose a log digest from system_log records and Store dedup state."""
    presentation = presentation or _ha_presentation(hass)
    now = _aware(now or datetime.now(timezone.utc))
    state = {} if manual_window else await storage.async_get_digest_state(KIND_LOG_DIGEST, cadence)
    since = (window_start - timedelta(microseconds=1) if window_start is not None else
             _parse_datetime(state.get("last_digest")) or _period_start(cadence, now))
    entries = await async_get_system_log_entries(hass)
    retained_entries = entries
    if window_end is not None:
        # Retained aggregates have a last-observed timestamp and lifetime count,
        # not exact event occurrences within a calendar window.
        if manual_window:
            entries = [row for row in entries if _entry_time(row) is not None and
                       window_start <= _entry_time(row) < window_end and _entry_time(row) <= now]
        else:
            entries = [row for row in entries if _entry_time(row) is None or _entry_time(row) < window_end]
    payload = build_log_digest_payload(
        entries,
        cadence=cadence,
        now=now,
        since=since,
        seen_keys=[] if window_start is not None else state.get("seen_keys") or [],
        max_entries=max_entries, presentation=presentation, _finalize=False,
    )
    source = {**payload["summary"].get("source", {}), "name": "system_log", "count_semantics": "retained_aggregates",
              "complete": False, "retained_entries": len(entries),
              "reason": "retained_last_observed_aggregates_not_full_event_history"}
    payload["summary"]["source"] = source
    if not payload["summary"]["unique_problems"]:
        # A volatile, bounded buffer cannot prove an empty completed calendar period.
        payload["summary"].update(total=None, counts_complete=False, history_unavailable=True)
        title = _title(KIND_LOG_DIGEST, cadence, presentation)
        payload["body"] = (_date_line(now, presentation) + "\n" + _render_log_plain(title, payload["summary"])
                           )
        payload["html"] = render_log_digest_html(title, payload["summary"], now, presentation=presentation)
        explanation = ('Period history unavailable: no retained entries in the requested window. '
                       'Home Assistant restarts and buffer eviction can remove history; zero errors is not confirmed.')
        payload["body"] += '\n\n' + explanation
        if window_end is not None:
            current = [row for row in retained_entries if _entry_time(row) is not None and
                       window_end <= _entry_time(row) <= now]
            if current:
                current_summary = summarize_log_entries(current, until=now, max_entries=max_entries)
                payload['summary']['current_context'] = current_summary
                label = 'Current diagnostics · outside the requested report period'
                current_model = build_log_chart_model(current_summary)
                for chart in current_model['charts']:
                    chart['title'] = 'Current ' + chart['title'].lower()
                details = ''.join('<li>' + _severity_badge(row.get('level') or 'UNKNOWN') + ' <strong>' + escape(str(row.get('name') or 'unknown')) + '</strong>: '
                                  + escape(row.get('_message') or '') + '</li>'
                                  for row in (current_summary['recent_errors'] + current_summary['recent_warnings'])[:max_entries])
                append_mail_html(payload, '<h2 style="font-size:20px;margin:28px 0 10px">' + label + '</h2>'
                    + '<p>Available now in the current HA buffer. These lifetime aggregates are separate from the requested period.</p>'
                    + render_charts_html(current_model, embedded=True, include_context=False) + '<ul>' + details + '</ul>')
                payload['body'] += ('\n\n' + label + '\n' + _render_log_plain(label, current_summary)
                                    + '\n' + render_charts_plain(current_model, include_context=False))
    context = "Counts describe retained aggregates, not exact events or complete history."
    if window_start is not None and window_end is not None:
        payload["summary"]["period"] = {"from": window_start.isoformat(), "to": window_end.isoformat(),
                                        "semantics": "previous_completed_calendar_period"}
        context += f" Last-observed window: {_report_date(window_start, presentation)} → {_report_date(window_end, presentation)}."
    payload["body"] += "\n\n" + context
    append_mail_html(payload, f"<p>{escape(context)}</p>")
    if _finalize:
        _finish_plain(payload, now, presentation)
    return payload


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


def _energy_grid_import_ids(prefs: dict[str, Any]) -> list[str]:
    """Read unique grid imports with the frontend's flat-before-flow rule."""
    sources = prefs.get("energy_sources", [])
    if not isinstance(sources, list):
        return []
    ids: dict[str, None] = {}
    for source in sources:
        if not isinstance(source, dict) or source.get("type") != "grid":
            continue
        flat = source.get("stat_energy_from")
        if flat:
            candidates = [flat]
        else:
            flows = source.get("flow_from", [])
            candidates = [flow.get("stat_energy_from") for flow in flows
                          if isinstance(flow, dict)] if isinstance(flows, list) else []
        for sid in candidates:
            if isinstance(sid, str) and sid.strip():
                ids.setdefault(sid, None)
    return list(ids)


async def async_grid_sensors(hass: Any) -> list[dict[str, Any]]:
    """Resolve precisely the configured Energy Dashboard grid imports."""
    sensors = []
    try:
        from homeassistant.components.energy.data import async_get_manager
        manager = await async_get_manager(hass)
        prefs = manager.data or {}
        ids = _energy_grid_import_ids(prefs)
        for sid in ids:
            state = hass.states.get(sid) if hasattr(hass.states, "get") else None
            sensors.append({"entity_id": sid, "name": (getattr(state, "attributes", {}) or {}).get("friendly_name") or sid})
    except Exception:
        # A partially resolved source set cannot represent complete grid import.
        return []
    return sensors


async def async_build_energy_report_payload(
    hass: Any, *, cadence: str, now: datetime | None = None,
    window_start: datetime | None = None, window_end: datetime | None = None,
    report_timezone: str | None = None, compare_calendar: bool = True,
    presentation: dict[str, Any] | None = None, _finalize: bool = True,
    actor=None, purpose: str = 'report_read', report_preferences=None,
    period_semantics: str | None = None,
) -> dict[str, Any]:
    """Actual Energy composition: schema2 producer and its common renderer only."""
    from .report_window import resolve_report_window
    bucket = hass.data.get('ha_tools_email',{})
    service = bucket.get('energy_analysis')
    if service is None:
        raise RuntimeError('energy_analysis_not_ready')
    if (window_start is None) != (window_end is None):
        raise ValueError('invalid_report_window')
    # Existing caller defaults are preserved only as legacy presentation context.
    presentation = presentation or _ha_presentation(hass)
    zone = report_timezone or getattr(hass.config,'time_zone','UTC')
    mode = 'calendar' if period_semantics == 'previous_completed_calendar_period' or (
        period_semantics is None and window_start is not None and compare_calendar) else 'rolling'
    request = resolve_report_window(cadence,now or datetime.now(timezone.utc),zone,mode=mode,
        bounds=(window_start,window_end) if window_start is not None else None)
    preferences = dict(report_preferences) if report_preferences is not None else {
        'language':presentation['language'],
        # HA YAML/ZoneInfo can retain a str subclass in this trusted default.
        # Keep explicit caller preferences under the unchanged strict validator.
        'timezone_name':str(getattr(presentation['zone'],'key','UTC')),'hour_cycle':'24'}
    return await service.async_prepare_report(request,actor,purpose=purpose,**preferences)


def _calendar_comparison_window(
    cadence: str, start: datetime, end: datetime, timezone_name: str | None, explicit: bool,
) -> dict[str, Any]:
    """Align full or partial calendar windows; never reinterpret manual rolling usage.

    Midnight resolution mirrors scheduler._resolve_wall: first fold and UTC roundtrip
    for gaps. Kept pure here to avoid importing the scheduler's send/composer chain.
    """
    comparison = {"status": "unknown", "reason": "rolling_or_custom_window",
                  "previous_kwh": None, "delta_kwh": None, "percent": None}
    if not explicit:
        return comparison
    try:
        zone = ZoneInfo(timezone_name) if isinstance(timezone_name, str) else None
    except (ValueError, ZoneInfoNotFoundError):
        zone = None
    if zone is None:
        return {**comparison, "reason": "report_timezone_unavailable"}
    if (not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.utcoffset() is None or end.utcoffset() is None):
        return comparison
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def midnight(day):
        return datetime(day.year, day.month, day.day, tzinfo=zone, fold=0).astimezone(timezone.utc)

    day = start.astimezone(zone).date()
    if start != midnight(day):
        return comparison
    if cadence == CADENCE_DAILY:
        limit_day = day + timedelta(days=1)
        previous_day = day - timedelta(days=1)
    elif cadence == CADENCE_WEEKLY and day.weekday() == 0:
        limit_day = day + timedelta(days=7)
        previous_day = day - timedelta(days=7)
    elif cadence == CADENCE_MONTHLY and day.day == 1:
        limit_day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        previous_day = (day - timedelta(days=1)).replace(day=1)
    elif cadence == 'yearly' and day.month == 1 and day.day == 1 and day.year > 1:
        limit_day = day.replace(year=day.year + 1)
        previous_day = day.replace(year=day.year - 1)
    else:
        return comparison
    if not start < end <= midnight(limit_day):
        return comparison
    partial = end < midnight(limit_day)
    previous_start = midnight(previous_day)
    previous_end, clamped = start, False
    if partial:
        local_end = end.astimezone(zone)
        if cadence == CADENCE_MONTHLY:
            last_day = monthrange(previous_day.year, previous_day.month)[1]
            if local_end.day > last_day:
                # A shorter prior month cannot supply nonexistent extra days.
                # Compare its actual extent and expose this alignment limit.
                target_day, clamped = day, True
            else:
                target_day = previous_day.replace(day=local_end.day)
        elif cadence == 'yearly':
            target_month_days = monthrange(previous_day.year, local_end.month)[1]
            target_day = previous_day.replace(month=local_end.month,
                                             day=min(local_end.day, target_month_days))
            clamped = local_end.day > target_month_days
        else:
            target_day = previous_day + (local_end.date() - day)
        if cadence == CADENCE_MONTHLY and clamped:
            previous_end = start
        else:
            wall = datetime(target_day.year, target_day.month, target_day.day,
                local_end.hour, local_end.minute, local_end.second, local_end.microsecond,
                tzinfo=zone, fold=local_end.fold)
            previous_end = wall.astimezone(timezone.utc)
            clamped = clamped or (previous_end.astimezone(zone).replace(tzinfo=None)
                                   != wall.replace(tzinfo=None))
    current_hours = (end - start).total_seconds() / 3600
    previous_hours = (previous_end - previous_start).total_seconds() / 3600
    comparison.update(timezone=timezone_name, current_start=start.isoformat(), current_end=end.isoformat(),
                      previous_start=previous_start.isoformat(), previous_end=previous_end.isoformat(),
                      current_local_start=start.astimezone(zone).isoformat(),
                      current_local_end=end.astimezone(zone).isoformat(),
                      previous_local_start=previous_start.astimezone(zone).isoformat(),
                      previous_local_end=previous_end.astimezone(zone).isoformat(),
                      current_hours=current_hours, previous_hours=previous_hours,
                      partial=partial, calendar_clamped=clamped, calendar_kind=cadence)
    # Recorder hourly changes cannot truthfully resolve half/quarter-hour boundaries.
    if any(bound.timestamp() % 3600 for bound in (previous_start, previous_end, start, end)):
        return {**comparison, "reason": "calendar_boundary_not_recorder_hour"}
    return {**comparison, "status": "pending", "reason": None}


def _compare_energy_snapshots(current, previous, comparison):
    """Only compare complete, same-source snapshots with explicit compatible provenance."""
    if "source_buckets" in current or "source_buckets" in previous:
        return _compare_source_intervals(current, previous, comparison)
    result = {**comparison, "status": "unknown", "reason": "incomplete_statistics",
              "delta_kwh": None, "percent": None}

    def measured(snapshot):
        ids = snapshot.get("source_ids") or []
        devices = snapshot.get("devices") or []
        if (snapshot.get("status") != "ready" or not ids or len(set(ids)) != len(ids)
                or len(devices) != len(ids) or {r.get("entity_id") for r in devices} != set(ids)):
            return None
        values = [row.get("kwh") for row in devices]
        if any(type(v) not in (int, float) or not isfinite(v) or v < 0 for v in values):
            return None
        total = sum(values)
        return total if isfinite(total) else None

    current_kwh, previous_kwh = measured(current), measured(previous)
    # An independently measured zero is useful even if a comparison is unavailable.
    result["previous_kwh"] = previous_kwh
    if current_kwh is None or previous_kwh is None:
        return result
    ids = set(current["source_ids"])
    if ids != set(previous["source_ids"]):
        return {**result, "reason": "source_set_changed"}
    provenance = current.get("measurement_provenance")
    prior_provenance = previous.get("measurement_provenance")
    if (not isinstance(provenance, dict) or not isinstance(prior_provenance, dict)
            or set(provenance) != ids or set(prior_provenance) != ids
            or any(not valid_provenance(provenance[sid]) for sid in ids)
            or provenance != prior_provenance):
        return {**result, "reason": "measurement_provenance_unavailable_or_changed"}
    delta = current_kwh - previous_kwh
    percent = delta / previous_kwh * 100 if previous_kwh else None
    return {**result, "status": "ready", "reason": None, "delta_kwh": delta,
            "percent": percent if percent is not None and isfinite(percent) else None}


def _compare_source_intervals(current, previous, comparison):
    """Derive coverage from measured source hours, then compare the common mask.

    Complete calendar totals retain their actual DST/month lengths. Incomplete
    periods compare only matching local source hours, with no extrapolation.
    """
    result = {**comparison, "status": "unknown", "reason": "incomplete_statistics",
              "current_kwh": None, "previous_kwh": None, "delta_kwh": None,
              "percent": None, "full_current_kwh": None, "full_previous_kwh": None}
    try:
        ids, prior_ids = validated_source_ids(current), validated_source_ids(previous)
    except ValueError:
        return result
    if set(ids) != set(prior_ids):
        return {**result, "reason": "source_set_changed"}
    provenance, prior = current.get("measurement_provenance"), previous.get("measurement_provenance")
    if (not isinstance(provenance, dict) or not isinstance(prior, dict)
            or set(provenance) != set(ids) or provenance != prior
            or any(not valid_provenance(provenance[sid]) for sid in ids)):
        return {**result, "reason": "measurement_provenance_unavailable_or_changed"}
    try:
        grids = [calendar_grid(comparison[f'{prefix}_start'], comparison[f'{prefix}_end'],
                               comparison['timezone'], comparison.get('calendar_kind'))
                 for prefix in ('current', 'previous')]
        # Process one source at a time: memory is bounded by two calendar years,
        # rather than a source-count by year matrix.
        measured = [0, 0]
        common_count = 0
        totals, shared = [[], []], [[], []]
        for sid in ids:
            cells = []
            for index, snapshot in enumerate((current, previous)):
                valid = source_cells(snapshot, sid, grids[index])
                measured[index] += len(valid)
                totals[index].append(fsum(valid.values()))
                cells.append(valid)
            common = cells[0].keys() & cells[1].keys()
            common_count += len(common)
            for index in (0, 1):
                shared[index].append(fsum(cells[index][key] for key in common))
        expected = [len(grid) * len(ids) for grid in grids]
        coverage = {"current": measured[0] / expected[0], "previous": measured[1] / expected[1],
                    "common_current": common_count / expected[0], "common_previous": common_count / expected[1],
                    "expected_current_seconds": expected[0] * 3600,
                    "expected_previous_seconds": expected[1] * 3600}
        full = [fsum(totals[i]) if measured[i] == expected[i] else None for i in (0, 1)]
        result.update(coverage=coverage, full_current_kwh=full[0], full_previous_kwh=full[1],
                      previous_kwh=full[1])
        complete = all(value is not None for value in full)
        if not complete and any(coverage[key] < .9 for key in
                               ("current", "previous", "common_current", "common_previous")):
            return result
        values = full if complete else [fsum(values) for values in shared]
        delta = values[0] - values[1]
        percent = delta / values[1] * 100 if values[1] else None
        if any(not isfinite(value) for value in (*values, delta)):
            return result
        return {**result, "status": "ready", "reason": None,
                "comparison_scope": "calendar_totals" if complete else "common_measurements",
                "current_kwh": values[0], "previous_kwh": values[1], "delta_kwh": delta,
                "percent": percent if percent is not None and isfinite(percent) else None}
    except (KeyError, TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        return result


def _energy_comparison_lines(comparison, presentation=None):
    presentation = presentation or _presentation()
    lines = []
    def bound(key):
        return _report_date(datetime.fromisoformat(comparison[key]), presentation)
    if "previous_start" in comparison:
        lines.extend([
            f"Current: [{bound('current_start')} → {bound('current_end')}); "
            f"{comparison['current_hours']:g} elapsed hours.",
            f"Previous: [{bound('previous_start')} → {bound('previous_end')}); "
            f"{comparison['previous_hours']:g} elapsed hours. Timezone: {comparison['timezone']}. "
            f"Display timezone: {presentation['zone']}.",
            ("Common measured source hours only; missing readings are not extrapolated. "
             if comparison.get("comparison_scope") == "common_measurements" else
             "Calendar-period totals; unequal month or DST lengths are not normalized. ") +
            "Both periods use the current configured grid sources; historical configuration is not reconstructed.",
        ])
    coverage = comparison.get("coverage")
    if isinstance(coverage, dict):
        lines.append(f"Measured coverage: current {coverage['current']:.1%}, previous {coverage['previous']:.1%}; "
                     f"common hours {coverage['common_current']:.1%} / {coverage['common_previous']:.1%}.")
    previous = comparison.get("previous_kwh")
    lines.append(f"Previous measured grid import: {previous:.2f} kWh." if previous is not None
                 else "Previous measured grid import: Unknown.")
    if comparison["status"] == "ready":
        percent = comparison.get("percent")
        percent_text = (f"{percent:+.1f}%" if percent is not None else
                        "Unknown (zero baseline or unrepresentable percentage)")
        lines.append(f"Change in grid import: {comparison['delta_kwh']:+.2f} kWh; {percent_text}.")
    else:
        reason = {
            "rolling_or_custom_window": "comparison requires an aligned local calendar day, ISO week, month or year",
            "report_timezone_unavailable": "report timezone is unavailable",
            "calendar_boundary_not_recorder_hour": "local calendar boundaries do not align with Recorder hours",
            "incomplete_statistics": "insufficient compatible measured hours (at least 90% common coverage required)",
            "source_set_changed": "the measured grid source sets differ",
            "measurement_provenance_unavailable_or_changed": "measurement provenance is unavailable or differs",
        }.get(comparison["reason"], "comparison is unavailable")
        lines.append(f"Change in grid import: Unknown ({reason}).")
    return lines


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


async def async_get_energy_metadata(hass: Any, ids: list[str]) -> dict[str, Any]:
    """Read normalization metadata without querying measurement history."""
    if not ids:
        return {}
    try:
        from homeassistant.components.recorder.statistics import get_metadata
        from homeassistant.components.recorder.util import get_instance
        return await get_instance(hass).async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids=set(ids)))
    except Exception:
        return {}


async def async_get_energy_usage(
    hass: Any,
    sensors: list[dict[str, Any]],
    start: datetime,
    end: datetime,
    cadence: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep valid measured sources; withhold only totals that require missing data."""
    ids = list(dict.fromkeys(sensor["entity_id"] for sensor in sensors))
    snapshot = {"status": "no_data", "devices": [], "source_ids": ids}
    if not ids:
        return snapshot
    try:
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.components.recorder.util import get_instance
        recorder = get_instance(hass)
        if metadata is None:
            metadata = await async_get_energy_metadata(hass, ids)
        result = await recorder.async_add_executor_job(
            statistics_during_period, hass, start, end, set(ids), "hour", {}, {"change"},
        )
    except Exception:
        return snapshot
    names = {sensor["entity_id"]: sensor["name"] for sensor in sensors}
    expected = set(range(int(start.timestamp()), int(end.timestamp()), 3600))
    devices = []
    provenance = {}
    source_buckets = {}
    unavailable = []
    unsupported = []
    for sid in ids:
        raw_meta = metadata.get(sid)
        meta = raw_meta[1] if isinstance(raw_meta, tuple) else raw_meta
        factor = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}.get(meta.get("unit_of_measurement")) if meta else None
        if not meta or not meta.get("has_sum") or factor is None or meta.get("unit_class") not in {None, "energy"}:
            unavailable.append(sid)
            if meta:
                unsupported.append(sid)
            continue
        provenance[sid] = {"method": "recorder_hourly_change_v1", "unit": "kWh",
                           "source_unit": meta["unit_of_measurement"],
                           "unit_class": meta.get("unit_class"), "has_sum": True}
        points = (result or {}).get(sid) or []
        buckets = {}
        invalid = set()
        for point in points:
            try:
                stamp = _entry_time({"timestamp": point.get("start")})
                key = int(stamp.timestamp()) if stamp else None
                raw_change = point.get("change")
                change = raw_change if type(raw_change) in (int, float) else None
            except (TypeError, ValueError, OverflowError):
                change = None
                key = None
            if key not in expected:
                continue
            if key in buckets or key in invalid or change is None or not isfinite(change) or change < 0 or not isfinite(change * factor):
                invalid.add(key)
                buckets.pop(key, None)
                continue
            buckets[key] = change * factor
        value = sum(buckets.values())
        if not isfinite(value):
            unavailable.append(sid)
            continue
        source_buckets[sid] = buckets
        if buckets:
            name = names[sid]
            if name == sid and isinstance(meta.get('name'), str) and meta['name'].strip():
                name = meta['name']
            devices.append({"name": name, "entity_id": sid, "kwh": value,
                            "measured_hours": len(buckets), "expected_hours": len(expected),
                            "complete": set(buckets) == expected})
    if not isfinite(sum(device["kwh"] for device in devices)):
        return {**snapshot, "status": "partial"}
    hourly = []
    for key in sorted(expected):
        complete = len(source_buckets) == len(ids) and all(key in rows for rows in source_buckets.values())
        value = sum(rows[key] for rows in source_buckets.values()) if complete else None
        hourly.append({"from": key * 1000, "to": (key + 3600) * 1000,
                       "kwh": value if value is not None and isfinite(value) else None})
    complete = bool(expected) and all(row["kwh"] is not None for row in hourly)
    has_samples = any((result or {}).get(sid) for sid in ids)
    status = ("ready" if complete else "partial" if devices else "unsupported" if unsupported
              else "partial" if has_samples else "no_data")
    coverage = {"complete": complete, "measured_source_hours": sum(len(rows) for rows in source_buckets.values()),
                "expected_source_hours": len(expected) * len(ids), "sources": len(ids),
                "complete_sources": sum(row["complete"] for row in devices),
                "unavailable_sources": len(ids) - len(devices)}
    per_source = {sid: [{"from": key * 1000, "to": (key + 3600) * 1000,
                        "kwh": source_buckets.get(sid, {}).get(key)} for key in sorted(expected)]
                  for sid in ids}
    return {**snapshot, "status": status, "devices": devices, "hourly_buckets": hourly,
            "source_buckets": per_source,
            "measurement_provenance": provenance, "coverage": coverage}


def _energy_rate(value: float | None) -> float | None:
    return value if isinstance(value, (int, float)) and isfinite(value) and value >= 0 else None


def _energy_cost_text(value: float | None, currency: str) -> str:
    return f"{value:.2f} {currency}" if value is not None else "Tariff not configured"


def _clean_energy_devices(
    devices: Iterable[dict[str, Any]], price_per_kwh: float | None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for device in devices:
        try:
            kwh = float(device.get("kwh", device.get("month")))
        except (TypeError, ValueError):
            continue
        if not isfinite(kwh) or kwh < 0:
            continue
        rows.append(
            {
                "name": str(device.get("name") or device.get("entity_id") or "Unknown"),
                "entity_id": str(device.get("entity_id") or ""),
                "kwh": kwh,
                "cost": kwh * _energy_rate(price_per_kwh) if _energy_rate(price_per_kwh) is not None else None,
                "share": 0.0,
                **({key: device[key] for key in ("measured_hours", "expected_hours", "complete") if key in device}),
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
        f"Total counted warnings/errors: {_log_count_text(summary['total'])}",
        "",
        "Counts by level:",
    ]
    if summary["level_counts"]:
        lines.extend(
            f"- {level}: {_log_count_text(count)}"
            for level, count in sorted(summary["level_counts"].items())
        )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Top loggers:")
    if summary["logger_counts"]:
        lines.extend(
            f"- {logger}: {_log_count_text(count)}"
            for logger, count in list(summary["logger_counts"].items())[:10]
        )
    else:
        lines.append("- none")
    lines.append("")
    lines.append(f"Top {summary.get('max_entries', 10)} recent errors:")
    if summary["recent_errors"]:
        lines.extend(
            f"- {entry.get('level')} {entry.get('name')}: {entry.get('_message')} "
            f"x{_log_count_text(entry.get('count'))}"
            for entry in summary["recent_errors"]
        )
    else:
        lines.append("- none")
    lines.append("")
    lines.append(f"Top {summary.get('max_entries', 10)} recent warnings:")
    if summary["recent_warnings"]:
        lines.extend(
            f"- {entry.get('name')}: {entry.get('_message')} "
            f"x{_log_count_text(entry.get('count'))}"
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


def _log_count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 9007199254740991 else None


def _log_count_sum(left: int | None, right: int | None) -> int | None:
    return _log_count(left + right) if left is not None and right is not None else None


def _log_count_total(values: Iterable[int | None]) -> int | None:
    total = 0
    for value in values:
        total = _log_count_sum(total, value)
    return total


def _log_count_text(value: int | None) -> str:
    return str(value) if value is not None else "Unknown"


def _log_exact(value: Any) -> tuple:
    # Type and structure are identity; rendered/truncated text is not.
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_log_exact(item) for item in value))
    if isinstance(value, dict):
        return ("object", tuple((key, _log_exact(value[key])) for key in sorted(value)))
    return ("unsupported", type(value).__name__)


def _group_retained_logs(entries: Iterable[dict[str, Any]], *, until: datetime | None = None) -> list[dict[str, Any]]:
    groups = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        when = _entry_time(raw)
        if until and when and when > _aware(until):
            continue
        supplied = next((key for key in ("id", "entry_id", "key", "hash") if
                         isinstance(raw.get(key), str) or type(raw.get(key)) in (int, float)
                         and isfinite(raw[key])), None)
        identity = ("backend", supplied, _log_exact(raw[supplied])) if supplied else (
            "exact", tuple((key in raw, _log_exact(raw.get(key))) for key in
                           ("level", "name", "source", "message", "exception")))
        count = _log_count(raw.get("count"))
        previous = groups.get(identity)
        if previous is None:
            groups[identity] = {**raw, "count": count}
            continue
        before_count = previous["count"]
        combined = (max(before_count, count) if supplied and before_count is not None and count is not None
                    else _log_count_sum(before_count, count))
        stamp = _entry_time(raw)
        previous_stamp = _entry_time(previous)
        if stamp is not None and (previous_stamp is None or stamp >= previous_stamp):
            groups[identity] = {**raw, "count": combined}
        else:
            previous["count"] = combined
    return list(groups.values())


def _entry_time(entry: dict[str, Any]) -> datetime | None:
    raw = entry.get("timestamp")
    if raw is None:
        raw = entry.get("first_occurred")
    if raw is None:
        return None
    try:
        if type(raw) in (int, float):
            return datetime.fromtimestamp(raw, tz=timezone.utc) if isfinite(raw) else None
        if isinstance(raw, str):
            return _parse_datetime(raw)
        if isinstance(raw, datetime):
            return _aware(raw)
    except (ValueError, OverflowError, OSError):
        return None
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
