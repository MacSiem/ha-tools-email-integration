"""Pure, bounded evidence from normalized per-source Recorder hour intervals."""
from copy import deepcopy
from datetime import datetime, timezone
from math import fsum, isfinite
from zoneinfo import ZoneInfo

MAX_SOURCES = 512
MAX_PERIOD_HOURS = 366 * 24
# A maximum-source seven-day chunk; annual consumers must stream chunks rather
# than retain an entire source-count by year matrix in every pending request.
MAX_EVIDENCE_CELLS = MAX_SOURCES * 7 * 24


def validated_source_ids(snapshot):
    ids = snapshot.get('source_ids') if isinstance(snapshot, dict) else None
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SOURCES
            or any(not isinstance(sid, str) or not sid for sid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('invalid_source_ids')
    return list(ids)


def valid_provenance(value):
    return (isinstance(value, dict) and value.get('method') == 'recorder_hourly_change_v1'
            and value.get('unit') == 'kWh' and value.get('source_unit') in ('Wh', 'kWh', 'MWh')
            and value.get('has_sum') is True and value.get('unit_class') in (None, 'energy'))


def _instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('invalid_measurement_boundary')
    return value.astimezone(timezone.utc)


def hour_grid(start, end):
    first, last = _instant(start).timestamp(), _instant(end).timestamp()
    if first % 3600 or last % 3600 or not 0 < last - first <= MAX_PERIOD_HOURS * 3600:
        raise ValueError('invalid_measurement_boundary')
    return {stamp * 1000: stamp * 1000 for stamp in range(int(first), int(last), 3600)}


def calendar_grid(start, end, timezone_name, calendar_kind):
    zone = ZoneInfo(timezone_name)
    origin = _instant(start).astimezone(zone).date()
    grid = hour_grid(start, end)
    for stamp in grid:
        local = datetime.fromtimestamp(stamp / 1000, zone)
        day = (local.month, local.day) if calendar_kind == 'yearly' else (local.date() - origin).days
        grid[stamp] = (day, local.hour, local.minute, local.fold)
    return grid


def _measured(value):
    try:
        return type(value) in (int, float) and isfinite(value) and value >= 0
    except OverflowError:
        return False


def source_cells(snapshot, sid, grid):
    """Return valid cells; duplicate start invalidates the cell permanently."""
    buckets = snapshot.get('source_buckets')
    rows = buckets.get(sid) if isinstance(buckets, dict) else None
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError('invalid_source_intervals')
    seen, valid = set(), {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp, stop, value = row.get('from'), row.get('to'), row.get('kwh')
        if type(stamp) not in (int, float) or stamp not in grid:
            continue
        key = grid[stamp]
        if stamp in seen:
            valid.pop(key, None)
            continue
        seen.add(stamp)
        if type(stop) in (int, float) and stop == stamp + 3600000 and _measured(value):
            valid[key] = value
    return valid


def _sum(values):
    try:
        result = fsum(values)
        return result if isfinite(result) else None
    except OverflowError:
        return None


def measurement_evidence(snapshot, start, end):
    """Recompute chunk coverage and household hours without trusting status/counts.

    per_source is internal verified evidence, never a public registry/measurement
    export. A partial source hour cannot become a complete household hour.
    """
    ids, grid = validated_source_ids(snapshot), hour_grid(start, end)
    expected = len(ids) * len(grid)
    if expected > MAX_EVIDENCE_CELLS:
        raise ValueError('measurement_chunk_too_large')
    provenance = snapshot.get('measurement_provenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    per_source, issues = {}, []
    for sid in ids:
        if not valid_provenance(provenance.get(sid)):
            per_source[sid] = {}
            issues.append({'reason': 'measurement_provenance_unavailable', 'source_id': sid})
            continue
        try:
            per_source[sid] = source_cells(snapshot, sid, grid)
        except ValueError:
            per_source[sid] = {}
            issues.append({'reason': 'invalid_source_intervals', 'source_id': sid})
    measured = sum(len(cells) for cells in per_source.values())
    hourly = []
    for stamp in grid:
        values = [cells.get(stamp) for cells in per_source.values()]
        value = _sum(values) if all(value is not None for value in values) else None
        known = [value for value in values if value is not None]
        hourly.append({'from': stamp, 'to': stamp + 3600000, 'kwh': value,
                       'recorded_kwh': _sum(known) if known else None,
                       'measured_sources': len(known), 'expected_sources': len(ids)})
    observed = _sum(value for cells in per_source.values() for value in cells.values()) if measured else None
    known_hours = [row['kwh'] for row in hourly if row['kwh'] is not None]
    recorded = _sum(known_hours) if known_hours else None
    if (measured and observed is None) or (known_hours and recorded is None):
        issues.append({'reason': 'unrepresentable_measurement_sum'})
    complete = measured == expected and all(row['kwh'] is not None for row in hourly) and not issues
    coverage = {'complete': complete, 'ratio': measured / expected,
                'expected_source_seconds': expected * 3600, 'measured_source_seconds': measured * 3600}
    return {'status': 'ready' if complete else 'partial' if measured else 'no_data',
            'kwh': recorded if complete else None, 'recorded_household_kwh': recorded,
            'observed_source_kwh': observed, 'coverage': coverage, 'hourly_buckets': hourly,
            'per_source': per_source, 'source_ids': ids, 'issues': issues,
            'measurement_provenance': {sid: deepcopy(provenance[sid]) for sid in ids
                                       if valid_provenance(provenance.get(sid))}}
