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
from .report_window import ReportWindow
from .report_actor import _ScheduleActor
from .feature_access import validate_feature_access
from .energy_patterns_periods import resolve_patterns_window
from .analysis_provider import (capture_energy_configuration,
                                async_read_energy_chunk,
                                async_require_energy_source_access)
from .composer import async_get_energy_metadata
from .const import DOMAIN
from .energy import _source_signature
from .energy_analysis import EnergyAnalysisAccumulator
from .energy_comparison_stream import PairedComparisonAccumulator
from .energy_comparison_chart import unknown_comparison_chart
from .energy_comparisons import compare_daily_mean7
from .energy_group_comparisons import compare_period_groups
from .energy_group_stream import GroupComparisonAccumulator
from .energy_group_mean7 import compare_daily_group_mean7


MAX_PENDING_ANALYSES = 4
CACHE_SECONDS = 300


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _finish(accumulator, context, provenance, comparisons=None, baseline_accumulator=None, group_pair=None, mean_groups=None):
    result = accumulator.finish()
    # The pure accumulator deliberately accepts only a small public context.
    # Add operational tokens/provenance before the authoritative final hash.
    result['context'].update(deepcopy(context))
    result['groups']['domain_provenance'] = provenance
    if comparisons is not None:
        result['comparisons'] = deepcopy(comparisons)
        for comparison in result['comparisons'].values():
            if 'chart' not in comparison:
                comparison['chart'] = unknown_comparison_chart(
                    result['period'].get('timezone'), result['period'].get('kind'),
                    comparison.get('reason') or 'comparison_projection_unavailable',
                    comparison.get('comparison_scope'))
        if baseline_accumulator is not None:
            baseline = baseline_accumulator.finish()
            baseline['groups']['domain_provenance'] = provenance
            summary = {key: deepcopy(baseline[key]) for key in (
                'schema', 'generated_at', 'context', 'period', 'coverage', 'status', 'pricing_status',
                'totals', 'groups', 'chart', 'rates', 'fees', 'rounding', 'active_tariff_id', 'measurement_fingerprint')}
            summary['snapshot_id'] = _digest(summary)
            comparison = result['comparisons'][context['comparison_kind']]
            comparison['baseline_analysis'] = summary
            groups = compare_period_groups(result['groups'], baseline['groups'], comparison)
            groups['reconciles_household_cost_delta'] = False
            if group_pair is not None:
                streamed = group_pair.finish(comparison)
                # A proven partial cost allocation includes every row, even
                # structural zero partitions whose old energy totals are full.
                # Complete calendar groups retain ConsumerPeriod allocation.
                adjusted_costs = streamed['reconciles_household_cost_delta']
                groups['reconciles_household_cost_delta'] = adjusted_costs
                for level in ('devices', 'areas', 'floors'):
                    for key, row in groups[level].items():
                        if adjusted_costs or row['reason'] == 'group_common_measurements_required':
                            groups[level][key] = streamed[level][key]
                for key in ('unassigned', 'unmeasured'):
                    if adjusted_costs or groups[key]['reason'] == 'group_common_measurements_required':
                        groups[key] = streamed[key]
                if not groups['reconciles_household_delta']:
                    groups['reconciles_household_delta'] = streamed['reconciles_household_delta']
            comparison['groups'] = groups
        if mean_groups is not None:
            groups = deepcopy(mean_groups)
            groups['membership_as_of'] = result['groups'].get('captured_at')
            groups['domain_provenance'] = provenance
            result['comparisons']['mean7']['groups'] = groups
    result.pop('snapshot_id', None)
    result['snapshot_id'] = _digest(result)
    return result


def _finish_patterns(accumulator, context):
    result = accumulator.finish()
    result['context'].update(deepcopy(context))
    result.pop('snapshot_id', None)
    result['snapshot_id'] = _digest(result)
    return result


def _project_comparison_day(period, context, consumers, membership, domain, grid, readings, generated_at):
    """Project one bounded day without finishing or duplicating baseline DTOs."""
    target = EnergyAnalysisAccumulator(None, {**period, 'kind': 'day'}, context,
        generated_at=generated_at, consumers=consumers, membership=membership,
        consumer_domain=domain)
    return target.add_chunk(grid, period['start'], period['end'],
        consumer_snapshot=readings, include_group_hours=True)


_ACCESS_PURPOSES = {
    'analysis': ('energy_analysis', 'read'),
    'report_read': ('energy_reports', 'read'),
    'report_execute': ('energy_reports', 'execute'),
}


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
        self._update_subscriptions = set()

    def async_stop(self):
        """Synchronous unload fence; running CPU jobs retain their existing lane."""
        self._stopped = True
        self._cache = None
        for unsubscribe in tuple(self._update_subscriptions):
            unsubscribe()

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

    def _capture(self, kind, anchor=None, comparison=None, *, purpose='analysis'):
        # Server-owned purpose; never accept a client-selected feature gate.
        if not isinstance(purpose, str) or purpose not in _ACCESS_PURPOSES:
            raise ValueError('invalid_analysis_purpose')
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
        context = {'access_purpose': purpose, 'entry_id': self.entry_id, 'service_generation': self._nonce,
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

    def _capture_patterns(self, days):
        self._check_running()
        window = resolve_patterns_window(days, self._clock(), self.hass.config.time_zone)
        if window['status'] != 'ready':
            raise ValueError(window['reason'])
        configuration = capture_energy_configuration(self._manager.data or {})
        if not configuration['grid_sensors']:
            raise ValueError('energy_analysis_unavailable')
        context = {'request_kind': 'patterns', 'access_purpose': 'analysis',
                   'patterns_days': days, 'window_signature': _digest(window),
                   'entry_id': self.entry_id, 'service_generation': self._nonce,
                   'tariff_revision': self.storage.current_tariff_revision,
                   'feature_access_revision': self.storage.current_feature_access_revision,
                   'registry_revision': self.registry.revision,
                   'measurement_timezone': window['timezone'],
                   'source_signature': configuration['source_signature']}
        return configuration, window, context

    def _schedule_actor(self, schedule, generation):
        actor = _ScheduleActor(self, schedule, generation)
        actor.check()
        return actor

    @staticmethod
    def _actor_signature(actor):
        if type(actor) is _ScheduleActor:
            actor.check()
            return _digest({'schedule_capability': actor.token})
        if type(actor) is not str or not actor or len(actor) > 128:
            raise PermissionError('energy_report_actor_required')
        return _digest({'authenticated_user': actor})

    def _capture_report(self, request, purpose, actor_signature):
        if type(request) is not ReportWindow or purpose not in ('report_read','report_execute'):
            raise ValueError('invalid_report_request')
        self._check_running()
        period = request.period(self._clock())
        configuration = capture_energy_configuration(self._manager.data or {})
        if not configuration['grid_sensors']:
            raise ValueError('energy_analysis_unavailable')
        context = {'request_kind':'report_window','access_purpose':purpose,
            'report_request':request.request(),'actor_signature':actor_signature,
            'entry_id':self.entry_id,'service_generation':self._nonce,
            'tariff_revision':self.storage.current_tariff_revision,
            'feature_access_revision':self.storage.current_feature_access_revision,
            'registry_revision':self.registry.revision,
            'measurement_timezone':self.hass.config.time_zone,
            'source_signature':configuration['source_signature'],
            'period_signature':_digest(period)}
        return configuration, period, context

    def _recapture(self, context):
        if context.get('request_kind') == 'report_window':
            return self._capture_report(ReportWindow(**context['report_request']),
                context['access_purpose'],context['actor_signature'])
        if context.get('request_kind') == 'patterns':
            if context.get('access_purpose') != 'analysis':
                raise ValueError('invalid_analysis_purpose')
            return self._capture_patterns(context['patterns_days'])
        if context.get('request_kind') is not None:
            raise ValueError('invalid_analysis_request_kind')
        return self._capture(context['period_kind'], context.get('period_anchor'),
                             context.get('comparison_kind'), purpose=context['access_purpose'])

    def _assert_current(self, context):
        _, _, current = self._recapture(context)
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
        if context.get('request_kind') == 'report_window':
            if context['actor_signature'] != self._actor_signature(user_id):
                raise PermissionError('energy_report_actor_mismatch')
            if type(user_id) is _ScheduleActor:
                if user_id.service is not self or context['access_purpose'] != 'report_execute':
                    raise PermissionError('energy_report_actor_mismatch')
                user_id.check()
                author = user_id.schedule.get('author_user_id')
                if author is None:
                    # Explicit server principal, not individual native entity access.
                    # Only validated legacy admin defaults permit authorless automation.
                    policy = validate_feature_access(state['policy'])
                    mode = policy['features'].get('energy_reports',{}).get('execute',{'mode':'inherit'})['mode']
                    if mode not in ('inherit','admin'):
                        raise PermissionError('energy_schedule_author_required')
                    user_id.check()
                    self._assert_current(context)
                    return
                user_id = author
            if context['access_purpose'] == 'report_execute':
                user = await self.hass.auth.async_get_user(user_id)
                if user is None or not user.is_active or not user.is_admin:
                    raise PermissionError('energy_report_admin_required')
                self._assert_current(context)
        ids = [row['entity_id'] for row in self._sensors(configuration)]
        feature, operation = _ACCESS_PURPOSES[context['access_purpose']]
        await async_require_energy_source_access(self.hass, user_id, ids,
            feature_policy=state['policy'], feature=feature, operation=operation)
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

    async def async_get_analysis(self, kind, user_id, *, anchor=None, comparison=None, purpose='analysis', _report_window=None):
        if not isinstance(purpose, str) or purpose not in _ACCESS_PURPOSES:
            raise ValueError('invalid_analysis_purpose')
        if _report_window is not None:
            if type(_report_window) is not ReportWindow or comparison is not None or anchor is not None:
                raise ValueError('invalid_report_request')
            _report_window.period(self._clock())
            self._actor_signature(user_id)
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
                configuration, period, context = (
                    self._capture_report(_report_window,purpose,self._actor_signature(user_id))
                    if _report_window is not None else
                    self._capture(kind, anchor, comparison, purpose=purpose))
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
                plan, pair, comparisons, current_day, baseline_accumulator = None, None, None, None, None
                group_pair, mean_groups, mean_projections = None, None, []
                active_tariff = (next((row for row in state['book']['tariffs']
                    if row['id'] == state['book'].get('active_tariff_id')), None) if state['book'] else None)
                if comparison is not None:
                    resolved = resolve_analysis_periods(kind, self._clock(), context['measurement_timezone'],
                                                       anchor=context['period_anchor'])
                    self._assert_current(context)
                    plan = resolved['comparisons'][comparison]
                    comparisons = {}
                    if plan['status'] == 'ready' and comparison != 'mean7':
                        pair = await self._cpu(partial(PairedComparisonAccumulator, period,
                            plan['baseline_periods'][0], timezone_name=context['measurement_timezone'],
                            calendar_kind={'day': 'daily', 'week': 'weekly', 'month': 'monthly', 'year': 'yearly'}[kind],
                            tariff=active_tariff), guard)
                        group_pair = await self._cpu(partial(GroupComparisonAccumulator, period,
                            plan['baseline_periods'][0], calendar_plan=pair.calendar_plan,
                            tariff=active_tariff if configuration['consumer_domain'] == 'grid_import' else None), guard)
                        baseline_accumulator = await self._cpu(partial(EnergyAnalysisAccumulator,
                            deepcopy(state['book']), {**plan['baseline_periods'][0], 'kind': kind}, deepcopy(context),
                            generated_at=self._clock(), consumers=deepcopy(configuration['consumers']),
                            membership=membership, consumer_domain=configuration['consumer_domain']), guard)
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
                                                          include_consumers=True)
                    await guard()
                    target = accumulator if side == 'current' else baseline_accumulator
                    projection = await self._cpu(partial(target.add_chunk, chunk['grid'], cursor, stop,
                        consumer_snapshot=chunk['consumers'], include_group_hours=group_pair is not None or (comparison == 'mean7' and plan['status'] == 'ready')), guard)
                    if side == 'current' and comparison == 'mean7' and plan['status'] == 'ready':
                        current_day = chunk['grid']
                        mean_projections.append(projection)
                    if pair is not None:
                        await self._cpu(partial(pair.push, side, chunk['grid'], cursor, stop), guard)
                        await self._cpu(partial(group_pair.push, side, projection, cursor, stop), guard)
                    # Do not retain a raw source matrix across chunk boundaries.
                    del chunk, projection
                    cursors[index] = stop
                if comparison is not None:
                    if plan['status'] != 'ready':
                        compared = {'status': 'unknown', 'reason': plan['reason'], 'current_kwh': None,
                                    'baseline_kwh': None, 'delta_kwh': None, 'percent': None}
                    elif pair is not None:
                        compared = await self._cpu(pair.finish, guard)
                    else:
                        # Mean7 is daily only: eight bounded civil-day windows,
                        # including multi-hour rollbacks, sharing one mask.
                        days = []
                        for baseline in plan['baseline_periods']:
                            a, b = (datetime.fromisoformat(baseline[key]) for key in ('start', 'end'))
                            await guard()
                            chunk = await async_read_energy_chunk(self.hass, configuration, a, b,
                                                                  guard=guard, include_consumers=True)
                            await guard()
                            days.append(chunk['grid'])
                            projected = await self._cpu(partial(_project_comparison_day,
                                deepcopy(baseline), deepcopy(context), deepcopy(configuration['consumers']),
                                membership, configuration['consumer_domain'], chunk['grid'], chunk['consumers'],
                                self._clock()), guard)
                            mean_projections.append(projected)
                            del chunk, projected
                        compared = await self._cpu(partial(compare_daily_mean7, current_day, days,
                            period, plan['baseline_periods'], timezone_name=context['measurement_timezone'],
                            tariff=active_tariff), guard)
                        mean_groups = await self._cpu(partial(compare_daily_group_mean7, mean_projections,
                            [period, *plan['baseline_periods']], timezone_name=context['measurement_timezone'],
                            tariff=active_tariff, household_comparison=compared), guard)
                        del days, current_day, mean_projections
                    comparisons[comparison] = {**compared, 'baseline_kind': comparison,
                                               'baseline_periods': deepcopy(plan['baseline_periods'])}
                result = await self._cpu(partial(_finish, accumulator, deepcopy(context),
                                                configuration['domain_provenance'], comparisons, baseline_accumulator, group_pair, mean_groups), guard)
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

    async def async_get_patterns(self, days, user_id):
        """Rolling local-date household patterns on the existing guarded lane."""
        if type(days) is not int or days not in (7, 30):
            raise ValueError('invalid_patterns_days')
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
                policy_state = await self.storage.async_get_feature_access()
                self._check_running()
                configuration, window, context = self._capture_patterns(days)
                await self._require_access(context, configuration, user_id, policy_state=policy_state)
                self._assert_current(context)
                state = await self.storage.async_get_tariff_book()
                self._check_running()
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
                    if cached_key == key and 0 <= monotonic()-at < CACHE_SECONDS:
                        result = deepcopy(cached)
                        self._assert_current(context)
                        return result
                    self._cache = None
                accumulator = await self._cpu(partial(EnergyAnalysisAccumulator,
                    deepcopy(state['book']), deepcopy(window), deepcopy(context),
                    generated_at=self._clock(), patterns_window=deepcopy(window)), guard)
                cursor, end = (datetime.fromisoformat(window[field]) for field in ('start', 'end'))
                while cursor < end:
                    stop = min(cursor+timedelta(hours=168), end)
                    await guard()
                    chunk = await async_read_energy_chunk(self.hass, configuration, cursor, stop,
                                                          guard=guard, include_consumers=False)
                    await guard()
                    await self._cpu(partial(accumulator.add_chunk, chunk['grid'], cursor, stop), guard)
                    del chunk
                    cursor = stop
                result = await self._cpu(partial(_finish_patterns, accumulator, deepcopy(context)), guard)
                # No await after this final fence before detached cache/return.
                self._assert_current(context)
                self._cache = (key, monotonic(), deepcopy(result))
                return deepcopy(result)
        except BaseException:
            self._cache = None
            raise
        finally:
            self._pending -= 1

    async def async_prepare_report(self, request, actor, *, purpose, language,
                                   timezone_name, hour_cycle, date_language=None):
        """One authorized snapshot/render artifact; CSV is download data only."""
        from .energy_report import render_energy_analysis, validate_report_preferences
        options = {'language':language,'timezone_name':timezone_name,'hour_cycle':hour_cycle}
        if date_language is not None:
            options['date_language'] = date_language
        validate_report_preferences(**options)
        if purpose not in ('report_read','report_execute'):
            raise ValueError('invalid_report_purpose')
        if type(request) is not ReportWindow:
            raise ValueError('invalid_report_request')
        request.period(self._clock())
        snapshot = await self.async_get_analysis('report_window',actor,purpose=purpose,_report_window=request)
        if not await self.async_validate_result(snapshot,actor):
            raise RuntimeError('energy_analysis_context_changed')
        report = await self.coordinator._async_run_cpu(partial(render_energy_analysis,snapshot,**options))
        if not await self.async_validate_result(snapshot,actor) or not self.context_is_current(snapshot):
            raise RuntimeError('energy_analysis_context_changed')
        # Legacy aliases are references to producer values, never another price pass.
        period = deepcopy(snapshot['period'])
        summary = {'snapshot_id':snapshot['snapshot_id'],'schema':2,
            'cadence':request.cadence,'total_kwh':snapshot['totals']['kwh'],
            'total_cost':snapshot['totals']['total_cost'],
            'currency':snapshot['totals']['currency'],'status':snapshot['status'],
            'period':{**period,'from':period['start'],'to':period['end']},
            'totals':deepcopy(snapshot['totals']),'coverage':deepcopy(snapshot['coverage']),
            'groups':deepcopy(snapshot.get('groups',{})),'chart':deepcopy(snapshot['chart']),
            'source':{'name':'recorder','kind':'grid_import','status':snapshot['status'],
                      'complete':snapshot['coverage']['complete']}}
        return {**report,'subject':'Energy report · '+request.cadence,'body':report['text'],
            'summary':summary,'period':period,'context':deepcopy(snapshot['context'])}

    async def async_get_report(self, kind, user_id, *, language, timezone_name,
                               hour_cycle, anchor=None, comparison=None, date_language=None):
        """Render the same authorized calculation without requiring SMTP."""
        from .energy_report import render_energy_analysis, validate_report_preferences
        date_options = {} if date_language is None else {'date_language': date_language}
        validate_report_preferences(language, timezone_name, hour_cycle, **date_options)
        snapshot = await self.async_get_analysis(kind, user_id, anchor=anchor,
            comparison=comparison, purpose='report_read')
        if not await self.async_validate_result(snapshot, user_id):
            raise RuntimeError('energy_analysis_context_changed')
        report = await self.coordinator._async_run_cpu(partial(render_energy_analysis,
            snapshot, language=language, timezone_name=timezone_name, hour_cycle=hour_cycle,
            **date_options))
        # Renderer awaits the shared CPU lane. Reauthorize concrete sources and
        # this independent report purpose before releasing any rendered data.
        if not await self.async_validate_result(snapshot, user_id):
            raise RuntimeError('energy_analysis_context_changed')
        if not self.context_is_current(snapshot):
            raise RuntimeError('energy_analysis_context_changed')
        return {**report, 'context': deepcopy(snapshot['context']),
                'period': deepcopy(snapshot['period']),
                'display': {'language': language, 'timezone': timezone_name,
                            'hour_cycle': hour_cycle, **date_options}}

    def context_is_current(self, result):
        """Final no-await WS fence. Call async_validate_result after WS awaits."""
        try:
            context = result['context']
            self._assert_current(context)
            _, period, _ = self._recapture(context)
            if context.get('request_kind') == 'patterns':
                return (result.get('kind') == 'patterns' and result.get('window') == period
                        and context.get('access_purpose') == 'analysis')
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
            configuration, _, _ = self._recapture(context)
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
