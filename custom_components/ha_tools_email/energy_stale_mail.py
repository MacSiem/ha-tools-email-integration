"""Optional mail-only alert, restricted to missing IDs in this report."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MONTHS = {
    'pl': ('sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'),
    'en': ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'),
}


def _instant(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
        return stamp.astimezone(timezone.utc) if isinstance(stamp, datetime) and stamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def stale_mail_line(snapshot, statuses, *, language, timezone_name, now):
    """Select one authorized missing item whose gap intersects this report."""
    if not isinstance(statuses, dict) or not isinstance(snapshot, dict) or language not in _MONTHS:
        return None
    now = _instant(now)
    if now is None:
        return None
    model = snapshot.get('report_model') or snapshot
    coverage = model.get('coverage') or {}
    period = model.get('period') or snapshot.get('period') or {}
    if not isinstance(coverage, dict) or not isinstance(period, dict):
        return None
    start, end = _instant(period.get('start')), _instant(period.get('end'))
    if start is None or end is None or start >= end:
        return None
    candidates = []
    for key in ('missing_sources', 'missing_devices'):
        rows = coverage.get(key, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, str):
                sid = row
            elif isinstance(row, dict) and isinstance(row.get('id'), str):
                sid = row['id']
            else:
                continue
            fact = statuses.get(sid)
            if not isinstance(fact, dict) or fact.get('stale') is not True:
                continue
            since = _instant(fact.get('since') or fact.get('last_reading'))
            if since is None or since > now - timedelta(hours=24):
                continue
            if isinstance(row, str):
                # Schema 2 only proves a source was missing somewhere in the
                # period. Without an exact range, require the *whole* period
                # to fall inside the independently confirmed outage.
                if since <= start and end <= now:
                    groups = snapshot.get('groups') or {}
                    devices = groups.get('devices') if isinstance(groups, dict) else {}
                    device = devices.get(sid) if isinstance(devices, dict) else None
                    name = device.get('name') if isinstance(device, dict) else None
                    name = name if isinstance(name, str) and name.strip() else sid
                    if sid:
                        candidates.append((since, sid, name))
                continue
            ranges = row.get('ranges')
            if not isinstance(ranges, list):
                continue
            for gap in ranges:
                if not isinstance(gap, dict):
                    continue
                left, right = _instant(gap.get('start')), _instant(gap.get('end'))
                if left and right and max(start, left, since) < min(end, right, now):
                    name = row.get('name') or sid
                    if isinstance(name, str) and name.strip():
                        candidates.append((since, sid, name))
                    break
    if not candidates:
        return None
    since, _, name = min(candidates)
    try:
        local = since.astimezone(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None
    day = f'{local.day} {_MONTHS[language][local.month-1]}' if language == 'pl' else f'{_MONTHS[language][local.month-1]} {local.day}'
    days = (now - since).days
    if language == 'pl':
        age = f'{days} dzień' if days == 1 else f'{days} dni'
        return f'{name} — brak aktualizacji od {day} ({age})'
    age = f'{days} day' if days == 1 else f'{days} days'
    return f'{name} — no updates since {day} ({age})'
