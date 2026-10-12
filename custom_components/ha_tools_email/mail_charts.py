"""Self-contained static mail charts from retained report data, without I/O."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from math import isfinite
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HOUR_MS = 3_600_000
DAY_DISPLAY_LIMIT = 31
HTML_START = '<!-- HA Tools static charts -->'
HTML_END = '<!-- End HA Tools static charts -->'
TEXT_START = '\n\nStatic chart data\n'


def _finite(value: Any) -> int | float | None:
    try:
        return value if type(value) in (int, float) and isfinite(value) else None
    except OverflowError:
        return None


def _number(value: int | float | None) -> str:
    if value is None:
        return 'Unknown'
    if value == 0:
        return '0'
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else format(value, '.12g')
    return str(value)


def safe_energy_url(value: Any) -> str | None:
    """Allow a known HA HTTP(S) origin only; discard no potentially secret query."""
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value):
        return None
    if '\\' in value or '%' in value or '?' in value or '#' in value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        # Accessing port validates malformed and out-of-range ports.
        _ = parsed.port
        host = parsed.hostname
        if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:' for c in host):
            return None
    except ValueError:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, '/energy', '', ''))


def _row(label: Any, value: Any, **metadata: Any) -> dict[str, Any]:
    numeric = _finite(value)
    return dict(label=str(label), value=numeric, display=_number(numeric), **metadata)


def _chart(key: str, title: str, unit: str, rows: list[dict[str, Any]], *,
           note: str, limit: int, granularity: str) -> dict[str, Any]:
    # Scale describes displayed data only, including an explicit measured zero axis.
    shown = rows[:limit]
    values = [row['value'] for row in shown if row['value'] is not None]
    omitted = max(0, len(rows) - limit)
    return dict(id=key, title=title, unit=unit, rows=shown,
                granularity=granularity, total_rows=len(rows), displayed_rows=len(shown),
                omitted_rows=omitted, scale_min=min([0, *values]) if values else None,
                scale_max=max([0, *values]) if values else None,
                note=f'{note} {len(shown)} of {len(rows)} rows displayed; {omitted} omitted. '
                     'Unknown means a gap or unavailable value; zero is measured zero.')


def build_log_chart_model(summary: dict[str, Any]) -> dict[str, Any]:
    """Counts come from the same normalized retained aggregates as the mail tables."""
    note = 'Occurrences in retained aggregates; not exact events or complete event history.'
    charts = [
        _chart('log-levels', 'Retained occurrences by level', 'occurrences',
               [_row(k, v) for k, v in sorted(summary.get('level_counts', {}).items())],
               note=note, limit=24, granularity='category'),
        _chart('log-loggers', 'Retained occurrences by logger', 'occurrences',
               [_row(k, v) for k, v in summary.get('logger_counts', {}).items()],
               note=note, limit=10, granularity='category'),
    ]
    return dict(schema=1, charts=charts, interactive_url=None,
                context=note, unique_problems=summary.get('unique_problems'))


def _window(summary: dict[str, Any]) -> tuple[datetime, datetime] | None:
    period = summary.get('period', {})
    try:
        start = datetime.fromisoformat(period['start'])
        end = datetime.fromisoformat(period['end'])
        if start.utcoffset() is None or end.utcoffset() is None:
            return None
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if end <= start or (end - start).total_seconds() > 366 * 86400:
            return None
        if start.timestamp() * 1000 % HOUR_MS or end.timestamp() * 1000 % HOUR_MS:
            return None
        return start, end
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None


def _index(rows: list[dict[str, Any]], field: str) -> dict[int, int | float | None]:
    result: dict[int, int | float | None] = {}
    duplicates: set[int] = set()
    for row in rows:
        start, end = _finite(row.get('from')), _finite(row.get('to'))
        if start is None or end is None or start % HOUR_MS or end - start != HOUR_MS:
            # An invalid interval must never support a complete measured day.
            if start is not None and not start % HOUR_MS:
                duplicates.add(int(start))
            continue
        key = int(start)
        if key in result:
            duplicates.add(key)
        result[key] = _finite(row.get(field)) if row.get('available', True) else None
    for key in duplicates:
        result[key] = None
    return result


def _profile(window: tuple[datetime, datetime], buckets: list[dict[str, Any]],
             costs: list[dict[str, Any]], tz: Any) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    start, end = window
    usage = _index(buckets, 'kwh')
    energy_cost = _index(costs, 'energyCost')
    variable_cost = _index(costs, 'variableCost')
    columns = {'energy-usage': [], 'energy-cost': [], 'variable-fees': []}
    hours = int((end-start).total_seconds()/3600)
    intervals = []
    if hours <= 24:
        granularity = 'hour'
        for hour in range(hours):
            a, b = start + timedelta(hours=hour), start + timedelta(hours=hour+1)
            intervals.append((a, b, True))
    else:
        granularity = 'calendar_day'
        day = start.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        while day.astimezone(timezone.utc) < end:
            next_day = day + timedelta(days=1)
            a, b = day.astimezone(timezone.utc), next_day.astimezone(timezone.utc)
            intervals.append((a, b, a >= start and b <= end))
            day = next_day
    for a, b, fully_in_window in intervals:
        first, last = int(a.timestamp()*1000), int(b.timestamp()*1000)
        stamps = list(range(first, last, HOUR_MS))
        local_a, local_b = a.astimezone(tz), b.astimezone(tz)
        label = (f'{local_a.isoformat()} → {local_b.isoformat()}' if granularity == 'hour' else
                 f'{local_a.date().isoformat()} ({len(stamps)}h; '
                 f'{local_a.isoformat()} → {local_b.isoformat()})')
        measured = fully_in_window and bool(stamps) and all(usage.get(t) is not None for t in stamps)
        for key, index in [('energy-usage', usage), ('energy-cost', energy_cost), ('variable-fees', variable_cost)]:
            complete = measured and all(index.get(t) is not None for t in stamps)
            # Sum only complete, non-overlapping, absolute source intervals. Never interpolate.
            value = sum(index[t] for t in stamps) if complete else None
            complete = complete and _finite(value) is not None
            columns[key].append(_row(label, value, **{'from':first, 'to':last,
                                    'hours':len(stamps), 'complete':complete}))
    return granularity, columns


def build_energy_chart_model(summary: dict[str, Any], hourly_buckets: list[dict[str, Any]] | None = None,
                             tariff_rows: list[dict[str, Any]] | None = None, *,
                             timezone_name: str | None = None, interactive_url: Any = None,
                             source_kind: str = 'consumers') -> dict[str, Any]:
    """A public inspectable chart model, separate from all financial calculations."""
    try:
        tz = ZoneInfo(timezone_name) if timezone_name else timezone.utc
        tz_label = timezone_name or 'UTC (report timezone unavailable)'
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        tz, tz_label = timezone.utc, 'UTC (report timezone unavailable)'
    charts = []
    window = _window(summary)
    if window and hourly_buckets:
        granularity, columns = _profile(window, hourly_buckets, tariff_rows or [], tz)
        note = (f'Recorder grid-import usage. Timezone: {tz_label}. '
                'Only complete absolute intervals support a value; partial calendar days are Unknown. '
                'Hourly display up to 24 hours; longer windows use completed calendar days, up to 31 displayed rows.')
        charts.append(_chart('energy-usage', 'Measured grid-import usage', 'kWh', columns['energy-usage'],
                             note=note, limit=24 if granularity == 'hour' else DAY_DISPLAY_LIMIT, granularity=granularity))
        currency = summary.get('currency')
        if (tariff_rows and isinstance(currency, str) and len(currency) == 3
                and currency.isascii() and currency.isupper() and currency.isalpha()):
            price_note = (f'{note} Price basis: {summary.get("priceBasis") or "unknown"}. '
                          'Measured usage charges only; fixed monthly fees are separate, without proration.')
            for key, title in [('energy-cost', 'Measured energy cost'), ('variable-fees', 'Measured variable fees')]:
                charts.append(_chart(key, title, currency, columns[key], note=price_note,
                                     limit=24 if granularity == 'hour' else DAY_DISPLAY_LIMIT, granularity=granularity))
    if not charts or not any(row['value'] is not None for row in charts[0]['rows']):
        title = 'Grid import sources (not appliances)' if source_kind == 'grid' else 'Reported consumers'
        note = ('Existing source rows only; no parent/appliance combination and no inferred source flow. '
                'Displayed sources are a subset; no Other remainder is estimated.')
        if summary.get('status') == 'partial':
            note += ' Incomplete source coverage; period total withheld.'
        charts.append(_chart('energy-sources', title, 'kWh',
                     [_row(row.get('name') or row.get('entity_id') or 'Unknown', row.get('kwh'),
                           entity_id=row.get('entity_id', '')) for row in summary.get('devices', [])],
                     note=note, limit=10, granularity='category'))
        currency = summary.get('currency')
        devices = summary.get('devices', [])
        if (isinstance(currency, str) and len(currency) == 3 and currency.isascii()
                and currency.isupper() and currency.isalpha()
                and any(_finite(row.get('cost')) is not None for row in devices)):
            charts.append(_chart('energy-source-cost', 'Reported measured usage cost by source', currency,
                         [_row(row.get('name') or row.get('entity_id') or 'Unknown', row.get('cost'),
                               entity_id=row.get('entity_id', '')) for row in devices],
                         note=note + ' Existing source usage costs only; no fixed monthly fees.',
                         limit=10, granularity='category'))
    return dict(schema=1, charts=charts, timezone=tz_label, context='',
                interactive_url=safe_energy_url(interactive_url))


def _scale_text(chart: dict[str, Any]) -> str:
    if chart['scale_min'] is None or chart['scale_max'] is None:
        return f'Scale unavailable (no measured values). Zero axis: 0 {chart["unit"]}.'
    return (f'Scale: {_number(chart["scale_min"])} → {_number(chart["scale_max"])} {chart["unit"]}. '
            f'Zero axis: 0 {chart["unit"]}.')


def _bar(chart: dict[str, Any], row: dict[str, Any]) -> str:
    value, low, high = row['value'], chart['scale_min'], chart['scale_max']
    if value is None:
        return '<span style="font-size:14px;color:#475569">Unknown / gap</span>'
    if value == 0:
        return '<span style="font-size:14px;color:#475569">0 · measured zero</span>'
    # Normalize before subtraction to avoid overflow in geometry for very large finite values.
    magnitude = max(abs(low), abs(high))
    negative, positive = abs(low)/magnitude, high/magnitude
    whole = negative + positive
    left, right = negative/whole*100, positive/whole*100
    cells = []
    if left > 0:
        filled = abs(value/low)*100 if value < 0 else 0
        cells.append(_side(left, filled, negative=True))
    if right > 0:
        filled = value/high*100 if value > 0 else 0
        cells.append(_side(right, filled, negative=False))
    return ('<table role="presentation" aria-hidden="true" width="100%" cellspacing="0" cellpadding="0" '
            'style="width:100%;table-layout:fixed;border-collapse:collapse"><tr>' + ''.join(cells) + '</tr></table>')


def _side(width: float, fill: float, *, negative: bool) -> str:
    # Percentage widths may be subpixel; never add a minimum that would falsify tiny values.
    empty = max(0, 100-fill)
    filled_cell = (f'<td width="{fill:.12g}%" bgcolor="#2563eb" '
                   'style="height:10px;background:#2563eb;font-size:0;line-height:0">&nbsp;</td>') if fill > 0 else ''
    empty_cell = (f'<td width="{empty:.12g}%" bgcolor="#e2e8f0" '
                  'style="height:10px;background:#e2e8f0;font-size:0;line-height:0">&nbsp;</td>') if empty > 0 else ''
    inside = empty_cell + filled_cell if negative else filled_cell + empty_cell
    axis = 'border-right:2px solid #475569' if negative else 'border-left:2px solid #475569'
    return (f'<td width="{width:.12g}%" style="{axis}"><table role="presentation" width="100%" '
            'cellspacing="0" cellpadding="0" style="width:100%;table-layout:fixed;border-collapse:collapse">'
            f'<tr>{inside}</tr></table></td>')


def render_charts_html(model: dict[str, Any]) -> str:
    sections = []
    for chart in model['charts']:
        rows = []
        for row in chart['rows']:
            # One full-width label row and one full-width bar row fit a 320px mail viewport.
            rows.append('<tr><td style="padding:10px 0 4px;word-break:break-word;overflow-wrap:anywhere">'
                        f'<strong>{escape(row["label"])}</strong><br>'
                        f'{escape(row["display"])} {escape(chart["unit"])}</td></tr>'
                        f'<tr><td style="padding:0 0 8px">{_bar(chart, row)}</td></tr>')
        if not rows:
            rows.append('<tr><td style="padding:10px 0">No retained chart data.</td></tr>')
        sections.append('<table width="100%" cellspacing="0" cellpadding="0" '
                        'style="width:100%;table-layout:fixed;border-collapse:collapse;margin:18px 0;'
                        'font-size:14px;line-height:1.5;color:#0f172a;background:#ffffff">'
                        '<caption style="text-align:left;font-size:16px;font-weight:700;'
                        f'word-break:break-word">{escape(chart["title"])}</caption>'
                        '<tbody><tr><td style="padding:6px 0;word-break:break-word">'
                        f'{escape(_scale_text(chart))}</td></tr>{"".join(rows)}'
                        '<tr><td style="padding:10px 0;color:#475569;word-break:break-word">'
                        f'{escape(chart["note"])}</td></tr></tbody></table>')
    if model.get('unique_problems') is not None:
        sections.append(f'<p style="font-size:14px">Unique problems: {_number(_finite(model["unique_problems"]))} '
                        '(distinct retained groups; a separate metric from occurrences).</p>')
    link = safe_energy_url(model.get('interactive_url'))
    if link:
        sections.append(f'<p style="font-size:14px;word-break:break-word"><a href="{escape(link, quote=True)}" '
                        'style="color:#1d4ed8">Open Home Assistant Energy reference</a></p>')
    return HTML_START + '<div style="max-width:692px;margin:0 auto;padding:14px;font-family:Arial,sans-serif;background:#ffffff;color:#0f172a">' + ''.join(sections) + '</div>' + HTML_END


def render_charts_plain(model: dict[str, Any]) -> str:
    lines = []
    for chart in model['charts']:
        lines.extend([chart['title'], _scale_text(chart)])
        lines.extend(f'{row["label"]}: {row["display"]} {chart["unit"]}' for row in chart['rows'])
        lines.append(chart['note'])
    if model.get('unique_problems') is not None:
        lines.append(f'Unique problems: {_number(_finite(model["unique_problems"]))} '
                     '(distinct retained groups; separate from occurrences).')
    link = safe_energy_url(model.get('interactive_url'))
    if link:
        lines.append(f'Home Assistant Energy reference: {link}')
    return '\n'.join(lines)


def attach_mail_charts(payload: dict[str, Any], model: dict[str, Any]) -> None:
    """HTML and plaintext consume the exact same returned summary visualization."""
    payload['summary']['visualization'] = model
    payload['body'] += TEXT_START + render_charts_plain(model)
    payload['html'] = payload['html'].replace('</body>', render_charts_html(model) + '</body>')
