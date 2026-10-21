"""Bounded consumer period aggregates; no Recorder IO or fixed-fee allocation.

Keep disjoint measured residuals, not an entire source-by-year matrix. A caller
must establish the energy domain and authorize every source before using this.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR, localcontext

from .energy_measurements import measurement_evidence
from .energy_registry import build_interval_breakdown
from .energy_statistics_hours import group_cost_hour, require_statistics_projection, MAX_STATISTICS_HOURS
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
    def __init__(self, consumers, membership, domain, book, hours, *, include_statistics_hours=False, include_report_hours=False):
        require_statistics_projection(include_statistics_hours, hours)
        if type(include_report_hours) is not bool or (include_report_hours and
                (not 0 < hours <= 8784 or len(consumers) * hours > 262144)):
            raise ValueError('invalid_report_projection_size')
        self._report_hours = [] if include_report_hours else None
        self._statistics_hours = [] if include_statistics_hours else None
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
        # Derive once from the same detached registry snapshot as the values.
        # Each source appears once per alternative partition: O(consumers).
        self._group_membership = {'devices': {}, 'areas': {}, 'floors': {}, 'unassigned': []}
        for sid in self._ids:
            assignment = self._membership['sources'][sid]
            self._group_membership['devices'][sid] = [sid]
            area, floor = assignment['area_id'], assignment['floor_id']
            if area is None:
                self._group_membership['unassigned'].append(sid)
            else:
                self._group_membership['areas'].setdefault(area, []).append(sid)
            self._group_membership['floors'].setdefault(floor or '__no_floor__', []).append(sid)
        self._domain, self._hours = domain, hours
        self._states = {sid: _state() for sid in [*self._ids, REMAINDER]}
        self._provenance, self._issues, self._partition = None, set(), True
        tariff = next((t for t in book['tariffs'] if t['id'] == book.get('active_tariff_id')), None) if book else None
        self._rates = HistoricalTariffRates(tariff) if tariff else None

    def prepare_chunk(self, snapshot, household_rows, start, end):
        """Stage changes; caller commits only after the whole chunk is accepted."""
        return self._prepare_chunk(snapshot, household_rows, start, end, include_hours=False)[0]

    def prepare_chunk_with_projection(self, snapshot, household_rows, start, end):
        """Return staged state plus detached chunk-only disjoint hourly values.

        This internal projection is computed from validated evidence, never a
        client claim of attribution. The caller owns its bounded lifetime.
        """
        return self._prepare_chunk(snapshot, household_rows, start, end, include_hours=True)

    def _prepare_chunk(self, snapshot, household_rows, start, end, *, include_hours):
        if (self._statistics_hours is not None
                and len(self._statistics_hours) + len(household_rows) > min(self._hours, MAX_STATISTICS_HOURS)):
            raise ValueError('statistics_projection_too_large')
        evidence = measurement_evidence(snapshot, start, end) if self._ids else None
        if evidence is not None and (set(evidence['source_ids']) != set(self._ids)
                or self._provenance is not None and evidence['measurement_provenance'] != self._provenance):
            raise ValueError('consumer_sources_changed')
        states, issues, partition = deepcopy(self._states), set(self._issues), self._partition
        projected = [] if include_hours else None
        statistics = [] if self._statistics_hours is not None else None
        report = [] if self._report_hours is not None else None
        report_members = {sid: [] for sid in self._ids} if report is not None else {}
        parents = {row['stat_consumption']: row['included_in_stat'] for row in self._config}
        if report is not None:
            for candidate in self._ids:
                ancestor = candidate
                while ancestor is not None:
                    report_members[ancestor].append(candidate)
                    ancestor = parents[ancestor]
        with localcontext() as context:
            context.prec = 40
            for hour in household_rows:
                stamp = hour['from']
                consumers = [{**row, 'domain': self._domain,
                              'kwh': evidence['per_source'][row['stat_consumption']].get(stamp)} for row in self._config]
                breakdown = build_interval_breakdown(hour['kwh'], consumers, self._membership, domain=self._domain)
                if include_hours:
                    projected.append({'from': stamp, 'to': hour['to'], 'household_kwh': hour['kwh'],
                        'partition_complete': self._domain == 'grid_import' and breakdown['status'] == 'ready',
                        **{level: {key: cell['kwh_decimal'] for key, cell in breakdown[level].items()}
                           for level in ('devices', 'areas', 'floors')},
                        'unassigned': breakdown['unassigned']['kwh_decimal'],
                        'unmeasured': breakdown['unmeasured']['kwh_decimal']})
                partition = partition and breakdown['status'] == 'ready'
                issues.update(issue['reason'] for issue in breakdown['issues'])
                rate = self._rates.resolve_interval(stamp, hour['to']) if self._rates and self._domain == 'grid_import' else None
                hour_amounts = {} if statistics is not None or report is not None else None
                for sid, cell in {**breakdown['devices'], REMAINDER: breakdown['unmeasured']}.items():
                    state = states[sid]
                    value, measured = cell['kwh_decimal'], cell.get('measured_kwh_decimal')
                    if measured is not None:
                        state['measured_kwh'] += Decimal(measured); state['measured_observations'] += 1
                    if value is None:
                        continue
                    state['kwh'] += Decimal(value); state['observations'] += 1
                    if rate is not None and rate['available']:
                        amounts = calculate_energy_amounts(Decimal(value), rate['energy_rate'], rate['variable_rate'], rate['rate_semantics'])
                        if hour_amounts is not None:
                            hour_amounts[sid] = amounts
                        if amounts['usage_cost'] is not None:
                            for key in COMPONENTS:
                                if amounts[key] is not None:
                                    state['usage_parts'][key] += amounts[key]
                        for key, amount in amounts.items():
                            if amount is not None:
                                state['amounts'][key] += amount; state['priced'][key] += 1
                    elif rate is not None:
                        issues.add(rate['reason'])
                if report is not None:
                    # Pricing above is the sole calculation. Reassemble inclusive
                    # parent costs from the existing disjoint subtree amounts.
                    cells = {}
                    for sid in self._ids:
                        members = report_members[sid]
                        costs = {}
                        for key in COSTS:
                            parts = [hour_amounts.get(member, {}).get(key) for member in members]
                            costs[key] = (_text(sum(parts, Decimal(0)))
                                          if all(part is not None for part in parts) else None)
                        cells[sid] = {'kwh': breakdown['devices'][sid]['measured_kwh_decimal'],
                                      'costs': costs}
                    report.append({'from': stamp, 'to': hour['to'], 'devices': cells})
                if statistics is not None:
                    statistics.append(group_cost_hour(hour, breakdown, hour_amounts, rate,
                        self._group_membership, self._domain))
        provenance = deepcopy(evidence['measurement_provenance']) if evidence else None
        staged = states, issues, partition, provenance
        if statistics is not None:
            staged = (*staged, statistics)
        if report is not None:
            staged = (*staged, report)
        projection = ({'schema': 1, 'consumer_source_ids': sorted(self._ids),
            'consumer_provenance': deepcopy(provenance) if provenance is not None else {},
            'registry_revision': self._membership['revision'], 'membership_mode': 'current_snapshot',
            'energy_domain': self._domain, 'group_membership': deepcopy(self._group_membership),
            'hours': projected} if include_hours else None)
        return staged, projection

    def commit_chunk(self, staged):
        if self._report_hours is not None:
            staged, report = staged[:-1], staged[-1]
            if len(self._report_hours) + len(report) > self._hours:
                raise ValueError('report_projection_too_large')
            self._report_hours.extend(report)
        if self._statistics_hours is not None:
            states, issues, partition, provenance, statistics = staged
            if len(self._statistics_hours) + len(statistics) > min(self._hours, MAX_STATISTICS_HOURS):
                raise ValueError('statistics_projection_too_large')
            self._statistics_hours.extend(statistics)
            self._states, self._issues, self._partition, self._provenance = states, issues, partition, provenance
        else:
            self._states, self._issues, self._partition, self._provenance = staged

    def report_hours(self):
        """Internal inclusive facts; default disjoint/cent contract is unchanged."""
        if self._report_hours is None:
            raise ValueError('report_projection_not_enabled')
        return deepcopy(self._report_hours)

    def statistics_hours(self):
        """Detached accepted-chunk facts; membership remains a current snapshot."""
        if self._statistics_hours is None:
            raise ValueError('statistics_projection_not_enabled')
        return deepcopy({'membership_mode': 'current_snapshot',
            'membership_as_of': self._membership['captured_at'],
            'registry_revision': self._membership['revision'],
            'group_membership': self._group_membership,
            'consumer_source_ids': sorted(self._ids),
            'consumer_provenance': self._provenance, 'energy_domain': self._domain,
            'hours': self._statistics_hours})

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
