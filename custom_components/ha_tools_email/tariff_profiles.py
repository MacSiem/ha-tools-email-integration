"""Wire schema-2 profiles over the existing schema-2 tariff book.

This module selects no rates and computes no bills. ``validTo`` is exclusive
on the wire, unlike schema 1. The book remains the only historical source.
Callers must still use prepare_tariff_book_edit with the server's clock before
persisting a proposed edit. No function clears storage or rewrites reports.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext

from .tariff import TariffValidationError, _finite, validate_tariff_profile
from .tariff_history import (
    FEE_KEYS, _bounded, _day, _id, migrate_tariff_v1, validate_tariff_book,
)

SIDECAR = 'wire_profile_v2'
CATEGORY = {'subscription': 'subscription', 'capacity': 'capacity',
            'network': 'distribution', 'distribution': 'distribution'}
WIRE_ONLY = {'name', 'fixed', 'rateSemantics', 'fixedFeeSemantics', 'origin',
             'provenance', 'validityClock', 'requiresHolidayDates'}


def _fail(path, reason):
    raise TariffValidationError([{'path': path, 'reason': reason}])


def is_profile_v2(value):
    """Distinguish both schema-2 structures, including malformed hybrids."""
    return (isinstance(value, dict) and type(value.get('schema')) is int
            and value['schema'] == 2 and 'zones' in value
            and 'fixed' in value and 'tariffs' not in value)


def _fees(fixed, semantics):
    if not isinstance(fixed, list) or len(fixed) > 128:
        _fail('fixed', 'invalid_fixed_fees')
    ids, grouped, mapping = set(), {key: [] for key in FEE_KEYS}, []
    for index, item in enumerate(fixed):
        path = f'fixed.{index}'
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not 1 <= len(item['id']) <= 128 or item['id'] in ids):
            _fail(path, 'duplicate_or_invalid_fee_id')
        ids.add(item['id'])
        if not isinstance(item.get('label'), str) or not 1 <= len(item['label']) <= 256:
            _fail(path + '.label', 'invalid_label')
        amount = item.get('monthly')
        if 'monthly' not in item or (amount is not None and
                (not _finite(amount) or not 0 <= amount <= 1e12)):
            _fail(path + '.monthly', 'invalid_fee')
        category = CATEGORY.get(item['id'], 'other')
        grouped[category].append(amount)
        mapping.append({'id': item['id'], 'category': category})
    if semantics == 'legacy_combined':
        # A single undifferentiated schema-1 amount cannot become named fees.
        if len(fixed) != 1 or fixed[0]['id'] != 'fixed':
            _fail('fixed', 'invalid_legacy_fixed_fee')
        return dict.fromkeys(FEE_KEYS), fixed[0]['monthly'], mapping
    if semantics != 'components':
        _fail('fixedFeeSemantics', 'invalid_fixed_fee_semantics')
    totals = {}
    with localcontext() as context:
        context.prec = 40
        for key, amounts in grouped.items():
            # [] explicitly means no fee. A supplied unknown stays unknown.
            total = None if None in amounts else sum((Decimal(str(v)) for v in amounts), Decimal(0))
            if total is not None and total > Decimal('1e12'):
                _fail('fixed', 'fee_total_out_of_range')
            totals[key] = None if total is None else float(total)
    return totals, None, mapping


def _version(profile, version_id):
    _bounded(profile, 65536)
    if not is_profile_v2(profile):
        _fail('profile', 'invalid_wire_profile_v2')
    p = deepcopy(profile)
    if not isinstance(p.get('name'), str) or not 1 <= len(p['name']) <= 128:
        _fail('name', 'invalid_name')
    if p.get('requiresHolidayDates') not in (None, False):
        _fail('requiresHolidayDates', 'holiday_calendar_required')
    if 'fixedMonthly' in p:
        _fail('fixedMonthly', 'schema1_fixed_monthly_requires_migration')
    p.setdefault('clock', {'mode': 'civil'})
    start, end = p.get('validFrom'), p.get('validTo')
    provenance = p.get('provenance')
    origin = p.get('origin', {'kind': 'user'})
    if start is None:
        if (not isinstance(origin, dict) or origin.get('kind') != 'legacy'
                or provenance != 'migrated_unknown_start'):
            _fail('validFrom', 'missing_valid_from')
    else:
        _day(start)
    if end is not None:
        _day(end)
        if start is not None and end <= start:
            _fail('validTo', 'invalid_version_range')
    if not _id(version_id):
        _fail('version_id', 'invalid_version_id')
    fixed, combined, mapping = _fees(p['fixed'], p.get('fixedFeeSemantics', 'components'))
    engine = {key: deepcopy(value) for key, value in p.items()
              if key not in WIRE_ONLY | {'validFrom', 'validTo'}}
    engine['schema'] = 1
    # Validity is on the outer version, with exclusive end. Keeping it inside
    # the schema-1 profile would silently restore inclusive-end semantics.
    validate_tariff_profile(engine)
    result = {'id': version_id, 'valid_from': start, 'valid_to': end,
              'profile': engine, 'rate_semantics': p.get('rateSemantics', 'components'),
              'fixed_monthly': fixed, 'legacy_combined_monthly': combined,
              'validity_clock': p.get('validityClock', 'civil'),
              SIDECAR: {'profile': p, 'fee_projection': deepcopy(fixed),
                        'legacy_combined_monthly': combined,
                        'fee_mapping': mapping, 'projection': 'sum_before_book_rounding'}}
    if provenance is not None:
        result['provenance'] = provenance
    return result, origin


def validate_profile_v2(profile):
    """Validate a savable wire profile with the existing book schedule proof.

    Null-priced templates are drafts, never valid persisted prices. A null
    variable or fixed fee remains allowed and unknown, as in the book API.
    """
    version, origin = _version(profile, 'validation')
    validate_tariff_book({'schema': 2, 'active_tariff_id': 'validation',
        'tariffs': [{'id': 'validation', 'name': profile['name'],
                     'origin': origin, 'versions': [version]}]})
    return deepcopy(version[SIDECAR]['profile'])


def profile_to_book(profile, *, book=None, tariff_id=None, version_id=None):
    """Return a proposed book edit, retaining other tariffs and all versions.

    An exact validFrom edits that version; a new date inserts a version. A new
    insertion closes an open predecessor (or its old automatic boundary).
    Explicit expiry gaps are rejected by the book rather than silently filled.
    Existing version identifiers and metadata survive an edit. This is not a
    historical-edit authorization API; use prepare_tariff_book_edit afterwards.
    """
    clean = None if book is None else validate_tariff_book(book)
    target = tariff_id if tariff_id is not None else (
        clean.get('active_tariff_id') if clean else 'user')
    if not _id(target):
        _fail('tariff_id', 'explicit_tariff_required')
    existing = next((t for t in clean['tariffs'] if t['id'] == target), None) if clean else None
    start = profile.get('validFrom') if isinstance(profile, dict) else None
    old_version = next((v for v in existing['versions'] if v.get('valid_from') == start), None) if existing else None
    vid = version_id if version_id is not None else (
        old_version['id'] if old_version else 'v_' + str(start).replace('-', '_'))
    version, origin = _version(profile, vid)
    if clean is None:
        clean = {'schema': 2, 'active_tariff_id': target, 'tariffs': []}
    if existing is None:
        existing = {'id': target, 'name': profile['name'], 'origin': origin, 'versions': []}
        clean['tariffs'].append(existing)
    elif 'origin' in profile and origin != existing['origin']:
        _fail('origin', 'tariff_origin_change_requires_book_api')
    if old_version:
        if version_id is not None and version_id != old_version['id']:
            _fail('version_id', 'cannot_change_existing_version_id')
        replacement = deepcopy(old_version)
        replacement.update(version)
        existing['versions'][existing['versions'].index(old_version)] = replacement
    else:
        if any(v['id'] == vid for v in existing['versions']):
            _fail('version_id', 'duplicate_version_id')
        ordered = existing['versions']
        previous = next((v for v in reversed(ordered)
                         if (v.get('valid_from') or '') < (start or '')), None)
        following = next((v for v in ordered if (v.get('valid_from') or '') > (start or '')), None)
        if previous is not None and (previous.get('valid_to') is None or
                previous.get('valid_to') == (following or {}).get('valid_from')):
            previous['valid_to'] = start
        existing['versions'].append(version)
    # Book name denotes latest version; the sidecar retains historical names.
    latest = max(existing['versions'], key=lambda v: v.get('valid_from') or '')
    if latest['id'] == vid:
        existing['name'] = profile['name']
    return validate_tariff_book(clean)


def book_to_profile(book, *, tariff_id=None, version_id=None):
    """Project one explicit version, defaulting to the active tariff's latest.

    Preserve wire fee rows exactly. Refuse a stale fee sidecar after a legacy
    book API changed its aggregate: decomposing that sum would invent data.
    A book without a sidecar exposes its known categories without inference.
    """
    clean = validate_tariff_book(book)
    target = tariff_id if tariff_id is not None else clean.get('active_tariff_id')
    tariff = next((t for t in clean['tariffs'] if t['id'] == target), None)
    if tariff is None:
        _fail('tariff_id', 'unknown_tariff')
    version = (tariff['versions'][-1] if version_id is None else
               next((v for v in tariff['versions'] if v['id'] == version_id), None))
    if version is None:
        _fail('version_id', 'unknown_version')
    sidecar = version.get(SIDECAR)
    if sidecar is not None:
        if (not isinstance(sidecar, dict) or
                sidecar.get('fee_projection') != version['fixed_monthly'] or
                sidecar.get('legacy_combined_monthly') != version.get('legacy_combined_monthly')):
            _fail('fixed', 'wire_fee_projection_conflict')
        source = sidecar.get('profile')
        if not is_profile_v2(source):
            _fail('profile', 'invalid_wire_sidecar')
        projected, combined, _ = _fees(source['fixed'], source.get('fixedFeeSemantics', 'components'))
        if projected != version['fixed_monthly'] or combined != version.get('legacy_combined_monthly'):
            _fail('fixed', 'wire_fee_projection_conflict')
        p = deepcopy(source)
    else:
        p = {'name': tariff['name']}
        if version.get('legacy_combined_monthly') is not None or (
                tariff['origin']['kind'] == 'legacy' and
                all(value is None for value in version['fixed_monthly'].values())):
            p['fixed'] = [{'id': 'fixed', 'label': 'opłaty stałe',
                           'monthly': version.get('legacy_combined_monthly')}]
            p['fixedFeeSemantics'] = 'legacy_combined'
        else:
            p['fixed'] = [{'id': key, 'label': key, 'monthly': value}
                          for key, value in version['fixed_monthly'].items()]
    if version is tariff['versions'][-1]:
        p['name'] = tariff['name']
    # Start from the engine's current fields so old wire rates cannot mask an
    # edit made via the book API. Unknown custom engine metadata also survives.
    p.update(deepcopy(version['profile']))
    p.pop('fixedMonthly', None)
    p.update(schema=2, validFrom=version.get('valid_from'), validTo=version.get('valid_to'))
    p['rateSemantics'] = version['rate_semantics']
    p['origin'] = deepcopy(tariff['origin'])
    p['validityClock'] = version.get('validity_clock', 'civil')
    if version.get('provenance') is not None:
        p['provenance'] = version['provenance']
    else:
        p.pop('provenance', None)
    return p


def schema1_to_profile(profile, *, legacy_combined):
    """Use the existing migration; keep unknown start and combined provenance.

    Preserve source_schema1 on the canonical book by calling migrate_tariff_v1
    in persistence code. This helper only returns its wire representation.
    """
    result = book_to_profile(migrate_tariff_v1(profile, legacy_combined=legacy_combined))
    result['name'] = 'Taryfa'
    return result


def list_tariff_templates(*, currency, timezone, holiday_dates=None):
    """Unpriced civil-clock starting points; no operator or holiday guessing.

    G12w requires caller-supplied explicit holiday dates (an empty list is an
    explicit calendar choice). Templates have no effective date or rates.
    """
    if holiday_dates is not None:
        if not isinstance(holiday_dates, list) or len(holiday_dates) > 366:
            _fail('holiday_dates', 'invalid_dates')
        for day in holiday_dates:
            _day(day)
    base = {'schema': 2, 'currency': currency, 'priceBasis': 'gross', 'timezone': timezone,
            'clock': {'mode': 'civil'}, 'validFrom': None, 'validTo': None,
            'exceptions': [], 'fixed': []}
    all_day = [{'from': '00:00', 'to': '24:00'}]
    g11 = dict(deepcopy(base), name='G11', defaultZone='all', rules=[],
               zones=[{'id': 'all', 'label': 'cała doba', 'energyRate': None, 'variableRate': None}])
    g12 = dict(deepcopy(base), name='G12', defaultZone='exp',
               zones=[{'id': 'cheap', 'label': 'tania', 'energyRate': None, 'variableRate': None,
                       'classification': 'cheap'},
                      {'id': 'exp', 'label': 'droga', 'energyRate': None, 'variableRate': None,
                       'classification': 'expensive'}],
               rules=[{'zone': 'cheap', 'priority': 10, 'weekdays': list(range(7)),
                       'intervals': [{'from': '22:00', 'to': '24:00'},
                                     {'from': '00:00', 'to': '06:00'},
                                     {'from': '13:00', 'to': '15:00'}]}])
    g12w = deepcopy(g12)
    g12w.update(name='G12w', requiresHolidayDates=holiday_dates is None)
    g12w['rules'].append({'zone': 'cheap', 'priority': 20, 'weekdays': [0, 6],
                          'intervals': all_day})
    if holiday_dates:
        g12w['exceptions'] = [{'zone': 'cheap', 'priority': 20,
                              'dates': deepcopy(holiday_dates), 'intervals': deepcopy(all_day)}]
    for key, value in (('G11', g11), ('G12', g12), ('G12w', g12w)):
        value['origin'] = {'kind': 'template', 'reference': key}
    return [{'id': key, 'profile': value, 'origin': {'kind': 'template', 'reference': key},
             'notes': ['operator_hours_require_review', 'user_prices_required']}
            for key, value in (('G11', g11), ('G12', g12), ('G12w', g12w))]


def migrate_card_options(options, *, backend_book, valid_from, currency, timezone,
                         price_basis='gross'):
    """One-shot proposal only when storage explicitly has no backend tariff.

    The caller must atomically check the same empty state at persistence. Never
    infer a historical start: the server supplies valid_from. Flat/day-night
    rates retain schema-1/card semantics; distribution still needs user review.
    """
    if backend_book is not None:
        return None
    _bounded(options, 65536)
    if not isinstance(options, dict):
        _fail('options', 'invalid_options')
    mode = options.get('energy_tariff_mode')
    if mode not in ('flat', 'day_night'):
        return None
    templates = list_tariff_templates(currency=currency, timezone=timezone)
    p = deepcopy(templates[0 if mode == 'flat' else 1]['profile'])
    p.update(validFrom=valid_from, priceBasis=price_basis,
             origin={'kind': 'legacy', 'reference': 'card_options'},
             migrationNotes=['uzupełnij dystrybucję'],
             fixed=[{'id': 'fixed', 'label': 'opłaty stałe', 'monthly': None}])
    keys = ('energy_price',) if mode == 'flat' else ('off_peak_rate', 'peak_rate')
    for zone, key in zip(p['zones'], keys):
        rate = options.get(key)
        if not _finite(rate) or not 0 <= rate <= 1e12:
            _fail('options.' + key, 'invalid_rate')
        zone.update(energyRate=rate, variableRate=0)
    if mode == 'day_night':
        hours = []
        for key in ('energy_night_hour_start', 'energy_day_hour_start'):
            hour = options.get(key)
            if type(hour) is not int or not 0 <= hour <= 23:
                _fail('options.' + key, 'invalid_hour')
            hours.append(hour)
        if hours[0] == hours[1]:
            _fail('options', 'identical_day_night_hours')
        # Card day/night has one overnight band, no invented afternoon band.
        p['rules'][0]['intervals'] = [{'from': f'{hours[0]:02d}:00', 'to': f'{hours[1]:02d}:00'}]
    return validate_profile_v2(p)
