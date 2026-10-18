"""Pure schema-1 tariff model, matching shared/ha-tools-energy-tariff.js.

Absolute measured intervals are never apportioned across prices. Unknown variable
fees remain unknown; fixed monthly fees are metadata, never a prorated bill.
"""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from math import isfinite, floor
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TariffValidationError(ValueError):
    """Safe validation error containing only predefined field paths/reasons."""

    def __init__(self, errors):
        super().__init__('invalid_tariff_profile')
        self.errors = errors


def _finite(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def _integer(value):
    return _finite(value) and int(value) == value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _month_day(value):
    return isinstance(value, str) and bool(re.fullmatch(r'\d{2}-\d{2}', value)) and _date('2000-' + value)


def _stamp(value):
    """JS timestamp contract: milliseconds or an ISO string with explicit offset."""
    try:
        if _finite(value):
            # Date truncates fractional milliseconds toward zero.
            result = int(value)
        elif isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(Z|[+-]([01]\d|2[0-3]):[0-5]\d)', value):
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            instant = instant.replace(microsecond=(instant.microsecond // 1000) * 1000)
            delta = instant.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
            result = delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
        else:
            return None
        datetime.fromtimestamp(result / 1000, timezone.utc)
        return result
    except (ValueError, OverflowError, OSError):
        return None


def _minutes(value, end=False):
    if end and value == '24:00':
        return 1440
    if not isinstance(value, str) or not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', value):
        return None
    return int(value[:2]) * 60 + int(value[3:])


def validate_tariff_profile(profile):
    """Validate bounded JSON before any persistence; None explicitly clears it."""
    if profile is None:
        return None
    try:
        encoded = json.dumps(profile, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise TariffValidationError([{'path': 'profile', 'reason': 'invalid_profile'}]) from None
    if len(encoded) > 65536:
        raise TariffValidationError([{'path': 'profile', 'reason': 'profile_too_large'}])
    if isinstance(profile, dict):
        counts = [len(profile.get(key, [])) for key in ('rules', 'exceptions')
                  if isinstance(profile.get(key, []), list)]
        if sum(counts) > 512:
            raise TariffValidationError([{'path': 'rules', 'reason': 'too_many_rules'}])
    model = EnergyTariff(profile)
    if not model.valid:
        raise TariffValidationError(model.errors)
    return deepcopy(profile)


class EnergyTariff:
    """Compile a user tariff without importing HA or modifying caller data."""

    def __init__(self, profile: Any):
        self.errors = []
        self.profile = deepcopy(profile) if isinstance(profile, dict) else {}
        p = self.profile
        fail = self._fail
        if not isinstance(profile, dict):
            fail('profile', 'invalid_profile')
        if type(p.get('schema')) not in (int, float) or p.get('schema') != 1:
            fail('schema', 'unsupported_schema')
        if not isinstance(p.get('currency'), str) or not re.fullmatch(r'[A-Z]{3}', p['currency']):
            fail('currency', 'invalid_currency')
        if p.get('priceBasis') not in ('gross', 'net'):
            fail('priceBasis', 'invalid_price_basis')
        try:
            self.timezone = ZoneInfo(p.get('timezone', ''))
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            self.timezone = timezone.utc
            fail('timezone', 'invalid_timezone')
        self.clock = deepcopy(p.get('clock')) if isinstance(p.get('clock'), dict) else {}
        if self.clock.get('mode') not in ('civil', 'fixed'):
            fail('clock.mode', 'invalid_clock')
        offset = self.clock.get('offsetMinutes')
        if self.clock.get('mode') == 'fixed' and (not _integer(offset) or not -840 <= offset <= 840):
            fail('clock.offsetMinutes', 'invalid_offset')
        for key in ('validFrom', 'validTo'):
            if p.get(key) is not None and not _date(p[key]):
                fail(key, 'invalid_date')
        if _date(p.get('validFrom')) and _date(p.get('validTo')) and p['validFrom'] > p['validTo']:
            fail('validTo', 'reversed_validity')
        fee = p.get('fixedMonthly')
        if fee is not None and (not _finite(fee) or fee < 0):
            fail('fixedMonthly', 'invalid_fee')
        self.zones = {}
        zones = p.get('zones')
        if not isinstance(zones, list) or not 1 <= len(zones) <= 32:
            fail('zones', 'invalid_zones')
        for i, z in enumerate(zones if isinstance(zones, list) else []):
            path = f'zones.{i}'
            if not isinstance(z, dict) or not isinstance(z.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', z['id']) or z['id'] in self.zones:
                fail(path, 'invalid_zone')
                continue
            self.zones[z['id']] = {'zone': z['id'], 'label': z.get('label') if isinstance(z.get('label'), str) else z['id'],
                                   'energyRate': self._energy(z.get('energyRate'), path + '.energyRate'),
                                   'variableRate': self._variable(z.get('variableRate'), path + '.variableRate'),
                                   'classification': self._classification(z.get('classification'), path + '.classification')}
        self.default = p.get('defaultZone')
        if self.default is not None and (not isinstance(self.default, str) or self.default not in self.zones):
            fail('defaultZone', 'unknown_zone')
        self.rules = self._rules(p.get('rules'), 'rules')
        self.exceptions = self._rules(p.get('exceptions'), 'exceptions')
        self.dynamic = None
        if p.get('dynamic') is not None:
            dyn = p['dynamic']
            if not isinstance(dyn, dict) or not isinstance(dyn.get('intervals'), list) or len(dyn['intervals']) > 10000:
                fail('dynamic', 'invalid_dynamic')
            else:
                self.dynamic = []
                for i, row in enumerate(dyn['intervals']):
                    row = row if isinstance(row, dict) else {}
                    start, end = _stamp(row.get('from')), _stamp(row.get('to'))
                    path = f'dynamic.intervals.{i}'
                    if start is None or end is None or end <= start:
                        fail(path, 'invalid_interval')
                    self.dynamic.append({'from': start, 'to': end, 'zone': 'dynamic', 'label': 'Dynamic',
                                         'energyRate': self._energy(row.get('energyRate'), path + '.energyRate'),
                                         'variableRate': self._variable(row.get('variableRate'), path + '.variableRate'),
                                         'classification': self._classification(row.get('classification'), path + '.classification')})
                self.dynamic.sort(key=lambda r: r['from'] if r['from'] is not None else 0)
                for previous, current in zip(self.dynamic, self.dynamic[1:]):
                    if current['from'] is not None and previous['to'] is not None and current['from'] < previous['to']:
                        fail('dynamic', 'overlapping_prices')
        self.valid = not self.errors
        self._starts = [r['from'] for r in self.dynamic or []] if self.valid else []
        self._boundaries = sorted({t for r in self.dynamic or [] for t in (r['from'], r['to']) if t is not None})

    def _fail(self, path, reason):
        self.errors.append({'path': path, 'reason': reason})

    def _energy(self, value, path):
        if not _finite(value) or (value < 0 and self.profile.get('allowNegativeEnergy') is not True):
            self._fail(path, 'invalid_rate')
        return value

    def _variable(self, value, path):
        if value is not None and (not _finite(value) or value < 0):
            self._fail(path, 'invalid_fee')
        return value

    def _classification(self, value, path):
        if value is not None and value not in ('cheap', 'expensive', 'mixed'):
            self._fail(path, 'invalid_classification')
        return value

    def _rules(self, value, kind):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 512:
            self._fail(kind, 'invalid_rules')
            return []
        result = []
        for i, r in enumerate(value):
            path = f'{kind}.{i}'
            if not isinstance(r, dict):
                self._fail(path, 'invalid_rule')
                continue
            zone = r.get('zone')
            if not isinstance(zone, str) or zone not in self.zones:
                self._fail(path + '.zone', 'unknown_zone')
            priority = r.get('priority') if r.get('priority') is not None else 0
            if not _integer(priority) or abs(priority) > 10000:
                self._fail(path + '.priority', 'invalid_priority')
            weekdays, dates, season = r.get('weekdays'), r.get('dates'), r.get('season')
            if weekdays is not None and (not isinstance(weekdays, list) or not weekdays or any(not _integer(d) or not 0 <= d <= 6 for d in weekdays)):
                self._fail(path + '.weekdays', 'invalid_weekdays')
            if (dates is not None or kind == 'exceptions') and (not isinstance(dates, list) or not 1 <= len(dates) <= 3660 or any(not _date(d) for d in dates)):
                self._fail(path + '.dates', 'invalid_dates')
            if season is not None and (not isinstance(season, dict) or not _month_day(season.get('from')) or not _month_day(season.get('to'))):
                self._fail(path + '.season', 'invalid_season')
            intervals = []
            raw = r.get('intervals')
            if not isinstance(raw, list) or not 1 <= len(raw) <= 48:
                self._fail(path + '.intervals', 'invalid_intervals')
            else:
                for j, interval in enumerate(raw):
                    interval = interval if isinstance(interval, dict) else {}
                    start, end = _minutes(interval.get('from')), _minutes(interval.get('to'), True)
                    if start is None or end is None or start == end:
                        self._fail(f'{path}.intervals.{j}', 'invalid_interval')
                    else:
                        intervals.append((start, end))
            # Compile membership away from checkpoint scans without changing
            # the JSON profile. Invalid fields must retain validation errors,
            # never raise TypeError while trying to hash malformed input.
            if isinstance(dates, list) and all(isinstance(day, str) for day in dates):
                dates = frozenset(dates)
            if isinstance(weekdays, list) and all(_integer(day) for day in weekdays):
                weekdays = frozenset(weekdays)
            result.append(dict(zone=zone, priority=priority, weekdays=weekdays, dates=dates, season=season, intervals=intervals))
        return result

    @staticmethod
    def _unavailable(reason):
        return {'available': False, 'reason': reason}

    @staticmethod
    def _day_matches(rule, day):
        if rule['dates'] is not None and day not in rule['dates']:
            return False
        if rule['weekdays'] is not None and (date.fromisoformat(day).weekday() + 1) % 7 not in rule['weekdays']:
            return False
        season, mmdd = rule['season'], day[5:]
        return season is None or (season['from'] <= mmdd <= season['to'] if season['from'] <= season['to'] else mmdd >= season['from'] or mmdd <= season['to'])

    def _matches(self, rule, day, minute):
        for start, end in rule['intervals']:
            if start < end:
                if start <= minute < end and self._day_matches(rule, day):
                    return True
            elif minute >= start and self._day_matches(rule, day):
                return True
            elif minute < end and self._day_matches(rule, (date.fromisoformat(day) - timedelta(days=1)).isoformat()):
                return True
        return False

    def rate_at(self, instant):
        if not self.valid:
            return self._unavailable('invalid_profile')
        stamp = _stamp(instant)
        if stamp is None:
            return self._unavailable('invalid_timestamp')
        local = datetime.fromtimestamp(stamp / 1000, timezone.utc)
        if self.clock['mode'] == 'fixed':
            local += timedelta(minutes=self.clock['offsetMinutes'])
        else:
            local = local.astimezone(self.timezone)
        day, minute = local.date().isoformat(), local.hour * 60 + local.minute
        p = self.profile
        if (p.get('validFrom') and day < p['validFrom']) or (p.get('validTo') and day > p['validTo']):
            return self._unavailable('outside_validity')
        if self.dynamic is not None:
            index = bisect_right(self._starts, stamp) - 1
            price = self.dynamic[index] if index >= 0 else None
            return dict(available=True, **price, date=day) if price and stamp < price['to'] else self._unavailable('missing_rate')
        special = [r for r in self.exceptions if self._matches(r, day, minute)]
        candidates = special or [r for r in self.rules if self._matches(r, day, minute)]
        zone = self.default
        if candidates:
            priority = max(r['priority'] for r in candidates)
            ids = {r['zone'] for r in candidates if r['priority'] == priority}
            if len(ids) != 1:
                return self._unavailable('ambiguous_rules')
            zone = next(iter(ids))
        return dict(available=True, **self.zones[zone], date=day) if zone in self.zones else self._unavailable('missing_rate')

    def price_bucket(self, row):
        row = row if isinstance(row, dict) else {}
        start, end, kwh = _stamp(row.get('from')), _stamp(row.get('to')), row.get('kwh')
        frame = {'from': start, 'to': end}
        if start is None or end is None or end <= start or end - start > 86400000 or not _finite(kwh) or kwh < 0:
            return dict(self._unavailable('invalid_measurement'), **frame)
        first = self.rate_at(start)
        if not first['available']:
            return dict(first, **frame)
        checkpoints = {end - 1, *range((floor(start / 60000) + 1) * 60000, end, 60000)}
        low, high = bisect_right(self._boundaries, start), bisect_right(self._boundaries, end - 1)
        checkpoints.update(self._boundaries[low:high])
        zone_ids = {first['zone']}
        for stamp in sorted(checkpoints):
            next_rate = self.rate_at(stamp)
            if not next_rate['available']:
                return dict(next_rate, **frame)
            if (first['energyRate'], first['variableRate']) != (next_rate['energyRate'], next_rate['variableRate']):
                return dict(self._unavailable('mixed_rate_bucket'), **frame)
            zone_ids.add(next_rate['zone'])
        energy = kwh * first['energyRate']
        variable = None if first['variableRate'] is None else kwh * first['variableRate']
        usage = None if variable is None else energy + variable
        if any(v is not None and not _finite(v) for v in (energy, variable, usage)):
            return dict(self._unavailable('nonfinite_cost'), **frame)
        ids = sorted(zone_ids)
        return dict(available=True, **frame, kwh=kwh, zone=ids[0] if len(ids) == 1 else None,
                    zones=ids, zoneAttribution='single' if len(ids) == 1 else 'multiple', energyRate=first['energyRate'],
                    variableRate=first['variableRate'], energyCost=energy, variableCost=variable,
                    usageCost=usage)

    def price_buckets(self, rows, coverage=None):
        issues, priced = [], []
        if not self.valid:
            issues.append({'reason': 'invalid_profile'})
        if not isinstance(rows, list) or not rows:
            issues.append({'reason': 'no_data'})
        for index, row in enumerate(rows if isinstance(rows, list) else []):
            result = self.price_bucket(row)
            priced.append(dict(index=index, **result))
            if not result['available']:
                issues.append({'index': index, 'reason': result['reason']})
        ordered = sorted(priced, key=lambda r: r['from'] if r['from'] is not None else 0)
        for previous, current in zip(ordered, ordered[1:]):
            if current['from'] is not None and previous['to'] is not None and current['from'] < previous['to']:
                issues.append({'index': current['index'], 'reason': 'overlapping_measurement'})
        if coverage is not None:
            coverage = coverage if isinstance(coverage, dict) else {}
            start, end = _stamp(coverage.get('from')), _stamp(coverage.get('to'))
            if start is None or end is None or end <= start:
                issues.append({'reason': 'invalid_coverage'})
            elif not ordered or ordered[0]['from'] != start or ordered[-1]['to'] != end or any(c['from'] != p['to'] for p, c in zip(ordered, ordered[1:])):
                issues.append({'reason': 'missing_measurement'})
        complete = not issues
        energy = sum(r['energyCost'] for r in priced) if complete else None
        variable = sum(r['variableCost'] for r in priced) if complete and all(r['variableCost'] is not None for r in priced) else None
        kwh = sum(r['kwh'] for r in priced) if complete else None
        usage = energy + variable if energy is not None and variable is not None else None
        if complete and any(v is not None and not _finite(v) for v in (kwh, energy, variable, usage)):
            issues.append({'reason': 'nonfinite_total'})
            complete = False
            kwh = energy = variable = usage = None
        p = self.profile
        return dict(complete=complete, issues=issues, rows=priced,
                    kwh=kwh, energyCost=energy, variableCost=variable, usageCost=usage,
                    currency=p.get('currency'), priceBasis=p.get('priceBasis') if p.get('priceBasis') in ('gross', 'net') else None, timezone=p.get('timezone'),
                    clock=deepcopy(self.clock), fixedMonthly=p.get('fixedMonthly') if self.valid and _finite(p.get('fixedMonthly')) else None)


def price_measurements(profile, measurement, start, end, boundary_complete=True):
    """Shared report/entity pricing with a single completeness policy."""
    costs = EnergyTariff(profile).price_buckets(measurement.get('hourly_buckets', []),
        {'from': start.isoformat(), 'to': end.isoformat()})
    if measurement.get('status') != 'ready' or not boundary_complete:
        costs.update(complete=False, kwh=None, energyCost=None, variableCost=None, usageCost=None)
    return costs
