"""Pure schema-2 tariff history. Household rows only; no persistence or HA imports.

Amounts are decimal strings. Calendar fees are charged once per household period,
independently of measurement completeness. Schema-1 profiles remain unmodified.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .tariff import EnergyTariff, TariffValidationError, validate_tariff_profile, _stamp, _finite

FEE_KEYS = ('subscription', 'capacity', 'distribution', 'other')
ROUNDING = {'quantum': '0.01', 'mode': 'ROUND_HALF_UP',
            'stage': 'component_period_totals_then_sum', 'row_costs': 'unrounded_decimal',
            'partial_day': 'overlap_seconds/actual_local_day_seconds'}
MAX_ROWS = 10000
MAX_VERSIONS = 32


def _fail(reason):
    raise TariffValidationError([{'path': 'tariff_book', 'reason': reason}])


def _bounded(value, max_bytes=4000000):
    # Bound traversal before serialization/deepcopy, including cyclic Python input.
    stack, nodes, size = [(value, 0)], 0, 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 160000 or depth > 24:
            _fail('input_too_large')
        if isinstance(item, dict):
            if len(item) > 10000 or any(not isinstance(k, str) for k in item):
                _fail('invalid_json')
            stack.extend((k, depth + 1) for k in item)
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            if len(item) > 10000:
                _fail('input_too_large')
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            try:
                size += len(item.encode('utf-8'))
            except UnicodeError:
                _fail('invalid_json')
        elif item is None or type(item) is bool:
            pass
        elif not _finite(item):
            _fail('invalid_json')
        if size > max_bytes:
            _fail('input_too_large')
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError):
        _fail('invalid_json')
    if len(text.encode('utf-8')) > max_bytes:
        _fail('input_too_large')
    return text


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        _fail('invalid_date')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        _fail('invalid_date')
    if not 1900 <= result.year <= 2200:
        _fail('date_out_of_range')
    return result


def _amount(value):
    if value is not None and (not _finite(value) or value < 0 or value > 1e12):
        _fail('invalid_fee')


def _midnight(day, zone):
    return datetime.combine(day, time.min, zone).astimezone(timezone.utc)


def _schedule(profile):
    """Check static selector equivalence classes with the existing engine.

    Twenty-eight representative Gregorian years cover every month/day/weekday
    combination. Exact exception dates and their neighbors are added separately.
    Rule selectors for both today and yesterday cover overnight behavior.
    """
    rules = (profile.get('rules') or []) + (profile.get('exceptions') or [])
    dates = {value for rule in rules for value in (rule.get('dates') or [])}
    if len(rules) > 64 or len(dates) > 366:
        _fail('schedule_work_limit')
    dynamic = profile.get('dynamic')
    if dynamic is not None:
        intervals = sorted(dynamic['intervals'], key=lambda r: _stamp(r['from']))
        if not intervals or any(_stamp(a['to']) != _stamp(b['from']) for a, b in zip(intervals, intervals[1:])):
            _fail('schedule_gap')
        return
    # Static selector classes plus preceding-day overnight rules, at every
    # schedule transition. Validation ignores profile date metadata only here.
    p = deepcopy(profile)
    p.pop('validFrom', None)
    p.pop('validTo', None)
    model = EnergyTariff(p)
    minutes = {0, 1439}
    for rule in rules:
        for interval in rule['intervals']:
            for field in ('from', 'to'):
                hour, minute = map(int, interval[field].split(':'))
                point = (hour * 60 + minute) % 1440
                minutes.update((point, (point - 1) % 1440))
    zone = (timezone(timedelta(minutes=p['clock']['offsetMinutes']))
            if p['clock']['mode'] == 'fixed' else ZoneInfo(p['timezone']))
    candidates = set()
    day = date(2000, 1, 1)
    while day < date(2028, 1, 1):
        candidates.add(day)
        day += timedelta(days=1)
    for value in dates:
        exact = _day(value)
        candidates.update((exact - timedelta(days=1), exact, exact + timedelta(days=1)))
    selectors = model.rules + model.exceptions
    classes = {}
    for day in sorted(candidates):
        previous = (day - timedelta(days=1)).isoformat()
        signature = tuple((model._day_matches(rule, day.isoformat()),
                           model._day_matches(rule, previous)) for rule in selectors)
        classes.setdefault(signature, day)
    for day in classes.values():
        for minute in minutes:
            instant = datetime.combine(day, time.min, zone) + timedelta(minutes=minute)
            if not model.rate_at(instant.isoformat())['available']:
                _fail('schedule_gap_or_ambiguity')


def validate_tariff_book(book):
    """Return a detached normalized book; all version end dates are exclusive."""
    _bounded(book)
    if not isinstance(book, dict) or type(book.get('schema')) is not int or book['schema'] != 2:
        _fail('unsupported_schema')
    tariffs = book.get('tariffs')
    if not isinstance(tariffs, list) or not 1 <= len(tariffs) <= 8:
        _fail('invalid_tariffs')
    result, ids, count = deepcopy(book), set(), 0
    for tariff in result['tariffs']:
        if not isinstance(tariff, dict) or not _id(tariff.get('id')) or tariff['id'] in ids:
            _fail('duplicate_or_invalid_tariff_id')
        ids.add(tariff['id'])
        if not isinstance(tariff.get('name'), str) or not 1 <= len(tariff['name']) <= 128:
            _fail('invalid_name')
        origin = tariff.get('origin')
        if not isinstance(origin, dict) or origin.get('kind') not in ('user', 'template', 'legacy'):
            _fail('invalid_origin')
        if origin.get('reference') is not None and (not isinstance(origin['reference'], str) or len(origin['reference']) > 256):
            _fail('invalid_origin')
        if origin['kind'] == 'template' and not origin.get('reference'):
            _fail('template_requires_provenance')
        versions = tariff.get('versions')
        if not isinstance(versions, list) or not versions:
            _fail('invalid_versions')
        count += len(versions)
        if count > MAX_VERSIONS:
            _fail('too_many_versions')
        version_ids, starts = set(), set()
        for version in versions:
            if not isinstance(version, dict) or not _id(version.get('id')) or version['id'] in version_ids:
                _fail('duplicate_or_invalid_version_id')
            version_ids.add(version['id'])
            start = version.get('valid_from')
            if start is None:
                if origin['kind'] != 'legacy' or version.get('provenance') != 'migrated_unknown_start':
                    _fail('missing_valid_from')
            else:
                _day(start)
            if start in starts:
                _fail('duplicate_version_date')
            starts.add(start)
            end = version.get('valid_to')
            if end is not None:
                _day(end)
                if start is not None and end <= start:
                    _fail('invalid_version_range')
            if version.get('rate_semantics') not in ('components', 'legacy_combined'):
                _fail('invalid_rate_semantics')
            profile = validate_tariff_profile(version.get('profile'))
            if profile is None:
                _fail('missing_profile')
            if profile.get('energyUnit', 'kWh') != 'kWh':
                _fail('unsupported_energy_unit')
            for z in profile['zones']:
                if abs(z['energyRate']) > 1e12 or (z.get('variableRate') is not None and z['variableRate'] > 1e12):
                    _fail('rate_out_of_range')
                if version['rate_semantics'] == 'legacy_combined' and z.get('variableRate') not in (None, 0):
                    _fail('legacy_combined_with_components')
            for row in (profile.get('dynamic') or {}).get('intervals', []):
                if abs(row['energyRate']) > 1e12 or (row.get('variableRate') is not None and row['variableRate'] > 1e12):
                    _fail('rate_out_of_range')
                if version['rate_semantics'] == 'legacy_combined' and row.get('variableRate') not in (None, 0):
                    _fail('legacy_combined_with_components')
            _schedule(profile)
            fixed = version.get('fixed_monthly')
            if not isinstance(fixed, dict) or set(fixed) != set(FEE_KEYS):
                _fail('invalid_fixed_monthly')
            for value in fixed.values():
                _amount(value)
            _amount(version.get('legacy_combined_monthly'))
            if version.get('legacy_combined_monthly') is not None and any(v is not None for v in fixed.values()):
                _fail('ambiguous_fixed_fees')
        versions.sort(key=lambda v: v.get('valid_from') or '')
        for previous, current in zip(versions, versions[1:]):
            if previous.get('valid_to') is not None and previous['valid_to'] != current['valid_from']:
                _fail('version_gap_or_overlap')
            previous['valid_to'] = current['valid_from']
        # Boundaries are local civil dates. A tariff may change clock/currency/
        # basis, but timezone must stay stable to avoid overlapping calendars.
        if len({v['profile']['timezone'] for v in versions}) != 1:
            _fail('timezone_change')
    if result.get('active_tariff_id') is not None and (not _id(result['active_tariff_id']) or result['active_tariff_id'] not in ids):
        _fail('unknown_active_tariff')
    return result


def migrate_tariff_v1(profile, *, legacy_combined, tariff_id='legacy'):
    """Explicit semantics; never infer a combined rate from a missing fee."""
    if type(legacy_combined) is not bool:
        _fail('explicit_legacy_semantics_required')
    _bounded(profile, 65536)
    source = validate_tariff_profile(profile)
    if source is None:
        _fail('missing_profile')
    end = source.get('validTo')
    if end is not None:
        end = (_day(end) + timedelta(days=1)).isoformat()
    start = source.get('validFrom')
    version = {'id': 'baseline', 'valid_from': start, 'valid_to': end,
               'profile': deepcopy(source),
               'rate_semantics': 'legacy_combined' if legacy_combined else 'components',
               'fixed_monthly': dict.fromkeys(FEE_KEYS),
               'legacy_combined_monthly': source.get('fixedMonthly'),
               'provenance': 'migrated_unknown_start' if start is None else 'migrated_explicit_start'}
    book = {'schema': 2, 'active_tariff_id': tariff_id,
            'source_schema1': deepcopy(source), 'migration_issues': ['historical_reports_unchanged'],
            'tariffs': [{'id': tariff_id, 'name': 'Migrated tariff',
                         'origin': {'kind': 'legacy', 'reference': 'schema1'}, 'versions': [version]}]}
    if start is None:
        book['migration_issues'].append('unknown_historical_start')
    return validate_tariff_book(book)


def _bounds(version):
    zone = ZoneInfo(version['profile']['timezone'])
    return tuple(None if version.get(k) is None else int(_midnight(_day(version[k]), zone).timestamp() * 1000)
                 for k in ('valid_from', 'valid_to'))


def _version_at(versions, stamp):
    for version in versions:
        start, end = _bounds(version)
        # An unknown migrated start is provenance, never permission to price
        # invented history. Caller must date that baseline before v2 valuation.
        if start is not None and start <= stamp and (end is None or stamp < end):
            return version
    return None


def _money(value):
    return None if value is None else format(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '.2f')


def _decimal(value):
    return Decimal(str(value))


def _fees(versions, start, end):
    zone = ZoneInfo(versions[0]['profile']['timezone'])
    day = datetime.fromtimestamp(start / 1000, timezone.utc).astimezone(zone).date()
    totals = dict.fromkeys(FEE_KEYS + ('legacy_combined',), Decimal(0))
    known = dict.fromkeys(totals, True)
    parts, identities = [], set()
    while True:
        a = int(_midnight(day, zone).timestamp() * 1000)
        b = int(_midnight(day + timedelta(days=1), zone).timestamp() * 1000)
        if a >= end:
            break
        low, high = max(start, a), min(end, b)
        if low < high:
            v = _version_at(versions, low)
            weight = Decimal(high - low) / Decimal(b - a) / Decimal(monthrange(day.year, day.month)[1])
            values = dict.fromkeys(totals)
            if v is not None:
                identities.add((v['profile']['currency'], v['profile']['priceBasis']))
                values.update(v['fixed_monthly'])
                # In component mode no legacy monthly amount is a known zero;
                # a migrated combined monthly fee leaves named components unknown.
                values['legacy_combined'] = v.get('legacy_combined_monthly')
                if values['legacy_combined'] is None:
                    values['legacy_combined'] = 0
            for key, value in values.items():
                if value is None:
                    known[key] = False
                else:
                    totals[key] += _decimal(value) * weight
            parts.append({'day': day.isoformat(), 'version_id': None if v is None else v['id'],
                          'day_fraction': str(Decimal(high - low) / Decimal(b - a))})
        day += timedelta(days=1)
    named_complete = all(known[k] for k in FEE_KEYS)
    return {'components': {k: _money(totals[k]) if known[k] else None for k in totals},
            'known_components': {k: _money(v) for k, v in totals.items()},
            'complete': named_complete, 'days': parts}, totals, known, identities


def _price(tariff, rows, coverage, fingerprint):
    versions = tariff['versions']
    start, end = _stamp(coverage['from']), _stamp(coverage['to'])
    fee, fee_totals, fee_known, identities = _fees(versions, start, end)
    ordered, issues, priced = [], [], []
    sums = dict.fromkeys(('energy_cost', 'variable_cost', 'legacy_combined_cost'), Decimal(0))
    known = dict.fromkeys(sums, True)
    kwh = Decimal(0)
    usage_sum, usage_known = Decimal(0), True
    measurement_complete = bool(rows) and coverage.get('complete', True) is True
    models = {v['id']: EnergyTariff(v['profile']) for v in versions}
    for index, row in enumerate(rows):
        a, b = _stamp(row.get('from')), _stamp(row.get('to'))
        valid = a is not None and b is not None and 0 < b - a <= 86400000 and _finite(row.get('kwh')) and 0 <= row['kwh'] <= 1e12 and row.get('unit', 'kWh') == 'kWh'
        result = {'index': index, 'available': False, 'reason': 'invalid_measurement'}
        if valid:
            ordered.append((a, b))
            kwh += _decimal(row['kwh'])
            v = _version_at(versions, a)
            last = _version_at(versions, b - 1)
            if v is None:
                result['reason'] = 'unknown_historical_version'
            elif last is None or last['id'] != v['id']:
                result['reason'] = 'mixed_version_bucket'
            else:
                result = models[v['id']].price_bucket(row)
                result.update(index=index, version_id=v['id'], rate_semantics=v['rate_semantics'],
                              provenance=v.get('provenance', 'configured_valid_from'))
                identities.add((v['profile']['currency'], v['profile']['priceBasis']))
                if result['available']:
                    energy = _decimal(row['kwh']) * _decimal(result['energyRate'])
                    variable = None if result['variableRate'] is None else _decimal(row['kwh']) * _decimal(result['variableRate'])
                    amounts = {'energy_cost': energy, 'variable_cost': variable, 'legacy_combined_cost': Decimal(0)}
                    if v['rate_semantics'] == 'legacy_combined':
                        amounts = {'energy_cost': None, 'variable_cost': None, 'legacy_combined_cost': energy}
                    if v['rate_semantics'] == 'legacy_combined':
                        usage_sum += energy
                    elif variable is None:
                        usage_known = False
                    else:
                        usage_sum += energy + variable
                    result['costs'] = {k: None if value is None else str(value) for k, value in amounts.items()}
                    # Engine floats are compatibility diagnostics, never the bill.
                    for key in ('energyCost', 'variableCost', 'usageCost'):
                        result.pop(key, None)
                    for key, value in amounts.items():
                        if value is None:
                            known[key] = False
                        else:
                            sums[key] += value
        else:
            measurement_complete = False
        if not result['available']:
            issues.append({'index': index, 'reason': result['reason']})
            known = dict.fromkeys(known, False)
            usage_known = False
        priced.append(result)
    ordered.sort()
    invalid_layout = (not ordered or ordered[0][0] != start or ordered[-1][1] != end
                      or any(b != c for (_, b), (c, _) in zip(ordered, ordered[1:])))
    overlapping = any(b > c for (_, b), (c, _) in zip(ordered, ordered[1:]))
    if invalid_layout:
        measurement_complete = False
        issues.append({'reason': 'missing_or_overlapping_measurement'})
    pricing_complete = bool(rows) and all(r['available'] for r in priced)
    components_complete = pricing_complete and all(known.values()) and fee['complete']
    compatible = len(identities) == 1
    if not compatible:
        issues.append({'reason': 'incomparable_currency_or_basis'})
    rounded = {key: _money(value) if known[key] and measurement_complete and compatible else None for key, value in sums.items()}
    # Display components are rounded once; total uses those exact strings.
    usage = None
    if usage_known and measurement_complete and compatible and pricing_complete:
        if rounded['energy_cost'] is not None and rounded['variable_cost'] is not None:
            usage = _decimal(rounded['energy_cost']) + _decimal(rounded['variable_cost'])
        else:
            usage = _decimal(_money(usage_sum))
    fixed = sum((_decimal(fee['components'][k]) for k in FEE_KEYS), Decimal(0)) if fee['complete'] and compatible else None
    total = usage + fixed if components_complete and measurement_complete and compatible and usage is not None and fixed is not None else None
    currency, basis = next(iter(identities)) if compatible else (None, None)
    return {'tariff_id': tariff['id'], 'measurement_fingerprint': fingerprint,
            'currency': currency, 'price_basis': basis, 'timezone': versions[0]['profile']['timezone'],
            'measurement_complete': measurement_complete, 'pricing_complete': pricing_complete,
            'components_complete': components_complete, 'compatible': compatible,
            'kwh': str(kwh) if measurement_complete else None,
            **rounded, 'usage_cost': _money(usage), 'fixed_cost': _money(fixed),
            'total_cost': _money(total), 'effective_price': _money(total / kwh) if total is not None and kwh > 0 else None,
            'known_costs': {k: _money(v) if compatible and not overlapping else None for k, v in sums.items()},
            'fees': fee if compatible else None, 'rows': priced, 'issues': issues,
            'rates': [{'version_id': v['id'], 'valid_from': v.get('valid_from'), 'valid_to': v.get('valid_to'),
                       'rate_semantics': v['rate_semantics'], 'currency': v['profile']['currency'],
                       'price_basis': v['profile']['priceBasis'], 'clock': deepcopy(v['profile']['clock']),
                       'provenance': v.get('provenance', 'configured_valid_from')} for v in versions]}


def price_book(book, rows, coverage):
    """Price identical household intervals under every tariff, without activation.

    coverage has from/to (aware ISO or epoch milliseconds), optional complete
    bool and arbitrary JSON source/revision provenance included in fingerprint.
    A service must supply verified measurements; this pure function proves no IO.
    """
    normalized = validate_tariff_book(book)
    encoded = _bounded({'rows': rows, 'coverage': coverage})
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(r, dict) for r in rows):
        _fail('invalid_measurements')
    if not isinstance(coverage, dict) or ('complete' in coverage and type(coverage['complete']) is not bool):
        _fail('invalid_coverage')
    if coverage.get('unit', 'kWh') != 'kWh':
        _fail('unsupported_energy_unit')
    start, end = _stamp(coverage.get('from')), _stamp(coverage.get('to'))
    if start is None or end is None or not 0 < end - start <= 366 * 86400000:
        _fail('invalid_coverage')
    for stamp in (start, end):
        if not 1900 <= datetime.fromtimestamp(stamp / 1000, timezone.utc).year <= 2200:
            _fail('date_out_of_range')
    # Bound the engine minute-scan workload even on overlapping hostile rows.
    if sum(max(0, (_stamp(r.get('to')) or 0) - (_stamp(r.get('from')) or 0)) for r in rows) > 366 * 86400000:
        _fail('measurement_work_limit')
    fingerprint = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    with localcontext() as context:
        context.prec = 40
        results = [_price(tariff, rows, coverage, fingerprint) for tariff in normalized['tariffs']]
        active = next((r for r in results if r['tariff_id'] == normalized.get('active_tariff_id')), None)
        for result in results:
            comparable = (active is not None and active['total_cost'] is not None and result['total_cost'] is not None
                          and (active['currency'], active['price_basis']) == (result['currency'], result['price_basis']))
            mismatch = active is not None and (active['currency'], active['price_basis']) != (result['currency'], result['price_basis'])
            result.update(comparable=comparable, delta_cost=_money(_decimal(result['total_cost']) - _decimal(active['total_cost'])) if comparable else None,
                          comparison_reason=None if comparable else ('currency_or_basis_mismatch' if mismatch else 'incomplete_cost'))
    return {'schema': 2, 'active_tariff_id': normalized.get('active_tariff_id'),
            'measurement_fingerprint': fingerprint, 'rounding': deepcopy(ROUNDING), 'tariffs': results}
