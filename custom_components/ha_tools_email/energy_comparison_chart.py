"""Energy-only projections on the comparator's actual source/calendar mask.

Pair state is O(calendar bins); mean7 is bounded to eight <=48-hour windows.
No prices, source identities, household fees or raw source matrices are retained.
"""
from datetime import datetime
from decimal import Decimal, localcontext
from math import fsum
from zoneinfo import ZoneInfo


_GRAIN = {'daily': 'hour', 'weekly': 'day', 'monthly': 'day', 'yearly': 'month',
          'day': 'hour', 'week': 'day', 'month': 'day', 'year': 'month'}
_HOUR = 3600000


def unknown_comparison_chart(timezone_name, calendar_kind, reason, scope=None):
    return {'status': 'unknown', 'reason': reason, 'timezone': timezone_name,
            'grain': _GRAIN.get(calendar_kind), 'comparison_scope': scope,
            'coverage_kind': 'selected_household_comparison_mask',
            'fixed_fee_scope': 'household_period_only',
            'reconciles_comparison_energy': False, 'markers': []}


def unknown_mean7_chart(timezone_name, reason):
    return {**unknown_comparison_chart(timezone_name, 'daily', reason),
            'baseline_aggregation': 'mean7', 'baseline_periods': []}


def _text(value):
    if value == 0:
        return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _hour_value(values):
    # Same source-hour fsum as the comparator and household measurement evidence.
    # Pair state adds only one Decimal per hour, not one per source-year cell.
    return Decimal(str(fsum(values)))


def _key(stamp, grain, zone):
    if grain == 'hour':
        return stamp
    local = datetime.fromtimestamp(stamp / 1000, zone)
    return local.date().isoformat() if grain == 'day' else local.strftime('%Y-%m')


def _bins(grid, grain, zone):
    bins = {}
    for stamp in grid:
        key = _key(stamp, grain, zone)
        if key not in bins:
            bins[key] = {'from': stamp, 'to': stamp + _HOUR, 'hours': 0,
                         'count': 0, 'value': Decimal(0)}
        bins[key]['to'] = stamp + _HOUR
        bins[key]['hours'] += 1
    return bins


def _finish_chart(chart, markers, comparison, reconciles):
    ready = sum(row['status'] == 'ready' for row in markers)
    chart.update(markers=markers, comparison_scope=comparison.get('comparison_scope'),
                 reconciles_comparison_energy=bool(reconciles))
    if comparison['status'] != 'ready':
        chart.update(status='unknown', reason=comparison['reason'])
    elif ready == len(markers) and ready and reconciles:
        chart.update(status='ready', reason=None)
    else:
        chart.update(status='partial' if ready else 'unknown', reason='incomplete_marker_mapping')
    return chart


class PairChartProjection:
    def __init__(self, plan):
        self._zone = ZoneInfo(plan.timezone_name)
        self._timezone = plan.timezone_name
        self._kind = plan.calendar_kind
        self._grain = _GRAIN[self._kind]
        self._aligned = plan.aligned
        self._bins = [_bins(grid, self._grain, self._zone) for grid in plan.grids]
        self._matched = {key: {'baseline': None, 'mixed': False, 'frames': 0,
                              'count': 0, 'values': [Decimal(0), Decimal(0)]}
                         for key in self._bins[0]}

    def prepare_own(self, index, grid, cells):
        # Called before the parent commits a validated chunk; keep scratch O(bins).
        staged = {}
        with localcontext() as context:
            context.prec = 40
            for stamp, frame in grid.items():
                values = [source[frame] for source in cells.values() if frame in source]
                key = _key(stamp, self._grain, self._zone)
                state = staged.setdefault(key, {'count': 0, 'value': Decimal(0)})
                state['count'] += len(values)
                state['value'] += _hour_value(values)
        return staged

    def commit_own(self, index, staged):
        with localcontext() as context:
            context.prec = 40
            for key, delta in staged.items():
                self._bins[index][key]['count'] += delta['count']
                self._bins[index][key]['value'] += delta['value']

    def matched(self, current_stamp, baseline_stamp, values):
        key = _key(current_stamp, self._grain, self._zone)
        baseline_key = _key(baseline_stamp, self._grain, self._zone)
        state = self._matched[key]
        if state['baseline'] is not None and state['baseline'] != baseline_key:
            state['mixed'] = True
        state['baseline'] = baseline_key
        state['frames'] += 1
        state['count'] += len(values[0])
        with localcontext() as context:
            context.prec = 40
            for index in (0, 1):
                state['values'][index] += _hour_value(values[index])

    def finish(self, comparison, source_count, common_count):
        chart = unknown_comparison_chart(self._timezone, self._kind, comparison['reason'])
        scope = comparison.get('comparison_scope')
        published = [0, 0]
        markers = []
        for key, current in self._bins[0].items():
            matched = self._matched[key]
            baseline = self._bins[1].get(matched['baseline']) if not matched['mixed'] else None
            expected = [current['hours'] * source_count,
                        baseline['hours'] * source_count if baseline is not None else 0]
            reason = None
            if comparison['status'] != 'ready':
                reason = comparison['reason']
            elif not self._aligned:
                reason = 'unsupported_common_calendar_order'
            elif matched['mixed']:
                reason = 'mixed_baseline_ranges'
            elif baseline is None:
                reason = 'unmatched_calendar_frames'
            elif scope == 'calendar_totals':
                if (matched['frames'] != current['hours'] or matched['frames'] != baseline['hours']
                        or current['count'] != expected[0] or baseline['count'] != expected[1]):
                    reason = 'incomplete_calendar_bin_bijection'
            elif scope == 'common_measurements':
                if not matched['count']:
                    reason = 'no_common_measurements'
            else:
                reason = 'comparison_scope_unavailable'
            selected = ([current['count'], baseline['count']] if scope == 'calendar_totals' and baseline is not None
                        else [matched['count'], matched['count']])
            values = ([current['value'], baseline['value']] if scope == 'calendar_totals' and baseline is not None
                      else matched['values'])
            ready = reason is None
            if ready:
                for index in (0, 1):
                    published[index] += selected[index]
            markers.append({'status': 'ready' if ready else 'unknown', 'reason': reason,
                'current_from': current['from'], 'current_to': current['to'],
                'baseline_from': baseline['from'] if baseline is not None else None,
                'baseline_to': baseline['to'] if baseline is not None else None,
                'current_kwh': _text(values[0]) if ready else None,
                'baseline_kwh': _text(values[1]) if ready else None,
                'mapping': 'calendar_frame_bijection' if scope == 'calendar_totals' else 'common_source_frame_intersection',
                'expected_current_source_seconds': expected[0] * 3600,
                'expected_baseline_source_seconds': expected[1] * 3600,
                'selected_current_source_seconds': selected[0] * 3600 if ready else 0,
                'selected_baseline_source_seconds': selected[1] * 3600 if ready else 0})
        selected_totals = ([sum(row['count'] for row in side.values()) for side in self._bins]
                           if scope == 'calendar_totals' else [common_count, common_count])
        reconciles = (comparison['status'] == 'ready' and self._aligned
                      and published == selected_totals and all(selected_totals))
        return _finish_chart(chart, markers, comparison, reconciles)


class Mean7ChartProjection:
    def __init__(self, grids, timezone_name):
        self._timezone = timezone_name
        self._grids = grids  # Borrow the eight existing bounded calendar grids.
        self._stamps = [{frame: stamp for stamp, frame in grid.items()} for grid in grids]
        common = set.intersection(*(set(grid.values()) for grid in grids))
        sequences = [tuple(frame for frame in grid.values() if frame in common) for grid in grids]
        self._aligned = (all(len(reverse) == len(grid) for reverse, grid in zip(self._stamps, grids))
                         and all(sequence == sequences[0] for sequence in sequences[1:]))
        self._own = [{frame: [Decimal(0), 0] for frame in grid.values()} for grid in grids]
        self._common = [{frame: [Decimal(0), 0] for frame in grid.values()} for grid in grids]

    def add_source(self, cells, common):
        # The caller supplies exactly its validated eight-window intersection.
        with localcontext() as context:
            context.prec = 40
            for index, values in enumerate(cells):
                for frame, value in values.items():
                    own = self._own[index][frame]
                    own[0] += Decimal(str(value)); own[1] += 1
                    if frame in common:
                        shared = self._common[index][frame]
                        shared[0] += Decimal(str(value)); shared[1] += 1

    def finish(self, comparison, source_count, common_count):
        chart = unknown_mean7_chart(self._timezone, comparison['reason'])
        chart['baseline_periods'] = [{'from': next(iter(grid)), 'to': next(reversed(grid)) + _HOUR}
                                     for grid in self._grids[1:]]
        full = comparison.get('comparison_scope') == 'calendar_totals'
        states = self._own if full else self._common
        published = [0] * 8
        markers = []
        with localcontext() as context:
            context.prec = 40
            for stamp, frame in self._grids[0].items():
                mapped = all(frame in reverse for reverse in self._stamps)
                chosen = [state.get(frame, [Decimal(0), 0]) for state in states]
                reason = None
                if comparison['status'] != 'ready':
                    reason = comparison['reason']
                elif not self._aligned:
                    reason = 'unsupported_common_calendar_order'
                elif not mapped:
                    reason = 'unmatched_calendar_frames'
                elif full and any(count != source_count for _, count in chosen):
                    reason = 'incomplete_calendar_bin_bijection'
                elif not full and (not chosen[0][1] or any(item[1] != chosen[0][1] for item in chosen)):
                    reason = 'no_common_measurements'
                ready = reason is None
                if ready:
                    for index, (_, count) in enumerate(chosen):
                        published[index] += count
                ranges = ([{'from': reverse[frame], 'to': reverse[frame] + _HOUR}
                           for reverse in self._stamps[1:]] if mapped else [])
                markers.append({'status': 'ready' if ready else 'unknown', 'reason': reason,
                    'current_from': stamp, 'current_to': stamp + _HOUR,
                    'baseline_from': None, 'baseline_to': None,
                    'baseline_ranges': ranges, 'baseline_aggregation': 'mean7',
                    'current_kwh': _text(chosen[0][0]) if ready else None,
                    'baseline_kwh': _text(sum((item[0] for item in chosen[1:]), Decimal(0)) / Decimal(7)) if ready else None,
                    'mapping': 'mean7_calendar_frames' if full else 'mean7_common_source_frame_intersection',
                    'expected_current_source_seconds': source_count * 3600,
                    'expected_baseline_source_seconds': [source_count * 3600] * 7,
                    'selected_current_source_seconds': chosen[0][1] * 3600 if ready else 0,
                    'selected_baseline_source_seconds': [item[1] * 3600 if ready else 0 for item in chosen[1:]]})
        selected_totals = ([sum(item[1] for item in side.values()) for side in self._own]
                           if full else [common_count] * 8)
        reconciles = (comparison['status'] == 'ready' and self._aligned
                      and published == selected_totals and all(selected_totals))
        return _finish_chart(chart, markers, comparison, reconciles)
