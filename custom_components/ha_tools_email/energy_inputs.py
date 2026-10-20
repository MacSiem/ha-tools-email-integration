"""Guarded BUILD-1 collector on the existing analysis service's serialized lane.

Only detached normalized intervals survive each read. No authorization result,
source-by-year matrix or independent tariff/pricing engine is retained here.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
import hashlib
import json
from zoneinfo import ZoneInfo

from .analysis_periods import resolve_analysis_periods
from .analysis_provider import async_read_energy_chunk
from .composer import async_get_energy_metadata
from .energy_measurements import measurement_evidence
from .energy_model import build_report
from .tariff_history import HistoricalTariffRates, calculate_energy_amounts, _fees, _version_at
from .tariff_profiles import book_to_profile

UTC = timezone.utc


def report_clock(now, zone):
    now = now.astimezone(UTC)
    return {'as_of': now.astimezone(ZoneInfo(zone)).date().isoformat(),
            'cutoff': now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0).isoformat()}


def report_period(period):
    """Full accepted calendar descriptor; leave the legacy period untouched."""
    return {**deepcopy(period), 'end': period['calendar_end'],
            'local_end': period['calendar_local_end'], 'complete': not period['partial']}


def baseline_fact(period, hours, scope):
    return {'period': {'start': period['start'], 'end': period['end']},
            'hours': deepcopy(hours), 'scope': scope}


def color_totals(chunk, start, end, roots, totals):
    """Require >=90% actual hourly evidence per root across 30 whole days."""
    if not roots:
        return
    evidence = measurement_evidence(chunk['consumers'], start, end)
    for sid in roots:
        cells = evidence['per_source'].get(sid, {})
        totals[sid][0] += sum((Decimal(str(value)) for value in cells.values()), Decimal(0))
        totals[sid][1] += len(cells)


def identity_colors(totals, expected, as_of):
    qualified = [sid for sid, (_, count) in totals.items() if count >= expected * .9]
    qualified.sort(key=lambda sid: (-totals[sid][0], sid))
    return {'as_of': as_of, 'window_days': 30,
            'devices': {sid: 'd' + str(index + 1) for index, sid in enumerate(qualified[:4])}}


def normalize_short_hour(raw, metadata, ids, start, end):
    """One real partial hour, complete contiguous five-minute change cells only.

    Never call hourly measurement_evidence/hour_grid with partial bounds. A
    missing/duplicate/unsupported cell makes that source's partial hour unknown.
    """
    expected = set(range(int(start.timestamp()), int(end.timestamp()), 300))
    if (not expected or start.timestamp() % 3600 or end.timestamp() % 300
            or not 0 < (end-start).total_seconds() < 3600):
        raise ValueError('invalid_short_statistics_window')
    values = {}
    for sid in ids:
        meta = metadata.get(sid)
        if isinstance(meta, tuple):
            meta = meta[1]
        factor = ({'Wh': Decimal('.001'), 'kWh': Decimal(1), 'MWh': Decimal(1000)}
                  .get(meta.get('unit_of_measurement'))) if isinstance(meta, dict) else None
        if (factor is None or meta.get('has_sum') is not True
                or meta.get('unit_class') not in (None, 'energy')):
            values[sid] = None
            continue
        cells, seen = {}, set()
        points = raw.get(sid, []) if isinstance(raw, dict) else []
        if not isinstance(points, list) or len(points) > 12:
            values[sid] = None
            continue
        for point in points:
            try:
                stamp = point['start']
                if isinstance(stamp, str):
                    stamp = datetime.fromisoformat(stamp)
                if isinstance(stamp, datetime):
                    if stamp.utcoffset() is None:
                        continue
                    stamp = stamp.timestamp()
                if type(stamp) not in (int, float) or stamp not in expected:
                    continue
                if stamp in seen:
                    cells.pop(stamp, None)
                    continue
                seen.add(stamp)
                value = point.get('change')
                if type(value) not in (int, float):
                    continue
                value = Decimal(str(value)) * factor
                if value.is_finite() and 0 <= value <= Decimal('1e18'):
                    cells[stamp] = value
            except (KeyError, ValueError, TypeError, OverflowError):
                continue
        values[sid] = str(sum(cells.values(), Decimal(0))) if set(cells) == expected else None
    return values


async def async_short_hour(service, configuration, start, end, guard):
    ids = list(dict.fromkeys([row['entity_id'] for row in configuration['grid_sensors']] +
                            [row['stat_consumption'] for row in configuration['consumers']]))
    await guard()
    metadata = await async_get_energy_metadata(service.hass, ids)
    await guard()
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.components.recorder import get_instance
    raw = await get_instance(service.hass).async_add_executor_job(
        statistics_during_period, service.hass, start, end, set(ids), '5minute', {}, {'change'})
    await guard()
    return await service._cpu(partial(normalize_short_hour, raw, metadata, ids, start, end), guard)


def short_projection(values, configuration, tariff, start, end):
    def total(ids):
        rows = [values.get(sid) for sid in ids]
        return str(sum((Decimal(value) for value in rows), Decimal(0))) if all(v is not None for v in rows) else None
    rate = HistoricalTariffRates(tariff).resolve_interval(start.isoformat(), end.isoformat()) if tariff else None
    known = rate and rate['available']
    def costs(value):
        if value is None or not known:
            return {}
        return {key: str(value) if value is not None else None for key, value in
                calculate_energy_amounts(value, rate['energy_rate'], rate['variable_rate'], rate['rate_semantics']).items()}
    kwh = total([row['entity_id'] for row in configuration['grid_sensors']])
    return {'from': start.isoformat(), 'to': end.isoformat(), 'kwh': kwh,
        'currency': rate.get('currency') if known else None,
        'price_basis': rate.get('price_basis') if known else None,
        'zone': rate.get('zone') if known and rate.get('zone_attribution') == 'single' else None,
        'costs': costs(kwh), 'devices': {row['stat_consumption']: {
            'kwh': values.get(row['stat_consumption']),
            'costs': costs(values.get(row['stat_consumption'])) if configuration['consumer_domain'] == 'grid_import' else {}}
            for row in configuration['consumers']}}


async def async_collect_inputs(service, configuration, period, context, accumulator,
                               membership, book, guard, native=None):
    """Caller holds service lock and admission slot throughout all guarded IO."""
    now = datetime.fromisoformat(context['report_cutoff'])
    zone = ZoneInfo(period['timezone'])
    measured_until = min(datetime.fromisoformat(period['calendar_end']), now)
    start, completed = (datetime.fromisoformat(period[key]) for key in ('start', 'end'))
    scope = 'full_period' if measured_until == datetime.fromisoformat(period['calendar_end']) else (
        'same_local_window:' + measured_until.astimezone(zone).strftime('%m-%dT%H:%M:%S') +
        ':fold=' + str(measured_until.astimezone(zone).fold))
    projection = await service._cpu(accumulator.report_projection, guard)
    active = next((row for row in book['tariffs'] if row['id'] == book.get('active_tariff_id')), None) if book else None
    if measured_until > completed:
        values = await async_short_hour(service, configuration, completed, measured_until, guard)
        cell = await service._cpu(partial(short_projection, values, configuration, active, completed, measured_until), guard)
        projection['hours'].append(cell)
        projection['measurement_fingerprint'] = hashlib.sha256(json.dumps(
            {'completed': projection.get('measurement_fingerprint'), 'partial': cell},
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if active:
            # Reuse the very same engine's fixed-fee integration; never scale a
            # rounded period fee or split arbitrary named wire components.
            def add_fee():
                _, amounts, known, identities = _fees(active['versions'], completed.timestamp()*1000,
                                                       measured_until.timestamp()*1000)
                if projection['fixed_share'] is not None and all(known.values()) and identities == {(projection['currency'], projection['price_basis'])}:
                    projection['fixed_share'] = str(Decimal(projection['fixed_share']) + sum(amounts.values(), Decimal(0)))
                else:
                    projection['fixed_share'] = None
            await service._cpu(add_fee, guard)
    if native is not None:
        facts = await service._cpu(native.report_hours, guard)
        by_start = {row['from']: row for row in facts['hours']}
        projection['currency'], projection['price_basis'] = facts['currency'], 'ha_recorded_cost'
        projection['fixed_share'] = None
        for row in projection['hours']:
            instant = row['from'] if isinstance(row['from'], (int, float)) else datetime.fromisoformat(row['from']).timestamp()*1000
            row.update(currency=facts['currency'], price_basis='ha_recorded_cost',
                costs={'usage_cost': by_start.get(instant, {}).get('usage_cost')}, zone=None)
            for device in row['devices'].values():
                device['costs'] = {}
    # Thirty full local days; aggregate root source cells only, independent of
    # the selected historical period and registry ordering. Cache never grants ACL.
    as_of = context['report_color_as_of']
    color_key = {key: value for key, value in context.items() if key not in (
        'period_kind', 'period_signature', 'period_anchor', 'report_mode', 'report_cutoff',
        'actor_signature')}
    cached = service._report_colors
    if cached is not None and cached[0] == color_key:
        colors = deepcopy(cached[1])
    else:
        roots = [row['stat_consumption'] for row in configuration['consumers'] if row['included_in_stat'] is None]
        totals = {sid: [Decimal(0), 0] for sid in roots}
        local_end = datetime.fromisoformat(as_of).replace(tzinfo=zone)
        cursor, limit = (local_end-timedelta(days=30)).astimezone(UTC), local_end.astimezone(UTC)
        expected = (limit-cursor).total_seconds()/3600
        while roots and cursor < limit:
            stop = min(cursor+timedelta(hours=168), limit)
            chunk = await async_read_energy_chunk(service.hass, configuration, cursor, stop, guard=guard)
            await guard()
            await service._cpu(partial(color_totals, chunk, cursor, stop, roots, totals), guard)
            del chunk
            cursor = stop
        colors = await service._cpu(partial(identity_colors, totals, expected, as_of), guard)
        await guard()
        service._report_colors = (deepcopy(color_key), deepcopy(colors))
    resolved = resolve_analysis_periods(context['period_kind'], now, period['timezone'], anchor=context['period_anchor'])
    requests, baseline_cache = [], {}
    labels = {'previous': {'day':'previous_day','week':'previous_week','month':'previous_month','year':'previous_year'}[context['period_kind']], 'same_weekday': 'same_weekday_last_week', 'mean7': 'avg_7d', 'year_ago': 'same_week_last_year' if context['period_kind']=='week' else 'same_month_last_year' if context['period_kind']=='month' else 'previous_year'}
    for key, plan in resolved['comparisons'].items():
        rows = []
        for baseline in plan['baseline_periods'] if plan['status'] == 'ready' else []:
            baseline = deepcopy(baseline)
            a, b = (datetime.fromisoformat(baseline[k]) for k in ('start', 'end'))
            # Resolver owns DST/calendar clamping; append the same actual local
            # minute offset to its completed-hour cutoff, bounded by calendar end.
            tail = measured_until-completed
            target_end = min(b+tail, datetime.fromisoformat(baseline['calendar_end']))
            cache_key = (a.isoformat(), target_end.isoformat())
            if cache_key in baseline_cache:
                rows.append(deepcopy(baseline_cache[cache_key]))
                continue
            hours = []
            cursor = a
            while cursor < b:
                stop = min(cursor+timedelta(hours=168), b)
                chunk = await async_read_energy_chunk(service.hass, configuration, cursor, stop,
                                                      guard=guard, include_consumers=False)
                await guard()
                evidence = await service._cpu(partial(measurement_evidence, chunk['grid'], cursor, stop), guard)
                hours.extend(evidence['hourly_buckets'])
                del chunk, evidence
                cursor = stop
            if target_end > b:
                values = await async_short_hour(service, configuration, b, target_end, guard)
                values = [values.get(row['entity_id']) for row in configuration['grid_sensors']]
                quantity = str(sum((Decimal(value) for value in values), Decimal(0))) if all(v is not None for v in values) else None
                hours.append({'from': b.isoformat(), 'to': target_end.isoformat(), 'kwh': quantity})
            baseline['end'] = target_end.isoformat()
            fact = baseline_fact(baseline, hours, scope)
            baseline_cache[cache_key] = fact
            rows.append(fact)
        if key == 'mean7':
            requests.append({'key': 'avg_7d', 'baselines': rows})
        else:
            requests.append({'key': labels[key], **(rows[0] if rows else {})})
    tariff = {}
    if active:
        # Choose the version at the measured window end, rather than silently
        # labelling historical costs with the newest user profile.
        version = _version_at(active['versions'], (measured_until-timedelta(microseconds=1)).timestamp()*1000)
        if version is not None:
            tariff = await service._cpu(partial(book_to_profile, book, version_id=version['id']), guard)
            for item in tariff['zones']:
                amounts = calculate_energy_amounts(1,item.get('energyRate'),item.get('variableRate'),
                    tariff.get('rateSemantics','components'))
                item['rate'] = float(amounts['usage_cost']) if amounts['usage_cost'] is not None else None
    membership = deepcopy(membership)
    for row in configuration['consumers']:
        if row.get('name'):
            membership.setdefault('sources', {}).setdefault(row['stat_consumption'], {})['name'] = row['name']
    inputs = {'period': report_period(period), 'projection': projection, 'consumers': deepcopy(configuration['consumers']),
        'membership': deepcopy(membership), 'measured_until': measured_until.isoformat(), 'scope': scope,
        'comparisons': requests, 'colors': colors, 'color_as_of': as_of, 'tariff': tariff}
    return await service._cpu(partial(build_report, inputs), guard)
