"""Shared calendar bins from validated household hours and existing bill rows.

Only the accumulator calls this helper. Measurement grouping uses the captured
HA timezone; browser/user locale and display timezone belong to presentation.
Fixed fees stay on the household period and are never repeated per chart bin.
"""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .energy_consumers import COMPONENTS, _reconcile


def build_chart_bins(rows, period, active):
    grain = {'day': 'hour', 'week': 'day', 'month': 'day', 'year': 'month'}.get(period.get('kind'))
    chart = {'status': 'unknown', 'reason': None, 'timezone': period.get('timezone'),
             'grain': grain, 'fixed_fee_scope': 'household_period_only',
             'coverage_kind': 'complete_household_hours', 'bins': []}
    if grain is None:
        return {**chart, 'reason': 'calendar_kind_unavailable'}
    try:
        zone = ZoneInfo(period['timezone'])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return {**chart, 'reason': 'measurement_timezone_unavailable'}
    groups = {}
    priced = active['rows'] if active is not None else []
    compatible = active is not None and active['compatible']
    with localcontext() as context:
        context.prec = 40
        for index, hour in enumerate(rows):
            local = datetime.fromtimestamp(hour['from']/1000, zone)
            key = hour['from'] if grain == 'hour' else local.date().isoformat() if grain == 'day' else local.strftime('%Y-%m')
            if key not in groups:
                groups[key] = {'from': hour['from'], 'to': hour['to'], 'rows': 0, 'measured': 0,
                    'energy': Decimal(0), 'expected_seconds': 0, 'measured_seconds': 0,
                    'amounts': dict.fromkeys(COMPONENTS, Decimal(0)),
                    'known': dict.fromkeys(COMPONENTS, 0), 'usage_known': 0}
            state = groups[key]
            seconds = (hour['to']-hour['from'])/1000
            state['to'] = hour['to']; state['rows'] += 1
            state['expected_seconds'] += seconds
            if hour['kwh'] is not None:
                state['energy'] += Decimal(str(hour['kwh']))
                state['measured'] += 1; state['measured_seconds'] += seconds
            price = priced[index] if index < len(priced) else {}
            amounts = price.get('costs', {}) if price.get('available') and compatible else {}
            for component in COMPONENTS:
                value = amounts.get(component)
                if value is not None:
                    state['amounts'][component] += Decimal(value)
                    state['known'][component] += 1
            if ((amounts.get('energy_cost') is not None and amounts.get('variable_cost') is not None)
                    or amounts.get('legacy_combined_cost') is not None):
                state['usage_known'] += 1
        # Same cents as the period's known component subtotals, with stable
        # chronological ties. Per-hour rounding must not change the bill.
        allocated = {}
        for component in COMPONENTS:
            target = active.get('known_costs', {}).get(component) if compatible else None
            allocated[component] = (_reconcile({key: state['amounts'][component]
                for key, state in groups.items()}, Decimal(target)) if target is not None else {})
        bins = []
        for key, state in groups.items():
            complete = state['measured'] == state['rows']
            start = datetime.fromtimestamp(state['from']/1000, zone)
            end = datetime.fromtimestamp(state['to']/1000, zone)
            costs = {component: (allocated[component].get(key)
                if complete and state['known'][component] == state['rows'] else None) for component in COMPONENTS}
            known = {component: (allocated[component].get(key)
                if state['known'][component] else None) for component in COMPONENTS}
            usage = (format(sum((Decimal(value) for value in known.values() if value is not None), Decimal(0)), '.2f')
                     if complete and state['usage_known'] == state['rows'] else None)
            bins.append({'from': state['from'], 'to': state['to'],
                'start': start.astimezone(timezone.utc).isoformat(), 'end': end.astimezone(timezone.utc).isoformat(),
                'local_start': start.isoformat(), 'local_end': end.isoformat(), 'fold': start.fold,
                'kwh': str(state['energy']) if complete else None,
                'recorded_kwh': str(state['energy']) if state['measured'] else None,
                'complete': complete, 'expected_seconds': state['expected_seconds'],
                'measured_seconds': state['measured_seconds'], **costs, 'known_costs': known,
                'usage_cost': usage, 'fixed_cost': None, 'total_cost': None,
                'currency': active['currency'] if compatible else None,
                'price_basis': active['price_basis'] if compatible else None})
        return {**chart, 'status': 'ready', 'reason': None, 'bins': bins}
