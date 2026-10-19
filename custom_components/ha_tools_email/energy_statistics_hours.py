"""Internal bounded hourly cost facts, consuming existing calculated amounts.

No pricing engine, IO, authorization, public schema or Recorder publisher lives
here. Callers must retain current-context/source guards around this pure work.
Amounts stay unrounded: period bill cent allocation is deliberately separate.
"""
from decimal import Decimal, localcontext

MAX_STATISTICS_HOURS = 168
COMPONENTS = ('energy_cost', 'variable_cost', 'legacy_combined_cost')
COSTS = (*COMPONENTS, 'usage_cost')


def require_statistics_projection(enabled, hours):
    if type(enabled) is not bool:
        raise ValueError('invalid_statistics_projection')
    if enabled and not 0 < hours <= MAX_STATISTICS_HOURS:
        raise ValueError('statistics_projection_too_large')


def _identity(rate):
    return {key: rate.get(key) if rate else None for key in
            ('tariff_id', 'version_id', 'currency', 'price_basis', 'rate_semantics')}


def group_cost_hour(hour, breakdown, amounts, rate, membership, domain):
    """Sum the already computed disjoint device costs; never resolve prices."""
    partition = domain == 'grid_import' and breakdown['status'] == 'ready'
    identity = _identity(rate)
    if domain != 'grid_import':
        reason = 'grid_import_required'
    elif not partition:
        reason = 'partition_incomplete'
    elif rate is None:
        reason = 'tariff_unavailable'
    elif not rate['available']:
        reason = rate.get('reason') or 'price_unavailable'
    elif not identity['currency'] or not identity['price_basis']:
        reason = 'historical_currency_or_basis_unavailable'
    else:
        reason = None
    # A complete partition always supplies the measured remainder, even zero.
    # Its existing calculation proves which components are known; an empty
    # unassigned group must not make unknown-priced zero appear known.
    template = amounts.get('__unmeasured__', {})

    def cell(quantity, members):
        costs = {}
        for key in COSTS:
            values = [amounts.get(sid, {}).get(key) for sid in members]
            known = (all(value is not None for value in values) if members
                     else template.get(key) is not None)
            costs[key] = (format(sum(values, Decimal(0)), 'f')
                          if reason is None and quantity is not None and known else None)
        complete = costs['usage_cost'] is not None
        return {**costs, 'measurement_complete': quantity is not None,
                'pricing_complete': complete, 'complete': complete,
                'reason': None if complete else reason or 'price_unavailable'}

    with localcontext() as context:
        context.prec = 40
        groups = {**identity,
            'areas': {key: cell(row['kwh_decimal'], membership['areas'][key])
                      for key, row in breakdown['areas'].items()},
            'unassigned': cell(breakdown['unassigned']['kwh_decimal'], membership['unassigned']),
            'unmeasured': cell(breakdown['unmeasured']['kwh_decimal'], ['__unmeasured__'])}
    return {'from': hour['from'], 'to': hour['to'],
            'measurement_complete': hour['kwh'] is not None,
            'partition_complete': partition, 'groups': groups}


def household_cost_hours(rows, active):
    """Keep exact active bill row amounts and single-zone attribution.

    Per-row historical metadata does not depend on whole-period compatibility;
    a currency change yields separate correctly labelled hourly facts, never FX.
    """
    if len(rows) > MAX_STATISTICS_HOURS:
        raise ValueError('statistics_projection_too_large')
    rates = {row['version_id']: row for row in active['rates']} if active else {}
    priced = active['rows'] if active else []
    result = []
    with localcontext() as context:
        context.prec = 40
        for index, hour in enumerate(rows):
            price = priced[index] if index < len(priced) else {}
            rate = rates.get(price.get('version_id'))
            identity = _identity({**rate, 'tariff_id': active['tariff_id']} if rate else None)
            costs = dict.fromkeys(COSTS)
            if hour['kwh'] is None:
                reason = 'missing_measurement'
            elif active is None:
                reason = 'tariff_unavailable'
            elif not price.get('available'):
                reason = price.get('reason') or 'price_unavailable'
            elif (price.get('index') != index or price.get('from') != hour['from']
                  or price.get('to') != hour['to']):
                reason = 'priced_row_mismatch'
            elif rate is None or not identity['currency'] or not identity['price_basis']:
                reason = 'historical_currency_or_basis_unavailable'
            else:
                reason = None
                for key in COMPONENTS:
                    value = price.get('costs', {}).get(key)
                    costs[key] = value if value is not None else None
                semantics = identity['rate_semantics']
                parts = ([costs['energy_cost'], costs['variable_cost']] if semantics == 'components'
                         else [costs['legacy_combined_cost']] if semantics == 'legacy_combined' else [None])
                if all(value is not None for value in parts):
                    costs['usage_cost'] = format(sum((Decimal(value) for value in parts), Decimal(0)), 'f')
                else:
                    reason = 'price_unavailable'
            complete = reason is None and costs['usage_cost'] is not None
            household = {**identity, **costs, 'measurement_complete': hour['kwh'] is not None,
                'pricing_complete': complete, 'complete': complete, 'reason': reason}
            zone_id = price.get('zone')
            zone_reason = reason
            if zone_reason is None:
                catalog = rate['profile']['zones']
                if price.get('zoneAttribution') == 'multiple':
                    zone_reason = 'multiple_zones_in_measurement'
                elif (price.get('zoneAttribution') != 'single' or not isinstance(zone_id, str)
                      or price.get('zones') != [zone_id]
                      or not any(zone['id'] == zone_id for zone in catalog)):
                    zone_reason = 'zone_attribution_unavailable'
            zone = {**household, 'zone_id': zone_id if zone_reason is None else None,
                    'attribution': price.get('zoneAttribution') if price.get('zoneAttribution') in ('single', 'multiple') else None,
                    'complete': zone_reason is None, 'pricing_complete': zone_reason is None,
                    'reason': zone_reason}
            if zone_reason is not None:
                zone.update(dict.fromkeys(COSTS))
            result.append({'from': hour['from'], 'to': hour['to'],
                           'household': household, 'zone': zone})
    return result
