"""Cent allocation for the already proven household common-measurement mask.

No new energy or price masks are inferred here. Allocate disjoint device
residuals once, then derive every alternative registry partition from them.
"""
from copy import deepcopy
from decimal import Decimal

from .energy_consumers import COMPONENTS, REMAINDER, _reconcile
from .energy_mean7_costs import _mean_states
from .tariff_history import _money


SIDES = ('current', 'baseline')


def reconcile_common_costs(result, household, identity, keys, states):
    """Install an atomic allocation only with energy AND selected-price proof."""
    result['reconciles_household_cost_delta'] = False
    costs = household.get('costs', {})
    if (not result['reconciles_household_delta']
            or household.get('comparison_scope') != 'common_measurements'
            or costs.get('comparison_scope') != 'common_measurements'
            or costs.get('status') != 'ready'
            or identity['group_membership'] is None):
        return
    house = [costs[name] for name in SIDES]
    price_identity = (house[0]['currency'], house[0]['price_basis'])
    if (None in price_identity or any(not side['compatible']
            or not side['pricing_complete'] or side['usage_cost'] is None
            or (side['currency'], side['price_basis']) != price_identity for side in house)):
        return
    grids = len(identity['grid_source_ids'])
    membership = identity['group_membership']
    selected = {}
    # Energy proof already established exact UTC mask equality, rather than
    # merely equal counts. These seconds additionally check the cost mask.
    for key, state in zip(keys, states):
        row = result[key[0]] if key[1] is None else result[key[0]][key[1]]
        if 'costs' not in row:
            return
        selected[key] = (state['common_cost'] if row['comparison_scope'] == 'common_measurements'
                         else state['own_cost'])
        if key == ('unassigned', None) and not membership['unassigned'][None]:
            continue
        for index, name in enumerate(SIDES):
            side, raw, target = row['costs'][name], selected[key][index], house[index]
            if (target['mask_source_seconds'] % grids
                    or raw['seconds'] * grids != target['mask_source_seconds']
                    or not side['compatible'] or not side['pricing_complete']
                    or (side['currency'], side['price_basis']) != price_identity
                    or raw['priced']['usage_cost'] != raw['seconds']
                    or any(raw['priced'][part] * grids != target['priced_source_seconds'][part]
                           for part in COMPONENTS)):
                return
    _allocate_costs(result, house, identity, keys, selected,
                    comparison_scope='common_measurements')


def reconcile_mean7_costs(result, household, identity, keys, states):
    """Check all eight real price masks before averaging any group state.

    Energy proof belongs to the caller. Raw states contain only scalar amounts,
    counters and identities for the already selected actual eight windows.
    """
    result['reconciles_household_cost_delta'] = False
    costs = household.get('costs', {})
    scope = household.get('comparison_scope')
    if (not result['reconciles_household_delta']
            or identity['energy_domain'] != 'grid_import'
            or identity['group_membership'] is None
            or scope not in ('common_measurements', 'calendar_totals')
            or costs.get('comparison_scope') != scope
            or costs.get('status') != 'ready'
            or len(costs.get('baselines', ())) != 7):
        return
    house = [costs[name] for name in SIDES]
    actual = [house[0], *costs['baselines']]
    price_identity = (house[0]['currency'], house[0]['price_basis'])
    if (None in price_identity or any(not side['compatible']
            or not side['pricing_complete'] or side['usage_cost'] is None
            or (side['currency'], side['price_basis']) != price_identity
            for side in (*actual, house[1]))):
        return
    grids = len(identity['grid_source_ids'])
    membership = identity['group_membership']
    for key in keys:
        row = result[key[0]] if key[1] is None else result[key[0]][key[1]]
        raw_states = states.get(key)
        if 'costs' not in row or raw_states is None or len(raw_states) != 8:
            return
        structural_zero = key == ('unassigned', None) and not membership['unassigned'][None]
        if not structural_zero:
            for raw, target in zip(raw_states, actual):
                # Compare integer actual-window counters. Mean seconds may be
                # fractional (DST), and cannot prove any one of these masks.
                if (target['mask_source_seconds'] % grids
                        or raw['seconds'] * grids != target['mask_source_seconds']
                        or raw['identities'] != {price_identity}
                        or raw['priced']['usage_cost'] != raw['seconds']
                        or any(raw['priced'][part] * grids != target['priced_source_seconds'][part]
                               for part in (*COMPONENTS, 'usage_cost'))):
                    return
    # Only after all real windows have passed, average raw component amounts.
    selected = {key: (states[key][0], _mean_states(states[key][1:])) for key in keys}
    _allocate_costs(result, house, identity, keys, selected,
                    comparison_scope=('per_group_calendar_totals' if scope == 'calendar_totals' else scope),
                    fractional_seconds=True)


def _allocate_costs(result, house, identity, keys, selected, *, comparison_scope,
                    fractional_seconds=False):
    """One disjoint cent allocation, installed only after every side succeeds."""
    grids = len(identity['grid_source_ids'])
    membership = identity['group_membership']
    price_identity = (house[0]['currency'], house[0]['price_basis'])
    staged = deepcopy(result)
    disjoint = {sid: ('devices', sid) for sid in membership['devices']}
    disjoint[REMAINDER] = ('unmeasured', None)
    for index, name in enumerate(SIDES):
        target = house[index]
        allocated = {}
        for part in COMPONENTS:
            amount = target['known_costs'].get(part)
            if amount is None:
                continue
            values = {sid: selected[key][index]['amounts'][part] for sid, key in disjoint.items()}
            # Authoritative ConsumerPeriod allocator handles negative amounts
            # and deterministic source-ID ties, using household rounded parts.
            allocated[part] = _reconcile(values, Decimal(amount))
        if not allocated:
            return
        for key in keys:
            level, group = key
            row = staged[level] if group is None else staged[level][group]
            side = row['costs'][name]
            if level == 'unmeasured':
                members = (REMAINDER,)
            else:
                members = membership[level][group]
            structural_zero = level == 'unassigned' and not members
            if structural_zero:
                # No measurements exist for this empty partition. Borrow price
                # knowledge only from the same household mask, never calendar
                # hours outside it or a made-up zero for an unknown component.
                for field in ('compatible', 'currency', 'price_basis', 'rate_semantics', 'issues', 'pricing_complete'):
                    side[field] = deepcopy(target[field])
                side['mask_source_seconds'] = (target['mask_source_seconds'] / grids if fractional_seconds
                    else target['mask_source_seconds'] // grids)
                side['priced_source_seconds'] = {part: (seconds / grids if fractional_seconds else seconds // grids)
                    for part, seconds in target['priced_source_seconds'].items()}
            for part in COMPONENTS:
                value = (_money(sum((Decimal(allocated[part][sid]) for sid in members), Decimal(0)))
                         if part in allocated else None)
                side['known_costs'][part] = value
                # Mixed legacy/components pricing has known parts, but no full
                # named component: retain that distinction on every hierarchy.
                if structural_zero:
                    side[part] = value if target[part] is not None else None
                elif side[part] is not None:
                    side[part] = value
            side['usage_cost'] = _money(sum((Decimal(side['known_costs'][part])
                for part in allocated), Decimal(0)))
    for key in keys:
        level, group = key
        row = staged[level] if group is None else staged[level][group]
        a, b = (row['costs'][name] for name in SIDES)
        for part in (*COMPONENTS, 'usage_cost'):
            row['delta_'+part] = (_money(Decimal(a[part])-Decimal(b[part]))
                                 if a[part] is not None and b[part] is not None else None)
        row['delta_fixed_cost'] = None
        row['costs'].update(status='ready', reason=None, comparison_scope=comparison_scope,
                            delta_cost=row['delta_usage_cost'], delta_fixed_cost=None)
    staged['currency'], staged['price_basis'] = price_identity
    staged['reconciles_household_cost_delta'] = True
    result.clear()
    result.update(staged)
