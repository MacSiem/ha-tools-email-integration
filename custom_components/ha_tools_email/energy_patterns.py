"""Day/hour Patterns from one active bill, never independent rate evaluation."""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from .energy_chart_bins import build_chart_bins
from .energy_consumers import COMPONENTS, _reconcile
from .energy_zone_bins import ZoneBreakdown, zone_labels


_HOUR = 3600000


def _text(value):
    if value == 0:
        return '0'
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _usage_complete(price, amounts):
    return ((price.get('rate_semantics') == 'components'
             and amounts.get('energy_cost') is not None and amounts.get('variable_cost') is not None)
            or (price.get('rate_semantics') == 'legacy_combined'
                and amounts.get('legacy_combined_cost') is not None))


def build_patterns_projection(rows, active, window):
    """Consume existing priced rows once; retain <=30 days and bounded hours."""
    zone = ZoneInfo(window['timezone'])
    compatible = active is not None and active['compatible']
    priced = active['rows'] if active is not None else []
    labels = zone_labels(active)
    days = build_chart_bins(rows, window, active)['bins']
    by_date = {}
    for index, hour in enumerate(rows):
        key = datetime.fromtimestamp(hour['from']/1000, zone).date().isoformat()
        by_date.setdefault(key, []).append(index)
    if (len(by_date) > window['days'] or any(
            key < window['first_date'] or key > window['last_date'] for key in by_date)):
        raise ValueError('patterns_local_dates_outside_window')
    hours = []
    with localcontext() as context:
        context.prec = 40
        for day in days:
            local = datetime.fromtimestamp(day['from']/1000, zone)
            key = local.date().isoformat()
            indices = by_date[key]
            raw = {}
            prices = {}
            for index in indices:
                hour = rows[index]
                price = priced[index] if index < len(priced) else {}
                matched = (price.get('index') == index and price.get('from') == hour['from']
                           and price.get('to') == hour['to'])
                prices[index] = price
                raw[index] = (price.get('costs', {})
                              if price.get('available') and matched and compatible else {})
            allocated = {}
            for component in COMPONENTS:
                target = day['known_costs'].get(component)
                values = {index: Decimal(raw[index][component]) for index in indices
                          if raw[index].get(component) is not None}
                allocated[component] = (_reconcile(values, Decimal(target))
                                        if target is not None and values else {})
            usage_known = all(_usage_complete(prices[index], raw[index]) for index in indices)
            for index in indices:
                hour, price = rows[index], prices[index]
                start = datetime.fromtimestamp(hour['from']/1000, timezone.utc)
                end = datetime.fromtimestamp(hour['to']/1000, timezone.utc)
                a, b = start.astimezone(zone), end.astimezone(zone)
                known = {component: allocated[component].get(index) for component in COMPONENTS}
                complete = hour['kwh'] is not None
                usage = (format(sum((Decimal(value) for value in known.values() if value is not None),
                                    Decimal(0)), '.2f')
                         if complete and _usage_complete(price, raw[index]) else None)
                reason = ('tariff_unavailable' if active is None else
                          (price.get('reason') or 'price_unavailable') if not price.get('available') else
                          'currency_or_basis_mismatch' if not compatible else
                          'priced_row_mismatch' if not raw[index] else None)
                row = {'id': str(hour['from']), 'local_date': key,
                       'from': hour['from'], 'to': hour['to'],
                       'start': start.isoformat(), 'end': end.isoformat(),
                       'local_start': a.isoformat(), 'local_end': b.isoformat(), 'fold': a.fold,
                       'timezone': window['timezone'], 'kwh': str(hour['kwh']) if complete else None,
                       'recorded_kwh': str(hour['kwh']) if complete else None, 'complete': complete,
                       'expected_seconds': 3600, 'measured_seconds': 3600 if complete else 0,
                       **{component: known[component] if complete else None for component in COMPONENTS},
                       'known_costs': known, 'usage_cost': usage,
                       'currency': active['currency'] if compatible else None,
                       'price_basis': active['price_basis'] if compatible else None,
                       'status': 'ready' if complete else 'unknown',
                       'reason': None if complete else 'missing_measurement',
                       'pricing_reason': reason, 'version_id': price.get('version_id'),
                       'tariff_zone': price.get('zone'), 'tariff_zones': list(price.get('zones', [])),
                       'zone_attribution': price.get('zoneAttribution'),
                       'crosses_local_date': a.date() != (end-timedelta(microseconds=1)).astimezone(zone).date(),
                       'raw_costs': {component: raw[index].get(component) for component in COMPONENTS}}
                breakdown = ZoneBreakdown(active, labels)
                breakdown.add(hour, price, index)
                row['zone_breakdown'] = breakdown.finish(row)
                hours.append(row)
            calendar_start = datetime.combine(local.date(), time.min, zone).astimezone(timezone.utc)
            calendar_end = datetime.combine(local.date()+timedelta(days=1), time.min, zone).astimezone(timezone.utc)
            a, b = int(calendar_start.timestamp()*1000), int(calendar_end.timestamp()*1000)
            calendar_complete = (a % _HOUR == 0 and b % _HOUR == 0
                                 and day['from'] == a and day['to'] == b)
            complete = day['complete'] and calendar_complete
            day.update(id=key, local_date=key, timezone=window['timezone'],
                       calendar_from=a, calendar_to=b, calendar_complete=calendar_complete,
                       expected_seconds=(b-a)/1000, complete=complete,
                       status='ready' if complete else 'partial',
                       reason=None if complete else ('incomplete_local_day' if not calendar_complete
                                                    else 'missing_measurement'))
            # Complete named totals and usage require a full local day, while
            # recorded energy and period-reconciled known subtotals stay useful.
            if not complete:
                day['kwh'] = None
                for component in COMPONENTS:
                    day[component] = None
                day['usage_cost'] = None
                part = day['zone_breakdown']
                if part['status'] == 'ready':
                    part.update(status='partial', reason=day['reason'])
                for row in part['zones']:
                    row.update(kwh=None, complete=False, usage_cost=None)
                    for component in COMPONENTS:
                        row[component] = None
            elif not usage_known:
                # Chart's legacy non-applicable zero is not a proof that the
                # missing variable component is known in component semantics.
                day['usage_cost'] = None
            # Fixed fees are household metadata, never repeated per pattern bin.
            day.pop('fixed_cost', None)
            day.pop('total_cost', None)
        weekdays = []
        for weekday in range(7):
            selected = [day for day in days if day['complete'] and
                        (datetime.fromtimestamp(day['from']/1000, zone).weekday()+1)%7 == weekday]
            quantity = (sum((Decimal(day['kwh']) for day in selected), Decimal(0))/len(selected)
                        if selected else None)
            weekdays.append({'weekday': weekday, 'count': len(selected),
                             'kwh': _text(quantity) if quantity is not None else None})
        return {'days': days, 'hours': hours, 'weekdays': weekdays,
                'represented_days': len(days),
                'measured_days': sum(day['recorded_kwh'] is not None for day in days),
                'complete_days': sum(day['complete'] for day in days),
                'partial_window': len(days) != window['days'] or any(not day['complete'] for day in days),
                'rounding_stage': 'period_components_then_day_then_hour',
                'fixed_fee_scope': 'household_period_only'}
