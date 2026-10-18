"""Two bounded group-hour slots sharing one validated whole-period calendar.

Input is the detached internal EnergyAnalysisAccumulator projection. Counts are
recomputed from cells, never from caller coverage flags. No annual group maps,
Recorder IO, household fees or mean-of-seven semantics live here.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext
from math import isfinite

from .energy_comparison_core import (CalendarComparisonPlan, new_usage_cost_state,
                                     add_usage_cost, finish_usage_cost)
from .energy_measurements import _instant, hour_grid, valid_provenance
from .tariff_history import HistoricalTariffRates, _money

_LEVELS = ('devices', 'areas', 'floors')
_COSTS = ('energy_cost', 'variable_cost', 'legacy_combined_cost', 'usage_cost')
_IDENTITY = ('consumer_source_ids', 'consumer_provenance', 'grid_source_ids',
             'grid_provenance', 'registry_revision', 'membership_mode', 'energy_domain')


def _quantity(value):
    if value is None:
        return None
    if type(value) not in (str, int, float, Decimal):
        raise ValueError('invalid_group_quantity')
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError('invalid_group_quantity')
        return result
    except InvalidOperation as error:
        raise ValueError('invalid_group_quantity') from error


def _sources(projection, name, *, empty=False):
    ids = projection.get(name + '_source_ids')
    if (not isinstance(ids, list) or not (0 if empty else 1) <= len(ids) <= 512
            or any(not isinstance(sid, str) or not sid for sid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('invalid_group_sources')
    provenance = projection.get(name + '_provenance')
    if (not isinstance(provenance, dict) or not set(provenance) <= set(ids)
            or any(not valid_provenance(value) for value in provenance.values())):
        raise ValueError('invalid_group_provenance')
    return set(ids), set(provenance)


class GroupComparisonAccumulator:
    def __init__(self, current_period, baseline_period, *, calendar_plan, tariff=None):
        if type(calendar_plan) is not CalendarComparisonPlan:
            raise ValueError('validated_comparison_calendar_plan_required')
        calendar_plan.validate_matches((current_period, baseline_period),
            calendar_plan.timezone_name, calendar_plan.calendar_kind)
        self._calendar_plan = calendar_plan
        self._cursors, self._ends = list(calendar_plan.starts), list(calendar_plan.ends)
        self._slots = [None, None]
        self._identity = self._keys = self._states = None
        self._partition = [True, True]
        self._rates = HistoricalTariffRates(tariff) if tariff is not None else None
        self._result = None
        self.peak_retained_cells = 0

    @property
    def calendar_plan(self):
        return self._calendar_plan

    @property
    def retained_cells(self):
        return sum(len(slot['rows']) * len(self._keys) for slot in self._slots if slot)

    def needs_side(self):
        for index, side in enumerate(('current', 'baseline')):
            if self._slots[index] is None and self._cursors[index] < self._ends[index]:
                return side
        return None

    def _new_state(self):
        return {'own_count': [0, 0], 'own': [Decimal(0), Decimal(0)],
                'common_count': 0, 'common': [Decimal(0), Decimal(0)],
                'own_cost': [new_usage_cost_state(self._rates) for _ in range(2)] if self._rates else None,
                'common_cost': [new_usage_cost_state(self._rates) for _ in range(2)] if self._rates else None}

    def _validate(self, projection, start, end):
        if (not isinstance(projection, dict) or type(projection.get('schema')) is not int
                or projection['schema'] != 1
                or projection.get('membership_mode') != 'current_snapshot'
                or projection.get('energy_domain') not in ('grid_import', 'site_consumption')
                or 'registry_revision' not in projection):
            raise ValueError('invalid_group_projection')
        consumers, consumer_provenance = _sources(projection, 'consumer', empty=True)
        grids, grid_provenance = _sources(projection, 'grid')
        identity = {key: deepcopy(projection[key]) for key in _IDENTITY}
        # Source order is not an identity; detach and normalize it once.
        for key in ('consumer_source_ids', 'grid_source_ids'):
            identity[key].sort()
        if self._identity is not None and identity != self._identity:
            raise ValueError('group_context_changed')
        hours = projection.get('hours')
        stamps = hour_grid(start, end)
        if not isinstance(hours, list) or len(hours) != len(stamps):
            raise ValueError('invalid_group_hours')
        rows, keys, partition = [], self._keys, True
        with localcontext() as context:
            context.prec = 40
            for stamp, hour in zip(stamps, hours):
                if (not isinstance(hour, dict) or type(hour.get('from')) not in (int, float)
                        or type(hour.get('to')) not in (int, float)
                        or hour['from'] != stamp or hour['to'] != stamp + 3600000
                        or type(hour.get('partition_complete')) is not bool):
                    raise ValueError('invalid_group_hour_boundary')
                values = {}
                for level in _LEVELS:
                    cells = hour.get(level)
                    if (not isinstance(cells, dict) or len(cells) > len(consumers)
                            or any(not isinstance(key, str) or not key for key in cells)):
                        raise ValueError('invalid_group_shape')
                    if level == 'devices' and set(cells) != consumers:
                        raise ValueError('invalid_group_shape')
                    for key, value in cells.items():
                        values[(level, key)] = _quantity(value)
                for name in ('unassigned', 'unmeasured'):
                    if name not in hour:
                        raise ValueError('invalid_group_shape')
                    values[(name, None)] = _quantity(hour[name])
                if keys is None:
                    keys = tuple(sorted(values, key=lambda key: (key[0], key[1] or '')))
                if len(values) != len(keys) or any(key not in values for key in keys):
                    raise ValueError('group_context_changed')
                for sid in consumers - consumer_provenance:
                    if values[('devices', sid)] is not None:
                        raise ValueError('group_measurement_without_provenance')
                presence = hour.get('grid_source_presence')
                if (not isinstance(presence, list) or any(not isinstance(sid, str) for sid in presence)
                        or len(presence) != len(set(presence)) or not set(presence) <= grid_provenance):
                    raise ValueError('invalid_grid_source_presence')
                if 'household_kwh' not in hour:
                    raise ValueError('invalid_group_shape')
                household = _quantity(hour['household_kwh'])
                if household is not None and set(presence) != grids:
                    raise ValueError('partial_grid_cannot_supply_household')
                if (household is None or identity['energy_domain'] != 'grid_import') and values[('unmeasured', None)] is not None:
                    raise ValueError('invalid_unmeasured_projection')
                # A partition claim alone proves nothing about valid evidence.
                # Verify all cells and each alternative slice; never add levels.
                known_partition = (identity['energy_domain'] == 'grid_import'
                    and household is not None and set(presence) == grids
                    and all(value is not None for value in values.values()))
                if hour['partition_complete']:
                    if not known_partition:
                        raise ValueError('invalid_group_partition')
                    remainder = values[('unmeasured', None)]
                    partitions = [sum((value for (level, _), value in values.items() if level == 'devices'), Decimal(0)) + remainder,
                        sum((value for (level, _), value in values.items() if level == 'areas'), Decimal(0))
                            + values[('unassigned', None)] + remainder,
                        sum((value for (level, _), value in values.items() if level == 'floors'), Decimal(0)) + remainder]
                    if any(value != household for value in partitions):
                        raise ValueError('invalid_group_partition')
                partition = partition and hour['partition_complete'] and known_partition
                if self._rates and any(value is not None and not isfinite(float(value)) for value in values.values()):
                    raise ValueError('unrepresentable_group_cost_quantity')
                rows.append((stamp, tuple(values[key] for key in keys)))
        return identity, keys, rows, partition

    def push(self, side, projection, start, end):
        if side not in ('current', 'baseline') or self._result is not None:
            raise ValueError('invalid_comparison_push')
        index = 0 if side == 'current' else 1
        start, end = _instant(start), _instant(end)
        if (self._slots[index] is not None or start != self._cursors[index]
                or not start < end <= self._ends[index] or end-start > timedelta(hours=168)):
            raise ValueError('comparison_chunk_gap_overlap_or_too_large')
        identity, keys, rows, partition = self._validate(projection, start, end)
        # Stage O(G) scalars. Any rejected push leaves both slots/cursors intact.
        states = deepcopy(self._states) if self._states is not None else [self._new_state() for _ in keys]
        with localcontext() as context:
            context.prec = 40
            for stamp, values in rows:
                for state, value in zip(states, values):
                    if value is not None:
                        state['own_count'][index] += 1
                        state['own'][index] += value
                        if self._rates and identity['energy_domain'] == 'grid_import':
                            add_usage_cost(state['own_cost'][index], stamp, [value], self._rates)
        self._identity, self._keys, self._states = identity, keys, states
        self._partition[index] = self._partition[index] and partition
        self._slots[index] = {'rows': rows, 'head': 0}
        self._cursors[index] = end
        self.peak_retained_cells = max(self.peak_retained_cells, self.retained_cells)
        self._drain()

    def _advance(self, index):
        slot = self._slots[index]
        slot['head'] += 1
        if slot['head'] == len(slot['rows']):
            self._slots[index] = None

    def _drain(self):
        plan = self._calendar_plan
        if not plan.aligned:
            self._slots = [None, None]
            return
        with localcontext() as context:
            context.prec = 40
            while all(self._slots):
                rows = [slot['rows'][slot['head']] for slot in self._slots]
                frames = [plan.grids[i][row[0]] for i, row in enumerate(rows)]
                if frames[0] not in plan.ranks:
                    self._advance(0)
                elif frames[1] not in plan.ranks:
                    self._advance(1)
                elif frames[0] != frames[1]:
                    self._advance(0 if plan.ranks[frames[0]] < plan.ranks[frames[1]] else 1)
                else:
                    for state, a, b in zip(self._states, rows[0][1], rows[1][1]):
                        if a is not None and b is not None:
                            state['common_count'] += 1
                            for index, value in enumerate((a, b)):
                                state['common'][index] += value
                                if self._rates and self._identity['energy_domain'] == 'grid_import':
                                    add_usage_cost(state['common_cost'][index], rows[index][0], [value], self._rates)
                    self._advance(0)
                    self._advance(1)
        for index in (0, 1):
            if self._slots[index] and self._slots[1-index] is None and self._cursors[1-index] == self._ends[1-index]:
                self._slots[index] = None

    def _row(self, state):
        expected = [len(grid) for grid in self._calendar_plan.grids]
        counts, common_count = state['own_count'], state['common_count']
        own = [count / total for count, total in zip(counts, expected)]
        common = [common_count / total for total in expected]
        complete = counts == expected
        ready = complete or self._calendar_plan.aligned and all(value >= .9 for value in (*own, *common))
        selected = state['own'] if complete else state['common']
        row = {'status': 'ready' if ready else 'unknown',
            'reason': None if ready else ('unsupported_common_calendar_order'
                if not self._calendar_plan.aligned else 'incomplete_statistics'),
            'comparison_scope': ('per_group_calendar_totals' if complete else 'common_measurements') if ready else None,
            'current_kwh': str(selected[0]) if ready else None, 'baseline_kwh': str(selected[1]) if ready else None,
            'own_current_kwh': str(state['own'][0]), 'own_baseline_kwh': str(state['own'][1]),
            'full_current_kwh': str(state['own'][0]) if counts[0] == expected[0] else None,
            'full_baseline_kwh': str(state['own'][1]) if counts[1] == expected[1] else None,
            'delta_kwh': str(selected[0]-selected[1]) if ready else None, 'percent': None,
            'coverage': {'current': own[0], 'baseline': own[1], 'common_current': common[0], 'common_baseline': common[1],
                'expected_current_seconds': expected[0]*3600, 'expected_baseline_seconds': expected[1]*3600,
                'measured_current_seconds': counts[0]*3600, 'measured_baseline_seconds': counts[1]*3600,
                'common_group_seconds': common_count*3600},
            **{'delta_'+key: None for key in (*_COSTS, 'fixed_cost')}}
        if ready and selected[1]:
            percent = float((selected[0]-selected[1])/selected[1]*100)
            row['percent'] = percent if isfinite(percent) else None
        if self._rates:
            allowed = ready and self._identity['energy_domain'] == 'grid_import'
            sides = [finish_usage_cost(cost, eligible=allowed) for cost in
                     (state['own_cost'] if complete else state['common_cost'])]
            same = all(side['compatible'] for side in sides) and (
                sides[0]['currency'], sides[0]['price_basis']) == (sides[1]['currency'], sides[1]['price_basis'])
            for key in _COSTS:
                if same and all(side[key] is not None for side in sides):
                    row['delta_'+key] = _money(Decimal(sides[0][key])-Decimal(sides[1][key]))
            comparable = row['delta_usage_cost'] is not None
            row['costs'] = {'current': sides[0], 'baseline': sides[1],
                'status': 'ready' if comparable else 'unknown', 'comparison_scope': row['comparison_scope'],
                'reason': None if comparable else (row['reason'] or ('unsupported_energy_domain'
                    if self._identity['energy_domain'] != 'grid_import' else
                    'incomplete_cost' if same else 'currency_or_basis_mismatch')),
                'delta_cost': row['delta_usage_cost'], 'delta_fixed_cost': None}
        return row

    def finish(self, household_comparison=None):
        if self._result is not None:
            return deepcopy(self._result)
        if self._identity is None or self._cursors != self._ends or any(self._slots):
            raise ValueError('comparison_chunks_incomplete')
        with localcontext() as context:
            context.prec = 40
            result = {'membership_mode': 'current_snapshot', 'membership_as_of': None,
                'registry_revision': deepcopy(self._identity['registry_revision']),
                'energy_domain': self._identity['energy_domain'], 'attribution_label': 'current_registry_assignments',
                'reconciles_household_delta': False, 'currency': None, 'price_basis': None,
                **{level: {} for level in _LEVELS}}
            identities = set()
            for (level, key), state in zip(self._keys, self._states):
                row = self._row(state)
                if key is None:
                    result[level] = row
                else:
                    result[level][key] = row
                if self._rates:
                    sides = [row['costs'][name] for name in ('current', 'baseline')]
                    identities.update((side['currency'], side['price_basis']) for side in sides if side['compatible'])
            if len(identities) == 1 and self._identity['energy_domain'] == 'grid_import':
                result['currency'], result['price_basis'] = next(iter(identities))
            household = household_comparison or {}
            # Full partition evidence is required on EVERY actual hour; numerical
            # equality of final deltas never establishes mask/partition identity.
            result['reconciles_household_delta'] = (self._identity['energy_domain'] == 'grid_import'
                and all(self._partition) and all(state['own_count'] == [len(grid) for grid in self._calendar_plan.grids]
                    for state in self._states) and household.get('status') == 'ready'
                and household.get('comparison_scope') == 'calendar_totals')
            self._result = result
        return deepcopy(self._result)
