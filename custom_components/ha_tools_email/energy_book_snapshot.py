"""Privacy-bounded entity projection using the canonical historical tariff engine."""
from .energy import build_snapshot, measurement_window, next_cheap
from .tariff_history import (HistoricalTariffRates, validate_tariff_book, price_book,
                             calculate_energy_amounts, _version_at, TariffValidationError)


def _number(value):
    return None if value is None else float(value)


def build_book_snapshot(state, measurement, *, now, measurement_timezone):
    # Start from the established public allowlist, never the private analysis DTO.
    result = build_snapshot({'profile': None, 'revision': state['revision']}, measurement,
                            now=now, measurement_timezone=measurement_timezone)
    try:
        book = validate_tariff_book(state['book'])
        active = next((item for item in book['tariffs']
                       if item['id'] == book.get('active_tariff_id')), None)
        if active is None:
            return result, []
        model = HistoricalTariffRates(active)
    except TariffValidationError:
        return result, []
    current = model.rate_at(int(now.timestamp() * 1000))
    available = current['available']
    classification = current.get('classification') if available else None
    energy = current.get('energyRate') if available else None
    variable = current.get('variableRate') if available else None
    usage_rate = calculate_energy_amounts(1, energy, variable,
        current['rate_semantics'])['usage_cost'] if available else None
    result.update(currency=current.get('currency'), price_basis=current.get('price_basis'),
                  tariff_timezone=current.get('timezone'), tariff_clock=current.get('clock'))
    result['current'].update(available=available, reason=current.get('reason'),
        zone_id=current.get('zone') if available else None, zone_class=classification,
        cheap=None if classification is None else classification == 'cheap',
        energy_rate=energy, variable_rate=variable, usage_rate=_number(usage_rate))
    result['next_cheap'] = next_cheap(model, now)
    start, end, aligned = measurement_window(now, measurement_timezone)
    complete = measurement.get('status') == 'ready' and aligned
    priced = None
    if end > start:
        try:
            # Price only the selected tariff, preserving every effective version.
            selected = {'schema': 2, 'active_tariff_id': active['id'], 'tariffs': [active]}
            priced = price_book(selected, measurement.get('hourly_buckets', []),
                {'from': start.isoformat(), 'to': end.isoformat(),
                 'complete': complete})['tariffs'][0]
        except (TariffValidationError, ValueError, TypeError):
            pass
    today = result['today']
    today.update(energy_cost=None, variable_cost=None, usage_cost=None, fixed_monthly=None,
                 tariff_complete=False, issues=[])
    if priced is not None:
        identity = (priced['currency'], priced['price_basis'])
        if result['currency'] is None:
            result.update(currency=identity[0], price_basis=identity[1])
        compatible = identity == (result['currency'], result['price_basis'])
        today['tariff_complete'] = priced['pricing_complete'] and priced['compatible'] and compatible
        today['issues'] = [{'reason': item['reason']} for item in priced['issues'][:32]]
        if compatible:
            for key in ('energy_cost', 'variable_cost', 'usage_cost'):
                today[key] = _number(priced[key])
    if not complete:
        today['issues'].insert(0, {'reason': 'measurement_incomplete'})
    if not aligned:
        today['issues'].insert(0, {'reason': 'calendar_boundary_not_recorder_hour'})
    version = _version_at(active['versions'], int(now.timestamp() * 1000))
    if version is not None:
        fees = version['fixed_monthly'].values()
        if all(value is not None for value in fees):
            today['fixed_monthly'] = sum(fees)
        if version.get('legacy_combined_monthly') is not None:
            today['fixed_monthly'] = version['legacy_combined_monthly']
    result['status'] = ('ready' if available and today['usage_cost'] is not None
                        else 'partial' if available else 'unavailable')
    return result, model._boundaries
