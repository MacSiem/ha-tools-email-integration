"""Current calendar Energy analysis with guarded bounded IO and shared CPU work.

Runtime setup owns installation and registry lifecycle. No authorization result or
raw source-by-year matrix is cached here; only one detached schema2 DTO survives.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import json
from time import monotonic
from uuid import uuid4

from .analysis_periods import resolve_analysis_periods
from .analysis_provider import (capture_energy_configuration,
                                async_read_energy_chunk,
                                async_require_energy_source_access)
from .composer import async_get_energy_metadata
from .const import DOMAIN
from .energy import _source_signature
from .energy_analysis import EnergyAnalysisAccumulator
from .energy_comparison_stream import PairedComparisonAccumulator
from .energy_comparisons import compare_daily_mean7


MAX_PENDING_ANALYSES = 4
CACHE_SECONDS = 300


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _finish(accumulator, context, provenance, comparisons=None):
    result = accumulator.finish()
    # The pure accumulator deliberately accepts only a small public context.
    # Add operational tokens/provenance before the authoritative final hash.
    result['context'].update(deepcopy(context))
    result['groups']['domain_provenance'] = provenance
    if comparisons is not None:
        result['comparisons'] = deepcopy(comparisons)
    result.pop('snapshot_id', None)
    result['snapshot_id'] = _digest(result)
    return result


class EnergyAnalysisService:
    def __init__(self, hass, entry_id, coordinator, registry, *,
                 configuration_manager=None, clock=None):
        self.hass, self.entry_id = hass, entry_id
        self.coordinator, self.registry = coordinator, registry
        self.storage = coordinator.storage
        self._manager = configuration_manager
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entry_lookup = getattr(getattr(hass, 'config_entries', None),
                                     'async_get_entry', None)
        self._entry = self._entry_lookup(entry_id) if callable(self._entry_lookup) else None
        self._nonce = uuid4().hex
        self._lock = asyncio.Lock()
        self._pending = 0
        self._stopped = False
        self._cache = None
        self._normalization = None

    def async_stop(self):
        """Synchronous unload fence; running CPU jobs retain their existing lane."""
        self._stopped = True
        self._cache = None

    def _check_running(self):
        bucket = self.hass.data.get(DOMAIN, {})
        if (self._stopped or not self.coordinator.running
                or bucket.get('energy_analysis') is not self
                or bucket.get('energy_snapshot') is not self.coordinator
                or bucket.get('storage') is not self.storage
                or self.coordinator.storage is not self.storage
                or callable(self._entry_lookup) and (
                    self._entry is None or self._entry_lookup(self.entry_id) is not self._entry)):
            self._cache = None
            raise RuntimeError('energy_snapshot_unloaded')

    def _capture(self, kind, anchor=None, comparison=None):
        self._check_running()
        configuration = capture_energy_configuration(self._manager.data or {})
        if not configuration['grid_sensors']:
            raise ValueError('energy_analysis_unavailable')
        zone = self.hass.config.time_zone
        resolved = resolve_analysis_periods(kind, self._clock(), zone, anchor=anchor)
        if resolved['status'] != 'ready':
            raise ValueError(resolved['reason'])
        if comparison is not None and (not isinstance(comparison, str) or comparison not in resolved['comparisons']):
            raise ValueError('invalid_analysis_comparison')
        period = {**resolved['current'], 'kind': kind}
        context = {'entry_id': self.entry_id, 'service_generation': self._nonce,
                   'tariff_revision': self.storage.current_tariff_revision,
                   'feature_access_revision': self.storage.current_feature_access_revision,
                   'registry_revision': self.registry.revision,
                   'measurement_timezone': zone,
                   'source_signature': configuration['source_signature'],
                   'period_kind': kind, 'period_signature': _digest(period),
                   'period_anchor': period['local_start'][:10] if anchor is not None else None}
        if comparison is not None:
            context.update(comparison_kind=comparison,
                           comparison_signature=_digest(resolved['comparisons'][comparison]))
        return configuration, period, context

    def _assert_current(self, context):
        _, _, current = self._capture(context['period_kind'], context.get('period_anchor'), context.get('comparison_kind'))
        if any(context.get(key) != value for key, value in current.items()):
            self._cache = None
            raise RuntimeError('energy_analysis_context_changed')
        if ('normalization_signature' in context
                and context['normalization_signature'] != self._normalization):
            self._cache = None
            raise RuntimeError('energy_analysis_context_changed')

    @staticmethod
    def _sensors(configuration):
        rows = [*configuration['grid_sensors'],
                *({'entity_id': row['stat_consumption']} for row in configuration['consumers'])]
        return list({row['entity_id']: row for row in rows}.values())

    async def _require_access(self, context, configuration, user_id, *, policy_state=None):
        state = policy_state if policy_state is not None else await self.storage.async_get_feature_access()
        self._assert_current(context)
        if state['revision'] != context['feature_access_revision']:
            raise RuntimeError('energy_analysis_context_changed')
        if state['status'] != 'ready':
            raise PermissionError('energy_feature_unauthorized')
        ids = [row['entity_id'] for row in self._sensors(configuration)]
        await async_require_energy_source_access(self.hass, user_id, ids, feature_policy=state['policy'])
        self._assert_current(context)

    async def _guard(self, context, configuration, user_id):
        """Nonrecursive fresh access + external Recorder normalization fence."""
        self._assert_current(context)
        sensors = self._sensors(configuration)
        ids = [row['entity_id'] for row in sensors]
        await self._require_access(context, configuration, user_id)
        self._assert_current(context)
        metadata = await async_get_energy_metadata(self.hass, ids)
        self._assert_current(context)
        signature = _digest(_source_signature(sensors, metadata))
        self._normalization = signature
        if ('normalization_signature' in context
                and context['normalization_signature'] != signature):
            self._cache = None
            raise RuntimeError('energy_analysis_context_changed')
        context['normalization_signature'] = signature
        # In particular, role revocation during metadata IO must not publish.
        await self._require_access(context, configuration, user_id)
        self._assert_current(context)

    async def _cpu(self, job, guard):
        await guard()
        result = await self.coordinator._async_run_cpu(job)
        await guard()
        return result

    async def async_get_analysis(self, kind, user_id, *, anchor=None, comparison=None):
        # Admission includes requests waiting for this service's own lock.
        self._check_running()
        if self._pending >= MAX_PENDING_ANALYSES:
            raise RuntimeError('energy_analysis_busy')
        self._pending += 1
        try:
            async with self._lock:
                self._check_running()
                if self._manager is None:
                    from homeassistant.components.energy.data import async_get_manager
                    self._manager = await async_get_manager(self.hass)
                    self._check_running()
                # Establish the policy revision before capturing the context.
                # Reading settings does not access Recorder, metadata or registry.
                policy_state = await self.storage.async_get_feature_access()
                self._check_running()
                configuration, period, context = self._capture(kind, anchor, comparison)
                # No metadata, Recorder or registry read precedes BOTH gates.
                await self._require_access(context, configuration, user_id, policy_state=policy_state)
                self._assert_current(context)
                state = await self.storage.async_get_tariff_book()
                self._check_running()
                # First storage load may establish a previously absent revision.
                if context['tariff_revision'] is None:
                    context['tariff_revision'] = state['revision']
                self._assert_current(context)
                if state['revision'] != context['tariff_revision']:
                    raise RuntimeError('energy_analysis_context_changed')
                guard = partial(self._guard, context, configuration, user_id)
                await guard()
                key = _digest(context)
                if self._cache is not None:
                    cached_key, at, cached = self._cache
                    if cached_key == key and 0 <= monotonic() - at < CACHE_SECONDS:
                        # guard already refreshed all source permissions and units.
                        result = deepcopy(cached)
                        self._assert_current(context)
                        return result
                    self._cache = None
                membership = deepcopy(self.registry.capture(
                    [row['stat_consumption'] for row in configuration['consumers']]))
                self._assert_current(context)
                accumulator = await self._cpu(partial(EnergyAnalysisAccumulator,
                    deepcopy(state['book']), deepcopy(period), deepcopy(context),
                    generated_at=self._clock(), consumers=deepcopy(configuration['consumers']),
                    membership=membership, consumer_domain=configuration['consumer_domain']), guard)
                plan, pair, comparisons, current_day = None, None, None, None
                if comparison is not None:
                    resolved = resolve_analysis_periods(kind, self._clock(), context['measurement_timezone'],
                                                       anchor=context['period_anchor'])
                    self._assert_current(context)
                    plan = resolved['comparisons'][comparison]
                    comparisons = {}
                    if plan['status'] == 'ready' and comparison != 'mean7':
                        pair = await self._cpu(partial(PairedComparisonAccumulator, period,
                            plan['baseline_periods'][0], timezone_name=context['measurement_timezone'],
                            calendar_kind={'day': 'daily', 'week': 'weekly', 'month': 'monthly', 'year': 'yearly'}[kind]), guard)
                windows = [period, plan['baseline_periods'][0]] if pair is not None else [period]
                cursors = [datetime.fromisoformat(p['start']) for p in windows]
                ends = [datetime.fromisoformat(p['end']) for p in windows]
                while True:
                    side = pair.needs_side() if pair is not None else ('current' if cursors[0] < ends[0] else None)
                    if side is None:
                        break
                    index = 0 if side == 'current' else 1
                    cursor, end = cursors[index], ends[index]
                    stop = min(cursor + timedelta(days=7), end)
                    await guard()
                    chunk = await async_read_energy_chunk(self.hass, configuration,
                                                          cursor, stop, guard=guard,
                                                          include_consumers=side == 'current')
                    await guard()
                    if side == 'current':
                        await self._cpu(partial(accumulator.add_chunk, chunk['grid'], cursor, stop,
                                               consumer_snapshot=chunk['consumers']), guard)
                        if comparison == 'mean7' and plan['status'] == 'ready':
                            current_day = chunk['grid']
                    if pair is not None:
                        await self._cpu(partial(pair.push, side, chunk['grid'], cursor, stop), guard)
                    # Do not retain a raw source matrix across chunk boundaries.
                    del chunk
                    cursors[index] = stop
                if comparison is not None:
                    if plan['status'] != 'ready':
                        compared = {'status': 'unknown', 'reason': plan['reason'], 'current_kwh': None,
                                    'baseline_kwh': None, 'delta_kwh': None, 'percent': None}
                    elif pair is not None:
                        compared = await self._cpu(pair.finish, guard)
                    else:
                        # Mean7 is daily only: at most eight actual 23/24/25h
                        # source windows, sharing one intersection across all8.
                        days = []
                        for baseline in plan['baseline_periods']:
                            a, b = (datetime.fromisoformat(baseline[key]) for key in ('start', 'end'))
                            await guard()
                            chunk = await async_read_energy_chunk(self.hass, configuration, a, b,
                                                                  guard=guard, include_consumers=False)
                            await guard()
                            days.append(chunk['grid'])
                            del chunk
                        compared = await self._cpu(partial(compare_daily_mean7, current_day, days,
                            period, plan['baseline_periods'], timezone_name=context['measurement_timezone']), guard)
                        del days, current_day
                    comparisons[comparison] = {**compared, 'baseline_kind': comparison,
                                               'baseline_periods': deepcopy(plan['baseline_periods'])}
                result = await self._cpu(partial(_finish, accumulator, deepcopy(context),
                                                configuration['domain_provenance'], comparisons), guard)
                # No await between this final check, detachment, caching and return.
                self._assert_current(context)
                self._cache = (key, monotonic(), deepcopy(result))
                return deepcopy(result)
        except BaseException:
            # Fail closed; a revoked request cannot leave a previous cached DTO.
            self._cache = None
            raise
        finally:
            self._pending -= 1

    def context_is_current(self, result):
        """Final no-await WS fence. Call async_validate_result after WS awaits."""
        try:
            context = result['context']
            self._assert_current(context)
            _, period, _ = self._capture(context['period_kind'], context.get('period_anchor'), context.get('comparison_kind'))
            return result['period'] == period
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            return False

    async def async_validate_result(self, result, user_id):
        """Reauthorize and refresh units after the endpoint awaited this service.

        Return False for stale contexts; actual authorization denial propagates.
        Call context_is_current synchronously immediately before sending the DTO.
        """
        if not self.context_is_current(result):
            return False
        if self._pending >= MAX_PENDING_ANALYSES:
            raise RuntimeError('energy_analysis_busy')
        self._pending += 1
        try:
            context = deepcopy(result['context'])
            configuration, _, _ = self._capture(context['period_kind'], context.get('period_anchor'), context.get('comparison_kind'))
            await self._guard(context, configuration, user_id)
            return self.context_is_current(result)
        except PermissionError:
            self._cache = None
            raise
        except (ValueError, RuntimeError):
            self._cache = None
            return False
        finally:
            self._pending -= 1
