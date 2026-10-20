"""Pure HA Energy recorded costs and explicitly current-only price projections.

No Recorder/HA imports or IO. Caller supplies authorized detached data. Historical
money comes exclusively from explicitly configured statistic change samples.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re

MAX_SOURCES = 512
MAX_PERIOD_HOURS = 366 * 24
MAX_CHUNK_HOURS = 168
MAX_ROWS_PER_SOURCE = 10000
MAX_VALUE = Decimal('1e18')


def _decimal(value, *, string=False):
    if type(value) not in ((int, float, str) if string else (int, float)):
        raise ValueError('invalid_number')
    text = str(value)
    if len(text) > 80:
        raise ValueError('invalid_number')
    try:
        result = Decimal(text)
    except InvalidOperation as err:
        raise ValueError('invalid_number') from err
    if not result.is_finite() or result.copy_abs() > MAX_VALUE or result.as_tuple().exponent < -18:
        raise ValueError('invalid_number')
    return result


def _canonical(value):
    if value == 0:
        return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _currency(value):
    return value is None or isinstance(value, str) and re.fullmatch(r'[A-Z]{3}', value) is not None


def _id(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and len(value) <= 255


def _instant(value, *, timestamp=False):
    try:
        if timestamp and type(value) in (int, float):
            # Reject subsecond/misaligned input before datetime rounds precision.
            if value % 3600:
                raise ValueError('unaligned_timestamp')
            value = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError('invalid_boundary')
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError) as err:
        raise ValueError('invalid_boundary') from err


def _boundary(value):
    value = _instant(value)
    if value.minute or value.second or value.microsecond:
        raise ValueError('unaligned_boundary')
    return value


def capture_native_pricing(preferences, grid_ids, currency):
    """Capture imports only; flat grid wins over legacy flow_from within a row.

    Missing configuration is normal. Duplicate energy maps (even identical) or a
    cost statistic shared by imports invalidate the whole capture, avoiding an
    arbitrary winner and accidental double billing. No derived cost ID is guessed.
    """
    result = {'currency': currency if _currency(currency) else None,
              'sources': [], 'valid': True, 'reason': None}

    def invalid(reason):
        return {**result, 'sources': [], 'valid': False, 'reason': reason}

    if not _currency(currency):
        return invalid('invalid_currency')
    if not isinstance(grid_ids, (list, tuple)) or len(grid_ids) > MAX_SOURCES:
        return invalid('invalid_grid_sources')
    if any(not _id(sid) for sid in grid_ids) or len(set(grid_ids)) != len(grid_ids):
        return invalid('invalid_grid_sources')
    selected, mapped, used_cost = set(grid_ids), {}, set()
    if preferences is None:
        preferences = {}
    if not isinstance(preferences, dict):
        return invalid('invalid_native_configuration')
    energy_sources = preferences.get('energy_sources', [])
    if not isinstance(energy_sources, list):
        return invalid('invalid_native_configuration')
    for source in energy_sources:
        if not isinstance(source, dict) or source.get('type') != 'grid':
            continue
        flows = [source] if source.get('stat_energy_from') else source.get('flow_from', [])
        if not isinstance(flows, list):
            return invalid('invalid_native_configuration')
        for flow in flows:
            if not isinstance(flow, dict):
                continue
            sid = flow.get('stat_energy_from')
            if not isinstance(sid, str) or sid not in selected:
                continue
            if sid in mapped:
                return invalid('duplicate_energy_source')
            cost, entity, number = flow.get('stat_cost'), flow.get('entity_energy_price'), flow.get('number_energy_price')
            if cost is not None and not _id(cost) or entity is not None and not _id(entity):
                return invalid('invalid_native_configuration')
            if cost is not None and cost in used_cost:
                return invalid('shared_cost_statistic')
            if cost is not None:
                used_cost.add(cost)
            if entity is not None and number is not None:
                return invalid('conflicting_current_price')
            try:
                number = _canonical(_decimal(number, string=True)) if number is not None else None
            except ValueError:
                return invalid('invalid_current_price')
            mapped[sid] = {'energy_id': sid, 'cost_statistic_id': cost,
                           'price_entity_id': entity, 'number_price': number}
    result['sources'] = [mapped.get(sid, {'energy_id': sid, 'cost_statistic_id': None,
        'price_entity_id': None, 'number_price': None}) for sid in grid_ids]
    return result


class NativeCostAccumulator:
    """Stream aligned contiguous chunks; retain no source-by-year matrix.

    Invalid samples/metadata leave holes, rather than zeroes. Duplicate rows
    permanently invalidate that source-hour within the current chunk. Structural
    boundary failures raise before updating the cursor or totals.
    """
    def __init__(self, configuration, period):
        self._configuration = deepcopy(configuration)
        self._start, self._end = _boundary(period['start']), _boundary(period['end'])
        hours = (self._end - self._start).total_seconds() / 3600
        if not 0 < hours <= MAX_PERIOD_HOURS:
            raise ValueError('invalid_period')
        self._hours = int(hours)
        self._cursor = self._start
        self._sources = self._configuration.get('sources', [])
        if not isinstance(self._sources, list) or len(self._sources) > MAX_SOURCES:
            raise ValueError('invalid_sources')
        self._valid = self._configuration.get('valid') is True
        self._currency = self._configuration.get('currency')
        ids, cost_ids = [], []
        for source in self._sources:
            if not isinstance(source, dict) or not _id(source.get('energy_id')):
                raise ValueError('invalid_sources')
            ids.append(source['energy_id'])
            cost = source.get('cost_statistic_id')
            if cost is not None:
                if not _id(cost):
                    raise ValueError('invalid_sources')
                cost_ids.append(cost)
        if len(set(ids)) != len(ids) or len(set(cost_ids)) != len(cost_ids):
            self._valid = False
        self._total = Decimal(0)
        self._measured = 0
        self._reason = self._configuration.get('reason') if not self._valid else None

    def add_chunk(self, raw, metadata, start, end):
        first, last = _boundary(start), _boundary(end)
        hours = (last - first).total_seconds() / 3600
        if first != self._cursor or not 0 < hours <= MAX_CHUNK_HOURS or last > self._end:
            raise ValueError('invalid_chunk_boundary')
        raw = raw if isinstance(raw, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        subtotal, measured, reason = Decimal(0), 0, None
        if self._valid:
            for source in self._sources:
                sid = source.get('cost_statistic_id')
                if sid is None:
                    reason = reason or 'cost_statistic_not_configured'
                    continue
                meta = metadata.get(sid)
                if isinstance(meta, tuple) and len(meta) == 2:
                    meta = meta[1]
                if (not isinstance(meta, dict) or meta.get('has_sum') is not True
                        or self._currency is None or not _currency(self._currency)
                        or meta.get('unit_of_measurement') != self._currency
                        or meta.get('unit_class') not in (None, 'monetary')):
                    reason = reason or 'incompatible_cost_metadata'
                    continue
                points = raw.get(sid)
                if not isinstance(points, list) or len(points) > MAX_ROWS_PER_SOURCE:
                    reason = reason or 'cost_statistics_unavailable'
                    continue
                seen, cells = set(), {}
                for point in points:
                    if not isinstance(point, dict):
                        continue
                    try:
                        stamp = _instant(point.get('start'), timestamp=True)
                    except ValueError:
                        continue
                    if stamp < first or stamp >= last or stamp.minute or stamp.second or stamp.microsecond:
                        continue
                    if stamp in seen:
                        cells.pop(stamp, None)
                        continue
                    seen.add(stamp)
                    try:
                        cells[stamp] = _decimal(point.get('change'))
                    except ValueError:
                        pass
                with localcontext() as context:
                    context.prec = 64
                    subtotal += sum(cells.values(), Decimal(0))
                measured += len(cells)
                if len(cells) != int(hours):
                    reason = reason or 'incomplete_cost_statistics'
        with localcontext() as context:
            context.prec = 64
            self._total += subtotal
        self._measured += measured
        self._reason = self._reason or reason
        self._cursor = last

    def finish(self):
        expected = self._hours * len(self._sources)
        configured = any(s.get('cost_statistic_id') is not None for s in self._sources)
        complete = self._valid and expected > 0 and self._measured == expected and self._cursor == self._end
        status = ('unavailable' if not self._valid else 'not_configured' if not configured else
                  'ready' if complete else 'partial' if self._measured else 'unavailable')
        recorded = _canonical(self._total) if self._measured else None
        reason = None if complete else (self._reason or ('native_cost_not_configured' if not configured
                                                        else 'incomplete_cost_statistics'))
        return {'schema': 1, 'source': 'ha_energy', 'period_cost': {
            'status': status, 'amount': recorded if complete else None,
            'recorded_amount': recorded, 'currency': self._currency,
            'scope': 'grid_import_recorded_cost',
            'coverage': {'complete': complete, 'expected_source_hours': expected,
                         'measured_source_hours': self._measured},
            'components_status': 'unknown', 'fixed_fees_status': 'unknown', 'reason': reason},
            'current_prices': [], 'provenance': [
                {'energy_source_id': s['energy_id'], 'cost_statistic_id': s['cost_statistic_id']}
                for s in self._sources if s.get('cost_statistic_id') is not None]}


def project_current_prices(configuration, states, observed_at):
    """Normalize current rates only; never reconstruct historical money."""
    observed = _instant(observed_at).isoformat()
    if configuration.get('valid') is not True:
        return []
    currency = configuration.get('currency')
    states = states if isinstance(states, dict) else {}
    result = []
    for source in configuration.get('sources', []):
        number, entity = source.get('number_price'), source.get('price_entity_id')
        if number is None and entity is None:
            continue
        origin = 'number_energy_price' if number is not None else 'entity_energy_price'
        price, reason = None, None
        try:
            if currency is None or not _currency(currency):
                raise ValueError('currency_unavailable')
            if number is not None:
                price = _decimal(number, string=True)
            else:
                state = states.get(entity)
                if not isinstance(state, dict) or not isinstance(state.get('state'), str):
                    raise ValueError('current_price_unavailable')
                factor = {f'{currency}/Wh': Decimal(1000), f'{currency}/kWh': Decimal(1),
                          f'{currency}/MWh': Decimal('0.001')}.get(state.get('unit_of_measurement'))
                if factor is None:
                    raise ValueError('incompatible_price_unit')
                with localcontext() as context:
                    context.prec = 64
                    price = _decimal(state['state'], string=True) * factor
            price = _canonical(price)
        except (ValueError, TypeError) as err:
            reason = str(err)
            price = None
        result.append({'source_id': source['energy_id'], 'origin': origin, 'price': price,
            'currency': currency, 'energy_unit': 'kWh', 'observed_at': observed,
            'historical': False, 'status': 'ready' if price is not None else 'unavailable', 'reason': reason})
    return result
