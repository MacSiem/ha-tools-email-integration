"""Bounded, private-field-free facts for one report purpose's compact view."""
from datetime import datetime, timezone
import re

from .history import HISTORY_LIMIT, clean_attempt, retained_attempts
from .scheduler import calculate_next_fire, fire_period_key, normalize_schedule_calendar

MAX_STATUS_SCHEDULES = 128
PURPOSE_FEATURES = {'energy_report': 'energy_reports', 'log_digest': 'log_reports'}
_CALENDAR_KEYS = ('cadence', 'time', 'timezone', 'weekday', 'weekly_day', 'month_day',
                  'monthday', 'day', 'use_ha_timezone')
_ATTEMPT_LENGTHS = {'id': 36, 'timestamp': 64, 'source': 16, 'kind': 32,
                    'cadence': 16, 'period': 32, 'schedule_ref': 64, 'outcome': 64}


def _attempt(raw):
    if not isinstance(raw, dict):
        return None
    bounded = {key: raw.get(key) if type(raw.get(key)) is str
               and len(raw[key]) <= limit else None for key, limit in _ATTEMPT_LENGTHS.items()}
    count = raw.get('recipient_count')
    bounded['recipient_count'] = count if type(count) is int and 0 <= count <= 100000 else None
    return clean_attempt(bounded)


def project_report_status(data, purpose, *, now, local_timezone, consumed=None):
    """Read fixed keys only; never return schedule IDs/labels, addresses or SMTP.

    next_report_at is a configured calendar occurrence, not a claim that SMTP,
    source data or delivery is available. last_attempt.timestamp is dispatch
    start time (the fact stored by history), never invented completion/delivery.
    recipient_count describes only that actual attempt, never SMTP defaults.
    """
    if purpose not in PURPOSE_FEATURES or not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('invalid_report_status_request')
    now = now.astimezone(timezone.utc)
    result = {'schema': 1, 'purpose': purpose, 'generated_at': now.isoformat(),
              'schedule_state': 'unavailable', 'next_report_at': None,
              'history_state': 'unavailable', 'last_attempt': None}
    if not isinstance(data, dict):
        return result
    rows = data.get('schedules', [])
    consumed = consumed if isinstance(consumed, dict) else {}
    state = data.get('scheduler_state', {})
    if isinstance(rows, list) and len(rows) <= MAX_STATUS_SCHEDULES and isinstance(state, dict):
        matching = enabled = 0
        candidates = []
        invalid = False
        for row in rows:
            if not isinstance(row, dict) or row.get('kind') != purpose:
                continue
            matching += 1
            active = row.get('enabled', True)
            if type(active) is not bool:
                invalid = True
                continue
            if not active:
                continue
            enabled += 1
            try:
                sid = row.get('id')
                if type(sid) is not str or not 0 < len(sid) <= 128:
                    raise ValueError('invalid_status_schedule')
                calendar = {}
                for key in _CALENDAR_KEYS:
                    if key in row:
                        value = row[key]
                        if type(value) not in (str, int, bool) or (type(value) is str and len(value) > 128):
                            raise ValueError('invalid_status_calendar')
                        calendar[key] = value
                if calendar.get('cadence') not in ('daily', 'weekly', 'monthly'):
                    raise ValueError('invalid_status_cadence')
                if not isinstance(calendar.get('time'), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', calendar['time']):
                    raise ValueError('invalid_status_time')
                calendar = normalize_schedule_calendar(calendar)
                fire = calculate_next_fire(calendar, now, local_timezone=local_timezone)
                persisted = state.get(sid, {})
                if not isinstance(persisted, dict):
                    raise ValueError('invalid_status_state')
                period = fire_period_key(calendar, fire, local_timezone=local_timezone)
                if period in (persisted.get('last_fired_period'), consumed.get(sid)):
                    fire = calculate_next_fire(calendar, fire, local_timezone=local_timezone)
                candidates.append(fire.astimezone(timezone.utc))
            except (ValueError, TypeError, KeyError, OverflowError):
                invalid = True
        if not invalid:
            result['schedule_state'] = 'enabled' if enabled else 'disabled' if matching else 'unconfigured'
            result['next_report_at'] = min(candidates).isoformat() if candidates else None
    history = data.get('attempt_history')
    if history is None:
        history = {}
    if (isinstance(history, dict) and type(history.get('schema', 0)) is int
            and history.get('schema', 0) in (0, 1) and isinstance(history.get('attempts', []), list)
            and len(history.get('attempts', [])) <= HISTORY_LIMIT):
        clean = [_attempt(row) for row in history.get('attempts', [])]
        retained = retained_attempts({'schema': 1, 'attempts': [row for row in clean if row]}, now)
        matching = [row for row in retained if row['kind'] == purpose]
        result['history_state'] = 'ready'
        if matching:
            last = max(matching, key=lambda row: (row['timestamp'], row['id']))
            result['last_attempt'] = {key: last[key] for key in ('timestamp', 'outcome', 'recipient_count')}
    return result
