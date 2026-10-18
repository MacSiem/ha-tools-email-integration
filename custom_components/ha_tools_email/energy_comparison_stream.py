"""Calendar comparison using two bounded source-hour slots, without annual matrices.

Caller owns IO/access. Input is borrowed only for push; validation creates at
most one incoming slot plus a per-source scratch map. Raw caller input is not
included in retained_cells: whole-process peak memory must be measured by IO.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext
from math import fsum, isfinite

from .energy_measurements import hour_grid, source_cells, validated_source_ids, valid_provenance, _instant
from .energy_comparison_chart import PairChartProjection

from .tariff_history import (HistoricalTariffRates, calculate_energy_amounts,
                             FEE_KEYS, ROUNDING, _fees, _money)


from .energy_comparison_core import (CalendarComparisonPlan, new_usage_cost_state,
                                     add_usage_cost, finish_usage_cost, _COMPONENTS)


class PairedComparisonAccumulator:
    def __init__(self, current_period, baseline_period, *, timezone_name, calendar_kind,
                 tariff=None, calendar_plan=None):
        if calendar_plan is None:
            calendar_plan = CalendarComparisonPlan(current_period, baseline_period,
                timezone_name=timezone_name, calendar_kind=calendar_kind)
        elif type(calendar_plan) is not CalendarComparisonPlan:
            raise ValueError('validated_comparison_calendar_plan_required')
        calendar_plan.validate_matches((current_period, baseline_period), timezone_name, calendar_kind)
        self._calendar_plan = calendar_plan
        self._chart = PairChartProjection(calendar_plan)
        self._periods = calendar_plan.periods
        self._grids = calendar_plan.grids
        self._aligned = calendar_plan.aligned
        self._ranks = calendar_plan.ranks
        self._ends = list(calendar_plan.ends)
        self._cursors = list(calendar_plan.starts)
        self._slots = [None, None]
        self._ids = self._provenance = None
        self._measured = [0, 0]
        # Only aggregate scalars per chunk/hour survive; no source-by-year map.
        self._totals = [[], []]
        self._common_totals = [[], []]
        self._common = 0
        self.peak_retained_cells = 0
        # The resolver validates and detaches once; reuse its normalized versions
        # for the authoritative calendar-fee helper, without a second proof.
        self._rates = HistoricalTariffRates(tariff) if tariff is not None else None
        self._versions = tuple(item[2] for item in self._rates._compiled) if self._rates else ()
        self._own_costs = [new_usage_cost_state(self._rates), new_usage_cost_state(self._rates)] if self._rates else None
        self._common_costs = [new_usage_cost_state(self._rates), new_usage_cost_state(self._rates)] if self._rates else None
        self._result = None

    @property
    def calendar_plan(self):
        return self._calendar_plan

    @property
    def retained_cells(self):
        return sum(sum(len(values) for values in slot['cells'].values())
                   for slot in self._slots if slot is not None)

    def needs_side(self):
        for index, name in enumerate(('current', 'baseline')):
            if self._slots[index] is None and self._cursors[index] < self._ends[index]:
                return name
        return None

    def push(self, side, snapshot, start, end):
        if side not in ('current', 'baseline') or self._result is not None:
            raise ValueError('invalid_comparison_push')
        index = 0 if side == 'current' else 1
        start, end = _instant(start), _instant(end)
        if (self._slots[index] is not None or start != self._cursors[index]
                or not start < end <= self._ends[index]
                or end-start > timedelta(hours=168)):
            raise ValueError('comparison_chunk_gap_overlap_or_too_large')
        ids = sorted(validated_source_ids(snapshot))
        raw_provenance = snapshot.get('measurement_provenance')
        raw_provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
        provenance = {sid: raw_provenance[sid] for sid in ids if valid_provenance(raw_provenance.get(sid))}
        if self._ids is not None and (ids != self._ids or provenance != self._provenance):
            raise ValueError('energy_sources_changed')
        grid = {stamp: self._grids[index][stamp] for stamp in hour_grid(start, end)}
        cells = {sid: source_cells(snapshot, sid, grid) if sid in provenance else {} for sid in ids}
        count = sum(len(values) for values in cells.values())
        total = fsum(value for values in cells.values() for value in values.values())
        if not isfinite(total):
            raise ValueError('unrepresentable_measurement_sum')
        own_cost = None
        if self._rates:
            own_cost = deepcopy(self._own_costs[index])
            for stamp, frame in grid.items():
                values = [values[frame] for values in cells.values() if frame in values]
                self._add_cost(own_cost, stamp, values)
        chart_own = self._chart.prepare_own(index, grid, cells)
        # No state mutation before the complete incoming chunk is validated.
        if self._ids is None:
            self._ids, self._provenance = ids, deepcopy(provenance)
        self._chart.commit_own(index, chart_own)
        self._slots[index] = {'frames': list(grid.values()), 'cells': cells, 'head': 0,
                              'stamps': list(grid)}
        if self._rates:
            self._own_costs[index] = own_cost
        self._measured[index] += count
        self._totals[index].append(total)
        self._cursors[index] = end
        self.peak_retained_cells = max(self.peak_retained_cells, self.retained_cells)
        self._drain()

    def _advance(self, index):
        slot = self._slots[index]
        slot['head'] += 1
        if slot['head'] == len(slot['frames']):
            self._slots[index] = None

    def _drain(self):
        if not self._aligned:
            # Actual complete-period totals do not depend on frame alignment.
            # Conflicting historical common-frame orders cannot supply a
            # partial matched total; retain own evidence and report unknown.
            self._slots = [None, None]
            return
        while True:
            left, right = self._slots
            if left is not None and right is not None:
                a, b = left['frames'][left['head']], right['frames'][right['head']]
                if a not in self._ranks:
                    self._advance(0)
                elif b not in self._ranks:
                    self._advance(1)
                elif a == b:
                    self._matched_frame(left, right, a)
                    self._advance(0); self._advance(1)
                else:
                    self._advance(0 if self._ranks[a] < self._ranks[b] else 1)
                continue
            # Unmatched tails already contributed their own actual totals.
            for i in (0, 1):
                if self._slots[i] is not None and self._slots[1-i] is None and self._cursors[1-i] == self._ends[1-i]:
                    self._slots[i] = None
            return

    def _matched_frame(self, left, right, a):
        """Consume one equal calendar frame before either slot advances."""
        values = [[], []]
        for sid in self._ids:
            x, y = left['cells'][sid].get(a), right['cells'][sid].get(a)
            if x is not None and y is not None:
                self._common += 1
                values[0].append(x); values[1].append(y)
        self._chart.matched(left['stamps'][left['head']], right['stamps'][right['head']], values)
        for i in (0, 1):
            self._common_totals[i].append(fsum(values[i]))
            if self._rates:
                slot = self._slots[i]
                self._add_cost(self._common_costs[i], slot['stamps'][slot['head']], values[i])

    def finish(self):
        if self._result is not None:
            return deepcopy(self._result)
        if self._ids is None or self._cursors != self._ends or any(self._slots):
            raise ValueError('comparison_chunks_incomplete')
        expected = [len(grid)*len(self._ids) for grid in self._grids]
        own = [n/d for n, d in zip(self._measured, expected)]
        common = [self._common/d for d in expected]
        complete = self._measured == expected
        totals = [fsum(values) for values in self._totals]
        result = {'status': 'unknown', 'reason': 'incomplete_statistics',
                  'current_kwh': None, 'baseline_kwh': None, 'delta_kwh': None, 'percent': None,
                  'full_current_kwh': totals[0] if self._measured[0] == expected[0] else None,
                  'full_baseline_kwh': totals[1] if self._measured[1] == expected[1] else None,
                  'coverage': {'current': own[0], 'baseline': own[1],
                      'common_current': common[0], 'common_baseline': common[1],
                      'expected_current_seconds': expected[0]*3600,
                      'expected_baseline_seconds': expected[1]*3600,
                      'measured_current_seconds': self._measured[0]*3600,
                      'measured_baseline_seconds': self._measured[1]*3600,
                      'common_source_seconds': self._common*3600}}
        if not self._aligned:
            result['reason'] = 'unsupported_common_calendar_order'
        elif len(self._provenance) != len(self._ids):
            result['reason'] = 'measurement_provenance_unavailable'
        if complete or self._aligned and all(value >= .9 for value in (*own, *common)):
            actual, baseline = totals if complete else [fsum(parts) for parts in self._common_totals]
            delta = actual-baseline
            percent = delta/baseline*100 if baseline else None
            if all(isfinite(value) for value in (actual, baseline, delta)):
                result.update(status='ready', reason=None,
                    comparison_scope='calendar_totals' if complete else 'common_measurements',
                    current_kwh=actual, baseline_kwh=baseline, delta_kwh=delta,
                    percent=percent if percent is not None and isfinite(percent) else None)
        if self._rates:
            with localcontext() as context:
                context.prec = 40
                result['costs'] = self._finish_costs(result, complete)
        result['chart'] = self._chart.finish(result, len(self._ids), self._common)
        self._result = result
        return deepcopy(result)


    def _add_cost(self, state, stamp, values):
        add_usage_cost(state, stamp, values, self._rates, _calculate=calculate_energy_amounts)

    def _cost_side(self, state, index, eligible, complete):
        start, end = (int(_instant(self._periods[index][key]).timestamp() * 1000)
                      for key in ('start', 'end'))
        fee, _, _, fee_identities = _fees(self._versions, start, end)
        usage = finish_usage_cost(state, eligible=eligible, identity_constraints=fee_identities)
        compatible = usage['compatible']
        fixed = (sum((Decimal(fee['components'][key]) for key in FEE_KEYS), Decimal(0))
                 if fee['complete'] and compatible else None)
        total = (Decimal(usage['usage_cost']) + fixed
                 if complete and usage['pricing_complete'] and fixed is not None
                 and all(usage[key] is not None for key in _COMPONENTS) else None)
        return {**usage, 'fixed_scope': 'household_period', 'fixed_cost': _money(fixed),
                'fees': fee if compatible else None, 'total_cost': _money(total),
                'effective_price': _money(total / state['kwh'])
                    if total is not None and state['kwh'] > 0 else None}

    def _finish_costs(self, energy, complete):
        states = self._own_costs if complete else self._common_costs
        sides = [self._cost_side(state, i, energy['status'] == 'ready', complete)
                 for i, state in enumerate(states)]
        a, b = sides
        same_identity = (a['compatible'] and b['compatible']
                         and (a['currency'], a['price_basis']) == (b['currency'], b['price_basis']))
        comparable = same_identity and all(side['usage_cost'] is not None for side in sides)
        delta, percent = None, None
        if comparable:
            baseline = Decimal(b['usage_cost'])
            delta = Decimal(a['usage_cost']) - baseline
            percent = str(delta / baseline * 100) if baseline else None
        total_delta = (Decimal(a['total_cost']) - Decimal(b['total_cost'])
                       if same_identity and all(side['total_cost'] is not None for side in sides) else None)
        return {'comparison_scope': energy.get('comparison_scope'), 'metric': 'usage_cost',
                'current': a, 'baseline': b, 'status': 'ready' if comparable else 'unknown',
                'reason': None if comparable else (energy['reason'] or ('currency_or_basis_mismatch'
                    if not same_identity else 'incomplete_cost')),
                'delta_cost': _money(delta), 'percent': percent,
                'delta_total_cost': _money(total_delta), 'rounding': deepcopy(ROUNDING)}
