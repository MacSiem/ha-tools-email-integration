"""Bounded tariff-zone aggregates from the active bill's existing priced rows.

No rate lookup, source IO, subhour allocation or household fees belong here.
Each component is reconciled to the already published calendar-bin cents.
"""
from decimal import Decimal

from .energy_consumers import COMPONENTS, _reconcile


def _state():
    return {'rows': 0, 'measured': 0, 'energy': Decimal(0),
            'measured_seconds': 0, 'missing_seconds': 0,
            'amounts': dict.fromkeys(COMPONENTS, Decimal(0)),
            'known': dict.fromkeys(COMPONENTS, 0), 'usage_known': 0}


def zone_labels(active):
    """Build one shared catalog per chart, using only public label fields."""
    labels = {}
    if active is not None:
        for rate in active['rates']:
            for zone in rate['profile']['zones']:
                label = zone.get('label')
                labels[(rate['version_id'], zone['id'])] = (
                    label if isinstance(label, str) else zone['id'])
    return labels


class ZoneBreakdown:
    """One bin of scalar sums; never retain source cells or individual hours."""

    def __init__(self, active, labels):
        self._active = active
        self._compatible = active is not None and active['compatible']
        self._labels = labels
        self._zones = {}
        self._unattributed = _state()
        self._reasons = set()

    def add(self, hour, price, index):
        seconds = (hour['to'] - hour['from']) / 1000
        quantity = hour['kwh']
        version_id, zone_id = price.get('version_id'), price.get('zone')
        key = (version_id, zone_id)
        reason = None
        if quantity is None:
            reason = 'missing_measurement'
        elif self._active is None:
            reason = 'tariff_unavailable'
        elif not price.get('available'):
            reason = price.get('reason') or 'price_unavailable'
        elif (price.get('index') != index or price.get('from') != hour['from']
              or price.get('to') != hour['to']):
            reason = 'priced_row_mismatch'
        elif price.get('zoneAttribution') == 'multiple':
            reason = 'multiple_zones_in_measurement'
        elif (price.get('zoneAttribution') != 'single'
              or not isinstance(zone_id, str) or price.get('zones') != [zone_id]
              or key not in self._labels):
            reason = 'zone_attribution_unavailable'
        if reason is None:
            state = self._zones.setdefault(key, _state())
        else:
            state = self._unattributed
            self._reasons.add(reason)
        state['rows'] += 1
        if quantity is None:
            state['missing_seconds'] += seconds
        else:
            state['energy'] += Decimal(str(quantity))
            state['measured'] += 1
            state['measured_seconds'] += seconds
        # Unknown attribution retains the entire known amount in one bucket.
        # Currency/basis incompatibility forbids every monetary aggregation.
        amounts = price.get('costs', {}) if price.get('available') and self._compatible else {}
        for component in COMPONENTS:
            value = amounts.get(component)
            if value is not None:
                state['amounts'][component] += Decimal(value)
                state['known'][component] += 1
        semantics = price.get('rate_semantics')
        if ((semantics == 'components' and amounts.get('energy_cost') is not None
             and amounts.get('variable_cost') is not None)
                or (semantics == 'legacy_combined' and amounts.get('legacy_combined_cost') is not None)):
            state['usage_known'] += 1

    def finish(self, bin_row):
        # Numeric prefix keeps the unknown bucket comparable to zone tuple keys.
        # All zone tuple fields are validated identifiers, never user labels.
        states = {(0, *key): state for key, state in self._zones.items()}
        unknown_key = (1, '', '')
        states[unknown_key] = self._unattributed
        allocations = {}
        targets = bin_row['known_costs']
        for component in COMPONENTS:
            target = targets.get(component) if self._compatible else None
            values = {key: state['amounts'][component] for key, state in states.items()
                      if state['known'][component]}
            allocations[component] = (_reconcile(values, Decimal(target))
                                      if target is not None and values else {})
        exact = bin_row['complete'] and not self._reasons

        def known_costs(key):
            return {component: allocations[component].get(key) for component in COMPONENTS}

        zones = []
        for key in sorted(self._zones):
            state = self._zones[key]
            known = known_costs((0, *key))
            costs = {component: known[component]
                     if exact and state['known'][component] == state['rows'] else None
                     for component in COMPONENTS}
            usage = (format(sum((Decimal(value) for value in known.values() if value is not None),
                                Decimal(0)), '.2f')
                     if exact and state['usage_known'] == state['rows'] else None)
            zones.append({'tariff_id': self._active['tariff_id'],
                'version_id': key[0], 'zone_id': key[1], 'label': self._labels[key],
                'kwh': str(state['energy']) if exact else None,
                'recorded_kwh': str(state['energy']), 'complete': exact,
                'measured_seconds': state['measured_seconds'],
                **costs, 'known_costs': known, 'usage_cost': usage})
        unknown = self._unattributed
        unattributed = {'recorded_kwh': str(unknown['energy']) if unknown['measured'] else None,
                       'measured_seconds': unknown['measured_seconds'],
                       'missing_seconds': unknown['missing_seconds'],
                       'known_costs': known_costs(unknown_key), 'reasons': sorted(self._reasons)}
        if self._active is None:
            status, reason = 'unknown', 'tariff_unavailable'
        elif not self._compatible:
            status, reason = 'partial', 'currency_or_basis_mismatch'
        elif exact:
            status, reason = 'ready', None
        else:
            status, reason = 'partial', sorted(self._reasons)[0]
        observed = [state['energy'] for state in states.values() if state['measured']]
        recorded = bin_row['recorded_kwh']
        reconciles_energy = ((recorded is None and not observed)
                             or (recorded is not None and observed
                                 and sum(observed, Decimal(0)) == Decimal(recorded)))
        has_target = any(value is not None for value in targets.values())
        reconciles_money = self._compatible and has_target and all(
            target is None or sum((Decimal(value) for value in allocations[component].values()),
                                  Decimal(0)) == Decimal(target)
            for component, target in targets.items())
        return {'status': status, 'reason': reason, 'scope': 'complete_household_hours',
                'fixed_fee_scope': 'household_period_only',
                'currency': self._active['currency'] if self._compatible else None,
                'price_basis': self._active['price_basis'] if self._compatible else None,
                'zones': zones, 'unattributed': unattributed,
                'reconciles_recorded_kwh': bool(reconciles_energy),
                'reconciles_known_costs': bool(reconciles_money)}
