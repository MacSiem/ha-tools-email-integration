"""Calendar comparison using two bounded source-hour slots, without annual matrices.

Caller owns IO/access. Input is borrowed only for push; validation creates at
most one incoming slot plus a per-source scratch map. Raw caller input is not
included in retained_cells: whole-process peak memory must be measured by IO.
"""
from copy import deepcopy
from datetime import timedelta
from math import fsum, isfinite

from .energy_measurements import calendar_grid, hour_grid, source_cells, validated_source_ids, valid_provenance, _instant


class PairedComparisonAccumulator:
    def __init__(self, current_period, baseline_period, *, timezone_name, calendar_kind):
        if calendar_kind not in ('daily', 'weekly', 'monthly', 'yearly'):
            raise ValueError('invalid_calendar_kind')
        self._periods = [deepcopy(current_period), deepcopy(baseline_period)]
        self._grids = [calendar_grid(p['start'], p['end'], timezone_name, calendar_kind)
                       for p in self._periods]
        common = set(self._grids[0].values()) & set(self._grids[1].values())
        sequences = [[key for key in grid.values() if key in common] for grid in self._grids]
        # Multi-hour rollback is not lexicographically sorted. Match in the
        # actual shared frame order and skip frames absent on the other side.
        self._aligned = sequences[0] == sequences[1]
        self._ranks = {key: rank for rank, key in enumerate(sequences[0])}
        self._ends = [_instant(p['end']) for p in self._periods]
        self._cursors = [_instant(p['start']) for p in self._periods]
        self._slots = [None, None]
        self._ids = self._provenance = None
        self._measured = [0, 0]
        # Only aggregate scalars per chunk/hour survive; no source-by-year map.
        self._totals = [[], []]
        self._common_totals = [[], []]
        self._common = 0
        self.peak_retained_cells = 0
        self._result = None

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
        # No state mutation before the complete incoming chunk is validated.
        if self._ids is None:
            self._ids, self._provenance = ids, deepcopy(provenance)
        self._slots[index] = {'frames': list(grid.values()), 'cells': cells, 'head': 0}
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
                    values = [[], []]
                    for sid in self._ids:
                        x, y = left['cells'][sid].get(a), right['cells'][sid].get(b)
                        if x is not None and y is not None:
                            self._common += 1
                            values[0].append(x); values[1].append(y)
                    for i in (0, 1):
                        self._common_totals[i].append(fsum(values[i]))
                    self._advance(0); self._advance(1)
                else:
                    self._advance(0 if self._ranks[a] < self._ranks[b] else 1)
                continue
            # Unmatched tails already contributed their own actual totals.
            for i in (0, 1):
                if self._slots[i] is not None and self._slots[1-i] is None and self._cursors[1-i] == self._ends[1-i]:
                    self._slots[i] = None
            return

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
        self._result = result
        return deepcopy(result)
