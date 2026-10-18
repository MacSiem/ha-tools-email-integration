"""Pure, lossless schema-2 energy report; authorization and delivery belong to callers.

All three formats use the same ordered facts. Prices, deltas and fee proration
are backend results, never recomputed here. Explicit source paths retain the
meaning of partial/common-measurement values and metadata in exported reports.
"""
from collections.abc import Mapping
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from io import StringIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# These are semantic numeric fields, not arbitrary strings that happen to be numbers.
_NUMBERS = frozenset(('kwh', 'energyRate', 'variableRate', 'effective_price', 'percent',
    'ratio', 'day_fraction', 'subscription', 'capacity', 'distribution', 'other',
    'legacy_combined', 'legacy_combined_monthly', 'quantum', 'current', 'baseline',
    'common_current', 'common_baseline', 'hours', 'calendar_hours', 'observed_hours',
    'expected_hours', 'expected_seconds', 'measured_seconds', 'offsetMinutes',
    'priority', 'fold', 'start_fold', 'end_fold'))
_INSTANTS = frozenset(('generated_at', 'captured_at', 'membership_as_of', 'start',
    'end', 'local_start', 'local_end', 'calendar_end', 'calendar_local_end'))
_LABELS = {
    'snapshot_id': ('Identyfikator migawki', 'Snapshot ID'),
    'report': ('Raport energii', 'Energy report'),
    'period': ('Okres', 'Period'), 'coverage': ('Pokrycie pomiarami', 'Measurement coverage'),
    'totals': ('Podsumowanie', 'Totals'), 'chart': ('Dane wykresu', 'Chart data'),
    'buckets': ('Pomiary godzinowe', 'Hourly measurements'),
    'groups': ('Odbiorniki i lokalizacje', 'Consumers and locations'),
    'devices': ('Urządzenia', 'Devices'), 'areas': ('Pokoje', 'Areas'),
    'floors': ('Piętra', 'Floors'), 'unassigned': ('Bez pokoju', 'Unassigned'),
    'unmeasured': ('Niezmierzone', 'Unmeasured'),
    'rates': ('Historia stawek', 'Rate history'),
    'fees': ('Opłaty naliczone za okres', 'Fees charged for the period'),
    'fixed_monthly': ('Stawki miesięczne', 'Monthly fee rates'),
    'components': ('Składniki', 'Components'), 'known_components': ('Znane składniki', 'Known components'),
    'comparisons': ('Porównania', 'Comparisons'), 'alternatives': ('Alternatywne taryfy', 'Alternative tariffs'),
    'baseline_analysis': ('Analiza okresu bazowego', 'Baseline analysis'),
    'current': ('Bieżący', 'Current'), 'baseline': ('Bazowy', 'Baseline'),
    'kwh': ('Energia', 'Energy'), 'recorded_kwh': ('Zarejestrowana energia', 'Recorded energy'),
    'recorded_household_kwh': ('Zarejestrowana energia domu', 'Recorded household energy'),
    'energy_cost': ('Koszt energii', 'Energy cost'),
    'variable_cost': ('Koszt zmienny', 'Variable cost'),
    'legacy_combined_cost': ('Koszt łączny dawnego modelu', 'Legacy combined cost'),
    'usage_cost': ('Koszt zużycia', 'Usage cost'), 'fixed_cost': ('Opłaty stałe', 'Fixed fees'),
    'total_cost': ('Koszt całkowity', 'Total cost'),
    'effective_price': ('Cena efektywna', 'Effective price'),
    'known_costs': ('Znane koszty', 'Known costs'),
    'valid_from': ('Obowiązuje od', 'Valid from'), 'valid_to': ('Obowiązuje do (wyłącznie)', 'Valid until (exclusive)'),
    'rules': ('Reguły', 'Rules'), 'exceptions': ('Wyjątki', 'Exceptions'),
    'dynamic': ('Stawki dynamiczne', 'Dynamic rates'), 'zones': ('Strefy', 'Zones'),
    'energyRate': ('Stawka energii', 'Energy rate'), 'variableRate': ('Stawka zmienna', 'Variable rate'),
    'status': ('Stan', 'Status'), 'reason': ('Powód', 'Reason'),
    'comparison_scope': ('Zakres porównania', 'Comparison scope'),
    'currency': ('Waluta', 'Currency'), 'price_basis': ('Podstawa ceny', 'Price basis'),
}
_CSS = ('body{margin:0;background:#fff;color:#17212b;font:14px Arial,sans-serif}'
        'table{border-collapse:collapse;width:100%;margin-bottom:20px}'
        'caption,th,td{text-align:left;padding:8px;vertical-align:top;overflow-wrap:anywhere}'
        'caption{font-size:18px;font-weight:bold}th,td{border:1px solid #b8c1cc}'
        'th{background:#edf1f5}td{white-space:pre-wrap}'
        '@media(prefers-color-scheme:dark){body{background:#14191f;color:#f2f4f6}'
        'th{background:#293440}th,td{border-color:#627080}}')


def validate_report_preferences(language, timezone_name, hour_cycle):
    """Validate caller display settings without IO; return the explicit ZoneInfo.

    Transports can preflight before Recorder IO. No language, clock or timezone
    preference is inferred from the host, the measurements or tariff profiles.
    """
    if type(language) is not str or language not in ('pl', 'en'):
        raise ValueError('report_language_required')
    if type(hour_cycle) is not str or hour_cycle not in ('12', '24'):
        raise ValueError('report_hour_cycle_required')
    if type(timezone_name) is not str or not timezone_name:
        raise ValueError('report_timezone_required')
    try:
        return ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as err:
        raise ValueError('report_timezone_required') from err


def render_energy_analysis(snapshot, *, language, timezone_name, hour_cycle):
    """Return snapshot_id/html/text/csv without IO, mutation, activation or pricing.

    language is exactly pl/en; hour_cycle is exactly the string '12'/'24'.
    timezone_name is an explicit IANA timezone. Naive source timestamps fail
    closed: the caller must supply an aware producer DTO, never a server default.
    Scalar facts retain producer paths; optional/future sections are traversed so
    field additions cannot silently disappear from exports. Operational context
    (including access_purpose) is intentionally omitted from exported content.
    """
    zone = validate_report_preferences(language, timezone_name, hour_cycle)
    if (not isinstance(snapshot, Mapping) or snapshot.get('schema') != 2
            or not isinstance(snapshot.get('snapshot_id'), str) or not snapshot['snapshot_id']):
        raise ValueError('schema2_snapshot_required')
    pl = language == 'pl'
    unknown = 'Brak danych' if pl else 'Unknown'
    empty = 'Brak pozycji' if pl else 'No entries'

    def label(key):
        pair = _LABELS.get(key)
        return pair[0 if pl else 1] if pair else key.replace('_', ' ')

    def instant(value):
        if isinstance(value, bool):
            raise ValueError('invalid_report_instant')
        if isinstance(value, (int, float, Decimal)):
            # Producer epoch is milliseconds; timedelta avoids float conversion.
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError('invalid_report_instant')
            stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(number * 1000))
        else:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if stamp.utcoffset() is None:
                raise ValueError('aware_report_instant_required')
        stamp = stamp.astimezone(zone)
        date = stamp.strftime('%d.%m.%Y' if pl else '%Y-%m-%d')
        clock = stamp.strftime('%I:%M:%S %p' if str(hour_cycle) == '12' else '%H:%M:%S')
        # Numeric offset disambiguates repeated DST hours; never trust local_start
        # or fold as a substitute for converting the actual aware instant.
        return f'{date} {clock} {stamp.strftime("%z")} [{timezone_name}]'

    def calendar_date(value):
        day = date.fromisoformat(value)
        months = (('sty','lut','mar','kwi','maj','cze','lip','sie','wrz','paź','lis','gru')
                  if pl else ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'))
        return (f'{day.day} {months[day.month-1]} {day.year}' if pl
                else f'{months[day.month-1]} {day.day}, {day.year}')

    def display(value, key, path):
        if value is None:
            return unknown, False
        if isinstance(value, bool):
            return ('Tak' if value else 'Nie') if pl else ('Yes' if value else 'No'), False
        if (key in _INSTANTS and isinstance(value, str) and 'T' in value
                or key in ('from', 'to') and any(part in path for part in ('bins', 'buckets', 'dynamic'))
                and (isinstance(value, (int, float, Decimal)) or isinstance(value, str) and 'T' in value)):
            return instant(value), False
        if (isinstance(value, str) and len(value) == 10
                and (key in ('valid_from', 'valid_to', 'validFrom', 'validTo', 'day') or 'dates' in path)):
            return calendar_date(value), False
        numeric = (isinstance(value, (int, float, Decimal)) or key in _NUMBERS
                   or key.endswith(('_cost', '_kwh', '_seconds')))
        if numeric:
            try:
                number = Decimal(str(value))
                if number.is_finite():
                    result = format(number, 'f')
                    return result.replace('.', ',') if pl else result, True
            except InvalidOperation:
                pass
        return str(value), False

    # Area/floor aggregates lack labels: use captured device registry labels, not
    # an external HA lookup. Keep the source ID as well for ambiguous names.
    registry = snapshot.get('groups', {}).get('devices', {})
    names = {'devices': {}, 'areas': {}, 'floors': {}}
    for entity, device in registry.items():
        names['devices'][entity] = device.get('name') or entity
        for kind, prefix in (('areas', 'area'), ('floors', 'floor')):
            identity = device.get(prefix + '_id')
            name = device.get(prefix + '_name')
            if identity is not None and name:
                names[kind].setdefault(identity, name)

    sections = []
    total_currency = snapshot.get('totals', {}).get('currency')

    def walk(value, path, caption, currency, rows):
        key = path[-1]
        if isinstance(value, Mapping):
            if 'currency' in value:
                currency = value['currency']
            # A delta may use a unit only when the producer sides explicitly
            # agree on currency and price basis. No currency conversion occurs.
            current, baseline = value.get('current'), value.get('baseline')
            if isinstance(current, Mapping) and isinstance(baseline, Mapping):
                currency = (current.get('currency') if current.get('currency')
                    and current.get('currency') == baseline.get('currency')
                    and current.get('price_basis') == baseline.get('price_basis') else None)
            # Each analysis (including historical baseline) has its own currency.
            if isinstance(value.get('totals'), Mapping):
                currency = value['totals'].get('currency')
            if not value:
                rows.append(('.'.join(path), caption, empty, '', False))
            for child, item in value.items():
                if child == 'context':
                    continue
                child = str(child)
                title = label(child)
                if key in names:
                    name = value[child].get('name') if isinstance(value[child], Mapping) else None
                    title = (name or names[key].get(child) or child) + f' [{child}]'
                walk(item, (*path, child), caption + ' / ' + title, currency, rows)
        elif isinstance(value, (list, tuple)):
            if not value:
                rows.append(('.'.join(path), caption, empty, '', False))
            for index, item in enumerate(value):
                walk(item, (*path, str(index)), caption + f' / {index + 1}', currency, rows)
        else:
            text, numeric = display(value, key, path)
            unit = ''
            if key.endswith('kwh'):
                unit = 'kWh'
            elif key.endswith('_cost') or key in ('energyRate', 'variableRate', 'effective_price', 'legacy_combined_monthly'):
                unit = currency or unknown
                if key in ('energyRate', 'variableRate', 'effective_price'):
                    unit += '/kWh'
                elif key == 'legacy_combined_monthly':
                    unit += '/miesiąc' if pl else '/month'
            elif any(part in path for part in ('fixed_monthly', 'components', 'known_components')) and key in _NUMBERS:
                unit = currency or unknown
                if 'fixed_monthly' in path:
                    unit += '/month' if not pl else '/miesiąc'
            rows.append(('.'.join(path), caption, text, unit, numeric))

    # General projection is deliberate: comparison masks/coverage, partial known
    # amounts, historical fees, rules, exceptions and future producer facts are
    # visible alongside the main numbers, with no presentation-side arithmetic.
    for key, value in snapshot.items():
        if key == 'context':
            continue
        rows = []
        walk(value, (key,), label(key), None if key == 'comparisons' else total_currency, rows)
        sections.append((label(key), rows))
    snapshot_id = snapshot['snapshot_id']
    note = ('Alternatywy: te same pomiary; raport nie aktywuje taryfy.' if pl else
            'Alternatives use the same measurements; this report does not activate a tariff.')
    heading = label('report')
    text_parts = [heading, label('snapshot_id') + ': ' + snapshot_id,
                  timezone_name + ' / ' + str(hour_cycle), note]
    html_parts = ['<!doctype html><html lang="' + language + '"><head><meta charset="utf-8">',
                  '<meta name="color-scheme" content="light dark"><style>' + _CSS + '</style>',
                  '</head><body><table role="presentation"><tr><th>' + escape(heading) + '</th></tr>',
                  '<tr><td>' + escape(snapshot_id) + '</td></tr><tr><td>' + escape(note) + '</td></tr>',
                  '<tr><td>' + escape(timezone_name + ' / ' + str(hour_cycle)) + '</td></tr></table>']
    output = StringIO(newline='')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerow(('snapshot_id', 'section', 'field', 'label', 'value', 'unit'))

    def csv_text(value):
        # Treat names, IDs, metadata and paths as untrusted spreadsheet text.
        probe = value.lstrip(' \t\r\n\ufeff')
        return "'" + value if probe.startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value

    for title, rows in sections:
        html_parts.append('<table><caption>' + escape(title) + '</caption><tr><th>'
                          + ('Pole' if pl else 'Field') + '</th><th>'
                          + ('Wartość' if pl else 'Value') + '</th><th>'
                          + ('Jednostka' if pl else 'Unit') + '</th></tr>')
        text_parts.append('\n' + title)
        for path, caption, value, unit, numeric in rows:
            text_parts.append(f'{caption}: {value}' + (f' {unit}' if unit else ''))
            html_parts.append('<tr><td>' + escape(caption) + '</td><td>'
                              + escape(value) + '</td><td>' + escape(unit) + '</td></tr>')
            writer.writerow((csv_text(snapshot_id), csv_text(title), csv_text(path), csv_text(caption),
                             value if numeric else csv_text(value), csv_text(unit)))
        html_parts.append('</table>')
    html_parts.append('</body></html>')
    return {'snapshot_id': snapshot_id, 'html': ''.join(html_parts),
            'text': '\n'.join(text_parts) + '\n', 'csv': output.getvalue()}
