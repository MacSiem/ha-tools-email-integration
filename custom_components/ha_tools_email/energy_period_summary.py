"""Whole-period zone/classification/peak facts from one existing priced stream.

No rate lookup, pricing, source IO, chart-cent sum or fixed-fee allocation.
Only zone/version scalar states plus one peak representative survive the loop.
"""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from .energy_zone_bins import ZoneBreakdown, zone_labels


def _text(value):
    return format(value, 'f') if value is not None else None


def build_period_summary(rows, period, active, totals):
    """Project validated accumulator hours; never accept frontend price guesses.

    Completeness is the household measurement proof for the captured [start,end)
    window. An unfinished calendar's future hours are outside this window.
    Monetary compatibility does not constrain the independent energy share.
    """
    with localcontext() as context:
        context.prec = 40
        zone = ZoneInfo(period['timezone'])
        breakdown = ZoneBreakdown(active, zone_labels(active))
        priced = active['rows'] if active is not None else ()
        cheap, classified = Decimal(0), Decimal(0)
        measured_hours = classified_hours = 0
        peak = None
        tied_count = 0
        for index, hour in enumerate(rows):
            price = priced[index] if index < len(priced) else {}
            breakdown.add(hour, price, index)
            quantity = hour['kwh']
            if quantity is None:
                continue
            quantity = Decimal(str(quantity))
            measured_hours += 1
            # A row can supply explicit classification even when several zones
            # share an equal price. Attribution and classification are distinct.
            aligned = (price.get('index') == index and price.get('from') == hour['from']
                       and price.get('to') == hour['to'])
            classification = price.get('classification') if price.get('available') and aligned else None
            if classification in ('cheap', 'expensive'):
                classified += quantity
                classified_hours += 1
                if classification == 'cheap':
                    cheap += quantity
            if peak is None or quantity > peak[2]:
                peak = (hour['from'], hour['to'], quantity)
                tied_count = 1
            elif quantity == peak[2]:
                tied_count += 1
                if hour['from'] < peak[0]:
                    peak = (hour['from'], hour['to'], quantity)

        complete = totals['measurement_complete'] is True
        total = Decimal(totals['kwh']) if complete and totals['kwh'] is not None else None
        share = {'status': 'unknown', 'reason': None,
                 'scope': 'complete_household_hours',
                 'classification_source': 'explicit_tariff_classification',
                 'known_cheap_kwh': _text(cheap) if measured_hours else None,
                 'classified_kwh': _text(classified) if measured_hours else None,
                 'total_kwh': _text(total), 'ratio': None}
        if not measured_hours:
            share['reason'] = 'no_recorded_hours'
        elif not complete:
            share.update(status='partial', reason='incomplete_household_measurements')
        elif total == 0:
            share['reason'] = 'zero_total_kwh'
        elif active is None:
            share['reason'] = 'tariff_unavailable'
        elif classified_hours != len(rows):
            share.update(status='partial' if classified_hours else 'unknown',
                         reason='incomplete_classification' if classified_hours else 'classification_unavailable')
        else:
            share.update(status='ready', ratio=_text(cheap / total))
        peak_hour = {'status': 'unknown', 'reason': 'no_recorded_hours',
                     'scope': 'complete_household_hours', 'id': None,
                     'from': None, 'to': None, 'start': None, 'end': None,
                     'local_start': None, 'local_end': None, 'fold': None,
                     'end_fold': None, 'kwh': None, 'tied_count': 0}
        if peak is not None:
            a, b, quantity = peak
            start = datetime.fromtimestamp(a / 1000, timezone.utc)
            end = datetime.fromtimestamp(b / 1000, timezone.utc)
            local_start, local_end = start.astimezone(zone), end.astimezone(zone)
            peak_hour.update(status='ready' if complete else 'partial',
                reason=None if complete else 'incomplete_period',
                id=f'{int(a)}:{int(b)}', **{'from': a, 'to': b},
                start=start.isoformat(), end=end.isoformat(),
                local_start=local_start.isoformat(), local_end=local_end.isoformat(),
                fold=local_start.fold, end_fold=local_end.fold,
                kwh=_text(quantity), tied_count=tied_count)
        period_zones = breakdown.finish({
            'complete': complete, 'recorded_kwh': totals['recorded_household_kwh'],
            'known_costs': active['known_costs'] if active is not None else {}})
        return {'window': {key: period[key] for key in ('start', 'end', 'timezone')},
                'zone_breakdown': period_zones, 'cheap_zone_share': share,
                'peak_hour': peak_hour}
