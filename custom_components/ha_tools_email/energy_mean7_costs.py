"""Daily mean pricing on the authoritative eight-window source mask.

The caller supplies selected actual UTC source values, not declared coverage.
Average seven unrounded component sums before the one display rounding. Fees
stay once on each real household period and are then averaged separately.
"""
from copy import deepcopy
from decimal import Decimal, localcontext

from .energy_comparison_core import new_usage_cost_state, add_usage_cost, finish_usage_cost
from .energy_measurements import _instant
from .tariff_history import HistoricalTariffRates, FEE_KEYS, ROUNDING, _fees, _money


def _mean_states(states):
    # All seven days participate, including measured zero days and unknown
    # prices. Unknown pricing never changes the denominator to available days.
    first = states[0]
    return {
        'kwh': sum((row['kwh'] for row in states), Decimal(0)) / 7,
        'amounts': {key: sum((row['amounts'][key] for row in states), Decimal(0)) / 7
                    for key in first['amounts']},
        'priced': {key: sum(row['priced'][key] for row in states) / 7 for key in first['priced']},
        'seconds': sum(row['seconds'] for row in states) / 7,
        **{key: set().union(*(row[key] for row in states))
           for key in ('identities', 'semantics', 'issues')},
    }


def _mean_fees(fees):
    totals = {key: sum((row[1][key] for row in fees), Decimal(0)) / 7 for key in fees[0][1]}
    known = {key: all(row[2][key] for row in fees) for key in totals}
    return ({'components': {key: _money(value) if known[key] else None for key, value in totals.items()},
             'known_components': {key: _money(value) for key, value in totals.items()},
             'complete': all(known[key] for key in FEE_KEYS),
             'periods': [deepcopy(row[0]) for row in fees]}, totals, known,
            set().union(*(row[3] for row in fees)))


def _side(state, fee_data, *, eligible, complete):
    fee, _, _, identities = fee_data
    usage = finish_usage_cost(state, eligible=eligible, identity_constraints=identities)
    fixed = (sum((Decimal(fee['components'][key]) for key in FEE_KEYS), Decimal(0))
             if fee['complete'] and usage['compatible'] else None)
    total = (Decimal(usage['usage_cost']) + fixed
             if complete and usage['pricing_complete'] and fixed is not None
             and all(usage[key] is not None for key in
                     ('energy_cost', 'variable_cost', 'legacy_combined_cost')) else None)
    return {**usage, 'fixed_scope': 'household_period', 'fixed_cost': _money(fixed),
            'fees': deepcopy(fee) if usage['compatible'] else None,
            'total_cost': _money(total), 'effective_price': _money(total / state['kwh'])
                if total is not None and state['kwh'] > 0 else None}


def price_mean7(selected, periods, tariff, energy, *, complete):
    """Internal eight-window computation after source/calendar validation."""
    if len(selected) != 8 or len(periods) != 8:
        raise ValueError('eight_pricing_windows_required')
    rates = HistoricalTariffRates(tariff)
    versions = tuple(item[2] for item in rates._compiled)
    with localcontext() as context:
        context.prec = 40
        states = [new_usage_cost_state(rates) for _ in selected]
        for state, hours in zip(states, selected):
            for stamp, values in hours.items():
                add_usage_cost(state, stamp, values, rates)
        fees = [_fees(versions, *(int(_instant(period[key]).timestamp()*1000)
                                 for key in ('start', 'end'))) for period in periods]
        allowed = energy['status'] == 'ready'
        sides = [_side(state, fee, eligible=allowed, complete=complete)
                 for state, fee in zip(states, fees)]
        baseline = _side(_mean_states(states[1:]), _mean_fees(fees[1:]),
                         eligible=allowed, complete=complete)
        baseline.update(period_count=7, aggregation='arithmetic_mean_seven_unrounded_components',
                        fixed_scope='arithmetic_mean_seven_household_periods')
        a, b = sides[0], baseline
        same = (a['compatible'] and b['compatible']
                and (a['currency'], a['price_basis']) == (b['currency'], b['price_basis']))
        comparable = same and a['usage_cost'] is not None and b['usage_cost'] is not None
        delta = Decimal(a['usage_cost']) - Decimal(b['usage_cost']) if comparable else None
        percent = str(delta / Decimal(b['usage_cost']) * 100) if comparable and Decimal(b['usage_cost']) else None
        total_delta = (Decimal(a['total_cost']) - Decimal(b['total_cost'])
                       if same and a['total_cost'] is not None and b['total_cost'] is not None else None)
        return {'comparison_scope': energy.get('comparison_scope'), 'metric': 'usage_cost',
                'current': a, 'baseline': baseline, 'baselines': sides[1:],
                'status': 'ready' if comparable else 'unknown',
                'reason': None if comparable else (energy['reason'] or
                    ('currency_or_basis_mismatch' if not same else 'incomplete_cost')),
                'delta_cost': _money(delta), 'percent': percent,
                'delta_total_cost': _money(total_delta), 'rounding': deepcopy(ROUNDING)}
