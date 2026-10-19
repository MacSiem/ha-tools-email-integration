"""Internal, bounded per-schedule facts; never entity attributes or a public API."""
from datetime import datetime, timedelta, timezone
import hashlib

from .feature_access import _bounded_json
from .history import _OUTCOMES, HISTORY_LIMIT, retained_attempts
from .report_status import _attempt, MAX_STATUS_SCHEDULES, PURPOSE_FEATURES, project_report_status
from .scheduler import calculate_next_fire, normalize_schedule_calendar

RESULT_OPTIONS = sorted(_OUTCOMES)
FAILURES = {'preparation_failed', 'authentication_failed', 'recipients_refused', 'tls_failed', 'send_failed'}


def repair_facts(attempts):
    ordered = sorted(attempts, key=lambda row: (row['timestamp'], row['id']), reverse=True)
    auth = None
    for row in ordered:
        if row['outcome'] == 'accepted_by_smtp':
            auth = False
            break
        if row['outcome'] == 'authentication_failed':
            auth = True
            break
    consecutive = 0
    three = None
    for row in ordered:
        if row['outcome'] == 'accepted_by_smtp':
            three = False
            break
        if row['outcome'] not in FAILURES:
            break
        consecutive += 1
        if consecutive == 3:
            three = True
            break
    return {'email_native_auth': auth, 'email_native_failures': three}


def project_native_reports(data, _purpose=None, *, now, local_timezone, consumed=None, schedule_id_resolver):
    if now.utcoffset() is None:
        raise ValueError('invalid_native_report_clock')
    rows = data.get('schedules', [])
    if not isinstance(rows, list) or len(rows) > MAX_STATUS_SCHEDULES:
        raise ValueError('native_report_schedules_unavailable')
    section = data.get('attempt_history')
    history_ready = section is None or (isinstance(section, dict)
        and type(section.get('schema', 0)) is int and section.get('schema', 0) in (0, 1)
        and isinstance(section.get('attempts', []), list)
        and len(section.get('attempts', [])) <= HISTORY_LIMIT)
    attempts = []
    if history_ready:
        clean = [_attempt(row) for row in (section or {}).get('attempts', [])]
        attempts = retained_attempts({'schema': 1, 'attempts': [row for row in clean if row]}, now)
    result = {'schema': 1, 'schedules': {}, 'identities': {},
              'repairs': repair_facts(attempts) if history_ready else {}, 'attempts': {},
              'history_ids': [row['id'] for row in attempts] if history_ready else None}
    for raw in rows:
        if (not isinstance(raw, dict) or not isinstance(raw.get('kind'), str)
                or len(raw['kind']) > 32 or raw['kind'] not in PURPOSE_FEATURES):
            continue
        # The canonical ID-less legacy identity hashes the entire stored row.
        # Bound it before that existing normalizer reads JSON. No such fields
        # escape this worker; normal explicit IDs do not inspect private fields.
        if not raw.get('id'):
            _bounded_json(raw)
        sid = schedule_id_resolver(raw)
        if not isinstance(sid, str) or len(sid) > 65536:
            raise ValueError('native_schedule_identity_unavailable')
        key = hashlib.sha256(sid.encode()).hexdigest()
        if key in result['schedules']:
            raise ValueError('ambiguous_native_schedule_identity')
        safe = {'id': key, 'kind': raw['kind'], 'enabled': raw.get('enabled', True)}
        for field in ('cadence', 'time', 'timezone', 'weekday', 'weekly_day', 'month_day', 'monthday', 'day'):
            if field in raw:
                value = raw[field]
                if type(value) not in (str, int) or (type(value) is str and len(value) > 128):
                    raise ValueError('invalid_native_calendar')
                safe[field] = value
        if safe.get('cadence') not in ('daily', 'weekly', 'monthly') or type(safe['enabled']) is not bool:
            raise ValueError('invalid_native_schedule')
        safe.setdefault('timezone', local_timezone)
        safe = normalize_schedule_calendar(safe)
        matching = [row for row in attempts if row['schedule_ref'] == key and row['kind'] == raw['kind']]
        # Reuse the aggregate projector on exactly one schedule, rewriting only
        # the internal scheduler-state lookup key to its nonprivate hash.
        persisted = data.get('scheduler_state', {}).get(sid, {})
        subset = {'schedules': [safe], 'scheduler_state': {key: persisted},
                  'attempt_history': {'schema': 1, 'attempts': matching}}
        status = project_report_status(subset, raw['kind'], now=now, local_timezone=local_timezone,
            consumed={key: (consumed or {}).get(sid)})
        calendar = {field: safe[field] for field in ('cadence', 'time', 'timezone', 'weekday', 'month_day') if field in safe}
        result['schedules'][key] = {'kind': raw['kind'], 'cadence': safe.get('cadence'),
            'enabled': safe['enabled'] is True, 'calendar': calendar,
            'schedule_state': status['schedule_state'], 'next_report_at': status['next_report_at'],
            'history_ready': history_ready, 'last_attempt': status['last_attempt'] if history_ready else None,
            'errors_24h': None, 'warnings_24h': None}
        result['identities'][key] = sid
        result['attempts'][key] = [{field: row[field] for field in ('id', 'timestamp', 'outcome')} for row in matching]
    return result


def calendar_occurrences(calendar, start, end, *, local_timezone):
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError('invalid_native_calendar_range')
    # Compare actual instants: Python compares wall times when two datetimes
    # share a ZoneInfo object, ignoring fold differences during autumn DST.
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if not start < end or end - start > timedelta(days=366):
        raise ValueError('invalid_native_calendar_range')
    cursor = start - timedelta(microseconds=1)
    result = []
    for _ in range(512):
        fire = calculate_next_fire(calendar, cursor, local_timezone=local_timezone).astimezone(timezone.utc)
        if fire >= end:
            return result
        result.append(fire)
        cursor = fire
    raise ValueError('native_calendar_range_limit')
