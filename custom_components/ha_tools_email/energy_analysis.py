"""One immutable household calculation assembled from bounded Recorder chunks.

Pure CPU model: callers own Recorder, authorization and current-context fences.
Only aggregated household hours survive each chunk; raw source matrices do not.
Consumer groups and comparison arithmetic are attached by the analysis service.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json

from .energy_measurements import hour_grid, measurement_evidence
from .tariff_history import _bounded, price_book, validate_tariff_book


def _stamp(value):
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError('aware_analysis_boundary_required')
    return instant.astimezone(timezone.utc)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


class EnergyAnalysisAccumulator:
    """Append contiguous chunks and price the actual whole period exactly once."""

    def __init__(self, book, period, context, *, generated_at):
        _bounded(period, 65536)
        _bounded(context, 65536)
        self._start, self._end = _stamp(period['start']), _stamp(period['end'])
        self._hours = len(hour_grid(self._start, self._end))
        self._generated = _stamp(generated_at)
        if self._generated < self._end:
            raise ValueError('future_analysis_measurement')
        self._book = validate_tariff_book(book) if book is not None else None
        self._period, self._context = deepcopy(period), deepcopy(context)
        self._period.update(start=self._start.isoformat(), end=self._end.isoformat())
        self._cursor = self._start
        self._rows, self._issues = [], []
        self._ids, self._provenance, self._signature = None, None, None
        self._measured_seconds = 0
        self._result = None

    def add_chunk(self, snapshot, start, end):
        if self._result is not None:
            raise ValueError('analysis_already_finished')
        start, end = _stamp(start), _stamp(end)
        if start != self._cursor or not start < end <= self._end:
            raise ValueError('analysis_chunk_gap_overlap_or_outside_period')
        if (end - start).total_seconds() > 7 * 86400:
            raise ValueError('analysis_chunk_too_large')
        evidence = measurement_evidence(snapshot, start, end)
        ids, provenance = sorted(evidence['source_ids']), evidence['measurement_provenance']
        if self._ids is not None and (ids != self._ids or provenance != self._provenance):
            raise ValueError('energy_sources_changed')
        # Commit only after every chunk check has succeeded. A rejected chunk
        # cannot advance the cursor, change coverage or poison an explicit retry.
        if self._ids is None:
            self._ids, self._provenance = ids, deepcopy(provenance)
            self._signature = _digest({'source_ids': ids, 'provenance': provenance})
        self._rows.extend(evidence['hourly_buckets'])
        self._measured_seconds += evidence['coverage']['measured_source_seconds']
        for issue in evidence['issues']:
            # Keep diagnostics bounded by unique kind, not hours times sources.
            reason = issue['reason']
            if reason not in self._issues:
                self._issues.append(reason)
        self._cursor = end

    def finish(self):
        if self._result is not None:
            return deepcopy(self._result)
        if self._cursor != self._end or self._ids is None:
            raise ValueError('analysis_chunks_incomplete')
        expected_seconds = self._hours * len(self._ids) * 3600
        complete = (self._measured_seconds == expected_seconds
                    and all(row['kwh'] is not None for row in self._rows))
        with localcontext() as decimal_context:
            decimal_context.prec = 40
            recorded = sum((Decimal(str(row['kwh'])) for row in self._rows if row['kwh'] is not None), Decimal(0))
        has_recorded = any(row['kwh'] is not None for row in self._rows)
        total_kwh = str(recorded) if complete else None
        coverage = {'complete': complete, 'ratio': self._measured_seconds / expected_seconds,
                    'expected_source_seconds': expected_seconds,
                    'measured_source_seconds': self._measured_seconds}
        price_coverage = {'from': self._period['start'], 'to': self._period['end'],
                          'complete': complete, 'unit': 'kWh', 'source_signature': self._signature,
                          'entry_id': self._context.get('entry_id'),
                          'measurement_revision': self._context.get('measurement_revision')}
        fingerprint = _digest({'rows': self._rows, 'coverage': price_coverage})
        pricing, active = None, None
        if self._book is not None:
            pricing = price_book(self._book, self._rows, price_coverage)
            fingerprint = pricing['measurement_fingerprint']
            active = next((row for row in pricing['tariffs']
                           if row['tariff_id'] == pricing['active_tariff_id']), None)
        cost_keys = ('energy_cost', 'variable_cost', 'legacy_combined_cost', 'usage_cost',
                     'fixed_cost', 'total_cost', 'effective_price', 'currency', 'price_basis')
        totals = {key: active.get(key) if active is not None else None for key in cost_keys}
        totals.update(kwh=total_kwh, recorded_household_kwh=str(recorded) if has_recorded else None,
                      measurement_complete=complete,
                      pricing_complete=active['pricing_complete'] if active is not None else False,
                      components_complete=active['components_complete'] if active is not None else False,
                      known_costs=deepcopy(active['known_costs']) if active is not None else {})
        status = 'ready' if complete and totals['total_cost'] is not None else 'partial'
        pricing_status = ('not_configured' if self._book is None else 'no_active_tariff' if active is None
                          else 'ready' if totals['total_cost'] is not None else 'partial')
        alternatives = []
        if pricing is not None:
            for row in pricing['tariffs']:
                # Never return one diagnostic row matrix per alternative. The
                # snapshot carries a single household bucket series plus totals.
                alternatives.append({key: deepcopy(value) for key, value in row.items()
                                     if key not in ('rows', 'fees', 'rates')})
        context = {**self._context, 'grid_source_signature': self._signature}
        result = {'schema': 2, 'generated_at': self._generated.isoformat(), 'context': context,
                  'period': deepcopy(self._period), 'coverage': coverage, 'status': status,
                  'pricing_status': pricing_status, 'totals': totals,
                  'measurement_fingerprint': fingerprint, 'buckets': deepcopy(self._rows),
                  'alternatives': alternatives, 'rates': deepcopy(active['rates']) if active is not None else [],
                  'rounding': deepcopy(pricing['rounding']) if pricing is not None else None,
                  'issues': list(self._issues)}
        result['snapshot_id'] = _digest(result)
        self._result = result
        return deepcopy(result)
