"""Shared immutable calendar and scalar usage arithmetic; no household fees.

Plans retain only O(hours) calendar metadata. Usage states retain only scalar
sums/counts; callers own measurement masks and must supply validated rates.
"""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from math import fsum, isfinite
from types import MappingProxyType

from .energy_measurements import calendar_grid, _instant
from .tariff_history import HistoricalTariffRates, calculate_energy_amounts, _money, _usage_decimal


_COMPONENTS = ('energy_cost', 'variable_cost', 'legacy_combined_cost')
_COSTS = (*_COMPONENTS, 'usage_cost')


@dataclass(frozen=True, slots=True, init=False, eq=False)
class CalendarComparisonPlan:
    """Validated whole-period frames shared by household and future group models.

    Only detached UTC boundaries are retained from period descriptors. Mapping
    proxies wrap private dictionaries whose values are immutable frame tuples.
    No per-source or per-group configuration is retained.
    """
    periods: tuple
    timezone_name: str
    calendar_kind: str
    grids: tuple
    common_order: tuple
    ranks: object
    aligned: bool
    starts: tuple
    ends: tuple

    def __init__(self, current_period, baseline_period, *, timezone_name, calendar_kind):
        if calendar_kind not in ('daily', 'weekly', 'monthly', 'yearly'):
            raise ValueError('invalid_calendar_kind')
        if not isinstance(timezone_name, str):
            raise ValueError('invalid_comparison_timezone')
        periods = tuple(MappingProxyType({key: _instant(period[key]) for key in ('start', 'end')})
                        for period in (current_period, baseline_period))
        # calendar_grid validates the zone, aware hour boundaries and <=366 days.
        grids = tuple(calendar_grid(p['start'], p['end'], timezone_name, calendar_kind)
                      for p in periods)
        # Ambiguous repeated yearly dates cannot have a unique merge rank.
        if any(len(set(grid.values())) != len(grid) for grid in grids):
            raise ValueError('duplicate_comparison_calendar_frame')
        common = set(grids[0].values()) & set(grids[1].values())
        sequences = tuple(tuple(key for key in grid.values() if key in common) for grid in grids)
        for name, value in (
                ('periods', periods), ('timezone_name', timezone_name), ('calendar_kind', calendar_kind),
                ('grids', tuple(MappingProxyType(grid) for grid in grids)),
                ('common_order', sequences[0]),
                ('ranks', MappingProxyType({key: rank for rank, key in enumerate(sequences[0])})),
                ('aligned', sequences[0] == sequences[1]),
                ('starts', tuple(p['start'] for p in periods)), ('ends', tuple(p['end'] for p in periods))):
            object.__setattr__(self, name, value)

    def validate_matches(self, periods, timezone_name, calendar_kind):
        """Cheap boundary/identity check; never rebuild or copy the annual grids."""
        periods = tuple(periods)
        if (timezone_name != self.timezone_name or calendar_kind != self.calendar_kind
                or len(periods) != 2
                or tuple(_instant(p['start']) for p in periods) != self.starts
                or tuple(_instant(p['end']) for p in periods) != self.ends):
            raise ValueError('comparison_calendar_plan_mismatch')


def new_usage_cost_state(rates):
    """Start scalars using a real resolver, already validated once by its owner."""
    if type(rates) is not HistoricalTariffRates:
        raise ValueError('validated_historical_rates_required')
    return {'kwh': Decimal(0), 'amounts': dict.fromkeys(_COMPONENTS, Decimal(0)),
            'priced': dict.fromkeys(_COSTS, 0), 'seconds': 0,
            'identities': set(), 'semantics': set(), 'issues': set()}


def add_usage_cost(state, stamp, values, rates, *, _calculate=calculate_energy_amounts):
    """Accumulate one actual UTC hour on the caller's selected source mask.

    Values follow the normalized measurement contract. Preserve the existing
    source-hour float fsum before the authoritative Decimal amount calculation.
    _calculate preserves the stream's existing diagnostic patch point.
    """
    if type(rates) is not HistoricalTariffRates:
        raise ValueError('validated_historical_rates_required')
    if not values:
        return
    rate = rates.resolve_interval(stamp, stamp + 3600000)
    _add_usage_cost_rate(state, values, rate, _calculate)


@dataclass(frozen=True, slots=True, init=False, eq=False)
class UsageCostHour:
    """One validated actual UTC hour, reusable across all projected groups.

    Keep only scalar pricing fields. This is a constructor proof from a real
    validated resolver, never a caller-supplied flag or rate dictionary.
    """
    stamp: int
    _rate: object

    def __init__(self, stamp, rates):
        if type(rates) is not HistoricalTariffRates:
            raise ValueError('validated_historical_rates_required')
        if type(stamp) not in (int, float) or not isfinite(stamp) or stamp % 3600000:
            raise ValueError('invalid_usage_hour')
        rate = rates.resolve_interval(stamp, stamp + 3600000)
        keys = ('available', 'reason', 'currency', 'price_basis', 'rate_semantics',
                'energy_rate', 'variable_rate')
        object.__setattr__(self, 'stamp', int(stamp))
        object.__setattr__(self, '_rate', MappingProxyType({key: rate[key] for key in keys if key in rate}))

    def add(self, state, values):
        if values:
            _add_usage_cost_rate(state, values, self._rate, calculate_energy_amounts)

    def add_group(self, state, quantity):
        # Projected residuals are already exact Decimal quantities. Converting
        # them through source float fsum could cross a cent-rounding boundary.
        quantity = _usage_decimal(quantity)
        _add_usage_cost_quantity(state, quantity, 3600, self._rate, calculate_energy_amounts)


def _add_usage_cost_rate(state, values, rate, calculate):
    _add_usage_cost_quantity(state, fsum(values), len(values) * 3600, rate, calculate)


def _add_usage_cost_quantity(state, quantity, seconds, rate, calculate):
    state['seconds'] += seconds
    with localcontext() as context:
        context.prec = 40
        state['kwh'] += Decimal(str(quantity))
    if 'currency' in rate:
        state['identities'].add((rate['currency'], rate['price_basis']))
    if not rate['available']:
        state['issues'].add(rate['reason'])
        return
    state['semantics'].add(rate['rate_semantics'])
    try:
        amounts = calculate(quantity, rate['energy_rate'], rate['variable_rate'], rate['rate_semantics'])
    except ValueError:
        state['issues'].add('unrepresentable_usage_amount')
        return
    with localcontext() as context:
        context.prec = 40
        for key, amount in amounts.items():
            if amount is not None:
                state['priced'][key] += seconds
                if key in _COMPONENTS:
                    state['amounts'][key] += amount
            else:
                state['issues'].add('unknown_' + key)


def finish_usage_cost(state, *, eligible, identity_constraints=()):
    """Finalize selected usage alone; constraints may include household fee IDs.

    Period parts round separately, including mixed legacy/named components.
    Fixed fees, total cost and effective price belong only to the household.
    """
    with localcontext() as context:
        context.prec = 40
        identities = state['identities'] | set(identity_constraints)
        compatible = len(identities) == 1
        currency, basis = next(iter(identities)) if compatible else (None, None)
        mask = state['seconds']
        known = {key: _money(state['amounts'][key])
                 if compatible and state['priced'][key] else None for key in _COMPONENTS}
        amounts = {key: known[key] if eligible and mask and state['priced'][key] == mask else None
                   for key in _COMPONENTS}
        usage_ready = bool(eligible and compatible and mask and state['priced']['usage_cost'] == mask)
        usage = sum((Decimal(_money(value)) for value in state['amounts'].values()), Decimal(0))
        amounts['usage_cost'] = _money(usage) if usage_ready else None
        issues = set(state['issues'])
        if not compatible:
            issues.add('incomparable_currency_or_basis')
        return {**amounts, 'known_costs': known,
                'mask_source_seconds': mask, 'priced_source_seconds': dict(state['priced']),
                'pricing_complete': usage_ready, 'compatible': compatible,
                'currency': currency, 'price_basis': basis,
                'rate_semantics': sorted(state['semantics']), 'issues': sorted(issues)}
