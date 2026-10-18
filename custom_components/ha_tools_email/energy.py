"""Bounded, read-only Energy snapshot shared by household UI and entities.

Only EnergyTariff prices measurements. This module never renders or sends mail.
The public snapshot contains no meter IDs, names, or persisted profile content.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite
from functools import partial
from bisect import bisect_right
import json
from math import ceil
from zoneinfo import ZoneInfo

from .composer import async_grid_sensors, async_get_energy_usage, async_get_energy_metadata
from .tariff import EnergyTariff, validate_tariff_profile, price_measurements, _stamp

_OVERRIDE_UNSET = object()


def measurement_window(now, measurement_timezone):
    """HA-local midnight through the last completed absolute Recorder hour."""
    local = now.astimezone(ZoneInfo(measurement_timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    aligned = start.timestamp() % 3600 == 0 and end >= start
    return start, end, aligned


def next_cheap(model, now, horizon=timedelta(days=8)):
    """Find a strictly future cheap segment, never claim beyond the horizon.

    Civil schedules change on minute boundaries; dynamic prices may change at
    arbitrary milliseconds. Enumerating both also preserves DST folds and gaps.
    """
    start = int(now.timestamp() * 1000)
    end = int((now + horizon).timestamp() * 1000)
    result = {'start': None, 'status': 'unknown', 'reason': None,
              'searched_until': datetime.fromtimestamp(end / 1000, timezone.utc).isoformat()}
    previous = model.rate_at(start)
    if not previous['available'] or previous.get('classification') is None:
        result['reason'] = previous.get('reason', 'classification_unknown')
        return result
    points = set(range((start // 60000 + 1) * 60000, end + 1, 60000))
    points.update(t for t in model._boundaries if start < t <= end)
    points.add(end)
    previous_cheap = previous['classification'] == 'cheap'
    for point in sorted(points):
        row = model.rate_at(point)
        if not row['available'] or row.get('classification') is None:
            result['reason'] = row.get('reason', 'classification_unknown')
            return result
        cheap = row['classification'] == 'cheap'
        if cheap and not previous_cheap:
            result.update(start=datetime.fromtimestamp(point / 1000, timezone.utc).isoformat(), status='known')
            return result
        previous_cheap = cheap
    result.update(status='not_found_in_horizon', reason='bounded_horizon')
    return result


def build_snapshot(state, measurement, *, now, measurement_timezone, profile_source='shared'):
    profile = state.get('profile')
    model = EnergyTariff(profile)
    current_rate = model.rate_at(int(now.timestamp() * 1000))
    available = current_rate['available']
    classification = current_rate.get('classification') if available else None
    energy = current_rate.get('energyRate') if available else None
    variable = current_rate.get('variableRate') if available else None
    start, end, aligned = measurement_window(now, measurement_timezone)
    costs = price_measurements(profile, measurement, start, end, aligned)
    usage_rate = energy + variable if energy is not None and variable is not None else None
    if usage_rate is not None and not isfinite(usage_rate):
        usage_rate = None
    current = {'at': now.astimezone(timezone.utc).isoformat(), 'available': available,
        'reason': current_rate.get('reason', None), 'zone_id': current_rate.get('zone') if available else None,
        'zone_class': classification, 'cheap': None if classification is None else classification == 'cheap',
        'energy_rate': energy, 'variable_rate': variable,
        'usage_rate': usage_rate}
    issues = [{'reason': row['reason']} for row in costs['issues'][:32]]
    if not aligned:
        issues.insert(0, {'reason': 'calendar_boundary_not_recorder_hour'})
    if measurement.get('status') != 'ready':
        issues.insert(0, {'reason': 'measurement_incomplete'})
    coverage = {key: value for key, value in measurement.get('coverage', {}).items()
                if key in ('complete', 'measured_source_hours', 'expected_source_hours', 'sources',
                           'complete_sources', 'unavailable_sources')}
    # Schema extensions are retained in storage, never blindly reflected to UI.
    clock = {'mode': model.clock['mode']} if model.valid else None
    if clock and clock['mode'] == 'fixed':
        clock['offsetMinutes'] = model.clock['offsetMinutes']
    return {'schema': 1, 'generated_at': current['at'], 'tariff_revision': state.get('revision'),
        'profile_source': profile_source,
        'status': 'ready' if available and costs['complete'] else 'partial' if available else 'unavailable',
        'currency': model.profile.get('currency') if model.valid else None,
        'price_basis': model.profile.get('priceBasis') if model.valid else None,
        'measurement_timezone': measurement_timezone,
        'tariff_timezone': model.profile.get('timezone') if model.valid else None,
        'tariff_clock': clock,
        'current': current, 'next_cheap': next_cheap(model, now),
        'today': {'from': start.isoformat(), 'to': end.isoformat(), 'boundary_complete': aligned,
            'measurement_complete': measurement.get('status') == 'ready' and aligned,
            'tariff_complete': costs['complete'], 'measurement_status': measurement.get('status', 'no_data'),
            'coverage': coverage, 'energy_cost': costs['energyCost'], 'variable_cost': costs['variableCost'],
            'usage_cost': costs['usageCost'], 'fixed_monthly': costs['fixedMonthly'], 'issues': issues}}


def _source_signature(sensors, metadata):
    """Include Recorder normalization for external statistics without HA states."""
    result = []
    for row in sensors:
        sid = row['entity_id']
        raw = metadata.get(sid)
        meta = raw[1] if isinstance(raw, tuple) else raw
        meta = meta if isinstance(meta, dict) else {}
        result.append((sid, meta.get('has_sum'), meta.get('unit_of_measurement'), meta.get('unit_class')))
    return tuple(result)


MAX_PRICE_QUERIES = 64
MAX_PRICE_BUCKETS = 2048
MAX_PRICE_JSON_BYTES = 512 * 1024
MAX_PRICE_MINUTES = 2048 * 60
MAX_PRICE_WORK = 2_000_000
MAX_PENDING_PRICE_JOBS = 4


def _prepare_price_queries(queries, profile):
    """Bound JSON and the actual minute/rule-interval work before CPU dispatch."""
    try:
        size = len(json.dumps({'queries': queries, 'profile': profile},
                              ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError('invalid_energy_price_queries') from None
    if size > MAX_PRICE_JSON_BYTES or not isinstance(queries, list) or not 1 <= len(queries) <= MAX_PRICE_QUERIES:
        raise ValueError('invalid_energy_price_queries')
    model = EnergyTariff(profile)
    # rate_at can examine every rule and every interval, including exceptions.
    weight = 1 + sum(max(1, len(rule['intervals'])) for rule in model.rules + model.exceptions)
    count = minutes = work = 0
    for query in queries:
        if not isinstance(query, dict) or not isinstance(query.get('buckets'), list):
            raise ValueError('invalid_energy_price_queries')
        if 'coverage' in query and not isinstance(query['coverage'], dict):
            raise ValueError('invalid_energy_price_queries')
        rows = query['buckets']
        count += len(rows)
        if count > MAX_PRICE_BUCKETS:
            raise ValueError('invalid_energy_price_queries')
        for raw in rows:
            row = raw if isinstance(raw, dict) else {}
            start, end = _stamp(row.get('from')), _stamp(row.get('to'))
            # Invalid/oversized intervals return unknown immediately in the
            # existing model. They do not create an unbounded checkpoint scan.
            checkpoints = 1
            if start is not None and end is not None and 0 < end - start <= 86400000:
                span = ceil((end - start) / 60000)
                minutes += span
                checkpoints += span + 1
                checkpoints += bisect_right(model._boundaries, end - 1) - bisect_right(model._boundaries, start)
            work += checkpoints * weight
            if minutes > MAX_PRICE_MINUTES or work > MAX_PRICE_WORK:
                raise ValueError('invalid_energy_price_queries')
    return model, deepcopy(queries)


def _price_queries(model, queries, revision, profile_source):
    results = []
    for query in queries:
        priced = model.price_buckets(query['buckets'], query.get('coverage'))
        # Keep the exact model result, except unknown clock extension fields are
        # deliberately withheld from the public API.
        clock = {'mode': model.clock['mode']} if model.valid else {}
        if clock.get('mode') == 'fixed':
            clock['offsetMinutes'] = model.clock['offsetMinutes']
        priced['clock'] = clock
        results.append(priced)
    return {'schema': 1, 'tariff_revision': revision,
            'profile_source': profile_source, 'results': results}


class EnergySnapshotCoordinator:
    """Serialize refreshes; all five entities read one bounded public snapshot."""

    def __init__(self, hass, storage, *, provider=None):
        self.hass, self.storage, self.provider = hass, storage, provider
        self.data = None
        self._lock = asyncio.Lock()
        self._cpu_lock = asyncio.Lock()
        self._pricing_pending = 0
        self._measurement = None
        self._measurement_key = None
        self._measurement_at = None
        self._listeners = set()
        self._stopped = False
        self._unsub = None
        self._boundary_unsub = None
        self._model_boundaries = []

    @property
    def running(self):
        return not self._stopped

    def tariff_context_is_current(self, result):
        """No-await guard for the final WS send after authorization IO."""
        if self._stopped:
            return False
        if result.get('profile_source') == 'shared':
            return self.storage.current_tariff_revision == result.get('tariff_revision')
        return True

    async def async_price_queries(self, queries, *, profile=_OVERRIDE_UNSET):
        """Price card-supplied intervals; never read Recorder or update entities."""
        self._check_running()
        if self._pricing_pending >= MAX_PENDING_PRICE_JOBS:
            raise RuntimeError('energy_pricing_busy')
        self._pricing_pending += 1
        try:
            async with self._lock:
                self._check_running()
                shared = profile is _OVERRIDE_UNSET
                if shared:
                    state = await self.storage.async_get_tariff()
                    clean, revision = state['profile'], state['revision']
                else:
                    clean, revision = validate_tariff_profile(profile), None
                self._check_running()
                model, bounded = _prepare_price_queries(queries, clean)
                result = await self._async_run_cpu(partial(_price_queries, model, bounded, revision,
                                                          'shared' if shared else 'override'))
                self._check_running()
                if shared:
                    current = await self.storage.async_get_tariff()
                    self._check_running()
                    if current['revision'] != revision:
                        raise RuntimeError('energy_tariff_changed')
                return result
        finally:
            self._pricing_pending -= 1

    def async_add_listener(self, listener):
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    async def async_start(self):
        from homeassistant.helpers.event import async_track_time_interval
        await self._async_tick(None)
        self._unsub = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=30))
        self._schedule_boundary(datetime.now(timezone.utc))

    async def _async_tick(self, now):
        try:
            await self.async_refresh()
        except Exception:
            # The background reader cannot leave old monetary values available.
            self._invalidate()

    def _invalidate(self):
        self.data = None
        for listener in tuple(self._listeners):
            listener()

    def _schedule_boundary(self, now):
        if self._boundary_unsub:
            self._boundary_unsub()
            self._boundary_unsub = None
        if not self._unsub:
            return
        from homeassistant.helpers.event import async_track_point_in_utc_time
        model_boundaries = self._model_boundaries
        future = [value for value in model_boundaries if value > now.timestamp() * 1000]
        minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        boundary = min(minute, datetime.fromtimestamp(min(future) / 1000, timezone.utc)) if future else minute
        self._boundary_unsub = async_track_point_in_utc_time(self.hass, self._async_tick, boundary)

    def async_stop(self):
        self._stopped = True
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._boundary_unsub:
            self._boundary_unsub()
            self._boundary_unsub = None
        self._listeners.clear()
        self.data = None
        self._measurement = None

    async def async_refresh(self, *, now=None):
        async with self._lock:
            self._check_running()
            real_time = now is None
            now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            tz = self.hass.config.time_zone
            start, end, aligned = measurement_window(now, tz)
            sensors = await async_grid_sensors(self.hass)
            self._check_running()
            metadata = await async_get_energy_metadata(self.hass, [row['entity_id'] for row in sensors])
            self._check_running()
            key = (start, end, tz, _source_signature(sensors, metadata))
            # Price edits reuse measurement history; normalization metadata is checked independently.
            expired = self._measurement_at is None or (now - self._measurement_at).total_seconds() >= 300
            if key != self._measurement_key or expired:
                if not aligned:
                    measured = {'status': 'no_data'}
                elif self.provider:
                    measured = await self.provider(self.hass, start, end)
                else:
                    measured = await async_get_energy_usage(self.hass, sensors, start, end, 'daily', metadata=metadata)
                self._check_running()
                # A source/timezone change during Recorder IO invalidates the answer.
                latest = await async_grid_sensors(self.hass)
                latest_meta = await async_get_energy_metadata(self.hass, [row['entity_id'] for row in latest])
                self._check_running()
                if tz != self.hass.config.time_zone or _source_signature(latest, latest_meta) != key[3]:
                    self._measurement_key = None
                    self._invalidate()
                    raise RuntimeError('energy_sources_changed')
                self._measurement, self._measurement_key, self._measurement_at = measured, key, now
            state = await self.storage.async_get_tariff()
            self._check_running()
            if real_time:
                finished = datetime.now(timezone.utc)
                if measurement_window(finished, self.hass.config.time_zone)[:2] != (start, end):
                    self._invalidate()
                    raise RuntimeError('energy_calendar_changed')
                now = finished
            self._model_boundaries = EnergyTariff(state.get('profile'))._boundaries
            result = await self._async_build_snapshot(state, self._measurement, now=now, measurement_timezone=tz)
            self._check_running()
            # CPU work runs off-loop. Reject answers overtaken by a confirmed edit,
            # source normalization change, HA calendar change, or unload.
            latest = await async_grid_sensors(self.hass)
            latest_meta = await async_get_energy_metadata(self.hass, [row['entity_id'] for row in latest])
            latest_state = await self.storage.async_get_tariff()
            self._check_running()
            if (tz != self.hass.config.time_zone or _source_signature(latest, latest_meta) != key[3]
                    or latest_state['revision'] != state['revision']
                    or (real_time and measurement_window(datetime.now(timezone.utc), tz)[:2] != (start, end))):
                self._invalidate()
                raise RuntimeError('energy_snapshot_changed')
            self.data = result
            self._schedule_boundary(now)
            for listener in tuple(self._listeners):
                listener()
            return deepcopy(self.data)

    async def async_snapshot(self, *, profile=_OVERRIDE_UNSET, now=None):
        if profile is _OVERRIDE_UNSET:
            return await self.async_refresh(now=now)
        clean = validate_tariff_profile(profile)
        await self.async_refresh(now=now)
        # Capture the shared measurement/window before awaiting pure CPU work.
        instant = datetime.fromisoformat(self.data['generated_at'])
        tz, key = self.data['measurement_timezone'], self._measurement_key
        result = await self._async_build_snapshot({'profile': clean, 'revision': None}, self._measurement,
            now=instant, measurement_timezone=tz, profile_source='override')
        self._check_running()
        latest = await async_grid_sensors(self.hass)
        latest_meta = await async_get_energy_metadata(self.hass, [row['entity_id'] for row in latest])
        self._check_running()
        if (tz != self.hass.config.time_zone or _source_signature(latest, latest_meta) != key[3]
                or (now is None and measurement_window(datetime.now(timezone.utc), tz)[:2] != key[:2])):
            self._invalidate()
            raise RuntimeError('energy_snapshot_changed')
        return result

    async def _async_build_snapshot(self, state, measurement, **kwargs):
        build = partial(build_snapshot, state, measurement, **kwargs)
        return await self._async_run_cpu(build)

    async def _async_run_cpu(self, job):
        async with self._cpu_lock:
            self._check_running()
            executor = getattr(self.hass, 'async_add_executor_job', None)
            # Real Home Assistant always supplies an executor. The synchronous
            # path is only for bounded source-only test doubles.
            result = await executor(job) if executor is not None else job()
            self._check_running()
            return result

    def _check_running(self):
        if self._stopped:
            raise RuntimeError('energy_snapshot_unloaded')
