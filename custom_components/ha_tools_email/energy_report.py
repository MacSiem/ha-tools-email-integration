"""Pure, lossless schema-2 energy report; authorization and delivery belong to callers.

All three formats use the same ordered facts. Prices, deltas and fee proration
are backend results, never recomputed here. Explicit source paths retain the
meaning of partial/common-measurement values and metadata in exported reports.
"""
from collections.abc import Mapping
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from html import escape
from io import StringIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# These are semantic numeric fields, not arbitrary strings that happen to be numbers.
_NUMBERS = frozenset(('kwh', 'energyRate', 'variableRate', 'effective_price', 'percent',
    'ratio', 'day_fraction', 'subscription', 'capacity', 'distribution', 'other',
    'legacy_combined', 'legacy_combined_monthly', 'quantum', 'current', 'baseline',
    'common_current', 'common_baseline', 'hours', 'calendar_hours', 'observed_hours',
    'expected_hours', 'expected_seconds', 'measured_seconds', 'offsetMinutes',
    'priority', 'fold', 'start_fold', 'end_fold', 'amount', 'recorded_amount', 'price',
    'expected_source_hours', 'measured_source_hours'))
_INSTANTS = frozenset(('generated_at', 'captured_at', 'membership_as_of', 'start',
    'end', 'local_start', 'local_end', 'calendar_end', 'calendar_local_end', 'observed_at'))
_LABELS = {
    'native_pricing': ('Fakty cenowe HA Energy', 'HA Energy pricing facts'),
    'period_cost': ('Koszt zapisany w HA', 'Cost recorded in HA'),
    'amount': ('Koszt całego okresu', 'Full-period recorded cost'),
    'recorded_amount': ('Zapisany podzbiór kosztu', 'Recorded cost subset'),
    'current_prices': ('Bieżące ceny · teraz', 'Current prices · now'),
    'price': ('Cena teraz', 'Price now'), 'observed_at': ('Czas odczytu', 'Observed at'),
    'components_status': ('Stan rozbicia kosztu', 'Cost breakdown status'),
    'fixed_fees_status': ('Stan opłat stałych', 'Fixed fees status'),
    'expected_source_hours': ('Oczekiwane godziny źródeł', 'Expected source hours'),
    'measured_source_hours': ('Zmierzone godziny źródeł', 'Measured source hours'),
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
_CSS = ('body{margin:0;background:#eef2f7;color:#202a38;font:14px Arial,sans-serif}'
        'table{border-collapse:collapse;width:100%;table-layout:fixed}'
        '.report{max-width:640px;margin:0 auto;background:#fff}'
        'caption,th,td{text-align:left;padding:5px;vertical-align:top;overflow-wrap:anywhere}'
        'caption{font-size:15px;font-weight:bold;padding-top:15px;padding-bottom:6px}'
        'th{background:#edf2f8}td{border-bottom:1px solid #d8e0eb;white-space:pre-wrap}'
        '.header{background:#284c80;color:#fff;font-size:21px;padding:16px}'
        '.period td{background:#284c80;color:#edf4ff;padding:0 16px 12px;border:0;font-size:12px}'
        '.kpis{margin-top:12px}.kpi-card{padding:5px;border:0}'
        '.kpi-card table{background:#eef4fc;border:1px solid #d8e0eb}'
        '.kpi-card td{padding:4px 7px;border:0}.kpi-label{font-size:12px;color:#536274}'
        '.kpi-number{font-size:20px;font-weight:bold}.kpi-unit{font-size:12px}'
        '.note{font-size:12px;color:#536274}.timeline .chart-column{padding:1px;vertical-align:bottom;border:0}'
        '.column{height:96px}.column td{padding:0;border:0;line-height:0;font-size:0}'
        '.axis td{padding:3px 0;border:0;font-size:12px;white-space:normal}'
        '.flow-source{margin-top:10px}.flow-source td,.flow-source th{padding:4px}'
        '.bar-track{background:#e4e9f0}.bar-track td{padding:0;border:0;height:7px;line-height:7px}'
        '.bar-fill,.zone-expensive{background:#2877cb}.zone-cheap{background:#25824b}'
        '.zone-unknown,.unmeasured{background:#7b8595}'
        '.device-0{background:#eb6834}.device-1{background:#6250d6}.device-2{background:#e87ba4}'
        '.device-3{background:#eda100}.device-4{background:#718096}'
        '[data-section="source-times"]{font-size:12px}'
        '@media(max-width:420px){.kpi-card{display:block;width:100%!important;box-sizing:border-box}'
        '.kpi-card table{table-layout:auto}.kpi-card .kpi-number{font-size:18px}'
        'caption,th,td{padding:4px}.header{font-size:19px}.axis td{font-size:12px}}'
        '@media(prefers-color-scheme:dark){body{background:#101318;color:#edf1f7}'
        '.report{background:#1b1f26}.header,.period td{background:#284c80;color:#fff}'
        'th,.kpi-card table{background:#272f3d;color:#edf1f7}'
        '.kpi-card table,td{border-color:#485468}.note,.kpi-label{color:#bdc7d6}'
        '.bar-track{background:#353d49}.bar-fill,.zone-expensive{background:#68aaff}'
        '.zone-cheap{background:#65ce8c}.zone-unknown,.unmeasured{background:#a6afbc}'
        '.device-0{background:#d95926}.device-1{background:#9085e9}.device-2{background:#d55181}'
        '.device-3{background:#c98500}.device-4{background:#a6afbc}}')

# Public producer metadata is useful in CSV; it is not the reading order of a mail.
_PRIVATE = frozenset(('context', 'user_id', 'user_ids', 'auth', 'credentials',
    'password', 'smtp', 'smtp_config', 'smtp_password', 'smtp_username',
    'recipients', 'access_token', 'refresh_token'))
_TECHNICAL = frozenset(('schema', 'snapshot_id', 'measurement_fingerprint',
    'source_signature', 'calculator_version', 'revision', 'registry_revision',
    'version_id', 'tariff_id', 'active_tariff_id', 'parent_id', 'area_id', 'floor_id',
    'domain_provenance', 'provenance', 'origin', 'reference', 'attribution',
    'membership_mode', 'membership_as_of', 'captured_at', 'name', 'area_name',
    'floor_name', 'local_start', 'local_end', 'calendar_local_end', 'zone_id',
    'defaultZone', 'id'))


def validate_report_preferences(language, timezone_name, hour_cycle, *, date_language=None):
    """Validate caller display settings without IO; return the explicit ZoneInfo.

    Transports can preflight before Recorder IO. No language, clock or timezone
    preference is inferred from the host, the measurements or tariff profiles.
    """
    if date_language is not None and (type(date_language) is not str or date_language not in ('pl', 'en')):
        raise ValueError('report_date_language_required')
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


def render_energy_analysis(snapshot, *, language, timezone_name, hour_cycle, date_language=None):
    """Return snapshot_id/html/text/csv without IO, mutation, activation or pricing.

    language is exactly pl/en; hour_cycle is exactly the string '12'/'24'.
    timezone_name is an explicit IANA timezone. Naive source timestamps fail
    closed: the caller must supply an aware producer DTO, never a server default.
    Scalar facts retain producer paths; optional/future sections are traversed so
    field additions cannot silently disappear from exports. Operational context
    (including access_purpose) is intentionally omitted from exported content.
    """
    zone = validate_report_preferences(language, timezone_name, hour_cycle, date_language=date_language)
    if (not isinstance(snapshot, Mapping) or snapshot.get('schema') != 2
            or not isinstance(snapshot.get('snapshot_id'), str) or not snapshot['snapshot_id']):
        raise ValueError('schema2_snapshot_required')
    pl = language == 'pl'
    dates_pl = (date_language or language) == 'pl'
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
        day = calendar_date(stamp.date().isoformat())
        clock = stamp.strftime('%I:%M:%S %p' if str(hour_cycle) == '12' else '%H:%M:%S')
        # Numeric offset disambiguates repeated DST hours; never trust local_start
        # or fold as a substitute for converting the actual aware instant.
        return f'{day} {clock} {stamp.strftime("%z")} [{timezone_name}]'

    def calendar_date(value):
        day = date.fromisoformat(value)
        months = (('sty','lut','mar','kwi','maj','cze','lip','sie','wrz','paź','lis','gru')
                  if dates_pl else ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'))
        return (f'{day.day} {months[day.month-1]} {day.year}' if dates_pl
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
                    and current.get('price_basis') and current.get('price_basis') == baseline.get('price_basis') else None)
            # Each analysis (including historical baseline) has its own currency.
            if isinstance(value.get('totals'), Mapping):
                currency = value['totals'].get('currency')
            if not value:
                rows.append(('.'.join(path), caption, empty, '', False))
            for child, item in value.items():
                if child in _PRIVATE:
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
            # Price coverage counters use component names, but measure time.
            if key.endswith('_seconds') or 'priced_source_seconds' in path:
                unit = 's'
            elif key.endswith('kwh'):
                unit = 'kWh'
            elif key in ('amount', 'recorded_amount', 'price') and 'native_pricing' in path:
                unit = (currency or unknown) + ('/kWh' if key == 'price' else '')
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
        if key in _PRIVATE:
            continue
        rows = []
        walk(value, (key,), label(key), None if key == 'comparisons' else total_currency, rows)
        sections.append((label(key), rows))
    snapshot_id = snapshot['snapshot_id']
    note = ('Alternatywy: te same pomiary; raport nie aktywuje taryfy.' if pl else
            'Alternatives use the same measurements; this report does not activate a tariff.')
    heading = label('report')
    # All presentation values below refer to this one lossless fact projection.
    facts = {row[0]: row for _, rows in sections for row in rows}
    text_parts = [heading]
    html_parts = ['<!doctype html><html lang="' + language + '"><head><meta charset="utf-8">',
                  '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">',
                  '<meta name="viewport" content="width=device-width,initial-scale=1">',
                  '<title>' + escape(heading) + '</title><style>' + _CSS + '</style></head><body>',
                  '<table class="report" role="presentation" width="100%"><tr><td>',
                  '<table role="presentation"><tr><th class="header">' + escape(heading) + '</th></tr></table>']

    def tr(pl_text, en_text):
        return pl_text if pl else en_text

    def path_of(parts):
        return '.'.join(str(part) for part in parts)

    def fact(parts):
        return facts.get(path_of(parts))

    def amount(parts):
        row = fact(parts)
        return user_value(row) + (' ' + row[3] if row[3] else '') if row else unknown

    # Known status/scope values get human explanations. Future codes remain in
    # CSV; no technical code is promoted to a user-facing diagnosis or price.
    words = {
        'ready': tr('Gotowe', 'Ready'),
        'partial': tr('Niepełne dane', 'Incomplete data'),
        'unknown': unknown,
        'common_measurements': tr('Tylko wspólne pomiary', 'Common measurements only'),
        'calendar_totals': tr('Cały okres kalendarzowy', 'Full calendar period'),
        'per_group_calendar_totals': tr('Okres kalendarzowy odbiornika', 'Consumer calendar period'),
        'gross': tr('Brutto', 'Gross'), 'net': tr('Netto', 'Net'),
        'incomparable_currency_or_basis': tr('Różna waluta lub podstawa cen', 'Different currency or price basis'),
        'currency_mismatch': tr('Różne waluty', 'Different currencies'),
        'missing_measurement': tr('Brak pomiaru', 'Missing measurement'),
        'incomplete_household_measurements': tr('Niekompletne pomiary domu', 'Incomplete household measurements'),
        'incomplete_classification': tr('Niepełna klasyfikacja stref', 'Incomplete zone classification'),
        'classification_unavailable': tr('Brak klasyfikacji stref', 'Zone classification unavailable'),
        'zero_total_kwh': tr('Zerowe zużycie okresu', 'Zero period consumption'),
        'no_recorded_hours': tr('Brak zapisanych godzin', 'No recorded hours'),
        'tariff_unavailable': tr('Taryfa niedostępna', 'Tariff unavailable'),
        'currency_or_basis_mismatch': tr('Niezgodna waluta lub podstawa cen', 'Currency or price basis mismatch'),
    }
    numeric_titles = {
        'known_cheap_kwh': tr('Znana energia w taniej strefie', 'Known cheap-zone energy'),
        'classified_kwh': tr('Energia ze znaną klasyfikacją', 'Energy with known classification'),
        'total_kwh': tr('Energia całego okresu', 'Full period energy'),
        'ratio': tr('Udział (0–1)', 'Share (0–1)'),
        'current_kwh': tr('Energia bieżąca w porównaniu', 'Current comparison energy'),
        'baseline_kwh': tr('Energia bazowa w porównaniu', 'Baseline comparison energy'),
        'full_current_kwh': tr('Energia całego bieżącego okresu', 'Full current period energy'),
        'full_baseline_kwh': tr('Energia całego okresu bazowego', 'Full baseline period energy'),
        'own_current_kwh': tr('Własne bieżące pomiary', 'Own current measurements'),
        'own_baseline_kwh': tr('Własne bazowe pomiary', 'Own baseline measurements'),
        'delta_kwh': tr('Zmiana energii', 'Energy change'),
        'delta_cost': tr('Zmiana kosztu zużycia', 'Usage cost change'),
        'delta_total_cost': tr('Zmiana kosztu całkowitego', 'Total cost change'),
        'percent': tr('Zmiana procentowa (%)', 'Percentage change (%)'),
        'subscription': tr('Abonament', 'Subscription'),
        'capacity': tr('Opłata mocowa', 'Capacity fee'),
        'distribution': tr('Dystrybucja', 'Distribution'),
        'other': tr('Pozostałe opłaty', 'Other fees'),
        'start': tr('Od', 'From'), 'end': tr('Do (wyłącznie)', 'Until (exclusive)'),
        'from': tr('Od', 'From'), 'to': tr('Do (wyłącznie)', 'Until (exclusive)'),
        'complete': tr('Kompletne pomiary', 'Complete measurements'),
        'measurement_complete': tr('Kompletne pomiary', 'Complete measurements'),
        'pricing_complete': tr('Kompletna wycena', 'Complete pricing'),
        'components_complete': tr('Kompletne składniki', 'Complete components'),
        'day': tr('Dzień', 'Day'), 'day_fraction': tr('Naliczona część dnia', 'Charged day fraction'),
        'recorded_kwh': tr('Zmierzona energia', 'Recorded energy'),
        'scope': tr('Zakres', 'Scope'), 'grain': tr('Krok pomiaru', 'Measurement interval'),
        'label': tr('Nazwa strefy', 'Zone name'),
        'tariff_name': tr('Taryfa', 'Tariff'),
        'zone': tr('Strefa', 'Zone'), 'classification': tr('Klasyfikacja', 'Classification'),
        'hour': tr('Godzina', 'Hour'), 'day': tr('Dzień', 'Day'),
    }

    def title(key):
        return numeric_titles.get(key, label(key))

    def user_value(row):
        key = row[0].rsplit('.', 1)[-1]
        if key in ('status', 'reason', 'comparison_scope', 'scope', 'price_basis', 'comparison_reason'):
            return words.get(row[2], unknown)
        if row[4] and (key.startswith('delta_') or key == 'percent'):
            # A sign is presentation, never a recalculated comparison. The
            # lossless source fact and CSV keep the producer's original value.
            if not row[2].startswith(('-', '+')) and Decimal(row[2].replace(',', '.')) > 0:
                return '+' + row[2]
        return row[2]

    def table(title_text, rows, *, section=None):
        text_parts.append('\n' + title_text)
        html_parts.append('<table' + (' data-section="' + section + '"' if section else '')
                          + '><caption>' + escape(title_text) + '</caption>')
        if not rows:
            rows = [(tr('Dane', 'Data'), unknown, '', None)]
        for caption, value, unit, bar in rows:
            text_parts.append(caption + ': ' + value + (' ' + unit if unit else ''))
            html_parts.append('<tr><th scope="row">' + escape(caption) + '</th><td>' + escape(value)
                + '</td><td>' + escape(unit) + '</td></tr>')
            if bar is not None:
                width, color = bar
                html_parts.append('<tr><td colspan="3"><table role="presentation" class="bar-track" width="100%"><tr>'
                    + '<td class="bar-fill ' + color + '" width="' + str(width) + '%" style="width:'
                    + str(width) + '%">&#160;</td><td width="' + str(100-width) + '%">&#160;</td>'
                    + '</tr></table></td></tr>')
        html_parts.append('</table>')

    def selected(parts, keys):
        rows = []
        for key in keys:
            row = fact((*parts, key))
            if row:
                rows.append((title(key), user_value(row), row[3], None))
        return rows

    def geometry(value, maximum):
        # The only presentation arithmetic: dimensionless bar geometry.
        try:
            number = Decimal(str(value))
            if number.is_finite() and maximum.is_finite() and number >= 0 and maximum > 0:
                return max(0, min(100, int(number / maximum * 100)))
        except (InvalidOperation, ValueError, TypeError):
            pass
        return None

    def maximum(items):
        values = []
        for item in items:
            try:
                value = Decimal(str(item.get('kwh')))
                if value.is_finite() and value >= 0:
                    values.append(value)
            except (InvalidOperation, ValueError, TypeError):
                pass
        return max(values, default=Decimal(0))

    # Presentation consumes only the formatted fact map. Full technical audit
    # and duplicate baseline timelines remain in CSV, not in the email body.
    def short_stamp(parts, *, clock_only=False):
        row = fact(parts)
        if not row or row[2] == unknown:
            return unknown
        # instant() already owns language, calendar, zone and explicit clock.
        # Removing only its source suffix and zero seconds cannot move a day.
        value = row[2].rsplit(' [', 1)[0].rsplit(' ', 1)[0]
        value = value.replace(':00 AM', ' AM').replace(':00 PM', ' PM') if hour_cycle == '12' else value
        if hour_cycle == '24' and value.endswith(':00'):
            value = value[:-3]
        if clock_only:
            pieces = value.rsplit(' ', 2 if hour_cycle == '12' else 1)
            return ' '.join(pieces[-2:]) if hour_cycle == '12' else pieces[-1]
        return value

    def exact(parts):
        row = fact(parts)
        return user_value(row) if row else unknown

    def unit(parts):
        row = fact(parts)
        return row[3] if row else ''

    def compact_number(value, fallback):
        """Main-layer typography only; exact facts stay in details/text/CSV."""
        try:
            if isinstance(value, bool):
                return fallback
            number = Decimal(str(value))
            if not number.is_finite():
                return fallback
            if number == 0:
                return '0'
            result = format(number, '.1f')
            # A small known nonzero must not become a measured zero.
            if Decimal(result) == 0 and number != 0:
                result = format(number, 'f')
            if '.' in result:
                result = result.rstrip('0').rstrip('.')
            return result.replace('.', ',') if pl else result
        except (InvalidOperation, ValueError, TypeError):
            return fallback

    def ready_period_share():
        """Match the captured producer window and explicit complete-hour proof.

        Never infer a classification/share from zone names, prices or chart bins.
        The ratio is a producer fact, not known_cheap_kwh / total_kwh here.
        """
        summary = snapshot.get('period_summary')
        period = snapshot.get('period')
        totals = snapshot.get('totals')
        if not all(isinstance(item, Mapping) for item in (summary, period, totals)):
            return None
        window, share = summary.get('window'), summary.get('cheap_zone_share')
        if (not isinstance(window, Mapping) or not isinstance(share, Mapping)
                or any(window.get(key) != period.get(key) for key in ('start', 'end', 'timezone'))
                or not all(isinstance(period.get(key), str) for key in ('start', 'end', 'timezone'))
                or share.get('status') != 'ready' or 'reason' not in share or share.get('reason') is not None
                or share.get('scope') != 'complete_household_hours'
                or share.get('classification_source') != 'explicit_tariff_classification'
                or totals.get('measurement_complete') is not True
                or not isinstance(snapshot.get('coverage'), Mapping)
                or snapshot['coverage'].get('complete') is not True):
            return None
        try:
            ZoneInfo(period['timezone'])
            start = datetime.fromisoformat(period['start'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(period['end'].replace('Z', '+00:00'))
            if start.utcoffset() is None or end.utcoffset() is None or end <= start:
                return None
            values = [share.get(key) for key in ('known_cheap_kwh', 'classified_kwh', 'total_kwh', 'ratio')]
            values.append(totals.get('kwh'))
            if any(value is None or isinstance(value, bool) for value in values):
                return None
            cheap, classified, total, ratio, household = map(lambda value: Decimal(str(value)), values)
            if (not all(value.is_finite() for value in (cheap, classified, total, ratio, household))
                    or not 0 <= cheap <= classified or classified != total or total != household
                    or total <= 0 or not 0 <= ratio <= 1):
                return None
        except (InvalidOperation, ValueError, TypeError, ZoneInfoNotFoundError):
            return None
        return share

    def matrix(caption, headers, rows, *, section, row_role=None):
        text_parts.append('\n' + caption)
        html_parts.append('<table data-section="' + section + '"><caption>' + escape(caption)
                          + '</caption><tr>' + ''.join('<th scope="col">' + escape(h) + '</th>'
                          for h in headers) + '</tr>')
        for cells in rows:
            text_parts.append(' · '.join(h + ': ' + value for h, value in zip(headers, cells)))
            html_parts.append('<tr' + (' data-role="' + row_role + '"' if row_role else '') + '>'
                + ''.join('<' + ('th scope="row"' if index == 0 else 'td') + '>' + escape(value)
                          + ('</th>' if index == 0 else '</td>') for index, value in enumerate(cells)) + '</tr>')
        if not rows:
            html_parts.append('<tr><td colspan="' + str(len(headers)) + '">' + escape(unknown) + '</td></tr>')
            text_parts.append(unknown)
        html_parts.append('</table>')

    # A short period immediately follows the blue title; offset/IANA provenance
    # is retained once in source details rather than repeated across main cells.
    period_caption = tr('Okres raportu', 'Report period')
    period_value = short_stamp(('period', 'start')) + ' – ' + short_stamp(('period', 'end'))
    text_parts.append(period_caption + ': ' + period_value)
    html_parts.append('<table data-section="period" class="period"><tr><td>'
                      + escape(period_value) + '</td></tr></table>')
    native = snapshot.get('native_pricing') if not snapshot.get('active_tariff_id') else None
    if not isinstance(native, Mapping) or native.get('schema') != 1 or native.get('source') != 'ha_energy':
        native = None
    native_cost = native.get('period_cost', {}) if native else {}
    native_ready = (native_cost.get('status') == 'ready' and native_cost.get('coverage', {}).get('complete') is True
                    and native_cost.get('amount') is not None and native_cost.get('currency') is not None)
    native_note = tr('Źródło: Home Assistant Energy. Rozbicie kosztu i opłaty stałe są nieznane; to nie jest pełny rachunek.',
                     'Source: Home Assistant Energy. Cost breakdown and fixed fees are unknown; this is not a complete bill.')
    share = ready_period_share()
    comparison_names = {'previous': tr('Poprzedni okres', 'Previous period'),
        'same_weekday': tr('Ten sam dzień tygodnia', 'Same weekday'), 'mean7': tr('Średnia 7 dni', '7-day mean'),
        'year_ago': tr('Ten sam okres rok temu', 'Same period last year')}

    def number(value, *, signed=False):
        if value is None or isinstance(value, bool):
            return None
        try:
            value = Decimal(str(value))
            return value if value.is_finite() and (signed or value >= 0) else None
        except (InvalidOperation, TypeError, ValueError):
            return None

    def energy_context():
        totals = snapshot.get('totals') or {}
        if totals.get('measurement_complete') is True and snapshot.get('coverage', {}).get('complete') is True:
            for kind in [(snapshot.get('context') or {}).get('comparison_kind') or 'previous']:
                if kind not in comparison_names:
                    continue
                c = (snapshot.get('comparisons') or {}).get(kind) or {}
                coverage = c.get('coverage') or {}
                baselines = coverage.get('baselines') if kind == 'mean7' else [coverage.get('baseline')]
                if (c.get('status') != 'ready' or c.get('reason') is not None
                        or c.get('comparison_scope') != 'calendar_totals'
                        or number(coverage.get('current')) != 1 or not isinstance(baselines, list)
                        or len(baselines) != (7 if kind == 'mean7' else 1)
                        or any(number(v) != 1 for v in baselines)):
                    continue
                current, full, main = [number(v) for v in (c.get('current_kwh'), c.get('full_current_kwh'), totals.get('kwh'))]
                baseline, full_baseline = [number(c.get(key)) for key in ('baseline_kwh', 'full_baseline_kwh')]
                if (None in (current, full, main, baseline, full_baseline) or current != full or full != main
                        or baseline != full_baseline or number(c.get('delta_kwh'), signed=True) is None):
                    continue
                delta = number(c['delta_kwh'], signed=True)
                direction = tr('Spadek', 'Decrease') if delta < 0 else tr('Wzrost', 'Increase') if delta > 0 else tr('Bez zmian', 'No change')
                result = comparison_names[kind] + ': ' + direction + ' ' + amount(('comparisons', kind, 'delta_kwh'))
                if number(c.get('percent'), signed=True) is not None:
                    result += ' · ' + exact(('comparisons', kind, 'percent')) + '%'
                return result
        return tr('Brak porównywalnego pełnego okresu bazowego', 'No comparable full-period baseline')

    html_parts.append('<table data-section="kpis" role="presentation" class="kpis"><tr>')
    for metric, caption in (('kwh', tr('Zmierzona energia', 'Measured energy')),
                            ('total_cost', tr('Koszt okresu', 'Period cost')),
                            (None, tr('Udział taniej strefy', 'Cheap-zone share'))):
        value = exact(('totals', metric)) if metric else unknown
        units = unit(('totals', metric)) if metric else ''
        context = energy_context() if metric == 'kwh' else ''
        if metric == 'total_cost' and snapshot.get('active_tariff_id'):
            context = label('effective_price') + ': ' + amount(('totals', 'effective_price'))
        if metric == 'total_cost' and native_ready:
            caption = label('period_cost')
            value = exact(('native_pricing', 'period_cost', 'amount'))
            units = unit(('native_pricing', 'period_cost', 'amount'))
            context = native_note
        if metric is None:
            if share is not None:
                ratio = Decimal(str(share['ratio']))
                with localcontext() as precision:
                    precision.prec = max(40, len(ratio.as_tuple().digits) + 2)
                    value = compact_number(ratio.scaleb(2), unknown)
                units = '%'
                context = amount(('period_summary', 'cheap_zone_share', 'known_cheap_kwh'))
            else:
                raw_summary = snapshot.get('period_summary')
                raw_share = raw_summary.get('cheap_zone_share') if isinstance(raw_summary, Mapping) else None
                state = raw_share.get('status') if isinstance(raw_share, Mapping) else None
                context = ((words['partial'] + ' · ') if state == 'partial' else '')
                context += tr('Brak opublikowanego udziału za okres', 'No published period share')
        text_parts.extend([caption + ': ' + value + (' ' + units if units else ''), context])
        html_parts.append('<td class="kpi-card" width="33%"><table role="presentation">'
            + '<tr><td data-role="kpi-label" class="kpi-label">' + escape(caption) + '</td></tr>'
            + '<tr><td data-role="kpi-number" class="kpi-number">' + escape(value) + '</td></tr>'
            + '<tr><td class="kpi-unit">' + escape(units) + '</td></tr>'
            + ('<tr><td class="note">' + escape(context) + '</td></tr>' if context else '')
            + '</table></td>')
    html_parts.append('</tr></table>')

    chart = snapshot.get('chart') or {}
    bins = chart.get('bins') or []
    high = maximum(bins)
    # The public profile uses zone.id; measured rows use version_id + zone_id.
    # Conflicting/missing classifications cannot establish a semantic colour.
    colors = {}
    for rate in snapshot.get('rates') or []:
        profile = rate.get('profile') or {}
        for zone_row in profile.get('zones') or []:
            identity = (rate.get('version_id'), zone_row.get('id'))
            if not all(isinstance(value, str) and value for value in identity):
                continue
            classification = zone_row.get('classification')
            color = ('zone-' + classification if classification in ('cheap', 'expensive')
                     and profile.get('dynamic') is None else 'zone-unknown')
            colors[identity] = color if identity not in colors or colors[identity] == color else 'zone-unknown'
    timeline_title = tr('Pomiary w czasie', 'Measured timeline')
    chart_legend = {}
    text_parts.append('\n' + timeline_title)
    html_parts.append('<table data-section="timeline" class="timeline" role="presentation"><caption>'
                      + escape(timeline_title) + '</caption><tr data-role="plot">')
    for index, item in enumerate(bins):
        height = geometry(item.get('kwh'), high)
        height = height if height is not None else 0
        proof = item.get('zone_breakdown') or {}
        zones = proof.get('zones') or []
        color = 'zone-unknown'
        zone_label = tr('Nieznana strefa', 'Unknown zone')
        if proof.get('status') == 'ready' and len(zones) == 1 and zones[0].get('complete') is True:
            color = colors.get((zones[0].get('version_id'), zones[0].get('zone_id')), 'zone-unknown')
            zone_label = str(zones[0].get('label') or zone_label)
        elif len(zones) > 1:
            zone_label = tr('Mieszane strefy', 'Mixed zones') + ': ' + ', '.join(
                str(row.get('label') or tr('Nieznana strefa', 'Unknown zone')) for row in zones)
        chart_legend[(zone_label, color)] = None
        full_label = amount(('chart', 'bins', index, 'start'))
        point_value = amount(('chart', 'bins', index, 'kwh'))
        html_parts.append('<td class="chart-column" data-bin="' + str(index) + '" title="'
            + escape(full_label + ': ' + point_value, quote=True) + '"><table role="presentation" class="column" height="96">'
            + '<tr><td height="' + str(100-height) + '%" style="height:' + str(100-height) + '%"></td></tr>'
            + '<tr><td data-role="bar" class="bar-fill ' + color + '" height="' + str(height)
            + '%" style="height:' + str(height) + '%">' + ('&#160;' if height else '')
            + '</td></tr></table></td>')
    if not bins:
        html_parts.append('<td>' + escape(unknown) + '</td>')
    html_parts.append('</tr><tr class="axis">')
    # A label spans its interval group. Putting 05:00 in one 1/24-width
    # cell wraps digits on narrow mail viewports even when ticks are sparse.
    stride = (1 if chart.get('grain') == 'month' else 6
              if chart.get('grain') == 'day' or hour_cycle == '12' else 3)
    for index in range(0, len(bins), stride):
        point = short_stamp(('chart', 'bins', index, 'start'), clock_only=chart.get('grain') == 'hour')
        if chart.get('grain') != 'hour' and point != unknown:
            date_part = point.rsplit(' ', 2 if hour_cycle == '12' else 1)[0]
            if chart.get('grain') == 'month':
                words_in_date = date_part.split()
                point = words_in_date[1] if dates_pl else words_in_date[0]
            else:
                point = date_part.rsplit(' ', 1)[0].rstrip(',')
        span = min(stride, len(bins)-index)
        html_parts.append('<td colspan="' + str(span) + '">' + escape(point) + '</td>')
    html_parts.append('</tr></table>')

    text_parts.append('\n' + tr('Legenda stref wykresu', 'Chart zone legend'))
    html_parts.append('<table data-section="chart-zone-legend"><caption>'
                      + tr('Legenda stref wykresu', 'Chart zone legend') + '</caption>')
    for zone_label, color in chart_legend:
        text_parts.append(zone_label)
        html_parts.append('<tr><td data-role="zone-swatch" class="' + color + '" width="12" aria-hidden="true">&#160;</td><td>'
                          + escape(zone_label) + '</td></tr>')
    if not chart_legend:
        text_parts.append(unknown)
        html_parts.append('<tr><td>' + escape(unknown) + '</td></tr>')
    html_parts.append('</table>')

    groups = snapshot.get('groups') or {}
    def friendly_name(kind, identity, index, item=None):
        value = (item or {}).get('name') or names[kind].get(identity)
        # Native entity IDs are technical audit metadata, never display names.
        return str(value) if value and value != identity else title(kind) + ' ' + str(index+1)

    def rank(pair):
        try:
            value = Decimal(str(pair[1].get('kwh')))
            return (0, value.copy_negate(), str(pair[0])) if value.is_finite() else (1, Decimal(0), str(pair[0]))
        except (InvalidOperation, ValueError, TypeError):
            return (1, Decimal(0), str(pair[0]))

    ranking_values = []
    # Match shared-card identity order; changes in rank never recolor a device.
    device_colors = {identity: 'device-' + str(min(index, 4))
                     for index, identity in enumerate(sorted(groups.get('devices') or {}))}

    def ranking(caption, entries, *, section):
        # One semantic table for the complete ranking, not one table per device.
        text_parts.append('\n' + caption)
        html_parts.append('<table data-section="' + section + '"><caption>' + escape(caption)
            + '</caption><tr><th>' + tr('Odbiornik', 'Consumer') + '</th><th>kWh</th><th>'
            + tr('Koszt zużycia', 'Usage cost') + '</th></tr>')
        ceiling = maximum([item for _, _, item in entries])
        for name, parts, item in entries:
            energy = exact((*parts, 'kwh'))
            money = exact((*parts, 'usage_cost'))
            money_unit = unit((*parts, 'usage_cost'))
            text_parts.append(name + ': ' + energy + ' kWh; ' + money + ' ' + money_unit)
            ranking_values.append([caption + ' · ' + name, energy,
                exact((*parts, 'recorded_kwh')), money + (' ' + money_unit if money_unit else '')])
            brief = compact_number(item.get('kwh'), energy)
            html_name = name
            if parts[:2] == ('groups', 'floors'):
                captured = item.get('name') or names['floors'].get(parts[-1])
                if captured and captured != parts[-1]:
                    html_name = str(captured)
                elif parts[-1] == '__no_floor__':
                    html_name = tr('Bez piętra', 'No floor')
            html_parts.append('<tr><th scope="row">' + escape(html_name) + '</th><td>' + escape(brief)
                + '</td><td>' + escape(money) + ' ' + escape(money_unit) + '</td></tr>')
            width = geometry(item.get('kwh'), ceiling)
            if width is not None:
                color = ('unmeasured' if parts[-1] == 'unmeasured' else device_colors.get(parts[-1], '')
                         if parts[:2] == ('groups', 'devices') else '')
                html_parts.append('<tr><td colspan="3"><table role="presentation" class="bar-track" width="100%"><tr>'
                    + '<td class="bar-fill ' + color + '" width="' + str(width) + '%" style="width:' + str(width)
                    + '%">&#160;</td><td width="' + str(100-width) + '%"></td></tr></table></td></tr>')
        if not entries:
            html_parts.append('<tr><td colspan="3">' + escape(unknown) + '</td></tr>')
            text_parts.append(unknown)
        html_parts.append('</table>')

    device_entries = []
    # Household import is the source, not another ranked device to be summed.
    import_caption = tr('Z sieci', 'From the grid')
    text_parts.append(import_caption + ': ' + amount(('totals', 'recorded_household_kwh')))
    html_parts.append('<table class="flow-source" role="presentation"><tr><th>' + escape(import_caption)
                      + '</th><td>' + escape(amount(('totals', 'recorded_household_kwh'))) + '</td></tr></table>')
    for index, (identity, item) in enumerate(sorted((groups.get('devices') or {}).items(), key=rank)):
        device_entries.append((friendly_name('devices', identity, index, item), ('groups', 'devices', identity), item))
    # unassigned is an area aggregate of these same device rows, not another
    # exclusive device. Consumer kwh already contains any parent residual.
    if isinstance(groups.get('unmeasured'), Mapping):
        device_entries.append((label('unmeasured'), ('groups', 'unmeasured'), groups['unmeasured']))

    # Same energy-conservation proof as shared analysis flow: complete current
    # registry device partition + residual, never areas/floors/unassigned again.
    def partition(values, total):
        values = [number(value) for value in [*values, total]]
        if any(value is None for value in values):
            return False
        low = min(v.as_tuple().exponent for v in values)
        high = max(v.adjusted() for v in values)
        if high - low > 4096:
            return False
        with localcontext() as precision:
            precision.prec = max(40, high - low + len(str(len(values))) + 2)
            return sum(values[:-1], Decimal(0)) == values[-1]

    totals = snapshot.get('totals') or {}
    total = number(totals.get('kwh'))
    complete = (totals.get('measurement_complete') is True and snapshot.get('coverage', {}).get('complete') is True
                and total is not None and total == number(totals.get('recorded_household_kwh')))
    revision = groups.get('revision')
    group_ready = (complete and groups.get('energy_domain') == 'grid_import'
        and groups.get('partition_complete') is True and groups.get('membership_mode') == 'current_snapshot'
        and type(revision) is int and abs(revision) <= 9007199254740991 and groups.get('registry_revision') == revision
        and bool(device_entries) and all(item.get('measurement_complete') is True
            and number(item.get('kwh')) is not None and number(item.get('kwh')) == number(item.get('recorded_kwh'))
            for _, _, item in device_entries)
        and partition([item.get('kwh') for _, _, item in device_entries], totals.get('kwh')))

    def source_partition():
        summary, period = snapshot.get('period_summary') or {}, snapshot.get('period') or {}
        window, proof = summary.get('window') or {}, summary.get('zone_breakdown') or {}
        if (not complete or groups.get('energy_domain') != 'grid_import'
                or any(window.get(key) != period.get(key) for key in ('start', 'end', 'timezone'))
                or proof.get('status') != 'ready' or proof.get('reason') is not None
                or proof.get('scope') != 'complete_household_hours' or proof.get('fixed_fee_scope') != 'household_period_only'
                or proof.get('reconciles_recorded_kwh') is not True or proof.get('reconciles_known_costs') is not True
                or proof.get('currency') != totals.get('currency') or proof.get('price_basis') != totals.get('price_basis')
                or proof.get('price_basis') not in ('gross', 'net')):
            return []
        try:
            start = datetime.fromisoformat(period['start'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(period['end'].replace('Z', '+00:00'))
            ZoneInfo(period['timezone'])
            if start.utcoffset() is None or end.utcoffset() is None or end <= start:
                return []
            seconds = Decimal(str((end - start).total_seconds()))
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
            return []
        u = proof.get('unattributed') or {}
        if (u.get('recorded_kwh') is not None or u.get('measured_seconds') != 0
                or u.get('missing_seconds') != 0 or u.get('reasons') != []):
            return []
        rows, keys = proof.get('zones'), set()
        if not isinstance(rows, list) or not rows:
            return []
        for row in rows:
            key = (row.get('version_id'), row.get('zone_id'))
            if not all(isinstance(v, str) and v for v in key) or key in keys:
                return []
            keys.add(key)
            rates = [r for r in snapshot.get('rates') or [] if r.get('version_id') == key[0]]
            matches = [z for z in rates[0].get('profile', {}).get('zones', []) if z.get('id') == key[1]] if len(rates) == 1 else []
            duration = number(row.get('measured_seconds'))
            if (len(matches) != 1 or row.get('label') != matches[0].get('label')
                    or not isinstance(row.get('label'), str) or not snapshot.get('active_tariff_id')
                    or row.get('tariff_id') != snapshot.get('active_tariff_id')
                    or rates[0].get('currency') != proof.get('currency') or rates[0].get('price_basis') != proof.get('price_basis')
                    or duration is None or duration > seconds or row.get('complete') is not True
                    or number(row.get('kwh')) is None or number(row.get('kwh')) != number(row.get('recorded_kwh'))):
                return []
        return rows if partition([row.get('kwh') for row in rows], totals.get('kwh')) else []

    source_rows = source_partition()

    def composition(side, caption, entries, ready):
        text_parts.append('\n' + caption)
        html_parts.append('<table data-section="flow-' + side + '-composition"><caption>' + escape(caption) + '</caption>')
        if ready and total is not None and total > 0:
            positive = [(name, parts, item, color) for name, parts, item, color in entries if number(item.get('kwh')) > 0]
            html_parts.append('<tr><td colspan="2"><table role="presentation" class="bar-track" width="100%" aria-hidden="true"><tr>')
            for name, parts, item, color in positive:
                width = format(number(item['kwh']) / total * 100, '.6f').rstrip('0').rstrip('.')
                html_parts.append('<td data-role="flow-segment" class="' + color + '" width="' + width
                    + '%" style="width:' + width + '%">&#160;</td>')
            html_parts.append('</tr></table></td></tr>')
            for name, parts, item, color in positive:
                share = compact_number(number(item['kwh']) / total * 100, unknown) + '%'
                value = name + ': ' + amount((*parts, 'kwh')) + ' · ' + share
                text_parts.append(value)
                html_parts.append('<tr><td data-role="flow-swatch" width="12" class="' + color
                    + '" aria-hidden="true">&#160;</td><td>' + escape(value) + '</td></tr>')
        else:
            note = tr('Zerowe zużycie w okresie', 'No energy recorded in this period') if ready and total == 0 else unknown
            text_parts.append(note)
            html_parts.append('<tr><td>' + escape(note) + '</td></tr>')
        html_parts.append('</table>')

    composition('source', tr('Skąd', 'From'), [(row['label'], ('period_summary', 'zone_breakdown', 'zones', i), row,
        colors.get((row['version_id'], row['zone_id']), 'zone-unknown')) for i, row in enumerate(source_rows)], bool(source_rows))
    composition('destination', tr('Dokąd', 'To'), [(name, parts, item,
        'unmeasured' if parts[-1] == 'unmeasured' else device_colors[parts[-1]]) for name, parts, item in device_entries], group_ready)
    ranking(tr('Z sieci do urządzeń', 'From the grid to devices'), device_entries, section='device-ranking')

    comparisons = snapshot.get('comparisons') or {}
    comparison_names = {'previous': tr('Poprzedni okres', 'Previous period'),
        'same_weekday': tr('Ten sam dzień tygodnia', 'Same weekday'), 'mean7': tr('Średnia 7 dni', '7-day mean'),
        'year_ago': tr('Ten sam okres rok temu', 'Same period last year')}
    comparison_rows, comparison_details, comparison_group_rows = [], [], []
    for kind, comparison in comparisons.items():
        caption = comparison_names.get(kind, tr('Wybrany okres bazowy', 'Selected baseline'))
        cp = ('comparisons', kind)
        scope = fact((*cp, 'comparison_scope'))
        status = fact((*cp, 'status'))
        comparison_rows.append([caption, amount((*cp, 'current_kwh')), amount((*cp, 'baseline_kwh')),
            amount((*cp, 'delta_kwh')), user_value(scope) if scope else unknown])
        if status and user_value(status) != words['ready']:
            comparison_rows.append([user_value(status), '', '', '', ''])
        # Comparison costs keep each side's own currency; published deltas are
        # echoed, never derived from the displayed current and baseline amounts.
        for side in ('current', 'baseline'):
            for field in ('usage_cost', 'fixed_cost', 'total_cost', 'effective_price', 'price_basis'):
                row = fact((*cp, 'costs', side, field))
                if row:
                    comparison_details.append((caption + ' · ' + label(side) + ' · ' + title(field),
                                               user_value(row), row[3], None))
        for field in ('percent', 'full_current_kwh', 'full_baseline_kwh'):
            row = fact((*cp, field))
            if row:
                comparison_details.append((caption + ' · ' + title(field), user_value(row), row[3], None))
        for field in ('delta_cost', 'delta_total_cost', 'percent', 'status', 'reason', 'comparison_scope'):
            row = fact((*cp, 'costs', field))
            if row and not (field == 'reason' and row[2] == unknown):
                comparison_details.append((caption + ' · ' + title(field), user_value(row), row[3], None))
        for group in ('devices', 'areas', 'floors', 'unassigned', 'unmeasured'):
            entries = (comparison.get('groups') or {}).get(group) or {}
            entries = {None: entries} if group in ('unassigned', 'unmeasured') else entries
            for index, (identity, item) in enumerate(entries.items()):
                group_name = label(group) if identity is None else friendly_name(group, identity, index)
                parts = (*cp, 'groups', group) + (() if identity is None else (identity,))
                group_scope = fact((*parts, 'comparison_scope'))
                group_status = fact((*parts, 'status'))
                group_label = caption + ' · ' + group_name
                if group_scope:
                    group_label += ' · ' + user_value(group_scope)
                if group_status and user_value(group_status) != words['ready']:
                    group_label += ' · ' + user_value(group_status)
                comparison_group_rows.append([group_label] + [exact((*parts, field)) for field in
                    ('current_kwh', 'baseline_kwh', 'delta_kwh', 'own_current_kwh',
                     'own_baseline_kwh', 'full_current_kwh', 'full_baseline_kwh')])
        # Historical totals/rates are useful. Duplicated baseline hour streams,
        # masks and registry projections are the CSV audit, not another report.
        if isinstance(comparison.get('baseline_analysis'), Mapping):
            bp = (*cp, 'baseline_analysis')
            for field in ('kwh', 'usage_cost', 'fixed_cost', 'total_cost', 'effective_price', 'price_basis'):
                row = fact((*bp, 'totals', field))
                if row:
                    comparison_details.append((caption + ' · ' + label('baseline_analysis') + ' · ' + title(field),
                                               user_value(row), row[3], None))
            baseline = comparison['baseline_analysis']
            for field in ('subscription', 'capacity', 'distribution', 'other', 'legacy_combined'):
                row = fact((*bp, 'fees', 'components', field))
                if row:
                    comparison_details.append((caption + ' · ' + label('baseline') + ' · ' + label('fees')
                                               + ' · ' + title(field), row[2], row[3], None))
            for ri, rate in enumerate(baseline.get('rates') or []):
                rp = (*bp, 'rates', ri)
                for field in ('valid_from', 'valid_to', 'legacy_combined_monthly'):
                    row = fact((*rp, field))
                    if row:
                        comparison_details.append((caption + ' · ' + label('baseline') + ' · ' + title(field),
                                                   row[2], row[3], None))
                for field in ('subscription', 'capacity', 'distribution', 'other'):
                    row = fact((*rp, 'fixed_monthly', field))
                    if row:
                        comparison_details.append((caption + ' · ' + label('baseline') + ' · '
                                                   + label('fixed_monthly') + ' · ' + title(field), row[2], row[3], None))
                for zi, zone_row in enumerate((rate.get('profile') or {}).get('zones') or []):
                    for field in ('energyRate', 'variableRate'):
                        row = fact((*rp, 'profile', 'zones', zi, field))
                        if row:
                            comparison_details.append((caption + ' · ' + label('baseline') + ' · '
                                + str(zone_row.get('label') or tr('Strefa', 'Zone')) + ' · ' + title(field),
                                row[2], row[3], None))
    matrix(tr('Wybrane porównania', 'Selected comparisons'),
        [tr('Porównanie', 'Comparison'), label('current'), label('baseline'), tr('Zmiana', 'Change'),
         tr('Zakres', 'Scope')], comparison_rows, section='comparisons')
    for kind, caption in (('areas', tr('Pokoje', 'Rooms')), ('floors', tr('Piętra', 'Floors'))):
        entries = [(friendly_name(kind, identity, index), ('groups', kind, identity), item)
                   for index, (identity, item) in enumerate(sorted((groups.get(kind) or {}).items(), key=rank))]
        if kind == 'areas' and isinstance(groups.get('unassigned'), Mapping):
            entries.append((label('unassigned'), ('groups', 'unassigned'), groups['unassigned']))
        if isinstance(groups.get('unmeasured'), Mapping):
            entries.append((label('unmeasured'), ('groups', 'unmeasured'), groups['unmeasured']))
        ranking(caption, entries, section=kind)
    if native:
        table(label('period_cost'), selected(('native_pricing', 'period_cost'),
            ('status', 'amount', 'recorded_amount', 'components_status', 'fixed_fees_status', 'reason'))
            + [(tr('Informacja', 'Note'), native_note, '', None)], section='native-recorded-cost')
        price_rows = []
        for index, price in enumerate(native.get('current_prices', [])):
            price_rows += selected(('native_pricing', 'current_prices', index),
                ('source_id', 'price', 'observed_at', 'status', 'reason'))
        if price_rows:
            price_rows.append((tr('Informacja', 'Note'), tr(
                'Odczytane teraz; nie są cenami za wybrany okres. Nie obliczamy z nich kosztu historycznego.',
                'Observed now; not prices for the selected period. No historical cost is calculated from them.'), '', None))
            table(label('current_prices'), price_rows, section='native-current-prices')
    else:
        table(tr('Koszt i stawki', 'Cost and rates'), selected(('totals',),
            ('energy_cost', 'variable_cost', 'legacy_combined_cost', 'usage_cost', 'fixed_cost',
             'total_cost', 'effective_price', 'price_basis')))
        table(label('fees'), selected(('fees', 'components'),
            ('subscription', 'capacity', 'distribution', 'other', 'legacy_combined')))
    rate_rows = []
    for index, rate in enumerate(snapshot.get('rates') or []):
        rp = ('rates', index)
        for field in ('valid_from', 'valid_to', 'price_basis', 'legacy_combined_monthly'):
            row = fact((*rp, field))
            if row:
                rate_rows.append((str(index+1) + ' · ' + title(field), user_value(row), row[3], None))
        for field in ('subscription', 'capacity', 'distribution', 'other'):
            row = fact((*rp, 'fixed_monthly', field))
            if row:
                rate_rows.append((label('fixed_monthly') + ' · ' + title(field), row[2], row[3], None))
        for zi, zone_row in enumerate((rate.get('profile') or {}).get('zones') or []):
            for field in ('energyRate', 'variableRate'):
                row = fact((*rp, 'profile', 'zones', zi, field))
                if row:
                    rate_rows.append((str(zone_row.get('label') or tr('Strefa', 'Zone')) + ' · ' + title(field),
                                      row[2], row[3], None))
        for di, interval in enumerate(((rate.get('profile') or {}).get('dynamic') or {}).get('intervals') or []):
            dp = (*rp, 'profile', 'dynamic', 'intervals', di)
            context = exact((*dp, 'from')) + ' – ' + exact((*dp, 'to'))
            for field in ('energyRate', 'variableRate'):
                row = fact((*dp, field))
                if row:
                    rate_rows.append((label('dynamic') + ' · ' + context + ' · ' + title(field), row[2], row[3], None))
    table(label('rates'), rate_rows)
    for index, alternative in enumerate(snapshot.get('alternatives') or []):
        table(label('alternatives') + ' · ' + str(alternative.get('tariff_name') or index+1),
              selected(('alternatives', index), ('kwh', 'usage_cost', 'fixed_cost', 'total_cost',
                       'effective_price', 'delta_cost', 'comparison_reason')))

    # Intentional secondary sections, with no recursive fact/path dump.
    table(tr('Szczegóły · Jakość pomiarów i wyceny', 'Details · Measurement and pricing quality'),
          selected(('totals',), ('measurement_complete', 'pricing_complete', 'components_complete'))
          + selected(('coverage',), ('complete', 'ratio')), section='report-status')
    table(tr('Szczegóły · Udział taniej strefy', 'Details · Cheap-zone share'),
          selected(('period_summary', 'cheap_zone_share'),
              ('status', 'reason', 'known_cheap_kwh', 'classified_kwh', 'total_kwh', 'ratio')),
          section='period-share-details')
    matrix(tr('Szczegóły · Energia odbiorników', 'Details · Consumer energy'),
        [tr('Widok i odbiornik', 'View and consumer'), 'kWh', tr('Zapisane kWh', 'Recorded kWh'),
         label('usage_cost')], ranking_values, section='ranking-values')
    table(tr('Szczegóły · Porównania', 'Details · Comparisons'), comparison_details,
          section='comparison-details')
    matrix(tr('Szczegóły · Porównanie odbiorników (kWh)', 'Details · Consumer comparison (kWh)'),
        [tr('Odbiornik i zakres', 'Consumer and scope'), label('current'), label('baseline'),
         tr('Zmiana', 'Change'), tr('Własne bieżące', 'Own current'), tr('Własne bazowe', 'Own baseline'),
         tr('Cały bieżący', 'Full current'), tr('Cały bazowy', 'Full baseline')],
        comparison_group_rows, section='comparison-groups')
    measurement_values, bin_costs, partial_costs = [], [], []
    source_times = selected(('period',), ('start', 'end'))
    for index, item in enumerate(bins):
        bp = ('chart', 'bins', index)
        row = [short_stamp((*bp, 'start'), clock_only=chart.get('grain') == 'hour'),
               exact((*bp, 'kwh')), exact((*bp, 'recorded_kwh')), exact((*bp, 'usage_cost')), unit((*bp, 'usage_cost'))]
        measurement_values.append(row)
        clock_label = short_stamp((*bp, 'start'), clock_only=chart.get('grain') == 'hour')
        component_fields = ('energy_cost', 'variable_cost', 'legacy_combined_cost')
        bin_costs.append([clock_label] + [amount((*bp, field)) for field in component_fields])
        for field in component_fields:
            known = fact((*bp, 'known_costs', field))
            full = fact((*bp, field))
            if known and (not full or known[2:4] != full[2:4]):
                partial_costs.append((clock_label + ' · ' + label('known_costs') + ' · ' + title(field),
                                      known[2], known[3], None))
        # Keep proof of every captured offset/fold outside the main axis.
        source_times.append((tr('Pomiar ', 'Measurement ') + str(index+1), amount((*bp, 'start')), '', None))
    matrix(tr('Szczegóły · Zużycie godzinowe / dzienne', 'Details · Hourly / daily energy'),
        [tr('Czas', 'Time'), 'kWh', tr('Zapisane kWh', 'Recorded kWh'), label('usage_cost'), label('currency')],
        measurement_values, section='measurement-values', row_role='measurement')
    matrix(tr('Szczegóły · Koszty pomiarów', 'Details · Measurement costs'),
        [tr('Czas', 'Time'), label('energy_cost'), label('variable_cost'), label('legacy_combined_cost')],
        bin_costs, section='measurement-costs')
    if partial_costs:
        table(tr('Szczegóły · Znane częściowe koszty', 'Details · Known partial costs'), partial_costs)
    # Zone values remain per published bin; their rounded costs are never summed.
    zone_rows = []
    for index, item in enumerate(bins):
        proof = item.get('zone_breakdown') or {}
        for zi, zone_row in enumerate(proof.get('zones') or []):
            zp = ('chart', 'bins', index, 'zone_breakdown', 'zones', zi)
            caption = short_stamp(('chart', 'bins', index, 'start'), clock_only=chart.get('grain') == 'hour')
            caption += ' · ' + str(zone_row.get('label') or tr('Strefa', 'Zone'))
            for field in ('kwh', 'recorded_kwh', 'usage_cost', 'energy_cost', 'variable_cost', 'legacy_combined_cost', 'complete'):
                row = fact((*zp, field))
                if row:
                    zone_rows.append((caption + ' · ' + title(field), user_value(row), row[3], None))
            for field in ('energy_cost', 'variable_cost', 'legacy_combined_cost'):
                known = fact((*zp, 'known_costs', field))
                full = fact((*zp, field))
                if known and (not full or known[2:4] != full[2:4]):
                    zone_rows.append((caption + ' · ' + label('known_costs') + ' · ' + title(field),
                                      known[2], known[3], None))
        for field in ('status', 'reason'):
            row = fact(('chart', 'bins', index, 'zone_breakdown', field))
            if row and not (field == 'reason' and row[2] == unknown):
                zone_rows.append((str(index+1) + ' · ' + title(field), user_value(row), row[3], None))
        for field in ('recorded_kwh', 'measured_seconds', 'missing_seconds'):
            row = fact(('chart', 'bins', index, 'zone_breakdown', 'unattributed', field))
            if row:
                zone_rows.append((str(index+1) + ' · ' + tr('Bez przypisania do strefy', 'Unattributed to zone')
                                  + ' · ' + title(field), row[2], row[3], None))
        for field in ('energy_cost', 'variable_cost', 'legacy_combined_cost'):
            row = fact(('chart', 'bins', index, 'zone_breakdown', 'unattributed', 'known_costs', field))
            if row:
                zone_rows.append((str(index+1) + ' · ' + tr('Bez przypisania do strefy', 'Unattributed to zone')
                                  + ' · ' + label('known_costs') + ' · ' + title(field), row[2], row[3], None))
    if zone_rows:
        table(tr('Szczegóły · Zmierzone strefy', 'Details · Measured zones'), zone_rows)
    fee_rows = []
    for index, day in enumerate((snapshot.get('fees') or {}).get('days') or []):
        row = fact(('fees', 'days', index, 'day_fraction'))
        if row:
            fee_rows.append((exact(('fees', 'days', index, 'day')), row[2], row[3], None))
    if fee_rows:
        table(tr('Szczegóły · Naliczona część dnia', 'Details · Charged day fraction'), fee_rows)
    table(tr('Szczegóły · Czas i źródło', 'Details · Source times'), source_times, section='source-times')
    table(tr('O raporcie', 'About this report'), [
        (label('snapshot_id'), snapshot_id, '', None),
        (tr('Wygenerowano', 'Generated'), amount(('generated_at',)), '', None),
        (tr('Sposób odczytu', 'How to read'), tr(
            'Brak pomiaru nie oznacza zera. Opłaty domu nie są kosztami urządzeń ani godzin. Pełne dane źródłowe i dokładność są w CSV.',
            'Missing measurements are not zero. Household fees are not device or hourly costs. Full source data and precision are in CSV.'), '', None),
        (tr('Informacja', 'Note'), note, '', None)])
    output = StringIO(newline='')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerow(('snapshot_id', 'section', 'field', 'label', 'value', 'unit'))

    def csv_text(value):
        # Treat names, IDs, metadata and paths as untrusted spreadsheet text.
        probe = value.lstrip(' \t\r\n\ufeff')
        return "'" + value if probe.startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value

    for section_title, rows in sections:
        for path, caption, value, unit, numeric in rows:
            writer.writerow((csv_text(snapshot_id), csv_text(section_title), csv_text(path), csv_text(caption),
                             value if numeric else csv_text(value), csv_text(unit)))
    html_parts.append('</td></tr></table></body></html>')
    return {'snapshot_id': snapshot_id, 'html': ''.join(html_parts),
            'text': '\n'.join(text_parts) + '\n', 'csv': output.getvalue()}
