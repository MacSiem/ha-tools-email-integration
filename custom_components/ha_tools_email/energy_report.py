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
_CSS = ('body{margin:0;background:#eef2f7;color:#202a38;font:14px Arial,sans-serif}'
        'table{border-collapse:collapse;width:100%;table-layout:fixed}'
        '.report{max-width:640px;margin:0 auto;background:#fff}'
        'caption,th,td{text-align:left;padding:8px;vertical-align:top;overflow-wrap:anywhere}'
        'caption{font-size:17px;font-weight:bold;padding-top:24px}'
        'th{background:#edf2f8}td{border-bottom:1px solid #d8e0eb;white-space:pre-wrap}'
        '.header{background:#284c80;color:#fff;font-size:22px;padding:20px}'
        '.kpi{background:#eef4fc;font-size:20px;font-weight:bold}'
        '.note{color:#536274;font-size:12px}.bar-track{background:#e4e9f0}'
        '.bar-track td{padding:0;border:0;height:9px;line-height:9px}'
        '.bar-fill{background:#2877cb}.zone-0{background:#2877cb}'
        '.zone-1{background:#8553b9}.zone-2{background:#c16a18}'
        '.zone-3{background:#167d73}.unmeasured{background:#7b8595}'
        '@media(prefers-color-scheme:dark){body{background:#101318;color:#edf1f7}'
        '.report{background:#1b1f26}.header{background:#24446f;color:#fff}'
        'th,.kpi{background:#272f3d;color:#edf1f7}'
        'td{border-color:#485468}.note{color:#bdc7d6}'
        '.bar-track{background:#353d49}.bar-fill,.zone-0{background:#68aaff}'
        '.zone-1{background:#ba8ee7}.zone-2{background:#edaa63}'
        '.zone-3{background:#59c9b6}.unmeasured{background:#a6afbc}}')

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
    text_parts = [heading, label('snapshot_id') + ': ' + snapshot_id,
                  timezone_name + ' / ' + str(hour_cycle), note]
    html_parts = ['<!doctype html><html lang="' + language + '"><head><meta charset="utf-8">',
                  '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">',
                  '<meta name="viewport" content="width=device-width,initial-scale=1">',
                  '<style>' + _CSS + '</style></head><body>',
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
        return row[2] + (' ' + row[3] if row[3] else '') if row else unknown

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
        'currency_or_basis_mismatch': tr('Niezgodna waluta lub podstawa cen', 'Currency or price basis mismatch'),
    }
    numeric_titles = {
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
        if key in ('status', 'reason', 'comparison_scope', 'scope', 'price_basis'):
            return words.get(row[2], unknown)
        return row[2]

    deferred = []

    def detail(title_text, rows):
        if rows:
            deferred.append((title_text, rows))

    def table(title_text, rows, *, kpi=False):
        text_parts.append('\n' + title_text)
        html_parts.append('<table><caption>' + escape(title_text) + '</caption>')
        if not rows:
            rows = [(tr('Dane', 'Data'), unknown, '', None)]
        for caption, value, unit, bar in rows:
            text_parts.append(caption + ': ' + value + (' ' + unit if unit else ''))
            html_parts.append('<tr><th scope="row">' + escape(caption) + '</th><td'
                + (' class="kpi"' if kpi else '') + '>' + escape(value)
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

    def measurement_rows(parts):
        # Secondary measurements remain under their semantic owner; source-only
        # metadata and duplicate raw bucket streams remain in lossless CSV.
        prefix = path_of(parts) + '.'
        rows = []
        for path, caption, value, unit, numeric in facts.values():
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix):].split('.')
            if any(part in _TECHNICAL for part in relative):
                continue
            direct = path.replace('.known_costs.', '.')
            direct = direct.replace('.known_components.', '.components.')
            if direct != path and direct in facts and facts[direct][2:4] == (value, unit):
                continue
            if relative[-1] in ('reason',) and value == unknown:
                continue
            # Labels are explicit for known fields; unknown extensions stay CSV.
            if not (numeric or relative[-1] in _LABELS or relative[-1] in numeric_titles
                    or relative[-1] in ('complete', 'measurement_complete', 'pricing_complete',
                                       'components_complete', 'day', 'from', 'to', 'start', 'end')):
                continue
            display_caption = ' / '.join(title(part) if not part.isdigit()
                                         else str(int(part)+1) for part in relative)
            rows.append((display_caption, user_value((path, caption, value, unit, numeric)), unit, None))
        return rows

    def project(data, root=(), *, historical=False):
        table(tr('Okres raportu', 'Report period'), selected((*root, 'period'),
            ('start', 'end', 'partial', 'hours')))
        for key, heading_text in (
            ('kwh', tr('Zmierzona energia', 'Measured energy')),
            ('total_cost', tr('Koszt okresu', 'Period cost'))):
            row = fact((*root, 'totals', key))
            table(heading_text, [(title(key), row[2] if row else unknown,
                row[3] if row else '', None)] + selected((*root, 'totals'),
                ('measurement_complete',) if key == 'kwh' else ('effective_price', 'currency',
                 'price_basis', 'pricing_complete', 'components_complete')), kpi=True)
        # No period summary is published by schema-2. A zone called "night"
        # proves neither cheapness nor a cheap share; never sum rounded bins.
        table(tr('Udział taniej strefy', 'Cheap-zone share'), [(
            tr('Brak opublikowanego udziału za okres', 'No published period share'), unknown, '', None)])
        chart = data.get('chart') or {}
        bins = chart.get('bins') or []
        table(tr('Pomiary w czasie', 'Measured timeline'), selected((*root, 'chart'),
            ('status', 'reason', 'grain')) + selected((*root, 'coverage'), ('complete', 'ratio')))
        high = maximum(bins)
        timeline_rows = []
        zone_colors = {}
        for item in bins:
            proof = item.get('zone_breakdown') or {}
            for zone_row in proof.get('zones') or []:
                identity = (zone_row.get('version_id'), zone_row.get('zone_id'))
                zone_colors.setdefault(identity, 'zone-' + str(len(zone_colors) % 4))
        for index, item in enumerate(bins):
            parts = (*root, 'chart', 'bins', index)
            rows = selected(parts, ('start', 'end', 'kwh', 'recorded_kwh', 'complete',
                'energy_cost', 'variable_cost', 'legacy_combined_cost', 'usage_cost',
                'fixed_cost', 'expected_seconds', 'measured_seconds'))
            width = geometry(item.get('kwh'), high)
            proof = item.get('zone_breakdown') or {}
            proven_zones = proof.get('zones') or []
            color = ''
            if proof.get('status') == 'ready' and len(proven_zones) == 1 and proven_zones[0].get('complete') is True:
                proven = proven_zones[0]
                color = zone_colors[(proven.get('version_id'), proven.get('zone_id'))]
            if width is not None:
                energy = fact((*parts, 'kwh'))
                timeline_rows.append((amount((*parts, 'start')), energy[2] if energy else unknown,
                                      energy[3] if energy else '', (width, color)))
            else:
                energy = fact((*parts, 'kwh'))
                timeline_rows.append((amount((*parts, 'start')), energy[2] if energy else unknown,
                                      energy[3] if energy else '', None))
            detail(tr('Pomiary i koszty · ', 'Measurements and costs · ') + str(index+1), rows)
            proof = item.get('zone_breakdown')
            if isinstance(proof, Mapping):
                detail(tr('Przypisanie stref', 'Zone attribution'), selected((*parts, 'zone_breakdown'),
                    ('status', 'reason', 'currency', 'price_basis')))
                zones = proof.get('zones') or []
                zone_high = maximum(zones)
                for zi, zone_row in enumerate(zones):
                    zp = (*parts, 'zone_breakdown', 'zones', zi)
                    zone_rows = measurement_rows(zp)
                    width = geometry(zone_row.get('kwh'), zone_high)
                    if width is not None:
                        zone_rows.append((tr('Strefa', 'Zone'), amount((*zp, 'kwh')), '',
                                          (width, zone_colors[(zone_row.get('version_id'), zone_row.get('zone_id'))])))
                    detail(str(zone_row.get('label') or tr('Strefa', 'Zone')), zone_rows)
                detail(tr('Bez przypisania do strefy', 'Unattributed to a zone'),
                      measurement_rows((*parts, 'zone_breakdown', 'unattributed')))
        table(tr('Zużycie godzinowe / dzienne', 'Hourly / daily energy'), timeline_rows)
        groups = data.get('groups') or {}
        table(tr('Z sieci do urządzeń', 'From the grid to devices'),
            selected((*root, 'totals'), ('recorded_household_kwh',)) +
            selected((*root, 'groups'), ('partition_complete', 'currency', 'price_basis')))
        for kind, caption in (('devices', tr('Urządzenia według zużycia', 'Devices by energy')),
                              ('unassigned', label('unassigned')), ('unmeasured', label('unmeasured'))):
            values = groups.get(kind) or {}
            if kind == 'devices':
                def rank(pair):
                    try:
                        value = Decimal(str(pair[1].get('kwh')))
                        return (0, value.copy_negate(), str(pair[0])) if value.is_finite() else (1, Decimal(0), str(pair[0]))
                    except (InvalidOperation, ValueError, TypeError):
                        return (1, Decimal(0), str(pair[0]))
                high = maximum(values.values())
                table(caption, []) if not values else None
                for identity, item in sorted(values.items(), key=rank):
                    parts = (*root, 'groups', kind, identity)
                    rows = selected(parts, ('kwh', 'usage_cost', 'measurement_complete'))
                    detail(str(item.get('name') or identity), measurement_rows(parts))
                    width = geometry(item.get('kwh'), high)
                    if width is not None:
                        rows.append((label('kwh'), amount((*parts, 'kwh')), '', (width, '')))
                    table(caption + ' · ' + str(item.get('name') or identity), rows)
            else:
                rows = selected((*root, 'groups', kind), ('kwh', 'recorded_kwh', 'usage_cost', 'measurement_complete'))
                detail(caption, measurement_rows((*root, 'groups', kind)))
                width = geometry(values.get('kwh'), maximum([values]))
                if width is not None:
                    rows.append((label('kwh'), amount((*root, 'groups', kind, 'kwh')), '',
                                 (width, 'unmeasured' if kind == 'unmeasured' else '')))
                table(caption, rows)
        comparisons = data.get('comparisons') or {}
        if not historical:
            table(tr('Wybrane porównania', 'Selected comparisons'), []) if not comparisons else None
            for kind, comparison in comparisons.items():
                comparison_title = {
                    'previous': tr('Poprzedni okres', 'Previous period'),
                    'same_weekday': tr('Ten sam dzień tygodnia', 'Same weekday'),
                    'mean7': tr('Średnia 7 dni', '7-day mean'),
                }.get(kind, tr('Wybrany okres bazowy', 'Selected baseline'))
                cp = (*root, 'comparisons', kind)
                table(tr('Wybrane porównania', 'Selected comparisons') + ' · ' + comparison_title,
                      selected(cp, ('status', 'reason', 'comparison_scope', 'current_kwh',
                      'baseline_kwh', 'delta_kwh', 'percent', 'full_current_kwh', 'full_baseline_kwh')))
                detail(tr('Koszty porównania', 'Comparison costs'), measurement_rows((*cp, 'costs')))
                detail(tr('Pokrycie porównania', 'Comparison coverage'), measurement_rows((*cp, 'coverage')))
                detail(tr('Okresy bazowe', 'Baseline periods'), measurement_rows((*cp, 'baseline_periods')))
                for group in ('devices', 'areas', 'floors', 'unassigned', 'unmeasured'):
                    entries = (comparison.get('groups') or {}).get(group) or {}
                    if group in ('unassigned', 'unmeasured'):
                        detail(label(group) + ' · ' + comparison_title,
                              measurement_rows((*cp, 'groups', group)))
                    else:
                        for identity in entries:
                            detail(label(group) + ' · ' + str(names[group].get(identity) or identity)
                                  + ' · ' + comparison_title,
                                  measurement_rows((*cp, 'groups', group, identity)))
                baseline = comparison.get('baseline_analysis')
                if isinstance(baseline, Mapping):
                    detail(label('baseline_analysis') + ' · ' + comparison_title,
                           measurement_rows((*cp, 'baseline_analysis', 'period'))
                           + measurement_rows((*cp, 'baseline_analysis', 'totals'))
                           + measurement_rows((*cp, 'baseline_analysis', 'fees'))
                           + measurement_rows((*cp, 'baseline_analysis', 'rates')))
                    for baseline_section in ('chart', 'groups', 'coverage', 'rounding', 'alternatives'):
                        detail(label('baseline_analysis') + ' · ' + comparison_title + ' · ' + label(baseline_section),
                               measurement_rows((*cp, 'baseline_analysis', baseline_section)))
        for kind, caption in (('areas', tr('Pokoje', 'Rooms')), ('floors', tr('Piętra', 'Floors'))):
            values = groups.get(kind) or {}
            table(caption, []) if not values else None
            high = maximum(values.values())
            for identity, item in values.items():
                rows = selected((*root, 'groups', kind, identity), ('kwh', 'usage_cost', 'measurement_complete'))
                detail(caption + ' · ' + str(names[kind].get(identity) or identity),
                       measurement_rows((*root, 'groups', kind, identity)))
                width = geometry(item.get('kwh'), high)
                if width is not None:
                    rows.append((label('kwh'), amount((*root, 'groups', kind, identity, 'kwh')), '', (width, '')))
                table(caption + ' · ' + str(names[kind].get(identity) or identity), rows)
        table(tr('Koszt i stawki', 'Cost and rates'), selected((*root, 'totals'),
            ('energy_cost', 'variable_cost', 'legacy_combined_cost', 'usage_cost', 'fixed_cost',
             'total_cost', 'effective_price', 'currency', 'price_basis', 'pricing_complete')))
        table(label('fees'), selected((*root, 'fees', 'components'),
              ('subscription', 'capacity', 'distribution', 'other', 'legacy_combined')))
        table(label('rates'), measurement_rows((*root, 'rates')))
        table(label('alternatives'), measurement_rows((*root, 'alternatives')))
        detail(label('fees'), measurement_rows((*root, 'fees')))
        detail(tr('Dokładność i pokrycie pomiarów', 'Precision and measurement coverage'),
              measurement_rows((*root, 'coverage')) + measurement_rows((*root, 'rounding')))

    project(snapshot)
    for detail_title, rows in deferred:
        table(tr('Szczegóły · ', 'Details · ') + detail_title, rows)
    table(tr('O raporcie', 'About this report'), [
        (label('snapshot_id'), snapshot_id, '', None),
        (tr('Wygenerowano', 'Generated'), amount(('generated_at',)), '', None),
        (tr('Sposób odczytu', 'How to read'), tr(
            'Brak pomiaru nie oznacza zera. Koszty pochodzą z zapisanej analizy; opłaty domu nie są dodawane do urządzeń.',
            'Missing measurements are not zero. Costs come from the captured analysis; household fees are not added to devices.'), '', None),
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
