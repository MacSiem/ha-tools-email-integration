"""Bounded daily group means from one eight-window calendar intersection."""
from decimal import Decimal, localcontext
from math import isfinite

from .energy_group_stream import group_projection_evidence
from .energy_measurements import calendar_grid
from .energy_comparison_core import UsageCostHour, new_usage_cost_state, finish_usage_cost
from .energy_mean7_costs import _mean_states
from .tariff_history import HistoricalTariffRates, _money

LEVELS = ('devices', 'areas', 'floors')
COSTS = ('energy_cost', 'variable_cost', 'legacy_combined_cost', 'usage_cost')


def _average(values):
    return sum(values, Decimal(0))/7


def _costs(states, row):
    allowed = row['status'] == 'ready'
    a = finish_usage_cost(states[0], eligible=allowed)
    b = finish_usage_cost(_mean_states(states[1:]), eligible=allowed)
    b.update(period_count=7, aggregation='arithmetic_mean_seven_unrounded_components')
    same = a['compatible'] and b['compatible'] and (a['currency'], a['price_basis']) == (b['currency'], b['price_basis'])
    for key in COSTS:
        row['delta_'+key] = _money(Decimal(a[key])-Decimal(b[key])) if same and a[key] is not None and b[key] is not None else None
    ready = row['delta_usage_cost'] is not None
    return {'current': a, 'baseline': b, 'comparison_scope': row['comparison_scope'],
            'status': 'ready' if ready else 'unknown', 'delta_cost': row['delta_usage_cost'],
            'reason': None if ready else (row['reason'] or ('incomplete_cost' if same else 'currency_or_basis_mismatch')),
            'delta_fixed_cost': None}


def compare_daily_group_mean7(projections, periods, *, timezone_name, tariff=None, household_comparison=None):
    if not isinstance(projections, list) or not isinstance(periods, list) or len(projections) != 8 or len(periods) != 8:
        raise ValueError('eight_group_windows_required')
    grids = [calendar_grid(p['start'], p['end'], timezone_name, 'daily') for p in periods]
    if any(len(grid) > 48 for grid in grids):
        raise ValueError('daily_comparison_window_too_long')
    identity, keys, rows = None, None, []
    for projection, period, grid in zip(projections, periods, grids):
        identity, keys, normalized, _ = group_projection_evidence(projection, period['start'], period['end'],
            previous_identity=identity, previous_keys=keys)
        if len(set(grid.values())) != len(grid):
            raise ValueError('duplicate_comparison_calendar_frame')
        rows.append({grid[record[0]]: record for record in normalized})
    rates = HistoricalTariffRates(tariff) if tariff is not None and identity['energy_domain'] == 'grid_import' else None
    prices = [{frame: UsageCostHour(record[0], rates) for frame, record in window.items()}
              for window in rows] if rates else None
    expected = [len(grid) for grid in grids]
    calendar_common = set.intersection(*(set(window) for window in rows))
    grid_ids = set(identity['grid_source_ids'])
    house_common, house_partition = set(), True
    for frame in calendar_common:
        presence = set.intersection(*(set(window[frame][2]) for window in rows))
        if presence == grid_ids:
            house_common.add(frame)
            house_partition = house_partition and all(window[frame][3] for window in rows)
        elif presence:
            house_partition = False
    household = household_comparison or {}
    common_proof = bool(identity['energy_domain'] == 'grid_import' and identity['group_membership'] is not None
        and house_common and house_partition and household.get('status') == 'ready'
        and household.get('comparison_scope') == 'common_measurements')
    full_proof = bool(identity['energy_domain'] == 'grid_import' and household.get('status') == 'ready'
        and household.get('comparison_scope') == 'calendar_totals'
        and all(record[3] for window in rows for record in window.values()))
    result = {'membership_mode': 'current_snapshot', 'membership_as_of': None,
              'registry_revision': identity['registry_revision'], 'energy_domain': identity['energy_domain'],
              'attribution_label': 'current_registry_assignments', 'currency': None, 'price_basis': None,
              'reconciles_household_delta': False, 'reconciles_household_cost_delta': False,
              **{level: {} for level in LEVELS}}
    identities = set()
    with localcontext() as context:
        context.prec = 40
        for position, key in enumerate(keys):
            known = [{frame for frame, record in window.items() if record[1][position] is not None} for window in rows]
            counts = [len(frames) for frames in known]
            sums = [sum((window[frame][1][position] for frame in frames), Decimal(0))
                    for window, frames in zip(rows, known)]
            common = set.intersection(*known)
            own_ratio = [count/total for count,total in zip(counts,expected)]
            common_ratio = [len(common)/total for total in expected]
            complete = counts == expected
            ready = complete or all(ratio >= .9 for ratio in (*own_ratio,*common_ratio))
            selected = known if complete else [common]*8
            amounts = [sum((window[frame][1][position] for frame in frames), Decimal(0))
                       for window,frames in zip(rows,selected)]
            actual, baseline = amounts[0], _average(amounts[1:])
            delta = actual-baseline
            percent = float(delta/baseline*100) if ready and baseline else None
            row = {'status': 'ready' if ready else 'unknown', 'reason': None if ready else 'incomplete_statistics',
                'comparison_scope': ('per_group_calendar_totals' if complete else 'common_measurements') if ready else None,
                'current_kwh': str(actual) if ready else None, 'baseline_kwh': str(baseline) if ready else None,
                'delta_kwh': str(delta) if ready else None, 'percent': percent if percent is not None and isfinite(percent) else None,
                'own_current_kwh': str(sums[0]), 'own_baseline_kwh': str(_average(sums[1:])),
                'full_current_kwh': str(sums[0]) if counts[0] == expected[0] else None,
                'full_baseline_kwh': str(_average(sums[1:])) if counts[1:] == expected[1:] else None,
                'coverage': {'current': own_ratio[0], 'baselines': own_ratio[1:],
                    'common_current': common_ratio[0], 'common_baselines': common_ratio[1:],
                    'expected_current_seconds': expected[0]*3600,
                    'expected_baseline_seconds': [n*3600 for n in expected[1:]],
                    'measured_current_seconds': counts[0]*3600,
                    'measured_baseline_seconds': [n*3600 for n in counts[1:]],
                    'common_group_seconds': len(common)*3600},
                **{'delta_'+name: None for name in (*COSTS,'fixed_cost')}}
            structural_zero = (key == ('unassigned',None) and identity['group_membership'] is not None
                               and not identity['group_membership']['unassigned'][None])
            common_proof = common_proof and ready and (structural_zero or
                common == house_common and (not complete or counts == [len(common)]*8))
            full_proof = full_proof and complete
            if rates:
                states = [new_usage_cost_state(rates) for _ in rows]
                for state,window,hour_prices,frames in zip(states,rows,prices,selected):
                    for frame in frames:
                        hour_prices[frame].add_group(state,window[frame][1][position])
                row['costs'] = _costs(states,row)
                identities.update((side['currency'],side['price_basis']) for side in
                    (row['costs']['current'],row['costs']['baseline']) if side['compatible'])
            level, group = key
            if group is None: result[level] = row
            else: result[level][group] = row
    if len(identities) == 1:
        result['currency'],result['price_basis'] = next(iter(identities))
    result['reconciles_household_delta'] = bool(common_proof or full_proof)
    return result
