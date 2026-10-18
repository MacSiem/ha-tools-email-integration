"""Pure finite rolling local-date windows, separate from calendar comparisons."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_PATTERNS_HOURS = 32 * 24


def resolve_patterns_window(days, now, timezone_name):
    if type(days) is not int or days not in (7, 30):
        return {'status': 'unknown', 'reason': 'invalid_patterns_days'}
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return {'status': 'unknown', 'reason': 'aware_server_clock_required'}
    try:
        if not isinstance(timezone_name, str):
            raise ValueError('invalid_zone')
        zone = ZoneInfo(timezone_name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return {'status': 'unknown', 'reason': 'measurement_timezone_unavailable'}
    try:
        end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        last = (end - timedelta(hours=1)).astimezone(zone).date()
        first = last - timedelta(days=days-1)
        if not 1900 <= first.year <= last.year <= 2200:
            raise ValueError('calendar_out_of_range')
        requested = datetime.combine(first, time.min, zone)
        instant = requested.astimezone(timezone.utc)
        stamp = int(instant.timestamp())
        start = datetime.fromtimestamp(((stamp + 3599) // 3600) * 3600, timezone.utc)
        hours = (end-start).total_seconds()/3600
        if not 0 < hours <= MAX_PATTERNS_HOURS:
            return {'status': 'unknown', 'reason': 'patterns_window_too_long'}
        return {'status': 'ready', 'reason': None, 'kind': 'rolling_local_dates',
                'days': days, 'timezone': zone.key,
                'first_date': first.isoformat(), 'last_date': last.isoformat(),
                'requested_local_start': requested.isoformat(),
                'query_start': start.isoformat(), 'query_end': end.isoformat(),
                'start': start.isoformat(), 'end': end.isoformat(),
                'boundary_complete': start == instant, 'hours': int(hours)}
    except (ValueError, OverflowError, OSError):
        return {'status': 'unknown', 'reason': 'calendar_out_of_range'}
