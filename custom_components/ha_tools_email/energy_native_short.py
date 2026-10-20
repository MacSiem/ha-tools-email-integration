"""Real native five-minute cells, normalized by the existing report kernel.

No actor, report DTO or alternate source route. Caller owns the same native
coordinator lock, source/metadata validation and final publication fence as its
hourly Recorder path. Raw cache is bounded to one current hour per grid source.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def cutoff_at(now):
    now = now.astimezone(timezone.utc)
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)


async def async_read_short_statistics(hass, start, end, ids):
    """Exactly the real Recorder 5minute/change API used by energy_inputs."""
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.components.recorder import get_instance
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, set(ids), '5minute', {}, {'change'})


def merge_raw(previous, current, ids, start, end):
    """Keep actual starts from the incremental read, never synthesize cells."""
    if len(ids) > 512:
        raise ValueError('native_short_source_limit')
    low, high = start.timestamp(), end.timestamp()
    output = {}
    for sid in ids:
        old = previous.get(sid, []) if isinstance(previous, dict) else []
        fresh = current.get(sid, []) if isinstance(current, dict) else []
        if not isinstance(old, list) or not isinstance(fresh, list) or len(old) > 12 or len(fresh) > 12:
            output[sid] = []  # Invalid evidence stays absent, never a declared cell count.
            continue
        points = list(old)
        for point in fresh:
            if not isinstance(point, dict):
                continue
            stamp = point.get('start')
            try:
                if isinstance(stamp, str):
                    stamp = datetime.fromisoformat(stamp)
                if isinstance(stamp, datetime):
                    if stamp.utcoffset() is None:
                        continue
                    stamp = stamp.timestamp()
                if type(stamp) in (int, float) and low <= stamp < high:
                    points.append({'start': point.get('start'), 'change': point.get('change')})
            except (TypeError, ValueError, OverflowError):
                continue
        output[sid] = points
    return output


def normalized_cells(raw, metadata, ids, start, end):
    """Prefix proofs reuse exact unit/source/duplicate rules; deltas are real cells.

    normalize_short_hour requires an absolute hour start. At most eleven prefix
    proofs recover each completed five-minute source delta from the actual raw
    prefix. There is no additional IO, scaling or allocation between intervals.
    A missing earlier cell cannot produce an invented later delta or total.
    """
    from .energy_inputs import normalize_short_hour
    if not ids or len(ids) > 512:
        return []
    if (start.timestamp() % 3600 or end.timestamp() % 300
            or not 0 < (end - start).total_seconds() < 3600):
        raise ValueError('invalid_native_short_window')
    rows, previous = [], {sid: Decimal(0) for sid in ids}
    cursor = start + timedelta(minutes=5)
    while cursor <= end:
        values = normalize_short_hour(raw, metadata, ids, start, cursor)
        totals = {sid: Decimal(value) if value is not None else None for sid, value in values.items()}
        pieces = [totals[sid] - previous[sid] if totals[sid] is not None and previous[sid] is not None else None for sid in ids]
        quantity = sum(pieces, Decimal(0)) if all(value is not None for value in pieces) else None
        rows.append({'from': (cursor - timedelta(minutes=5)).isoformat(), 'to': cursor.isoformat(),
                     'kwh': str(quantity) if quantity is not None else None, 'unit': 'kWh'})
        previous = totals
        cursor += timedelta(minutes=5)
    return rows
