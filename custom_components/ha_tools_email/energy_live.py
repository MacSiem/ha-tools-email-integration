"""Pure, bounded current-rate presentation over the existing historical engine.

No IO, measured-energy aggregation, fee allocation, tariff activation or cache.
Callers must authorize the book and guard its revision around collection. The
next transition is any actual change, including leaving a cheap segment,
becoming unavailable, and returning from a historical/dynamic coverage gap.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .tariff_history import HistoricalTariffRates, TariffValidationError, calculate_energy_amounts, validate_tariff_book

MAX_HORIZON = timedelta(days=8)


def _rate(raw):
    available = raw.get('available') is True
    energy = raw.get('energyRate') if available else None
    variable = raw.get('variableRate') if available else None
    amount = calculate_energy_amounts(1, energy, variable, raw['rate_semantics'])['usage_cost'] if available else None
    clock = raw.get('clock')
    public_clock = {'mode': clock['mode']} if clock else None
    if public_clock is not None and clock['mode'] == 'fixed':
        public_clock['offsetMinutes'] = clock['offsetMinutes']
    return {'available': available, 'reason': raw.get('reason') or ('usage_rate_unknown' if available and amount is None else None),
        'zone_id': raw.get('zone') if available else None,
        'label': raw.get('label') if available else None,
        'classification': raw.get('classification') if available else None,
        'rate': float(amount) if amount is not None else None,
        'energy_rate': energy, 'variable_rate': variable,
        'currency': raw.get('currency'), 'price_basis': raw.get('price_basis'),
        'rate_semantics': raw.get('rate_semantics'), 'tariff_timezone': raw.get('timezone'), 'clock': public_clock}


def _signature(row):
    # Identical future versions must not create fake midnight transitions.
    # All unknown intervals have the same availability; a reason-only change
    # does not pretend that a usable tariff has changed.
    if not row['available']:
        return (False,)
    return tuple(row.get(key) for key in ('available', 'zone_id', 'label', 'classification',
        'energy_rate', 'variable_rate', 'rate', 'currency', 'price_basis', 'rate_semantics'))


def build_current(book, *, now, horizon=MAX_HORIZON):
    """Return a fixed allowlist current dict, with a strictly future change.

    ``now`` must be an aware datetime. Horizon is elapsed UTC time, at most eight
    days. Static civil schedules use engine minute precision; dynamic/version
    boundaries come directly from HistoricalTariffRates (millisecond precision).
    ``next_status`` known/not_found_in_horizon/unavailable distinguishes a
    successful transition lookup from a bounded absence or unconfigured book.
    A known transition can lead to unavailable pricing: inspect next_available.

    Zoned now controls displayed offsets. With a fixed-offset datetime, use the
    selected tariff's civil timezone so future DST offsets remain accurate.
    All numeric rates are raw, unrounded; presentation uses energy_format.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError('aware_now_required')
    if not isinstance(horizon, timedelta) or not timedelta(0) < horizon <= MAX_HORIZON:
        raise ValueError('invalid_live_horizon')
    first = now.astimezone(timezone.utc)
    start, end = int(first.timestamp() * 1000), int((first + horizon).timestamp() * 1000)
    if end <= start:
        raise ValueError('live_horizon_below_millisecond')
    result = {**_rate({'available': False}), 'as_of': first.isoformat(),
        'next_change': None, 'next_label': None, 'next_zone_id': None,
        'next_rate': None, 'next_available': None, 'next_reason': None,
        'next_currency': None, 'next_price_basis': None,
        'next_status': 'unavailable', 'searched_until': None}
    if book is None:
        result['reason'] = 'tariff_not_configured'
        return result
    try:
        clean = validate_tariff_book(book)
        selected = next((tariff for tariff in clean['tariffs'] if tariff['id'] == clean.get('active_tariff_id')), None)
        if selected is None:
            result['reason'] = 'no_active_tariff'
            return result
        engine = HistoricalTariffRates(selected)
    except TariffValidationError:
        result['reason'] = 'invalid_tariff_book'
        return result
    raw = engine.rate_at(start)
    current = _rate(raw)
    display_zone = now.tzinfo
    if not isinstance(display_zone, ZoneInfo):
        # Even in an initial version gap, the detached selected profile declares
        # the timezone for upcoming local midnight boundaries.
        display_zone = ZoneInfo(raw.get('timezone') or selected['versions'][0]['profile']['timezone'])
    result.update(current, as_of=first.astimezone(display_zone).isoformat(),
        next_status='not_found_in_horizon', searched_until=datetime.fromtimestamp(end / 1000, timezone.utc).astimezone(display_zone).isoformat())
    initial = _signature(current)
    # Use absolute UTC iteration, never local datetime + timedelta across DST.
    points = set(range((start // 60000 + 1) * 60000, end + 1, 60000))
    points.update(point for point in engine._boundaries if start < point <= end)
    points.add(end)
    for point in sorted(points):
        candidate = _rate(engine.rate_at(point))
        if _signature(candidate) != initial:
            result.update(next_change=datetime.fromtimestamp(point / 1000, timezone.utc).astimezone(display_zone).isoformat(),
                next_label=candidate['label'], next_zone_id=candidate['zone_id'],
                next_rate=candidate['rate'], next_available=candidate['available'],
                next_reason=candidate['reason'], next_currency=candidate['currency'],
                next_price_basis=candidate['price_basis'], next_status='known')
            break
    return result
