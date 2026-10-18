"""Bounded consumer period aggregates; no Recorder IO or fixed-fee allocation.

Keep disjoint measured residuals, not an entire source-by-year matrix. A caller
must establish the energy domain and authorize every source before using this.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR, localcontext

from .energy_measurements import measurement_evidence
from .energy_registry import build_interval_breakdown
from .tariff_history import HistoricalTariffRates, calculate_energy_amounts, _bounded, _money

COMPONENTS = ('energy_cost', 'variable_cost', 'legacy_combined_cost')
COSTS = (*COMPONENTS, 'usage_cost')
REMAINDER = '__unmeasured__'


def _state():
    return {'kwh': Decimal(0), 'observations': 0, 'measured_kwh': Decimal(0), 'measured_observations': 0,
            'amounts': dict.fromkeys(COSTS, Decimal(0)), 'priced': dict.fromkeys(COSTS, 0),
            'usage_parts': dict.fromkeys(COMPONENTS, Decimal(0))}


def _text(value):
    return format(value, 'f')


def _reconcile(values, target):
    """Largest remainder in cents, including negative prices; stable ties by ID."""
    floors = {key: value.quantize(Decimal('.01'), rounding=ROUND_FLOOR) for key, value in values.items()}
    extra = int((target - sum(floors.values(), Decimal(0))) / Decimal('.01'))
    if not 0 <= extra <= len(values):
        raise ValueError('consumer_cost_partition_does_not_reconcile')
    order = sorted(values, key=lambda key: (-(values[key] - floors[key]), key))
    for key in order[:extra]:
        floors[key] += Decimal('.01')
    return {key: _money(value) for key, value in floors.items()}


class ConsumerPeriod:
    def __init__(self, consumers, membership, domain, book, hours):
        _bounded({'consumers': consumers, 'membership': membership}, 262144)
        if domain not in ('grid_import', 'site_consumption') or not isinstance(membership, dict):
            raise ValueError('invalid_consumer_configuration')
        if not isinstance(consumers, list):
            raise ValueError('invalid_consumer_configuration')
        # Validate the complete hierarchy once, even when no measurements exist.
        probe = build_interval_breakdown(0, [{**row, 'domain': domain, 'kwh': 0}
                                            for row in consumers if isinstance(row, dict)], membership, domain=domain)
        if len(consumers) != probe['configured_consumers']:
            raise ValueError('invalid_consumer_configuration')
        self._config = [{key: row.get(key) for key in ('stat_consumption', 'included_in_stat')}
                        for row in consumers]
        self._ids = [row['stat_consumption'] for row in self._config]
        if REMAINDER in self._ids:
            raise ValueError('invalid_consumer_configuration')
        self._membership = {'revision': membership.get('revision'), 'captured_at': membership.get('captured_at'),
                            'membership_mode': 'current_snapshot', 'floor_supported': membership.get('floor_supported') is True,
                            'sources': {}}
        for sid in self._ids:
            raw = membership.get('sources', {}).get(sid, {})
            self._membership['sources'][sid] = {
                key: value if isinstance(value, str) and len(value) <= 256 else None
                for key in ('name', 'area_id', 'area_name', 'floor_id', 'floor_name')
                for value in [raw.get(key)]}
        for row in consumers:
            name = row.get('name')
            if isinstance(name, str) and name.strip() and len(name) <= 256:
                self._membership['sources'][row['stat_consumption']]['name'] = name
        self._domain, self._hours = domain, hours
        self._states = {sid: _state() for sid in [*self._ids, REMAINDER]}
        self._provenance, self._issues, self._partition = None, set(), True
        tariff = next((t for t in book['tariffs'] if t['id'] == book.get('active_tariff_id')), None) if book else None
        self._rates = HistoricalTariffRates(tariff) if tariff else None

    def prepare_chunk(self, snapshot, household_rows, start, end):
        """Stage changes; caller commits only after the whole chunk is accepted."""
        evidence = measurement_evidence(snapshot, start, end) if self._ids else None
        if evidence is not None and (set(evidence['source_ids']) != set(self._ids)
                or self._provenance is not None and evidence['measurement_provenance'] != self._provenance):
            raise ValueError('consumer_sources_changed')
        states, issues, partition = deepcopy(self._states), set(self._issues), self._partition
        with localcontext() as context:
            context.prec = 40
            for hour in household_rows:
                stamp = hour['from']
                consumers = [{**row, 'domain': self._domain,
                              'kwh': evidence['per_source'][row['stat_consumption']].get(stamp)} for row in self._config]
                breakdown = build_interval_breakdown(hour['kwh'], consumers, self._membership, domain=self._domain)
                partition = partition and breakdown['status'] == 'ready'
                issues.update(issue['reason'] for issue in breakdown['issues'])
                rate = self._rates.resolve_interval(stamp, hour['to']) if self._rates and self._domain == 'grid_import' else None
                for sid, cell in {**breakdown['devices'], REMAINDER: breakdown['unmeasured']}.items():
                    state = states[sid]
                    value, measured = cell['kwh_decimal'], cell.get('measured_kwh_decimal')
                    if measured is not None:
                        state['measured_kwh'] += Decimal(measured); state['measured_observations'] += 1
                    if value is None:
                        continue
                    state['kwh'] += Decimal(value); state['observations'] += 1
                    if rate is not None and rate['available']:
                        amounts = calculate_energy_amounts(value, rate['energy_rate'], rate['variable_rate'], rate['rate_semantics'])
                        if amounts['usage_cost'] is not None:
                            for key in COMPONENTS:
                                if amounts[key] is not None:
                                    state['usage_parts'][key] += amounts[key]
                        for key, amount in amounts.items():
                            if amount is not None:
                                state['amounts'][key] += amount; state['priced'][key] += 1
                    elif rate is not None:
                        issues.add(rate['reason'])
        return states, issues, partition, deepcopy(evidence['measurement_provenance']) if evidence else None

    def commit_chunk(self, staged):
        self._states, self._issues, self._partition, self._provenance = staged

    def finish(self, active):
        with localcontext() as context:
            context.prec = 40
            return self._finish(active)

    def _finish(self, active):
        compatible = active is not None and active['compatible'] and self._domain == 'grid_import'
        output = {}
        for sid, state in self._states.items():
            full = state['observations'] == self._hours
            row = {'kwh': _text(state['kwh']) if full else None,
                   'recorded_kwh': _text(state['kwh']) if state['observations'] else None,
                   'measurement_complete': full,
                   'coverage': {'observed_hours': state['observations'], 'expected_hours': self._hours},
                   'known_costs': {}}
            for key in COSTS:
                row[key] = _money(state['amounts'][key]) if compatible and state['priced'][key] == self._hours else None
                row['known_costs'][key] = _money(state['amounts'][key]) if compatible and state['priced'][key] else None
            if compatible and state['priced']['usage_cost']:
                usage = _money(sum((Decimal(_money(value)) for value in state['usage_parts'].values()), Decimal(0)))
                row['known_costs']['usage_cost'] = usage
                if state['priced']['usage_cost'] == self._hours:
                    row['usage_cost'] = usage
            output[sid] = row
        reconciled = False
        if compatible and self._partition and active['pricing_complete']:
            allocated = {}
            for key in COMPONENTS:
                target = active['known_costs'].get(key)
                if target is not None and any(state['priced'][key] for state in self._states.values()):
                    values = {sid: state['amounts'][key] for sid, state in self._states.items()}
                    adjusted = _reconcile(values, Decimal(target))
                    allocated[key] = adjusted
                    for sid, value in adjusted.items():
                        if output[sid][key] is not None:
                            output[sid][key] = value
                        if output[sid]['known_costs'][key] is not None:
                            output[sid]['known_costs'][key] = value
                    reconciled = True
            if active.get('usage_cost') is not None and all(row['usage_cost'] is not None for row in output.values()):
                # The household bill sums rounded components, including their
                # known portions across legacy/new versions. Reconcile those
                # portions first; rounding the combined usage a second time
                # would produce a different bill at a version boundary.
                for sid in output:
                    value = _money(sum((Decimal(values[sid]) for values in allocated.values()), Decimal(0)))
                    output[sid]['usage_cost'] = output[sid]['known_costs']['usage_cost'] = value
                reconciled = True
        areas, floors, unassigned = {}, {}, []
        devices = {}
        parents = {row['stat_consumption']: row['included_in_stat'] for row in self._config}
        for sid in self._ids:
            metadata = self._membership['sources'][sid]
            row = {**output[sid], **metadata, 'parent_id': parents[sid],
                   'attribution': 'parent_residual' if sid in parents.values() else 'measured',
                   'measured_kwh': _text(self._states[sid]['measured_kwh'])
                       if self._states[sid]['measured_observations'] == self._hours else None}
            devices[sid] = row
            if metadata['area_id'] is None:
                unassigned.append(sid)
            else:
                areas.setdefault(metadata['area_id'], []).append(sid)
            floors.setdefault(metadata['floor_id'] or '__no_floor__', []).append(sid)

        def aggregate(ids):
            rows = [output[sid] for sid in ids]
            full = all(row['measurement_complete'] for row in rows)
            observed = sum(self._states[sid]['observations'] for sid in ids)
            total = sum((self._states[sid]['kwh'] for sid in ids), Decimal(0))
            row = {'kwh': _text(total) if full else None, 'recorded_kwh': _text(total) if observed or not ids else None,
                   'measurement_complete': full, 'known_costs': {},
                   'coverage': {'observed_hours': observed, 'expected_hours': len(ids) * self._hours}}
            for key in COSTS:
                row[key] = _money(sum((Decimal(r[key]) for r in rows), Decimal(0))) if all(r[key] is not None for r in rows) and (compatible or ids) else None
                known = [Decimal(r['known_costs'][key]) for r in rows if r['known_costs'][key] is not None]
                row['known_costs'][key] = _money(sum(known, Decimal(0))) if known or not ids and compatible else None
            return row

        return {**{key: value for key, value in self._membership.items() if key != 'sources'},
                'registry_revision': self._membership['revision'],
                'energy_domain': self._domain, 'partition_complete': self._partition,
                'devices': devices, 'areas': {key: aggregate(ids) for key, ids in areas.items()},
                'floors': {key: aggregate(ids) for key, ids in floors.items()},
                'unassigned': aggregate(unassigned), 'unmeasured': output[REMAINDER],
                'currency': active.get('currency') if compatible else None,
                'price_basis': active.get('price_basis') if compatible else None,
                'issues': sorted(self._issues), 'rounding': {'quantum': '0.01',
                    'mode': 'partition_components_largest_remainder' if reconciled else 'period_components_then_group_sum'}}
