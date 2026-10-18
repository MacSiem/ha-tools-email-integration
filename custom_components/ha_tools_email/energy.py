"""Bounded, read-only Energy snapshot shared by household UI and entities.

Only EnergyTariff prices measurements. This module never renders or sends mail.
The public snapshot contains no meter IDs, names, or persisted profile content.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .composer import async_grid_sensors, async_get_energy_usage
from .tariff import EnergyTariff, validate_tariff_profile

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


def price_measurements(profile, measurement, start, end, boundary_complete=True):
    """Shared report/entity pricing with a single completeness policy."""
    costs = EnergyTariff(profile).price_buckets(measurement.get('hourly_buckets', []),
        {'from': start.isoformat(), 'to': end.isoformat()})
    if measurement.get('status') != 'ready' or not boundary_complete:
        costs.update(complete=False, kwh=None, energyCost=None, variableCost=None, usageCost=None)
    return costs


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
    current = {'at': now.astimezone(timezone.utc).isoformat(), 'available': available,
        'reason': current_rate.get('reason', None), 'zone_id': current_rate.get('zone') if available else None,
        'zone_class': classification, 'cheap': None if classification is None else classification == 'cheap',
        'energy_rate': energy, 'variable_rate': variable,
        'usage_rate': energy + variable if energy is not None and variable is not None else None}
    issues = [{'reason': row['reason']} for row in costs['issues'][:32]]
    if not aligned:
        issues.insert(0, {'reason': 'calendar_boundary_not_recorder_hour'})
    if measurement.get('status') != 'ready':
        issues.insert(0, {'reason': 'measurement_incomplete'})
    coverage = {key: value for key, value in measurement.get('coverage', {}).items()
                if key in ('complete', 'measured_source_hours', 'expected_source_hours', 'sources',
                           'complete_sources', 'unavailable_sources')}
    return {'schema': 1, 'generated_at': current['at'], 'tariff_revision': state.get('revision'),
        'profile_source': profile_source,
        'status': 'ready' if available and costs['complete'] else 'partial' if available else 'unavailable',
        'currency': model.profile.get('currency') if model.valid else None,
        'price_basis': model.profile.get('priceBasis') if model.valid else None,
        'measurement_timezone': measurement_timezone,
        'tariff_timezone': model.profile.get('timezone') if model.valid else None,
        'tariff_clock': deepcopy(model.clock) if model.valid else None,
        'current': current, 'next_cheap': next_cheap(model, now),
        'today': {'from': start.isoformat(), 'to': end.isoformat(), 'boundary_complete': aligned,
            'measurement_complete': measurement.get('status') == 'ready' and aligned,
            'tariff_complete': costs['complete'], 'measurement_status': measurement.get('status', 'no_data'),
            'coverage': coverage, 'energy_cost': costs['energyCost'], 'variable_cost': costs['variableCost'],
            'usage_cost': costs['usageCost'], 'fixed_monthly': costs['fixedMonthly'], 'issues': issues}}


class EnergySnapshotCoordinator:
    """Serialize refreshes; all five entities read one bounded public snapshot."""

    def __init__(self, hass, storage, *, provider=None):
        self.hass, self.storage, self.provider = hass, storage, provider
        self.data = None
        self._lock = asyncio.Lock()
        self._measurement = None
        self._measurement_key = None
        self._measurement_at = None
        self._listeners = set()
        self._stopped = False
        self._unsub = None

    def async_add_listener(self, listener):
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    async def async_start(self):
        from homeassistant.helpers.event import async_track_time_interval
        await self.async_refresh()
        self._unsub = async_track_time_interval(self.hass, self._async_tick, timedelta(seconds=30))

    async def _async_tick(self, now):
        await self.async_refresh(now=now)

    def async_stop(self):
        self._stopped = True
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._listeners.clear()
        self.data = None
        self._measurement = None

    async def async_refresh(self, *, now=None):
        async with self._lock:
            self._check_running()
            now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            tz = self.hass.config.time_zone
            start, end, aligned = measurement_window(now, tz)
            sensors = await async_grid_sensors(self.hass)
            self._check_running()
            key = (start, end, tz, tuple(row['entity_id'] for row in sensors))
            # Price edits do not query Recorder. Retry incomplete data every five minutes.
            expired = self._measurement_at is None or (now - self._measurement_at).total_seconds() >= 300
            if key != self._measurement_key or expired:
                if not aligned:
                    measured = {'status': 'no_data'}
                elif self.provider:
                    measured = await self.provider(self.hass, start, end)
                else:
                    measured = await async_get_energy_usage(self.hass, sensors, start, end, 'daily')
                self._check_running()
                # A source/timezone change during Recorder IO invalidates the answer.
                latest = await async_grid_sensors(self.hass)
                self._check_running()
                if tz != self.hass.config.time_zone or tuple(row['entity_id'] for row in latest) != key[3]:
                    self._measurement_key = None
                    self.data = None
                    raise RuntimeError('energy_sources_changed')
                self._measurement, self._measurement_key, self._measurement_at = measured, key, now
            state = await self.storage.async_get_tariff()
            self._check_running()
            self.data = build_snapshot(state, self._measurement, now=now, measurement_timezone=tz)
            for listener in tuple(self._listeners):
                listener()
            return deepcopy(self.data)

    async def async_snapshot(self, *, profile=_OVERRIDE_UNSET, now=None):
        if profile is _OVERRIDE_UNSET:
            return await self.async_refresh(now=now)
        clean = validate_tariff_profile(profile)
        await self.async_refresh(now=now)
        # No intervening await: use the exact shared measurement window atomically.
        instant = datetime.fromisoformat(self.data['generated_at'])
        return build_snapshot({'profile': clean, 'revision': None}, self._measurement,
            now=instant, measurement_timezone=self.data['measurement_timezone'], profile_source='override')

    def _check_running(self):
        if self._stopped:
            raise RuntimeError('energy_snapshot_unloaded')
