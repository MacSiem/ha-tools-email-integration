"""Small public entity facts from the current coordinator's accepted producers.

No recorder IO, policy principal, report cache, fees or synthetic allocation.
Full hourly evidence and actual non-overlapping five-minute tail cells end at
the current server cutoff. Historical report selections never enter.
"""
from decimal import Decimal

from .energy_live import build_current
from .tariff import EnergyTariff, TariffValidationError, _stamp
from .tariff_history import HistoricalTariffRates, migrate_tariff_v1, validate_tariff_book


def _interval_facts(engine, start, end, quantity):
    from .tariff_history import calculate_energy_amounts
    if engine is None:
        return None, None, None
    probe = {'from': start, 'to': end, 'kwh': 0}
    if isinstance(engine, HistoricalTariffRates):
        resolved = engine.resolve_interval(start, end)
        if not resolved['available']:
            return None, None, None
        model = next((item[3] for item in engine._compiled if item[2]['id'] == resolved['version_id']), None)
        priced = model.price_bucket(probe) if model is not None else {}
        semantics = resolved['rate_semantics']
        identity = resolved['currency'], resolved['price_basis']
    else:
        priced = engine.price_bucket(probe)
        semantics = 'components'
        identity = engine.profile.get('currency'), engine.profile.get('priceBasis')
    if not priced.get('available'):
        return None, None, None
    try:
        usage = calculate_energy_amounts(quantity, priced.get('energyRate'),
            priced.get('variableRate'), semantics)['usage_cost']
    except ValueError:
        usage = None
    return priced.get('classification'), usage, identity


def _today(result, measurement, engine, short_rows, cutoff):
    today = result['today']
    start, end = _stamp(today.get('from')), _stamp(today.get('to'))
    through = int(cutoff.timestamp() * 1000)
    facts = {'import_kwh': None, 'cheap_kwh': None, 'cheap_share_pct': None, 'usage_cost': None,
        'measurement_from': today.get('from'), 'measurement_through': cutoff.isoformat(),
        'resolution': 'completed_5minute', 'coverage_complete': False,
        'reason': 'measurement_incomplete'}
    if start is None or end is None or not start <= end <= through or through - start > 25 * 3600000:
        return facts
    if through == start:
        facts['reason'] = 'no_completed_interval'
        return facts
    if today.get('boundary_complete') is not True or (end - start) % 3600000:
        facts['reason'] = 'calendar_boundary_not_recorder_hour'
        return facts
    hourly = measurement.get('hourly_buckets', [])
    if (not isinstance(hourly, list) or len(hourly) > 25
            or (end > start and measurement.get('status') != 'ready')
            or len(hourly) != (end - start) // 3600000):
        return facts
    short_rows = short_rows or []
    if len(short_rows) > 11 or len(short_rows) != (through - end) // 300000:
        return facts
    expected = {point: point + 3600000 for point in range(int(start), int(end), 3600000)}
    expected.update({point: point + 300000 for point in range(int(end), through, 300000)})
    cells = {}
    for row in [*hourly, *short_rows]:
        a, b, value = _stamp(row.get('from')), _stamp(row.get('to')), row.get('kwh')
        if (a not in expected or a in cells or b != expected[a] or value is None
                or isinstance(value, bool) or row.get('unit', 'kWh') != 'kWh'):
            return facts
        try:
            quantity = Decimal(str(value))
        except (ValueError, TypeError, ArithmeticError):
            return facts
        if not quantity.is_finite() or not 0 <= quantity <= Decimal('1e24'):
            return facts
        cells[a] = (b, quantity)
    if set(cells) != set(expected):
        return facts
    total, cheap, usage = Decimal(0), Decimal(0), Decimal(0)
    classification_known, pricing_known, identities = True, True, set()
    for a, (b, quantity) in sorted(cells.items()):
        total += quantity
        classification, amount, identity = _interval_facts(engine, a, b, quantity)
        if classification not in ('cheap', 'expensive'):
            classification_known = False
        if classification == 'cheap':
            cheap += quantity
        if amount is None:
            pricing_known = False
        else:
            usage += amount
        if identity is not None:
            identities.add(identity)
    compatible = identities == {(result.get('currency'), result.get('price_basis'))}
    facts.update(import_kwh=float(total), coverage_complete=True,
        usage_cost=float(usage) if pricing_known and compatible else None,
        cheap_kwh=float(cheap) if classification_known else None,
        cheap_share_pct=float(100 * cheap / total) if classification_known and total else None,
        reason=None if classification_known and total else
            'zero_import' if classification_known else 'classification_unknown')
    return facts


def build_entity_snapshot(state, measurement, *, _legacy_builder=None, _short_rows=None, **kwargs):
    """Wrap existing pure snapshot jobs; return (snapshot, boundaries).

    Internal producer state never becomes an entity attribute. Existing snapshot
    money, IDs, next_cheap and binary semantics are retained. The new bounded
    entity_facts allowlist contains no meter IDs, profile, source matrix or ACL.
    """
    if _legacy_builder is None:
        from .energy import build_snapshot as _legacy_builder
    engine, book, options = None, None, []
    if 'book' in state:
        from .energy_book_snapshot import build_book_snapshot
        result, boundaries = build_book_snapshot(state, measurement, **kwargs)
        try:
            book = validate_tariff_book(state['book'])
            selected = next((row for row in book['tariffs'] if row['id'] == book.get('active_tariff_id')), None)
            if selected is not None:
                engine = HistoricalTariffRates(selected)
                stamp = int(kwargs['now'].timestamp() * 1000)
                current_version = next((item[2] for item in engine._compiled
                    if item[0] is not None and item[0] <= stamp and (item[1] is None or stamp < item[1])), None)
                if current_version is not None:
                    options = [row['id'] for row in current_version['profile']['zones']]
                    if current_version['profile'].get('dynamic') is not None:
                        options.append('dynamic')
        except TariffValidationError:
            pass
    else:
        result = _legacy_builder(state, measurement, **kwargs)
        candidate = EnergyTariff(state.get('profile'))
        boundaries = candidate._boundaries
        if candidate.valid:
            engine = candidate
            options = list(candidate.zones)
            if candidate.dynamic is not None:
                options.append('dynamic')
            try:
                # Existing schema-1 snapshot has component semantics. This
                # detached migration supplies history lookup, never storage.
                book = migrate_tariff_v1(state['profile'], legacy_combined=False)
            except TariffValidationError:
                pass
    from .energy_native_short import cutoff_at
    facts = _today(result, measurement, engine, _short_rows, cutoff_at(kwargs['now']))
    facts.update(zone_options=list(dict.fromkeys(options)), label=None, next_change=None, next_zone=None)
    if book is not None:
        current = build_current(book, now=kwargs['now'])
        facts.update(label=current['label'], next_change=current['next_change'], next_zone=current['next_zone_id'])
    result['entity_facts'] = facts
    return result, boundaries
