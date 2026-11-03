"""Pure model/1 projection of verified, already priced producer facts.

No HA, Recorder IO, tariff resolution or synthetic allocation. ``inputs`` contains
period, projection (EnergyAnalysisAccumulator.report_projection), consumers
(Energy prefs), membership (resolve_membership snapshot), optional tariff display
metadata, colors (daily 30-day identity map) and comparisons (verified baselines).
The collector owns authorization, revisions and matching historical time windows.

Each projection hour has from/to (ISO aware or epoch milliseconds), kwh and
costs (energy_cost/variable_cost/usage_cost); devices maps source id to inclusive
kwh/costs. Native HA may supply usage_cost alone, fixed_share=None. Quantities
and costs may be Decimal strings. None always means unknown. Output numbers are
unrounded JSON floats; all intermediate aggregation uses Decimal.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from math import ceil
from zoneinfo import ZoneInfo

from .energy_measurements import calendar_grid

SCHEMA = 'ha_tools.energy_report/1'
ZERO = Decimal(0)
COSTS = ('energy_cost', 'variable_cost', 'usage_cost')


def _decimal(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError('invalid_report_number')
    result = Decimal(str(value))
    if not result.is_finite() or abs(result) > Decimal('1e100'):
        raise ValueError('invalid_report_number')
    return result


def _instant(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError('report_timezone_required')
    return result.astimezone(timezone.utc)


def _sum(values, *, empty=None):
    values = list(values)
    if not values:
        return empty
    return sum(values, ZERO) if all(value is not None for value in values) else None


def _known_sum(values):
    values = [value for value in values if value is not None]
    return sum(values, ZERO) if values else None


def _ratio(value, total):
    return value / total if value is not None and total is not None and total > 0 else None


def _json(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _tree(consumers):
    nodes = {}
    if not isinstance(consumers, list) or len(consumers) > 512:
        raise ValueError('invalid_report_consumers')
    for row in consumers:
        sid = row.get('stat_consumption')
        if not isinstance(sid, str) or not sid or sid in nodes:
            raise ValueError('invalid_report_consumers')
        nodes[sid] = row.get('included_in_stat')
    for sid in nodes:
        seen, node = set(), sid
        while node is not None:
            if node not in nodes or node in seen:
                raise ValueError('invalid_report_hierarchy')
            seen.add(node)
            node = nodes[node]
    return nodes


def _hours_label(hours):
    hours = sorted(set(hours))
    if not hours:
        return ''
    spans, first, previous = [], hours[0], hours[0]
    for hour in hours[1:]:
        if hour != previous + 1:
            spans.append((first, previous + 1))
            first = hour
        previous = hour
    spans.append((first, previous + 1))
    if len(spans) > 1 and spans[0][0] == 0 and spans[-1][1] == 24:
        spans = [(spans[-1][0], spans[0][1])] + spans[1:-1]
    return ', '.join(f'{first}–{last}' for first, last in spans)


def _coverage(hours, start, end):
    """Compute baseline coverage from facts; ignore caller-declared ratios."""
    if not isinstance(hours, list) or len(hours) > 8784:
        raise ValueError('baseline_projection_too_large')
    a, b = _instant(start), _instant(end)
    seconds = (b - a).total_seconds()
    if not 0 < seconds <= 366 * 86400:
        raise ValueError('invalid_baseline_period')
    seen, measured, value = set(), ZERO, ZERO
    any_known = False
    for row in hours:
        low, high = _instant(row['from']), _instant(row['to'])
        duration = (high - low).total_seconds()
        if low in seen or not a <= low < high <= b or not 0 < duration <= 3600:
            raise ValueError('invalid_baseline_hour')
        seen.add(low)
        quantity = _decimal(row.get('kwh'))
        if quantity is not None:
            if quantity < 0:
                raise ValueError('negative_import')
            value += quantity
            measured += Decimal(str(duration))
            any_known = True
    # Intervals must not overlap even when starts differ.
    intervals = sorted((_instant(row['from']), _instant(row['to'])) for row in hours)
    if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
        raise ValueError('overlapping_baseline_hours')
    return (value if any_known else None), measured / Decimal(str(seconds))


def _comparison_cells(period, hours, timezone_name, kind):
    """Use the shared calendar grid on qualified household-hour values.

    A short final interval keeps its actual duration in the frame, so only an
    equally long observed interval can be paired with it.
    """
    start, end = _instant(period['start']), _instant(period['end'])
    seconds = (end-start).total_seconds()
    count = ceil(seconds / 3600)
    grid_end = start + timedelta(hours=count)
    grid = calendar_grid(start, grid_end, timezone_name,
                         {'day':'daily','week':'weekly','month':'monthly','year':'yearly'}[kind])
    cells = {}
    for row in hours:
        a, b = _instant(row['from']), _instant(row['to'])
        stamp = int(a.timestamp()*1000)
        if stamp not in grid or b != min(a+timedelta(hours=1),end):
            raise ValueError('invalid_comparison_hour')
        frame = (*grid[stamp], int((b-a).total_seconds()))
        if frame in cells:
            raise ValueError('duplicate_comparison_frame')
        cells[frame] = _decimal(row.get('kwh'))
    frames = [(*frame, int(min(3600, (end-datetime.fromtimestamp(stamp/1000,timezone.utc)).total_seconds())))
              for stamp, frame in grid.items()]
    if len(set(frames)) != len(frames):
        raise ValueError('duplicate_comparison_frame')
    return frames, cells


def _comparisons(requests, total, coverage, scope, current_period, current_hours):
    result = []
    timezone_name, kind = current_period['timezone'], current_period['kind']
    _, coverage = _coverage(current_hours, current_period['start'], current_period['end'])
    try:
        current_frames, current_cells = _comparison_cells(current_period, current_hours, timezone_name, kind)
    except ValueError:
        current_frames, current_cells = [], {}
    for request in requests:
        key = request['key']
        baselines = request.get('baselines', []) if key == 'avg_7d' else [request]
        if key == 'avg_7d' and len(baselines) > 7:
            raise ValueError('too_many_mean_baselines')
        qualified, qualifications, dates, windows, closed = [], [], set(), [], []
        for baseline in baselines:
            period = baseline.get('period')
            if period and 'hours' in baseline:
                identity = (_instant(period['start']), _instant(period['end']))
                if identity in dates:
                    raise ValueError('duplicate_baseline')
                dates.add(identity)
                value, ratio = _coverage(baseline['hours'], period['start'], period['end'])
                same_scope = baseline.get('scope') == scope
                allowed = ratio >= Decimal('.9') and value is not None and same_scope
                reason = None if allowed else ('scope_mismatch' if not same_scope else 'insufficient_coverage')
            else:
                value, ratio, allowed, reason = None, ZERO, False, 'baseline_evidence_missing'
            if allowed:
                try:
                    frames = _comparison_cells(period, baseline['hours'], timezone_name, kind)
                except ValueError:
                    allowed, reason = False, 'unsupported_comparison_grid'
            qualifications.append({'coverage': ratio, 'qualified': allowed, 'reason': reason})
            if allowed:
                qualified.append(value)
                windows.append(frames)
                calendar_end = period.get('calendar_end')
                closed.append((_instant(period['end']) == _instant(calendar_end))
                              if calendar_end else scope == 'full_period')
        value = sum(qualified, ZERO) / len(qualified) if qualified else None
        eligible = value is not None and coverage >= Decimal('.9') and bool(current_frames)
        compared_current, compared_baseline, comparison_scope = None, None, None
        if eligible:
            complete = (coverage == 1 and current_period['end'] == current_period['calendar_end']
                        and all(closed) and all(q['coverage'] == 1 for q in qualifications if q['qualified']))
            if complete:
                compared_current, compared_baseline, comparison_scope = total, value, 'calendar_totals'
            else:
                grids = [(current_frames,current_cells), *windows]
                common = set.intersection(*(set(f for f in frames if cells.get(f) is not None) for frames,cells in grids))
                # Match the shared comparator: reject incompatible frame order and
                # require 90% common evidence on every participating period.
                orders = [[frame for frame in frames if frame in common] for frames,_ in grids]
                if (common and all(order == orders[0] for order in orders[1:]) and
                        all(Decimal(sum(frame[-1] for frame in common)) /
                            Decimal(sum(frame[-1] for frame in frames)) >= Decimal('.9')
                            for frames,_ in grids)):
                    compared_current = sum((current_cells[f] for f in orders[0]), ZERO)
                    compared_baseline = sum((sum((cells[f] for f in order),ZERO)
                                             for order,(_,cells) in zip(orders[1:],windows)),ZERO)/len(windows)
                    comparison_scope = 'common_measurements'
                else:
                    eligible = False
        delta = compared_current - compared_baseline if eligible and compared_current is not None else None
        result.append({'key': key, 'label': request.get('label', key), 'kwh': value,
            'compared_current_kwh': compared_current, 'compared_baseline_kwh': compared_baseline,
            'comparison_scope': comparison_scope, 'delta_kwh': delta,
            'delta_pct': 100 * delta / compared_baseline
                if delta is not None and compared_baseline != 0 else None,
            'coverage_ok': eligible, 'reason': None if eligible else
                ('current_coverage_below_90' if coverage < Decimal('.9') else
                 'unsupported_comparison_grid' if not current_frames else
                 'insufficient_common_coverage' if value is not None else 'baseline_unqualified'),
            'qualified_days': len(qualified), 'requested_days': 7 if key == 'avg_7d' else 1,
            'denominator': len(qualified), 'qualifications': qualifications})
    return result


def _previous_ticks(previous, period, zone):
    """Align actual facts by civil position and fold; never synthesize DST cells."""
    if not previous:
        return {}
    _coverage(previous.get('hours', []), previous['start'], previous['end'])
    first = _instant(previous['start']).astimezone(zone)
    def key(local):
        day = (local.month, local.day) if period['kind'] == 'year' else (local.date()-first.date()).days
        return (day, local.hour, local.fold)
    return {key(_instant(row['from']).astimezone(zone)): _decimal(row.get('kwh'))
            for row in previous.get('hours', [])}


def _daily_rows(hourly, zone):
    """Aggregate verified hourly producer facts, retaining gaps and real DST length."""
    days = {}
    for hour in hourly:
        local = _instant(hour['start']).astimezone(zone)
        day = days.setdefault(local.date(), [])
        day.append(hour)
    rows = []
    for date, hours in days.items():
        observed = [h for h in hours if not h['future']]
        zones = list(dict.fromkeys(h['zone'] for h in observed if h['zone'] is not None))
        rows.append({'start': datetime.combine(date,datetime.min.time(),zone).isoformat(),
            'hours':len(hours), 'kwh':_known_sum(h['kwh'] for h in observed),
            'recorded_kwh':_known_sum(h['recorded_kwh'] for h in observed),
            'prev_kwh':_sum(h['prev_kwh'] for h in hours),
            'cost':_sum(h['cost'] for h in observed if h['kwh'] is not None),
            'energy_cost':_sum(h['energy_cost'] for h in observed if h['kwh'] is not None),
            'variable_cost':_sum(h['variable_cost'] for h in observed if h['kwh'] is not None),
            'measured_kwh':_known_sum(h['measured_kwh'] for h in observed),
            'complete':bool(observed) and len(observed)==len(hours) and all(h['kwh'] is not None and not h['partial'] for h in hours),
            'partial':any(h['partial'] for h in hours) or bool(observed) and len(observed)!=len(hours),
            'future':not observed,
            'zones':[{'id':z,'kwh':_known_sum(h['kwh'] for h in observed if h['zone']==z)} for z in zones]})
    return rows


def build_report(inputs):
    """Build an independent model without mutating inputs or pricing again.

    period.end is the calendar end; measured_until optionally cuts a so-far
    window. Absent partial five-minute evidence stays null, never fabricated.
    scope is an opaque collector-verified matching-window identity carried by
    every comparison (e.g. full_day or elapsed_19h05). For avg_7d, average N
    qualified days and publish N/7 explicitly; never divide missing days by 7.
    """
    with localcontext() as context:
        context.prec = 40
        return _json(_build_report(inputs))


def _build_report(inputs):
    period = deepcopy(inputs['period'])
    start, end = _instant(period['start']), _instant(period['end'])
    zone = ZoneInfo(period['timezone'])
    duration = (end - start).total_seconds()
    if not 0 < duration <= 366 * 86400 or duration % 3600:
        raise ValueError('invalid_report_period')
    cutoff = _instant(inputs.get('measured_until', period['end']))
    if not start < cutoff <= end:
        raise ValueError('invalid_report_cutoff')
    period['hours'] = int(duration / 3600)
    period['complete'] = bool(period.get('complete', True)) and cutoff == end
    projection = inputs['projection']
    config = _tree(inputs.get('consumers', []))
    roots = [sid for sid, parent in config.items() if parent is None]
    domain = inputs.get('consumer_domain','grid_import')
    if domain not in ('grid_import','site_consumption'):
        raise ValueError('invalid_consumer_domain')
    partitioned = domain == 'grid_import' or not config
    if period['hours'] * max(1, len(config)) > 262144:
        raise ValueError('report_projection_too_large')
    sources = inputs.get('membership', {}).get('sources', {})
    provided = {}
    for row in projection.get('hours', []):
        instant = _instant(row['from'])
        finish = _instant(row['to'])
        if (instant in provided or not start <= instant < finish <= cutoff or
                (instant - start).total_seconds() % 3600 or
                finish != min(instant + timedelta(hours=1), cutoff)):
            raise ValueError('invalid_report_hour')
        provided[instant] = row
    if len(provided) > period['hours']:
        raise ValueError('report_projection_too_large')
    currency, basis = projection.get('currency'), projection.get('price_basis')
    identities = {(row.get('currency'), row.get('price_basis')) for row in provided.values()
                  if row.get('costs', {}).get('usage_cost') is not None}
    identity_ok = bool(currency and basis) and identities <= {(currency, basis)}
    issues, hourly, residuals, adjusted, device_hours = [], [], [], ZERO, {sid: [] for sid in config}
    double_counting, measured_hours = False, 0
    previous = _previous_ticks(inputs.get('previous'),period,zone)
    first_date = start.astimezone(zone).date()
    for offset in range(period['hours']):
        instant = start + timedelta(hours=offset)
        raw = provided.get(instant, {})
        kwh = _decimal(raw.get('kwh'))
        if kwh is not None and kwh < 0:
            raise ValueError('negative_import')
        eligible = instant < cutoff
        if eligible and kwh is not None:
            measured_hours += 1
        costs = {key: _decimal(raw.get('costs', {}).get(key)) if identity_ok and kwh is not None else None for key in COSTS}
        cells = raw.get('devices', {})
        root_values = [_decimal(cells.get(sid, {}).get('kwh')) for sid in roots]
        measured = _sum(root_values, empty=ZERO) if raw and partitioned else None
        for sid in config:
            cell = cells.get(sid, {})
            quantity = _decimal(cell.get('kwh'))
            if quantity is not None and quantity < 0:
                raise ValueError('negative_consumer')
            device_hours[sid].append({'kwh': quantity,
                'cost': _decimal(cell.get('costs', {}).get('usage_cost')) if identity_ok and quantity is not None and partitioned else None,
                'eligible': eligible})
        residual = kwh - measured if kwh is not None and measured is not None else None
        if residual is not None and residual < Decimal('-.05'):
            double_counting = True
        elif residual is not None and residual < 0:
            adjusted -= residual
            residual = ZERO
        if eligible:
            residuals.append(residual)
        local = instant.astimezone(zone)
        previous_day = (local.month,local.day) if period['kind']=='year' else (local.date()-first_date).days
        previous_value = previous.get((previous_day,local.hour,local.fold),_decimal(raw.get('prev_kwh')))
        recorded = _decimal(raw.get('recorded_kwh', raw.get('kwh')))
        if recorded is not None and recorded < 0:
            raise ValueError('negative_recorded_import')
        hourly.append({'start': local.isoformat(), 'hour': local.hour, 'fold': local.fold,
            'kwh': kwh, 'zone': raw.get('zone'), 'classification': raw.get('classification'), **costs, 'cost': costs['usage_cost'],
            'recorded_kwh': recorded, 'measured_sources': raw.get('measured_sources'),
            'expected_sources': raw.get('expected_sources'),
            'measured_kwh': measured, 'prev_kwh': previous_value,
            'partial': bool(raw) and _instant(raw['to']) - instant < timedelta(hours=1),
            'future': not eligible})
    expected = ceil((cutoff - start).total_seconds() / 3600)
    coverage_ratio = Decimal(measured_hours) / expected
    coverage = {'expected_hours': expected, 'measured_hours': measured_hours,
        'ratio': coverage_ratio, 'complete': measured_hours == expected,
        'status': 'complete' if measured_hours == expected else 'partial' if measured_hours else 'no_data'}
    share_ok = coverage['complete'] and partitioned
    imported = _known_sum(row['kwh'] for row in hourly if not row['future'])
    observed = [row for row in hourly if row['kwh'] is not None and not row['future']]
    totals = {key: _sum(row[key] for row in observed) for key in COSTS}
    usage = totals['usage_cost']
    fixed = _decimal(projection.get('fixed_share')) if identity_ok else None
    totals.update(import_kwh=imported,
        recorded_import_kwh=_known_sum(row['recorded_kwh'] for row in hourly if not row['future']),
        fixed_share=fixed,
        total_with_fixed=usage + fixed if coverage['complete'] and usage is not None and fixed is not None else None,
        effective_rate=_ratio(usage, imported))
    peak = max(observed, key=lambda row: row['kwh']) if observed else None
    totals.update(peak_hour=peak['hour'] if peak else None, peak_kwh=peak['kwh'] if peak else None)

    def share(value):
        return _ratio(value, imported) if share_ok else None

    all_devices = {}
    for sid in config:
        rows = [row for row in device_hours[sid] if row['eligible']]
        quantity = _known_sum(row['kwh'] for row in rows)
        cost = _sum(row['cost'] for row in rows if row['kwh'] is not None)
        meta = sources.get(sid, {})
        all_devices[sid] = {'id': sid, 'name': meta.get('name') or sid, 'kwh': quantity,
            'cost': cost, 'effective_rate': _ratio(cost, quantity), 'share': share(quantity),
            'area_id': meta.get('area_id'), 'floor_id': meta.get('floor_id'),
            'measurement_complete': all(row['kwh'] is not None for row in rows), 'children': []}
    # Iterative tree assembly, safe for the supported 512-node hierarchy.
    for sid, parent in config.items():
        if parent is not None:
            all_devices[parent]['children'].append(all_devices[sid])
    devices = sorted((all_devices[sid] for sid in roots), key=lambda row: (-(row['kwh'] or ZERO), row['id']))
    measured = _sum((row['kwh'] for row in devices), empty=ZERO) if partitioned and (devices or imported is not None) else None
    remainder = _known_sum(residuals)
    if double_counting:
        remainder = None
        issues.append({'code': 'double_counting', 'severity': 'warning', 'data': {}})
    device_cost = _sum((row['cost'] for row in devices), empty=ZERO)
    # A monetary residual needs the same eligible-hour evidence as R4's
    # quantity residual. Independent device costs can remain valid on their own
    # mask; subtracting them from a partial grid subtotal is not a valid balance.
    residual_supported = (partitioned and not double_counting and remainder is not None
        and all(value is not None for value in residuals))
    remainder_cost = usage - device_cost if residual_supported and usage is not None and device_cost is not None else None
    unmeasured = {'kwh': remainder, 'recorded_kwh': remainder, 'share': share(remainder), 'cost': remainder_cost,
        'status': 'double_counting' if double_counting else 'ok' if partitioned and all(value is not None for value in residuals) else 'unknown',
        'tolerance_adjustment_kwh': adjusted,
        'raw_balance_kwh': imported - measured if imported is not None and measured is not None else None}
    totals.update(measured_kwh=measured, unmeasured_kwh=remainder)

    colors = inputs.get('colors', {})
    color_map = colors.get('devices', {})
    known_colors = colors.get('as_of') == inputs.get('color_as_of', start.astimezone(zone).date().isoformat()) and colors.get('window_days') == 30
    selected = {sid: color for sid, color in color_map.items() if sid in roots and color in ('d1', 'd2', 'd3', 'd4')} if known_colors else {}
    if len(set(selected.values())) != len(selected):
        raise ValueError('duplicate_device_color')
    if not known_colors:
        issues.append({'code': 'device_colors_unavailable', 'severity': 'info', 'data': {}})
    ranking = [{**row, 'color': selected[row['id']]} for row in devices if row['id'] in selected]
    other = [row for row in devices if row['id'] not in selected]
    if other:
        quantity = _sum((row['kwh'] for row in other), empty=ZERO)
        ranking.append({'id': '_other_measured', 'name': 'Pozostałe zmierzone', 'kwh': quantity,
            'share': share(quantity), 'cost': _sum(row['cost'] for row in other),
            'color': 'other', 'count': len(other), 'children': deepcopy(other)})
    ranking.append({'id': '_unmeasured', 'name': 'Niezmierzone', **unmeasured, 'color': 'unmeasured'})

    def grouped(level):
        groups = {}
        for device in devices:
            sid, key = device['id'], device.get(level + '_id')
            meta = sources.get(sid, {})
            group = groups.setdefault(key, {level + '_id': key,
                'name': meta.get(level + '_name') or ('Bez pokoju' if level == 'area' else 'Bez piętra'),
                'floor_id': device.get('floor_id'), 'floor': meta.get('floor_name'), 'members': []})
            group['members'].append(device)
        result = []
        for key, group in groups.items():
            members = group.pop('members')
            quantity = _sum(row['kwh'] for row in members)
            if key is None and quantity == 0:
                continue
            result.append({**group, 'kwh': quantity, 'share': share(quantity),
                'cost': _sum(row['cost'] for row in members), 'devices': len(members)})
        result.sort(key=lambda row: (-(row['kwh'] or ZERO), row['name']))
        result.append({level + '_id': '_unmeasured', 'name': 'Niezmierzone', **unmeasured})
        return result

    catalog = inputs.get('tariff', {}).get('zones', [])
    zone_ids = list(dict.fromkeys([row['id'] for row in catalog] + [row['zone'] for row in hourly if row['zone'] is not None]))
    zones = []
    for zid in zone_ids:
        rows = [row for row in hourly if row['zone'] == zid and not row['future']]
        meta = next((row for row in catalog if row['id'] == zid), {})
        quantity = _known_sum(row['kwh'] for row in rows)
        measured_rows = [row for row in rows if row['kwh'] is not None]
        energy, variable = (_sum(row[key] for row in measured_rows) for key in ('energy_cost', 'variable_cost'))
        classifications = {row.get('classification') for row in rows if row['kwh'] is not None}
        classification = next(iter(classifications)) if len(classifications)==1 else None
        zones.append({'id': zid, 'label': meta.get('label', zid), 'classification': classification, 'kwh': quantity,
            'share': share(quantity), 'energy_cost': energy, 'variable_cost': variable,
            'cost': _sum(row['cost'] for row in measured_rows),
            'energy_rate': _ratio(energy, quantity), 'variable_rate': _ratio(variable, quantity),
            'rate': _ratio(_sum(row['cost'] for row in measured_rows), quantity),
            'hours': [row['hour'] for row in rows], 'hours_label': _hours_label([row['hour'] for row in rows])})
    if not coverage['complete']:
        issues.append({'code': 'partial_coverage' if measured_hours else 'no_data', 'severity': 'warning',
            'data': {'measured_hours': measured_hours, 'expected_hours': expected}})
    if not partitioned:
        issues.append({'code':'consumer_domain_mismatch','severity':'warning','data':{'consumer_domain':domain}})
    if not identity_ok:
        issues.append({'code': 'incomparable_currency_or_basis', 'severity': 'warning', 'data': {}})
    comparisons = _comparisons(inputs.get('comparisons', []), imported, coverage_ratio, inputs.get('scope', 'full_period'),
        {**period, 'calendar_end': end, 'end': cutoff}, list(provided.values()))
    tariff = deepcopy(inputs.get('tariff', {}))
    fixed_rows = tariff.get('fixed', [])
    monthly = _sum((_decimal(row.get('monthly')) for row in fixed_rows))
    tariff.update(valid_from=tariff.get('valid_from', tariff.get('validFrom')),
        price_basis=basis, fixed_monthly=_decimal(tariff.get('fixed_monthly'))
            if 'fixed_monthly' in tariff else monthly,
        days_in_month=monthrange(start.astimezone(zone).year, start.astimezone(zone).month)[1],
        history=deepcopy(projection.get('rates', [])))
    tariff['zones'] = [{**meta, 'label': meta.get('label') or meta['id'],
        'energy_rate': _decimal(meta.get('energy_rate', meta.get('energyRate'))),
        'variable_rate': _decimal(meta.get('variable_rate', meta.get('variableRate'))),
        'hours_label': next((row['hours_label'] for row in zones if row['id'] == meta['id']), '')}
        for meta in catalog]
    # Fee allocation is exclusively producer-owned; never derive fee shares
    # from display metadata or claim a zero for unknown native HA fees.
    tariff['fixed'] = [{**row, 'share': _decimal(row.get('share'))} for row in fixed_rows]
    return {'schema': SCHEMA, 'period': period, 'coverage': coverage, 'currency': currency,
        'price_basis': basis, 'totals': totals, 'zones': zones, 'hourly': hourly,
        'daily': _daily_rows(hourly,zone),
        'devices': devices, 'ranking': ranking, 'unmeasured': unmeasured,
        'rooms': grouped('area'), 'floors': grouped('floor'), 'comparisons': comparisons,
        'alternatives': deepcopy(inputs.get('alternatives', [])),
        'tariff': tariff, 'issues': issues,
        'provenance': {'measurement_fingerprint': projection.get('measurement_fingerprint'),
            'membership_revision': inputs.get('membership', {}).get('revision'),
            'color_as_of': colors.get('as_of') if known_colors else None}}
