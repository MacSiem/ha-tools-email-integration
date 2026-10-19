"""Exact scalar ledger and deterministic Recorder intents; no IO or repricing."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math

DOMAIN = 'ha_tools_email'
MAX_LEDGER_POINTS = 100000
MAX_SERIES = 4096


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def decimal_cost(value):
    if value is None:
        return None
    if type(value) is not str or len(value) > 128:
        raise ValueError('invalid_statistics_cost')
    try:
        amount = Decimal(value)
        if (not amount.is_finite() or not -128 <= amount.as_tuple().exponent <= 128
                or amount.adjusted() > 127 or not math.isfinite(float(amount))
                or amount != 0 and float(amount) == 0):
            raise ValueError('invalid_statistics_cost')
        return amount
    except (InvalidOperation, OverflowError) as error:
        raise ValueError('invalid_statistics_cost') from error


def statistic_id(identity):
    if (type(identity) is not dict or set(identity) !=
            {'entry_id', 'epoch', 'kind', 'key', 'currency', 'price_basis'}
            or identity['kind'] not in ('area', 'zone', 'unassigned', 'unmeasured')
            or any(type(identity[k]) is not str or not 0 < len(identity[k]) <= 256
                   for k in ('entry_id', 'epoch', 'currency', 'price_basis'))):
        raise ValueError('invalid_statistics_identity')
    key = identity['key']
    if identity['kind'] == 'zone':
        if type(key) is not list or len(key) != 3 or any(type(k) is not str or not 0 < len(k) <= 256 for k in key):
            raise ValueError('invalid_statistics_identity')
    elif type(key) is not str or not 0 < len(key) <= 256:
        raise ValueError('invalid_statistics_identity')
    return f"{DOMAIN}:energy_{identity['kind']}_{digest(identity)}_usage_cost"


def empty_state():
    return {'schema': 1, 'revision': 0, 'share': None, 'series': {}, 'pending': None}


def validate_state(raw, entry_id):
    if raw is None:
        return empty_state()
    if (type(raw) is not dict or set(raw) != {'schema', 'revision', 'share', 'series', 'pending'}
            or type(raw['schema']) is not int or raw['schema'] != 1 or type(raw['revision']) is not int or raw['revision'] < 0
            or type(raw['series']) is not dict or len(raw['series']) > MAX_SERIES):
        raise ValueError('invalid_statistics_ledger')
    count = 0
    for sid, record in raw['series'].items():
        if (type(record) is not dict or set(record) != {'statistic_id', 'identity', 'name', 'points'}
                or record['statistic_id'] != sid or statistic_id(record['identity']) != sid
                or record['identity']['entry_id'] != entry_id or type(record['points']) is not dict
                or type(record['name']) is not str or len(record['name']) > 255 or '\x00' in record['name']):
            raise ValueError('invalid_statistics_ledger')
        count += len(record['points'])
        if count > MAX_LEDGER_POINTS:
            raise ValueError('statistics_ledger_capacity')
        for stamp, fact in record['points'].items():
            if (type(stamp) is not str or not stamp.isdecimal() or str(int(stamp)) != stamp
                    or int(stamp) % 3600000 or type(fact) is not dict
                    or set(fact) != {'cost', 'membership_as_of', 'tariff_revision'}
                    or fact['membership_as_of'] is not None and type(fact['membership_as_of']) is not str
                    or fact['tariff_revision'] is not None and type(fact['tariff_revision']) is not int):
                raise ValueError('invalid_statistics_ledger')
            decimal_cost(fact['cost'])
    share = raw['share']
    if share is not None and (type(share) is not dict or set(share) != {'enabled', 'author', 'epoch', 'binding'}
            or type(share['enabled']) is not bool or type(share['author']) is not str or not share['author']
            or type(share['epoch']) is not str or not share['epoch'] or type(share['binding']) is not dict):
        raise ValueError('invalid_statistics_share')
    pending = raw['pending']
    if pending is not None:
        if (share is None or type(pending) is not dict or set(pending) != {'series', 'clear', 'epoch'}
                or pending['epoch'] != share['epoch']
                or any(type(pending[k]) is not list for k in ('series', 'clear'))
                or any(type(sid) is not str for k in ('series', 'clear') for sid in pending[k])
                or len(set(pending['series'])) != len(pending['series'])
                or not set(pending['clear']) <= set(pending['series'])
                or not set(pending['series']) <= set(raw['series'])
                or any(raw['series'][sid]['identity']['epoch'] != share['epoch'] for sid in pending['series'])):
            raise ValueError('invalid_statistics_intent')
    return deepcopy(raw)


def _incoming(facts, share, entry_id):
    if (facts.get('schema') != 1 or facts.get('cost_scope') != 'usage_only'
            or facts.get('membership_mode') != 'current_snapshot'
            or type(facts.get('hours')) is not list or not 0 < len(facts['hours']) <= 168):
        raise ValueError('invalid_statistics_projection')
    binding = share['binding']
    incoming, stamps = {}, set()
    for hour in facts['hours']:
        stamp, end = hour['from'], hour['to']
        if (type(stamp) not in (int, float) or stamp % 3600000 or end != stamp + 3600000
                or stamp in stamps):
            raise ValueError('invalid_statistics_hour')
        stamps.add(stamp)
        groups = hour['groups']
        compatible = (groups['currency'], groups['price_basis']) == (binding['currency'], binding['price_basis'])
        cells = [('area', key, row, facts.get('area_labels', {}).get(key, f'Obszar {key}'))
                 for key, row in groups['areas'].items()]
        cells += [('unassigned', 'unassigned', groups['unassigned'], 'Bez obszaru'),
                  ('unmeasured', 'unmeasured', groups['unmeasured'], 'Niezmierzone')]
        zone = hour['zone']
        if (all(isinstance(zone.get(key), str) and zone[key] for key in ('tariff_id', 'version_id', 'zone_id'))
                and (zone.get('currency'), zone.get('price_basis')) == (binding['currency'], binding['price_basis'])):
            cells.append(('zone', [zone['tariff_id'], zone['version_id'], zone['zone_id']], zone,
                          f"Strefa {zone['zone_id']}"))
        for kind, key, cell, label in cells:
            identity = {'entry_id': entry_id, 'epoch': share['epoch'], 'kind': kind, 'key': key,
                        'currency': binding['currency'], 'price_basis': binding['price_basis']}
            sid = statistic_id(identity)
            own_unit = compatible if kind != 'zone' else (
                (zone.get('currency'), zone.get('price_basis')) == (binding['currency'], binding['price_basis']))
            known = cell.get('complete') is True and own_unit
            cost = decimal_cost(cell.get('usage_cost')) if known else None
            scope = ('kompletne godziny; przypisanie z obliczenia'
                     if kind in ('area', 'unassigned') else 'kompletne godziny')
            basis = 'brutto' if binding['price_basis'] == 'gross' else 'netto'
            name = f"Koszt zużycia {binding['currency']} {basis} ({scope}) · {label}"
            record = incoming.setdefault(sid, {'statistic_id': sid, 'identity': identity,
                'name': name.replace('\x00', '')[:255], 'points': {}})
            record['points'][str(int(stamp))] = {'cost': format(cost, 'f') if cost is not None else None,
                'membership_as_of': facts.get('membership_as_of'),
                'tariff_revision': facts['context'].get('tariff_revision')}
    return incoming, {str(int(stamp)) for stamp in stamps}


def merge_facts(state, facts, entry_id):
    """Prepare a new durable intent; no mutation of accepted state or inputs."""
    candidate = deepcopy(state)
    share = candidate['share']
    incoming, stamps = _incoming(facts, share, entry_id)
    pending = candidate['pending']
    touched = set(pending['series']) if pending else set()
    clear = set(pending['clear']) if pending else set()
    for sid, record in incoming.items():
        old = candidate['series'].get(sid)
        if old is None:
            candidate['series'][sid] = record
            touched.add(sid)
        else:
            if old['name'] != record['name']:
                old['name'] = record['name']; touched.add(sid)
            for stamp, fact in record['points'].items():
                previous = old['points'].get(stamp)
                if previous is None or previous['cost'] != fact['cost']:
                    if previous is not None and previous['cost'] is not None and fact['cost'] is None:
                        clear.add(sid)
                    old['points'][stamp] = fact; touched.add(sid)
    # Unknown attribution or an area disappearing from a refreshed interval
    # must invalidate its prior fact instead of leaving a stale monetary row.
    for sid, record in candidate['series'].items():
        if record['identity']['epoch'] != share['epoch']:
            continue
        supplied = incoming.get(sid, {}).get('points', {})
        for stamp in stamps & set(record['points']) - set(supplied):
            if record['points'][stamp]['cost'] is not None:
                record['points'][stamp] = {'cost': None, 'membership_as_of': facts.get('membership_as_of'),
                    'tariff_revision': facts['context'].get('tariff_revision')}
                touched.add(sid); clear.add(sid)
    candidate['pending'] = ({'series': sorted(touched), 'clear': sorted(clear), 'epoch': share['epoch']}
                            if touched else None)
    validate_state(candidate, entry_id)
    return candidate, make_plan(candidate)


def make_plan(state):
    """Always reconstruct every later prefix from exact facts, including retry."""
    pending = state['pending']
    if pending is None:
        return []
    result = []
    for sid in pending['series']:
        record = state['series'][sid]
        parts = [(int(stamp), decimal_cost(fact['cost']))
                 for stamp, fact in sorted(record['points'].items(), key=lambda item: int(item[0]))
                 if fact['cost'] is not None]
        with localcontext() as context:
            # Enough significant digits for every finite term, their complete
            # fractional span and all possible carry digits; no prefix rounding.
            values = [value for _, value in parts]
            context.prec = max(40, (max(value.adjusted() for value in values)
                - min(value.as_tuple().exponent for value in values) + len(str(len(values))) + 3)
                if values else 40)
            total, rows = Decimal(0), []
            for stamp, value in parts:
                previous = total
                total += value
                decimal_cost(format(total, 'f'))
                if value != 0 and float(total) == float(previous):
                    raise ValueError('statistics_native_precision_exhausted')
                rows.append((stamp, format(total, 'f')))
        result.append({'statistic_id': sid, 'record': deepcopy(record), 'rows': rows,
                       'clear': sid in pending['clear']})
    return result


def verification_plan(state, facts):
    """Check only requested native hours; retain full ledger for any repair."""
    stamps = {str(int(hour['from'])) for hour in facts['hours']}
    probe = deepcopy(state)
    ids = sorted(sid for sid, record in probe['series'].items()
                 if record['identity']['epoch'] == probe['share']['epoch']
                 and set(record['points']) & stamps)
    probe['pending'] = {'series': ids, 'clear': [], 'epoch': probe['share']['epoch']}
    full = make_plan(probe)
    view = deepcopy(full)
    for item in view:
        item['record']['points'] = {stamp: value for stamp, value in item['record']['points'].items() if stamp in stamps}
        item['rows'] = [(stamp, total) for stamp, total in item['rows'] if str(stamp) in stamps]
    return probe, view
