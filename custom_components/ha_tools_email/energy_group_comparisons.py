"""Calendar deltas of complete groups attributed by one current registry snapshot.

Partial group totals cannot supply a matched-hour delta without hourly group
evidence. That case is explicit, independent of household comparison readiness.
"""
from copy import deepcopy
from decimal import Decimal, localcontext
from math import isfinite

from .tariff_history import _money


def compare_period_groups(current, baseline, household_comparison):
    same = (current.get('registry_revision') == baseline.get('registry_revision')
            and current.get('energy_domain') == baseline.get('energy_domain')
            and current.get('membership_mode') == baseline.get('membership_mode') == 'current_snapshot')
    currency = current.get('currency')
    price_basis = current.get('price_basis')
    cost_compatible = (same and current.get('energy_domain') == 'grid_import' and currency is not None
                       and currency == baseline.get('currency') and price_basis is not None
                       and price_basis == baseline.get('price_basis'))
    result = {'membership_mode': 'current_snapshot', 'membership_as_of': current.get('captured_at'),
              'registry_revision': current.get('registry_revision'),
              'energy_domain': current.get('energy_domain'), 'reconciles_household_delta': False,
              'attribution_label': 'current_registry_assignments',
              'currency': currency if cost_compatible else None,
              'price_basis': price_basis if cost_compatible else None}
    with localcontext() as context:
        context.prec = 40

        def delta(a, b):
            a, b = a or {}, b or {}
            row = {'status': 'unknown', 'reason': 'group_common_measurements_required',
                   'comparison_scope': None, 'current_kwh': a.get('kwh'), 'baseline_kwh': b.get('kwh'),
                   'delta_kwh': None, 'percent': None,
                   'coverage': {'current': deepcopy(a.get('coverage')), 'baseline': deepcopy(b.get('coverage'))},
                   'delta_energy_cost': None, 'delta_variable_cost': None,
                   'delta_legacy_combined_cost': None, 'delta_usage_cost': None, 'delta_fixed_cost': None}
            if not same:
                row['reason'] = 'group_context_changed'
                return row
            if (a.get('measurement_complete') is not True or b.get('measurement_complete') is not True
                    or a.get('kwh') is None or b.get('kwh') is None):
                return row
            actual, prior = Decimal(a['kwh']), Decimal(b['kwh'])
            value = actual-prior
            percent = float(value/prior*100) if prior else None
            row.update(status='ready', reason=None, comparison_scope='per_group_calendar_totals',
                       delta_kwh=str(value), percent=percent if percent is not None and isfinite(percent) else None)
            if cost_compatible:
                for key in ('energy_cost', 'variable_cost', 'legacy_combined_cost', 'usage_cost'):
                    if a.get(key) is not None and b.get(key) is not None:
                        row['delta_'+key] = _money(Decimal(a[key])-Decimal(b[key]))
            return row

        for level in ('devices', 'areas', 'floors'):
            left, right = current.get(level, {}), baseline.get(level, {})
            result[level] = {key: delta(left.get(key), right.get(key)) for key in sorted(set(left) | set(right))}
        for name in ('unassigned', 'unmeasured'):
            result[name] = delta(current.get(name), baseline.get(name))
        result['reconciles_household_delta'] = (same and current.get('energy_domain') == 'grid_import'
            and current.get('partition_complete') is True and baseline.get('partition_complete') is True
            and household_comparison.get('status') == 'ready'
            and household_comparison.get('comparison_scope') == 'calendar_totals'
            and all(row['status'] == 'ready' for row in
                    [*result['devices'].values(), *result['areas'].values(), result['unassigned'], result['unmeasured']]))
        return result
