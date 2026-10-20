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
from .native_energy_pricing import NativeCostAccumulator, project_current_prices
from .native_pricing_provider import async_read_native_cost_chunk
from .composer import async_get_energy_metadata
from .const import DOMAIN
from .energy import _source_signature
from .energy_analysis import EnergyAnalysisAccumulator
from .energy_measurements import measurement_evidence
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
                    result['period'].get('timezone'),
                    (result['period'].get('cadence') if result['period'].get('kind') == 'report_window'
                     else result['period'].get('kind')),
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


def _finish_native(snapshot, accumulator, configuration, states, observed_at):
    result = deepcopy(snapshot)
    result['native_pricing'] = accumulator.finish()
    result['native_pricing']['current_prices'] = project_current_prices(configuration, states, observed_at)
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
    'statistics': ('energy_analysis', 'read'),
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
        # Explicit test clocks/managers are not global native Repair producers.
        self._energy_repairs_realtime = clock is None and configuration_manager is None
        self._entry_lookup = getattr(getattr(hass, 'config_entries', None),
                                     'async_get_entry', None)
        self._entry = self._entry_lookup(entry_id) if callable(self._entry_lookup) else None
        self._nonce = uuid4().hex
        self._lock = asyncio.Lock()
        self._pending = 0
        self._stopped = False
        self._cache = None
        self._report_colors = None
        self._normalization = None
        self._native_normalization = None
        self._native_metadata = {}
        self._native_states = {}
        self._update_subscriptions = set()

    def async_stop(self):
        """Synchronous unload fence; running CPU jobs retain their existing lane."""
        self._stopped = True
        self._cache = None
        self._report_colors = None
        bucket = self.hass.data.get(DOMAIN, {})
        publisher = bucket.get('energy_statistics')
        if getattr(publisher, 'service', None) is self:
            bucket.pop('energy_statistics', None)
            publisher.stop()
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

    def _configuration(self):
        return capture_energy_configuration(self._manager.data or {},
            currency=getattr(self.hass.config, 'currency', None),
            cost_sensors=self.hass.data.get('energy', {}).get('cost_sensors', {}))

    def _capture(self, kind, anchor=None, comparison=None, *, purpose='analysis', report_model=False, report_mode='so_far', actor_signature=None):
        # Server-owned purpose; never accept a client-selected feature gate.
        if not isinstance(purpose, str) or purpose not in _ACCESS_PURPOSES:
            raise ValueError('invalid_analysis_purpose')
        self._check_running()
        configuration = self._configuration()
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
        if report_model:
            from .energy_inputs import report_clock
            timing = report_clock(self._clock(), zone)
            context.update(report_model=True, report_mode=report_mode,
                report_color_as_of=timing['as_of'], report_cutoff=timing['cutoff'],
                actor_signature=actor_signature)
        return configuration, period, context

    def _capture_patterns(self, days):
        self._check_running()
        window = resolve_patterns_window(days, self._clock(), self.hass.config.time_zone)
        if window['status'] != 'ready':
            raise ValueError(window['reason'])
        configuration = self._configuration()
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

    @staticmethod
    def _report_previous_plan(request, period, now):
        """Select only an actual full-calendar previous period, before private IO.

        Report kind stays report_window. The named kind below belongs solely to
        the existing calendar resolver and the baseline chart accumulator.
        """
        if request.semantics == 'rolling_completed_hours':
            return None
        kind = {'daily': 'day', 'weekly': 'week', 'monthly': 'month'}[request.cadence]
        resolved = resolve_analysis_periods(kind, now, request.timezone,
                                          anchor=period['local_start'][:10])
        current = resolved.get('current') or {}
        if any(current.get(key) != period[key] for key in ('start', 'end', 'timezone')):
            raise ValueError('report_comparison_plan_mismatch')
        plan = resolved.get('comparisons', {}).get('previous')
        if plan is None:
            plan = {'status': 'unknown', 'reason': resolved.get('reason') or 'comparison_unavailable',
                    'baseline_periods': []}
        # A ready plan must describe exactly one prior calendar window. The
        # resolver owns all DST/leap/fold alignment and existing period limits.
        if plan['status'] == 'ready':
            if len(plan['baseline_periods']) != 1:
                raise ValueError('report_comparison_plan_mismatch')
            prior = plan['baseline_periods'][0]
            if (prior.get('timezone') != request.timezone or prior.get('end') != period['start']
                    or prior.get('status') != 'ready' or prior.get('partial') is not False
                    or prior.get('calendar_end') != prior.get('end')):
                raise ValueError('report_comparison_plan_mismatch')
        return kind, deepcopy(plan)

    def _capture_report(self, request, purpose, actor_signature):
        if type(request) is not ReportWindow or purpose not in ('report_read','report_execute'):
            raise ValueError('invalid_report_request')
        self._check_running()
        now = self._clock()
        period = request.period(now)
        previous = self._report_previous_plan(request, period, now)
        configuration = self._configuration()
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
        if previous is not None:
            comparison_period_kind, plan = previous
            context.update(comparison_kind='previous', comparison_signature=_digest(plan),
                comparison_timezone=request.timezone, comparison_calendar_kind=request.cadence,
                comparison_period_kind=comparison_period_kind, report_comparison_plan=plan)
        return configuration, period, context

    def _capture_statistics(self, start, end, actor_signature):
        """Frozen internal completed-hour request, never a browser purpose flag."""
        self._check_running()
        start, end = datetime.fromisoformat(start), datetime.fromisoformat(end)
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError('aware_statistics_clock_required')
        if (any(value.utcoffset() is None or value.timestamp() % 3600 for value in (start, end))
                or not 0 < (end-start).total_seconds() <= 168*3600
                or end > now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)):
            raise ValueError('invalid_statistics_window')
        configuration = self._configuration()
        if not configuration['grid_sensors']:
            raise ValueError('energy_analysis_unavailable')
        period = {'kind': 'day', 'timezone': self.hass.config.time_zone,
                  'start': start.isoformat(), 'end': end.isoformat()}
        context = {'request_kind': 'statistics', 'access_purpose': 'statistics',
            'statistics_start': start.isoformat(), 'statistics_end': end.isoformat(),
            'actor_signature': actor_signature, 'entry_id': self.entry_id,
            'service_generation': self._nonce, 'tariff_revision': self.storage.current_tariff_revision,
            'feature_access_revision': self.storage.current_feature_access_revision,
            'registry_revision': self.registry.revision, 'measurement_timezone': self.hass.config.time_zone,
            'source_signature': configuration['source_signature'], 'period_signature': _digest(period)}
        return configuration, period, context

    def _recapture(self, context):
        if context.get('request_kind') == 'statistics':
            if context.get('access_purpose') != 'statistics':
                raise ValueError('invalid_statistics_purpose')
            return self._capture_statistics(context['statistics_start'], context['statistics_end'],
                                            context['actor_signature'])
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
                             context.get('comparison_kind'), purpose=context['access_purpose'],
                             report_model=context.get('report_model', False),
                             report_mode=context.get('report_mode', 'so_far'),
                             actor_signature=context.get('actor_signature'))

    def _assert_current(self, context):
        _, _, current = self._recapture(context)
        if any(context.get(key) != value for key, value in current.items()):
            self._cache = None
            raise RuntimeError('energy_analysis_context_changed')
        if ('normalization_signature' in context
                and context['normalization_signature'] != self._normalization):
            self._cache = None
            raise RuntimeError('energy_analysis_context_changed')

        if ('native_signature' in context
                and context['native_signature'] != self._native_normalization):
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
        if context.get('request_kind') == 'statistics':
            if type(user_id) is not str or context['actor_signature'] != self._actor_signature(user_id):
                raise PermissionError('statistics_native_admin_required')
            user = await self.hass.auth.async_get_user(user_id)
            self._assert_current(context)
            if user is None or not user.is_active or not user.is_admin:
                raise PermissionError('statistics_native_admin_required')
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
        if context.get('report_model') and context.get('actor_signature') != self._actor_signature(user_id):
            raise PermissionError('energy_report_actor_mismatch')
        ids = [row['entity_id'] for row in self._sensors(configuration)]
        feature, operation = _ACCESS_PURPOSES[context['access_purpose']]
        extra = {}
        if context.get('native_access'):
            extra['additional_source_ids'] = list(dict.fromkeys(source[key]
                for source in configuration['native_pricing']['sources']
                for key in ('cost_statistic_id', 'price_entity_id') if source[key] is not None))
        await async_require_energy_source_access(self.hass, user_id, ids,
            feature_policy=state['policy'], feature=feature, operation=operation, **extra)
        self._assert_current(context)

    async def _native_access(self, context, configuration, user_id):
        native = configuration['native_pricing']
        ids = list(dict.fromkeys(s[key] for s in native['sources']
            for key in ('cost_statistic_id', 'price_entity_id') if s[key] is not None))
        # Reuse the existing policy/actor machinery, independently of base energy.
        restricted = {**configuration, 'grid_sensors': [{'entity_id': sid} for sid in ids], 'consumers': []}
        try:
            await self._require_access(context, restricted, user_id)
            allowed = True
        except PermissionError:
            allowed = False
        self._assert_current(context)
        if 'native_access' in context and context['native_access'] != allowed:
            self._cache = None
            raise RuntimeError('energy_analysis_context_changed')
        context['native_access'] = allowed
        return allowed

    def _native_states_now(self, native):
        states = {}
        for source in native['sources']:
            sid = source['price_entity_id']
            if sid is None:
                continue
            state = self.hass.states.get(sid)
            states[sid] = None if state is None else {'state': state.state,
                'unit_of_measurement': state.attributes.get('unit_of_measurement'),
                'last_updated': state.last_updated.isoformat()}
        return states

    async def _guard_native(self, context, configuration, user_id):
        if not context.get('native_requested'):
            return
        if not await self._native_access(context, configuration, user_id):
            return
        native = configuration['native_pricing']
        ids = list(dict.fromkeys(s['cost_statistic_id'] for s in native['sources']
                                if s['cost_statistic_id'] is not None))
        metadata = await async_get_energy_metadata(self.hass, ids) if ids else {}
        self._assert_current(context)
        # Reauthorize after metadata awaits and before reading current states.
        await self._native_access(context, configuration, user_id)
        states = self._native_states_now(native)
        normalization = {}
        for sid in ids:
            meta = metadata.get(sid)
            if isinstance(meta, tuple) and len(meta) == 2:
                meta = meta[1]
            normalization[sid] = ({key: meta.get(key) for key in
                ('has_sum', 'unit_of_measurement', 'unit_class')} if isinstance(meta, dict) else None)
        signature = _digest({'metadata': normalization, 'states': states})
        if 'native_signature' in context and context['native_signature'] != signature:
            self._cache = None
            raise RuntimeError('energy_analysis_context_changed')
        self._native_normalization = signature
        context['native_signature'] = signature
        self._native_metadata, self._native_states = metadata, states
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
        await self._guard_native(context, configuration, user_id)
        if context.get('native_requested'):
            # Native IO introduced awaits after the previous base-source check.
            # Evaluate the union with one fresh principal as the final await.
            await self._require_access(context, configuration, user_id)
            self._assert_current(context)
            if context.get('native_access'):
                states = self._native_states_now(configuration['native_pricing'])
                if states != self._native_states:
                    self._cache = None
                    raise RuntimeError('energy_analysis_context_changed')

    async def _cpu(self, job, guard):
        await guard()
        result = await self.coordinator._async_run_cpu(job)
        await guard()
        return result

    async def async_get_analysis(self, kind, user_id, *, anchor=None, comparison=None, purpose='analysis', _report_window=None, _report_model=False, _report_mode='so_far'):
        if not isinstance(purpose, str) or purpose not in ('analysis', 'report_read', 'report_execute'):
            raise ValueError('invalid_analysis_purpose')
        if type(_report_model) is not bool or _report_mode not in ('so_far', 'full'):
            raise ValueError('invalid_report_model_request')
        if _report_model and (_report_window is not None or comparison is not None or kind not in ('day', 'week', 'month', 'year')):
            raise ValueError('invalid_report_model_request')
        if _report_model:
            self._actor_signature(user_id)
        if _report_window is not None:
            if type(_report_window) is not ReportWindow or comparison is not None or anchor is not None:
                raise ValueError('invalid_report_request')
            now = self._clock()
            report_period = _report_window.period(now)
            self._report_previous_plan(_report_window, report_period, now)
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
                    self._capture(kind, anchor, comparison, purpose=purpose,
                        report_model=_report_model, report_mode=_report_mode,
                        actor_signature=self._actor_signature(user_id) if _report_model else None))
                if _report_window is not None:
                    # Selection is owned by the validated frozen request, never
                    # by the public comparison argument (rejected above).
                    comparison = context.get('comparison_kind')
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
                if state['book'] is None and 'native_pricing' in configuration:
                    context['native_requested'] = True
                guard = partial(self._guard, context, configuration, user_id)
                await guard()
                key = _digest(context)
                if self._cache is not None:
                    cached_key, at, cached = self._cache
                    if cached_key == key and 0 <= monotonic() - at < (60 if _report_model else CACHE_SECONDS):
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
                    membership=membership, consumer_domain=configuration['consumer_domain'],
                    include_report_hours=_report_model), guard)
                plan, pair, comparisons, current_day, baseline_accumulator = None, None, None, None, None
                group_pair, mean_groups, mean_projections = None, None, []
                active_tariff = (next((row for row in state['book']['tariffs']
                    if row['id'] == state['book'].get('active_tariff_id')), None) if state['book'] else None)
                if comparison is not None:
                    if _report_window is not None:
                        plan = deepcopy(context['report_comparison_plan'])
                        comparison_timezone = context['comparison_timezone']
                        comparison_calendar_kind = context['comparison_calendar_kind']
                        comparison_period_kind = context['comparison_period_kind']
                    else:
                        resolved = resolve_analysis_periods(kind, self._clock(), context['measurement_timezone'],
                                                           anchor=context['period_anchor'])
                        plan = resolved['comparisons'][comparison]
                        comparison_timezone = context['measurement_timezone']
                        comparison_calendar_kind = {'day': 'daily', 'week': 'weekly', 'month': 'monthly', 'year': 'yearly'}[kind]
                        comparison_period_kind = kind
                    self._assert_current(context)
                    comparisons = {}
                    if plan['status'] == 'ready' and comparison != 'mean7':
                        pair = await self._cpu(partial(PairedComparisonAccumulator, period,
                            plan['baseline_periods'][0], timezone_name=comparison_timezone,
                            calendar_kind=comparison_calendar_kind,
                            tariff=active_tariff), guard)
                        group_pair = await self._cpu(partial(GroupComparisonAccumulator, period,
                            plan['baseline_periods'][0], calendar_plan=pair.calendar_plan,
                            tariff=active_tariff if configuration['consumer_domain'] == 'grid_import' else None), guard)
                        baseline_accumulator = await self._cpu(partial(EnergyAnalysisAccumulator,
                            deepcopy(state['book']), {**plan['baseline_periods'][0], 'kind': comparison_period_kind}, deepcopy(context),
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
                if context.get('native_access'):
                    native = configuration['native_pricing']
                    cost = await self._cpu(partial(NativeCostAccumulator, native, period, include_report_hours=_report_model), guard)
                    ids = list(dict.fromkeys(row['cost_statistic_id'] for row in native['sources']
                                            if row['cost_statistic_id'] is not None))
                    cursor, end = (datetime.fromisoformat(period[k]) for k in ('start', 'end'))
                    while cursor < end:
                        stop = min(cursor+timedelta(hours=168), end)
                        raw = await async_read_native_cost_chunk(self.hass, ids, cursor, stop, guard=guard)
                        await guard()
                        await self._cpu(partial(cost.add_chunk, raw, deepcopy(self._native_metadata), cursor, stop), guard)
                        del raw
                        cursor = stop
                    result = await self._cpu(partial(_finish_native, result, cost, native,
                        deepcopy(self._native_states), self._clock()), guard)
                if _report_model:
                    from .energy_inputs import async_collect_inputs
                    result['report_model'] = await async_collect_inputs(self, configuration,
                        period, context, accumulator, membership, state['book'], guard,
                        native=cost if context.get('native_access') else None)
                    # Include the opt-in flag and detached model in the final hash;
                    # preserve the legacy period for all validation consumers.
                    result['snapshot_id'] = _digest({k: v for k, v in result.items() if k != 'snapshot_id'})
                # No await between this final check, Repair publication, caching and return.
                self._assert_current(context)
                if self._energy_repairs_realtime:
                    from .energy_repairs import publish_analysis
                    publish_analysis(self, result)
                self._cache = (key, monotonic(), deepcopy(result))
                return deepcopy(result)
        except BaseException:
            # Fail closed; a revoked request cannot leave a previous cached DTO.
            self._cache = None
            raise
        finally:
            self._pending -= 1

    async def async_get_build1_model(self, kind, user_id, *, date=None, mode='so_far', purpose='analysis'):
        """Additive guarded model DTO; caller validates the outer result after awaits.

        WS should expose this through a new command (e.g. energy_model), keeping
        the existing energy_report language/timezone/hour_cycle HTML/text/CSV wire.
        The canonical model is result['report_model']; period/context remain the
        exact legacy validation envelope, not the model's full calendar period.
        """
        return await self.async_get_analysis(kind, user_id, anchor=date, purpose=purpose,
            _report_model=True, _report_mode=mode)

    async def async_get_statistics_hours(self, user_id, *, hours=1, end=None):
        """Native-admin internal producer; no cache, reports or background actor."""
        if type(hours) is not int or hours not in (1, 168) or type(user_id) is not str:
            raise ValueError('invalid_statistics_request')
        self._check_running()
        user = await self.hass.auth.async_get_user(user_id)
        self._check_running()
        if user is None or not user.is_active or not user.is_admin:
            raise PermissionError('statistics_native_admin_required')
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
                policy = await self.storage.async_get_feature_access()
                self._check_running()
                now = self._clock()
                if now.utcoffset() is None:
                    raise ValueError('aware_statistics_clock_required')
                latest = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                if end is None:
                    end = latest
                elif (type(end) is not datetime or end.utcoffset() is None
                        or end.timestamp() % 3600 or end > latest
                        or end-timedelta(hours=hours) < latest-timedelta(hours=168)):
                    raise ValueError('invalid_statistics_window')
                end = end.astimezone(timezone.utc)
                start = end-timedelta(hours=hours)
                configuration, period, context = self._capture_statistics(start.isoformat(), end.isoformat(),
                    self._actor_signature(user_id))
                await self._require_access(context, configuration, user_id, policy_state=policy)
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
                membership = deepcopy(self.registry.capture(
                    [row['stat_consumption'] for row in configuration['consumers']]))
                self._assert_current(context)
                accumulator = await self._cpu(partial(EnergyAnalysisAccumulator,
                    deepcopy(state['book']), deepcopy(period), deepcopy(context), generated_at=self._clock(),
                    consumers=deepcopy(configuration['consumers']), membership=membership,
                    consumer_domain=configuration['consumer_domain'], include_statistics_hours=True), guard)
                chunk = await async_read_energy_chunk(self.hass, configuration, start, end, guard=guard)
                await guard()
                def accept():
                    accumulator.add_chunk(chunk['grid'], start, end, consumer_snapshot=chunk['consumers'])
                    # Reuse verified interval evidence, never raw status/counts or prices.
                    snapshots = [chunk['grid']]
                    if configuration['consumers']:
                        snapshots.append(chunk['consumers'])
                    return all(measurement_evidence(snapshot, start, end)['coverage']['complete']
                               for snapshot in snapshots)
                measurement_ready = await self._cpu(accept, guard)
                del chunk
                def finish():
                    accumulator.finish()
                    facts = accumulator.statistics_hours()
                    facts['context'].update(deepcopy(context))
                    facts['measurement_ready'] = measurement_ready
                    facts['publication_policy_signature'] = _digest(policy['policy'])
                    facts['active_tariff_id'] = state['book'].get('active_tariff_id') if state['book'] else None
                    facts['area_labels'] = {row['area_id']: row['area_name']
                        for row in membership.get('sources', {}).values()
                        if isinstance(row.get('area_id'), str) and isinstance(row.get('area_name'), str)}
                    return facts
                facts = await self._cpu(finish, guard)
                self._assert_current(context)
                return facts
        finally:
            self._pending -= 1

    async def async_validate_statistics_hours(self, facts, user_id):
        """Repeat native author/source/purpose/context fences after caller awaits."""
        if type(user_id) is not str or facts.get('context', {}).get('request_kind') != 'statistics':
            raise PermissionError('invalid_statistics_actor')
        if self._pending >= MAX_PENDING_ANALYSES:
            raise RuntimeError('energy_analysis_busy')
        self._pending += 1
        try:
            context = deepcopy(facts['context'])
            configuration, _, _ = self._recapture(context)
            await self._guard(context, configuration, user_id)
            self._assert_current(context)
            return True
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
        comparisons = deepcopy(snapshot.get('comparisons', {}))
        previous = comparisons.get('previous')
        if previous is None:
            if request.semantics != 'rolling_completed_hours':
                raise RuntimeError('report_comparison_missing')
            legacy_comparison = {'status':'unknown','reason':'rolling_or_custom_window',
                                 'previous_kwh':None,'delta_kwh':None,'percent':None}
        else:
            legacy_comparison = {key:deepcopy(previous.get(key)) for key in
                ('status','reason','current_kwh','delta_kwh','percent','full_current_kwh',
                 'comparison_scope','coverage')}
            legacy_comparison.update(previous_kwh=previous.get('baseline_kwh'),
                                     full_previous_kwh=previous.get('full_baseline_kwh'))
            prior = previous.get('baseline_periods') or []
            if prior:
                for prefix, descriptor in (('current',period),('previous',prior[0])):
                    for key in ('start','end','local_start','local_end','hours'):
                        legacy_comparison[prefix+'_'+key] = descriptor.get(key)
                legacy_comparison.update(timezone=snapshot['context']['comparison_timezone'],
                    calendar_kind=snapshot['context']['comparison_calendar_kind'],
                    partial=period.get('partial'), calendar_clamped=prior[0].get('calendar_clamped'))
        summary = {'snapshot_id':snapshot['snapshot_id'],'schema':2,
            'cadence':request.cadence,'total_kwh':snapshot['totals']['kwh'],
            'total_cost':snapshot['totals']['total_cost'],
            'currency':snapshot['totals']['currency'],'status':snapshot['status'],
            'period':{**period,'from':period['start'],'to':period['end']},
            'totals':deepcopy(snapshot['totals']),'coverage':deepcopy(snapshot['coverage']),
            'groups':deepcopy(snapshot.get('groups',{})),'chart':deepcopy(snapshot['chart']),
            'comparisons':comparisons,'comparison':legacy_comparison,
            'source':{'name':'recorder','kind':'grid_import','status':snapshot['status'],
                      'complete':snapshot['coverage']['complete']}}
        if 'native_pricing' in snapshot:
            summary['native_pricing'] = deepcopy(snapshot['native_pricing'])
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
