"""Pure seven-day mean from actual compatible source-hour observations.

All eight windows share one intersection when any is incomplete. This daily
helper is bounded to 48 absolute hours per window; annual IO must stream.
"""
from math import fsum, isfinite
from zoneinfo import ZoneInfoNotFoundError

from .energy_measurements import (calendar_grid, source_cells, validated_source_ids,
                                  valid_provenance)


def compare_daily_mean7(current, baselines, current_period, baseline_periods, *, timezone_name):
    result = {'status': 'unknown', 'reason': 'incomplete_statistics', 'baseline_kind': 'mean7',
              'current_kwh': None, 'baseline_kwh': None, 'delta_kwh': None, 'percent': None,
              'full_current_kwh': None, 'full_baseline_kwh': None}
    if not isinstance(baselines, list) or not isinstance(baseline_periods, list) or len(baselines) != 7 or len(baseline_periods) != 7:
        return {**result, 'reason': 'seven_baselines_required'}
    snapshots, periods = [current, *baselines], [current_period, *baseline_periods]
    try:
        ids = validated_source_ids(current)
        if any(set(validated_source_ids(snapshot)) != set(ids) for snapshot in baselines):
            return {**result, 'reason': 'source_set_changed'}
        provenance = current.get('measurement_provenance')
        if (not isinstance(provenance, dict) or set(provenance) != set(ids)
                or any(not valid_provenance(provenance[sid]) for sid in ids)
                or any(snapshot.get('measurement_provenance') != provenance for snapshot in baselines)):
            return {**result, 'reason': 'measurement_provenance_unavailable_or_changed'}
        grids = [calendar_grid(period['start'], period['end'], timezone_name, 'daily') for period in periods]
        if any(len(grid) > 48 for grid in grids):
            return {**result, 'reason': 'daily_comparison_window_too_long'}
        measured, totals, shared = [0] * 8, [[] for _ in snapshots], [[] for _ in snapshots]
        common_count = 0
        for sid in ids:
            cells = [source_cells(snapshot, sid, grid) for snapshot, grid in zip(snapshots, grids)]
            common = set.intersection(*(set(values) for values in cells))
            common_count += len(common)
            for index, values in enumerate(cells):
                measured[index] += len(values)
                totals[index].append(fsum(values.values()))
                shared[index].append(fsum(values[key] for key in common))
        expected = [len(grid) * len(ids) for grid in grids]
        own, common = [count / total for count, total in zip(measured, expected)], [common_count / total for total in expected]
        full = [fsum(values) if count == total else None for values, count, total in zip(totals, measured, expected)]
        result.update(full_current_kwh=full[0],
                      full_baseline_kwh=fsum(full[1:]) / 7 if all(value is not None for value in full[1:]) else None,
                      coverage={'current': own[0], 'baselines': own[1:], 'common_current': common[0],
                                'common_baselines': common[1:], 'expected_current_seconds': expected[0] * 3600,
                                'expected_baseline_seconds': [total * 3600 for total in expected[1:]],
                                'measured_current_seconds': measured[0] * 3600,
                                'measured_baseline_seconds': [count * 3600 for count in measured[1:]]})
        complete = all(value is not None for value in full)
        if not complete and any(ratio < .9 for ratio in (*own, *common)):
            return result
        values = full if complete else [fsum(parts) for parts in shared]
        actual, mean = values[0], fsum(values[1:]) / 7
        delta = actual - mean
        if any(not isfinite(value) for value in (*values, mean, delta)):
            return result
        percent = delta / mean * 100 if mean else None
        return {**result, 'status': 'ready', 'reason': None,
                'comparison_scope': 'calendar_totals' if complete else 'common_measurements',
                'current_kwh': actual, 'baseline_kwh': mean, 'delta_kwh': delta,
                'percent': percent if percent is not None and isfinite(percent) else None}
    except (KeyError, TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        return result
