"""Presentation-only ha_tools.energy_report/1 renderer (Build 1, blocks 0–12).

No HA imports, input mutation, energy aggregation or tariff calculations. The optional
``daily`` list contains backend-computed {start,kwh,prev_kwh,zones:[{id,kwh}]} rows;
without it longer periods explicitly report that chart data is unavailable.
"""
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from . import energy_format as fmt
from .mail_frame import E, FONT, L, button, h2, hr, kpi_cell, paragraph, render_frame, safe_external_url, section

_LABELS = {
    'energy': ('Energia', 'Energy'), 'import': ('Zużycie (import)', 'Consumption (import)'),
    'usage': ('Koszt zużycia', 'Usage cost'), 'effective': ('cena efektywna', 'effective rate'),
    'cheap': ('W taniej strefie', 'In the cheaper zone'), 'zone': ('Strefa', 'Zone'),
    'comparison': ('Porównanie', 'Comparison'), 'hour_chart': ('Zużycie w ciągu doby', 'Consumption throughout the day'),
    'daily_chart': ('Zużycie w kolejnych dniach', 'Daily consumption'),
    'chart_note': ('kolor = strefa taryfy; kreska = poprzedni okres; luka = brak pomiaru', 'colour = tariff zone; tick = previous period; gap = missing reading'),
    'comparison_note': ('ten sam zakres godzin; % przy wystarczającym pokryciu', 'matching hours; % requires sufficient coverage'),
    'zones': ('Strefy taryfy', 'Tariff zones'), 'devices': ('Urządzenia', 'Devices'),
    'devices_note': ('4 największe, pozostałe zmierzone i niezmierzone (import − suma urządzeń)', 'Top 4, other measured and unmeasured (import − devices)'),
    'rooms': ('Pokoje', 'Rooms'), 'rooms_note': ('suma urządzeń w obszarach HA; bez rozdzielania niezmierzonej energii', 'Devices grouped by HA area; unmeasured energy is not allocated'),
    'costs': ('Koszty i stawki', 'Costs and rates'), 'active': ('Energia czynna', 'Active energy'),
    'variable': ('Dystrybucja zmienna', 'Variable distribution'), 'fixed': ('Opłaty stałe', 'Fixed fees'),
    'total': ('Razem z opłatami stałymi', 'Total including fixed fees'),
    'tariff': ('taryfa', 'tariff'), 'gross': ('Stawki brutto', 'Gross rates'), 'net': ('Stawki netto', 'Net rates'),
    'basis_unknown': ('Podstawa stawek nieustalona', 'Rate basis unavailable'), 'from': ('od', 'from'),
    'full': ('dane pełne', 'complete data'), 'partial': ('dane niepełne', 'partial data'),
    'no_data': ('Brak danych', 'No data'), 'chart_missing': ('Brak danych dziennych wykresu.', 'Daily chart data unavailable.'),
    'double': ('Suma urządzeń przekracza import — sprawdź panel Energia', 'Device total exceeds import — check the Energy dashboard'),
    'peak': ('Szczyt', 'Peak'), 'previous': ('poprzedni okres', 'previous period'),
    'read': ('Jak czytać te liczby.', 'How to read these numbers.'),
    'explain': ('Import z licznika z panelu Energia HA, statystyki godzinowe. Urządzenia to pomiary z panelu Energia; „Niezmierzone” = import − suma urządzeń (bez szacowania). Koszt zużycia według strefy każdej godziny; opłaty stałe pokazane osobno jako część miesiąca.', 'Import comes from the HA Energy grid meter and hourly statistics. Devices use Energy dashboard measurements; “Unmeasured” = import − device total (no estimates). Usage cost follows each hour’s tariff zone; fixed fees are shown separately for the period.'),
    'timezone': ('Strefa czasowa', 'Time zone'), 'open': ('Otwórz panel Energia', 'Open Energy dashboard'),
    'footer': ('Wysłano z HA Tools · zmień harmonogram lub odbiorców: HA → HA Tools → Raporty', 'Sent by HA Tools · change schedule or recipients: HA → HA Tools → Reports'),
    'alternative': ('Ta sama energia w taryfie', 'The same energy on tariff'),
    'informational': ('Informacyjnie, bez zmiany zużycia.', 'For information only, with unchanged consumption.'),
    'more': ('więcej', 'more'), 'less': ('mniej', 'less'), 'per_month': ('/ mies.', '/ month'),
    'period_share': ('udział za okres', 'period share'), 'included': ('w tym', 'included'),
    'unmeasured': ('Niezmierzone', 'Unmeasured'), 'other': ('Pozostałe zmierzone', 'Other measured'),
    'no_room': ('Bez pokoju', 'No room'), 'top': ('najwięcej', 'largest consumer'),
}
_WEEKDAYS = {
    'pl': ('poniedziałek', 'wtorek', 'środa', 'czwartek', 'piątek', 'sobota', 'niedziela'),
    'en': ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'),
}
_MONTHS = {
    'pl': ('styczeń', 'luty', 'marzec', 'kwiecień', 'maj', 'czerwiec', 'lipiec', 'sierpień', 'wrzesień', 'październik', 'listopad', 'grudzień'),
    'en': ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'),
}
_SHORT_MONTHS = {
    'pl': ('sty', 'lut', 'mar', 'kwi', 'maj', 'cze', 'lip', 'sie', 'wrz', 'paź', 'lis', 'gru'),
    'en': ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'),
}
_REPORTS = {'day': ('raport dzienny', 'daily report'), 'week': ('raport tygodniowy', 'weekly report'),
            'month': ('raport miesięczny', 'monthly report'), 'year': ('raport roczny', 'yearly report')}
_COMPARE = {
    'previous_day': ('poprzedni dzień', 'previous day'),
    'same_weekday_last_week': ('ten sam dzień tydzień temu', 'same weekday last week'),
    'avg_7d': ('średnia 7 dni', '7-day average'),
    'previous_week': ('poprzedni tydzień', 'previous week'),
    'previous_month': ('poprzedni miesiąc', 'previous month'),
    'same_week_last_year': ('ten sam tydzień rok temu', 'same week last year'),
    'same_month_last_year': ('ten sam miesiąc rok temu', 'same month last year'),
}


_EN_ZONE_MOBILE_CSS = """
@media screen and (max-width: 500px) {
  .energy-zones-en { table-layout:fixed; }
  .energy-zones-en tr { display:block; }
  .energy-zones-en .zone-label { display:block; width:100%; box-sizing:border-box; overflow-wrap:anywhere; }
  .energy-zones-en .zone-value { display:inline-block; box-sizing:border-box; vertical-align:top; border-top:0 !important; padding:0 8px 8px 0 !important; overflow-wrap:anywhere; }
  .energy-zones-en .zone-value:nth-child(2) { width:40%; text-align:left; }
  .energy-zones-en .zone-value:nth-child(3) { width:20%; text-align:center; }
  .energy-zones-en .zone-value:nth-child(4) { width:40%; padding-right:0 !important; text-align:right; }
}
"""

def _previous_tick(*, legend=False):
    # Actual foreground glyphs follow Gmail's readable-text inversion. A fixed
    # two-pixel glyph fits its line box: oversized clipped text becomes a faint
    # hairline in Gmail iOS. Keep the existing geometry and avoid image fetches.
    display, width = ('inline-block', '10px') if legend else ('block', '100%')
    return (f'<span class="tick-ink" aria-hidden="true" style="display:{display};width:{width};'
            'height:2px;overflow:hidden;vertical-align:3px;font:700 2px/2px monospace;'
            'white-space:nowrap;color:#111827">' + '█' * 128 + '</span>')


def _numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _pct_ratio(value):
    return value * 100 if _numeric(value) else None


def _items(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _dt(value, tz):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=tz) if result.tzinfo is None else result.astimezone(tz)
    except (ValueError, TypeError, OverflowError):
        return None


class _View:
    def __init__(self, model, language, timezone_name, hour_cycle, date_language):
        self.m = model
        self.lang = 'pl' if str(language).startswith('pl') else 'en'
        self.dlang = 'pl' if str(date_language or language).startswith('pl') else 'en'
        self.tz = ZoneInfo(timezone_name)
        self.tz_name = timezone_name
        self.cycle = hour_cycle
        self.period = model.get('period') or {}
        self.kind = self.period.get('kind', 'day')
        self.totals = model.get('totals') or {}
        self.tariff = model.get('tariff') or {}
        self.coverage = model.get('coverage') or {}
        self.complete = self.coverage.get('complete') is True
        self.currency = model.get('currency')
        self.zones = _items(model.get('zones'))
        self.comparisons = _items(model.get('comparisons'))
        self.start = _dt(self.period.get('start'), self.tz)
        self.end = _dt(self.period.get('end'), self.tz)
        calendar_zone = ZoneInfo(self.period.get('timezone') or timezone_name)
        self.calendar_start = _dt(self.period.get('start'), calendar_zone)
        self.calendar_end = _dt(self.period.get('end'), calendar_zone)
        self.double = ((model.get('unmeasured') or {}).get('status') == 'double_counting'
                       or any(i.get('code') == 'double_counting' for i in _items(model.get('issues'))))
        self.lines = []

    def tr(self, key):
        return _LABELS[key][self.lang == 'en']

    def n(self, value, places=1, signed=False):
        return fmt.number(value, places, self.lang, signed=signed) or self.tr('no_data')

    def energy(self, value, role='table'):
        return fmt.energy(value, role=role, period=self.kind, language=self.lang)

    def money(self, value):
        return fmt.money(value, self.currency, language=self.lang)

    def rate(self, value):
        return fmt.rate(value, self.currency, language=self.lang)

    def share(self, value):
        return fmt.percent(_pct_ratio(value), language=self.lang) if self.complete else '—'

    def change(self, comparison):
        value = comparison.get('delta_pct')
        if self.complete and comparison.get('coverage_ok') is True and _numeric(value):
            return fmt.percent(value, role='change', language=self.lang)
        return None

    def date(self, date, *, year=True, weekday=False):
        if date is None:
            return self.tr('no_data')
        text = f'{date.day} {_SHORT_MONTHS[self.dlang][date.month - 1]}'
        if year:
            text += f' {date.year}'
        if weekday:
            text = _WEEKDAYS[self.dlang][date.weekday()] + ', ' + text
        return text

    def period_label(self):
        if self.calendar_start is None:
            return self.tr('no_data')
        if self.kind == 'day':
            return self.date(self.calendar_start, weekday=True)
        if self.kind == 'month':
            return f'{_MONTHS[self.dlang][self.calendar_start.month - 1]} {self.calendar_start.year}'
        if self.kind == 'year':
            return str(self.calendar_start.year)
        last = self.calendar_end - timedelta(microseconds=1) if self.calendar_end else None
        prefix = 'tydzień' if self.dlang == 'pl' else 'week'
        return f'{prefix} {self.date(self.calendar_start)}–{self.date(last)}'

    def clock(self, date, *, end=False, offset=False):
        if date is None:
            return self.tr('no_data')
        if self.cycle in ('12', 'h12', 12):
            value = f'{date.hour % 12 or 12}:{date.minute:02d} {"AM" if date.hour < 12 else "PM"}'
        elif end and date.hour == 0 and date.minute == 0 and self.start and date.date() > self.start.date():
            value = '24:00'
        else:
            value = f'{date.hour:02d}:{date.minute:02d}'
        if offset:
            raw = date.strftime('%z')
            value += f' UTC{raw[:3]}:{raw[3:]}'
        return value

    def comparison_label(self, c):
        key = c.get('key')
        if self.calendar_start and key in ('previous_day', 'same_weekday_last_week'):
            prior = self.calendar_start - timedelta(days=1 if key == 'previous_day' else 7)
            return self.date(prior, weekday=True, year=False)
        if key in _COMPARE:
            return _COMPARE[key][self.lang == 'en']
        return str(c.get('label') or self.tr('previous'))

    def coverage_label(self):
        measured, expected = self.coverage.get('measured_hours'), self.coverage.get('expected_hours')
        label = self.tr('full') if self.complete else self.tr('partial')
        if measured == 0 or not _numeric(self.totals.get('import_kwh')):
            label = self.tr('no_data')
        return f'{label} ({self.n(measured, 0)}/{self.n(expected, 0)} h)'

    def zone_label(self, zone):
        label = str(zone.get('label') or self.tr('no_data'))
        if self.lang == 'en':
            return {'tania': 'cheap', 'droga': 'expensive'}.get(label.casefold(), label)
        return label

    def fixed_label(self, fixed):
        label = str(fixed.get('label') or self.tr('fixed'))
        if self.lang == 'en':
            return {'opłata stała sieciowa': 'fixed network fee', 'opłata abonamentowa': 'subscription fee',
                    'opłata mocowa': 'capacity fee', 'opłata handlowa': 'retail fee'}.get(label.casefold(), label)
        return label

    def subject_label(self):
        if self.kind == 'day' and self.calendar_start:
            return _WEEKDAYS[self.dlang][self.calendar_start.weekday()] + ' ' + self.date(self.calendar_start, year=False)
        return self.period_label()

    def color(self, zone_id):
        zone = next((z for z in self.zones if z.get('id') == zone_id), {})
        if 'classification' in zone:
            return {'cheap':'cheap','expensive':'exp'}.get(zone['classification'],'other')
        # Reference model/1 fixtures predate the explicit classification field.
        return zone_id if zone_id in ('cheap','exp') else 'other'

    def item_name(self, i):
        if i.get('id') == '_other_measured' or i.get('color') == 'other':
            return self.tr('other')
        if self.is_unmeasured(i):
            return self.tr('unmeasured')
        if i.get('name') == 'Bez pokoju':
            return self.tr('no_room')
        return str(i.get('name') or self.tr('no_data'))

    @staticmethod
    def is_unmeasured(i):
        return i.get('id') == '_unmeasured' or i.get('area_id') == '_unmeasured' or i.get('color') == 'unmeasured'

    def table(self, rows):
        return section('<table role="presentation" width="100%">' + ''.join(rows) + '</table>')

    def row(self, label, value, *, bold=False, sub='', cls='tx', label_bold=None):
        weight = 700 if bold else 400
        label_weight = weight if label_bold is None else 700 if label_bold else 400
        detail = f'<br><span class="tx2" style="font:400 12px/16px {FONT};color:{L["tx2"]}">{E(sub)}</span>' if sub else ''
        return (f'<tr><td class="tx bd" style="padding:8px 0;border-top:1px solid {L["bd"]};font:{label_weight} 14px/20px {FONT};color:{L["tx"]}">{E(label)}{detail}</td>'
                f'<td class="{cls} bd" align="right" valign="top" style="padding:8px 0;border-top:1px solid {L["bd"]};font:{weight} 14px/20px {FONT};color:{L[cls]};white-space:nowrap">{E(value)}</td></tr>')

    def comparison_row(self, description, baseline, value, cls):
        # Keep the baseline quantity and unit together, while the description may
        # wrap independently. Do not change the cost rows' typography.
        return (f'<tr><td class="tx bd" style="padding:8px 0;border-top:1px solid {L["bd"]};font:400 14px/20px {FONT};color:{L["tx"]}">{E(description)} '
                f'<span class="tx2 comparison-baseline" style="font:400 12px/16px {FONT};color:{L["tx2"]};white-space:nowrap">{E(baseline)}</span></td>'
                f'<td class="{cls} bd" align="right" valign="top" style="padding:8px 0;border-top:1px solid {L["bd"]};font:700 14px/20px {FONT};color:{L[cls]};white-space:nowrap">{E(value)}</td></tr>')

    def comparison_block(self):
        rows = []
        for c in self.comparisons:
            delta = c.get('delta_kwh')
            known = _numeric(delta)
            cls = 'warn' if known and delta > 0 else 'good' if known and delta < 0 else 'tx2'
            arrow = '▲ ' if known and delta > 0 else '▼ ' if known and delta < 0 else ''
            change = self.change(c)
            change_text = f' ({change})' if change else f' ({self.tr("partial")})' if not c.get('coverage_ok') or not self.complete else ' (—)'
            quantity = self.n(delta, 2 if self.kind == 'day' else 1, signed=True) + ' kWh' if known else '—'
            value = arrow + quantity + change_text
            description = f'vs {self.comparison_label(c)}'
            baseline = self.energy(c.get("kwh"), "tile")
            label = f'{description} · {baseline}'
            rows.append(self.comparison_row(description, baseline, value, cls))
            self.lines.append(f'- {label}: {value}')
        return self.table(rows) if rows else section(paragraph(self.tr('no_data')))

    def chart_note(self):
        daily = self.kind != 'day'
        unit = ('kWh na dzień' if daily else 'kWh na godzinę') if self.lang == 'pl' else ('kWh per day' if daily else 'kWh per hour')
        previous = self.comparison_label(self.comparisons[0]) if self.comparisons else self.tr('previous')
        text = f'{unit}, kolor = strefa taryfy, kreska = {previous}' if self.lang == 'pl' else f'{unit}, colour = tariff zone, tick = {previous}'
        points = _items(self.m.get('daily' if daily else 'hourly'))
        if any(not _numeric(point.get(key)) for point in points for key in ('kwh', 'prev_kwh')):
            text += '; luka = brak pomiaru' if self.lang == 'pl' else '; gap = missing reading'
        return text

    def chart(self):
        daily = self.kind != 'day'
        points = _items(self.m.get('daily' if daily else 'hourly'))
        if not points:
            msg = self.tr('chart_missing') if daily else self.tr('no_data')
            self.lines.append(msg)
            return section(paragraph(msg))
        values = [p[k] for p in points for k in ('kwh', 'prev_kwh') if _numeric(p.get(k)) and p[k] >= 0]
        maximum = max(values, default=1) or 1
        H = 72
        cols, labels = [], []
        if not daily and any(p.get('zone') == 'cheap' for p in points):
            self.lines.append('* ' + ('tania strefa' if self.lang == 'pl' else 'cheap zone'))
        starts = [_dt(p.get('start'), self.tz) for p in points]
        # Offset suffixes disambiguate repeated DST hours without merging either bar.
        repeated = {d.strftime('%Y-%m-%d %H:%M') for d in starts if d and
                    sum(x is not None and x.strftime('%Y-%m-%d %H:%M') == d.strftime('%Y-%m-%d %H:%M') for x in starts) > 1}
        for idx, p in enumerate(points):
            current, previous = p.get('kwh'), p.get('prev_kwh')
            b = min(H, max(0, round(H * current / maximum))) if _numeric(current) else 0
            marker = min(H - 2, max(0, round(H * previous / maximum) - 1)) if _numeric(previous) else None
            pixels = ['grid' if not _numeric(current) else None] * H
            if daily and _items(p.get('zones')):
                cursor = H
                for z in _items(p.get('zones')):
                    if _numeric(z.get('kwh')):
                        size = min(cursor, max(0, round(H * z['kwh'] / maximum)))
                        pixels[cursor-size:cursor] = [self.color(z.get('id', z.get('zone')))] * size
                        cursor -= size
            else:
                pixels[H-b:H] = [self.color(p.get('zone')) if not daily else 'room'] * b
            if marker is not None:
                top = H - marker - 2
                pixels[top:top+2] = ['tick'] * 2
            segments = []
            cursor = 0
            while cursor < H:
                key = pixels[cursor]
                end = cursor + 1
                while end < H and pixels[end] == key:
                    end += 1
                height = end - cursor
                color = L['tx'] if key == 'tick' else L[key] if key else None
                attrs = f' class="{key}" bgcolor="{color}"' if key else ''
                bg = f'background-color:{color};' if color else ''
                if key == 'tick':
                    segments.append(f'<tr><td height="{height}" style="height:{height}px;line-height:2px;font-size:2px">{_previous_tick()}</td></tr>')
                else:
                    segments.append(f'<tr><td{attrs} height="{height}" style="height:{height}px;{bg}line-height:0;font-size:0">&nbsp;</td></tr>')
                cursor = end
            date = starts[idx]
            date_key = date.strftime('%Y-%m-%d %H:%M') if date else ''
            label = self.date(date, year=False) if daily else self.clock(date, offset=date_key in repeated)
            if date is None and _numeric(p.get('hour')):
                hour = int(p['hour'])
                label = f'{hour:02d}:00' if self.cycle not in ('12', 'h12', 12) else f'{hour % 12 or 12}:00 {"AM" if hour < 12 else "PM"}'
            tip = f'{label}: {self.energy(current)}'
            cols.append(f'<td data-energy-bar="{idx}" title="{E(tip)}" valign="bottom" width="{100 / len(points):.4f}%" style="padding:0 1px"><table role="presentation" width="100%" style="table-layout:fixed">{"".join(segments)}</table></td>')
            visible = daily and (len(points) <= 7 or idx % 7 == 0) or not daily and date is not None and date.hour in (0, 6, 12, 18)
            axis_label = f'{date.hour:02d}' if not daily and date is not None and self.cycle not in ('12', 'h12', 12) and date_key not in repeated else label
            labels.append((idx, axis_label)) if visible else None
            self.lines.append(f'- {tip}{"*" if p.get("zone") == "cheap" else ""} · {p.get("zone") or ""} · {self.tr("previous")}: {self.energy(previous)}')
        # Whitespace between inline groups remains a line-break opportunity;
        # each marker and its label/date travel together in mail clients.
        legend_groups = []
        for z in self.zones:
            color = self.color(z.get('id'))
            dot = f'<span class="{color}" style="display:inline-block;width:10px;height:10px;border-radius:3px;background-color:{L[color]}"></span>'
            label = self.zone_label(z)
            hours = str(z.get('hours_label') or '')
            # The standard cheap/expensive labels match the reference legend;
            # custom/unknown labels retain their exact name and explicit hours.
            standard_cheap = color == 'cheap' and label.casefold() in ('tania', 'cheap')
            standard_expensive = color == 'exp' and label.casefold() in ('droga', 'expensive')
            if standard_cheap or standard_expensive:
                label += ' strefa' if self.lang == 'pl' else ' zone'
                hours = f' ({hours})' if standard_cheap and hours else ''
            elif hours:
                hours = ' ' + hours
            legend_groups.append(f'<span class="legend-group" style="display:inline-block;max-width:100%;white-space:normal;overflow-wrap:anywhere;margin-right:8px">{dot}&nbsp;{E(label)}{E(hours)}</span>')
        previous_label = self.comparison_label(self.comparisons[0]) if self.comparisons else self.tr('previous')
        legend_groups.append(f'<span class="legend-group" style="display:inline-block;max-width:100%;white-space:normal;overflow-wrap:anywhere;margin-right:0">{_previous_tick(legend=True)}&nbsp;{E(previous_label)}</span>')
        legend = ' '.join(legend_groups)
        axis = []
        for n, (idx, label) in enumerate(labels):
            following = labels[n + 1][0] if n + 1 < len(labels) else len(points)
            if n == 0 and idx:
                axis.append(f'<td colspan="{idx}"></td>')
            axis.append(f'<td colspan="{following - idx}" class="tx3" style="padding-top:4px;font:400 12px/16px {FONT};color:{L["tx3"]}">{E(label)}</td>')
        output = f'<table role="presentation" width="100%" style="table-layout:fixed"><tr>{"".join(cols)}</tr><tr>{"".join(axis)}</tr></table>' 
        output += f'<p class="tx2" style="margin:8px 0 0;font:400 12px/18px {FONT};color:{L["tx2"]}">{legend}</p>'
        peak = self.totals.get('peak_hour')
        if not daily and _numeric(peak) and _numeric(self.totals.get('peak_kwh')):
            found = next((d for d, p in zip(starts, points) if p.get('hour') == peak and d), None)
            # A duration in UTC correctly crosses a DST boundary.
            finish = (found.astimezone(ZoneInfo('UTC')) + timedelta(hours=1)).astimezone(self.tz) if found else None
            label = f'{self.clock(found)}–{self.clock(finish, end=True)}' if found else str(peak)
            peak_text = f'{self.tr("peak")}: {label} · {self.energy(self.totals.get("peak_kwh"))}'
            output += paragraph(peak_text, line_height=16)
            self.lines.append(peak_text)
        return section(output, padding='4px 24px 0')

    def zone_block(self):
        rows, segments = [], []
        for z in self.zones:
            cls = self.color(z.get('id'))
            share = z.get('share')
            if self.complete and _numeric(share) and share > 0:
                width = min(100, max(0, share * 100))
                segments.append(f'<td class="{cls}" width="{width:.4f}%" height="12" bgcolor="{L[cls]}" style="height:12px;background-color:{L[cls]};line-height:0;font-size:0">&nbsp;</td>')
            label = self.zone_label(z)
            label = label[:1].upper() + label[1:]
            hours = str(z.get('hours_label') or '')
            values = (self.energy(z.get('kwh')), self.share(share), self.money(z.get('cost')))
            dot = f'<span class="{cls}" style="display:inline-block;width:10px;height:10px;border-radius:3px;background-color:{L[cls]}"></span>'
            cells = f'<td class="tx bd zone-label" style="padding:8px 0;border-top:1px solid {L["bd"]};font:400 14px/20px {FONT};color:{L["tx"]}">{dot}&nbsp;{E(label)} <span class="tx2 zone-hours" style="font:400 12px/16px {FONT};color:{L["tx2"]}">{E(hours)}</span></td>'
            for idx, value in enumerate(values):
                text_color = 'tx2' if idx == 1 else 'tx'
                cells += f'<td class="{text_color} bd zone-value" align="right" style="padding:8px 0;border-top:1px solid {L["bd"]};font:{700 if idx == 0 else 400} 14px/20px {FONT};color:{L[text_color]}">{E(value)}</td>'
            rows.append('<tr>' + cells + '</tr>')
            self.lines.append(f'- {label} ({hours}): {" · ".join(values)}')
        gap = '<td width="4" style="width:4px;line-height:0;font-size:0">&nbsp;</td>'
        bar = f'<table role="presentation" width="100%" style="border-radius:6px"><tr>{gap.join(segments)}</tr></table>' if segments else ''
        table_class = ' class="energy-zones-en"' if self.lang == 'en' else ''
        return section(bar + f'<table role="presentation" width="100%"{table_class} style="margin-top:8px">{"".join(rows)}</table>' if rows else paragraph(self.tr('no_data')), padding='4px 24px 0')

    def rank_block(self, items, *, rooms=False):
        items = _items(items)
        if rooms:
            items = [i for i in items if not _numeric(i.get('kwh')) or i['kwh'] > 0 or self.is_unmeasured(i) and self.double]
        maximum = max((i['kwh'] for i in items if _numeric(i.get('kwh')) and i['kwh'] > 0), default=1)
        rows = []
        expanded = [(row, included) for parent in items for row, included in
                    [(parent, False)] + [(child, True) for child in _items(parent.get('children'))]]
        for i, included in expanded:
            blocked = self.double and self.is_unmeasured(i)
            cls = 'unm' if self.is_unmeasured(i) else 'room' if rooms else i.get('color') if i.get('color') in ('d1', 'd2', 'd3', 'd4', 'other') else 'other'
            value = i.get('kwh')
            width = min(100, max(1.5, value / maximum * 100)) if _numeric(value) and value > 0 and not blocked and not included else 0
            bar = (f'<table role="presentation" width="100%"><tr><td class="{cls}" width="{width:.4f}%" height="10" bgcolor="{L[cls]}" style="height:10px;background-color:{L[cls]};line-height:0;font-size:0">&nbsp;</td><td class="grid" style="background-color:{L["grid"]};line-height:0;font-size:0">&nbsp;</td></tr></table>') if width else ''
            name = self.item_name(i)
            if included:
                name = f'↳ {name} ({self.tr("included")})'
            name_html = E(name)
            if i.get('count') is not None:
                count = i['count']
                unit = fmt.plural(count, 'urządzenie' if self.lang == 'pl' else 'device', 'urządzenia' if self.lang == 'pl' else 'devices', 'urządzeń' if self.lang == 'pl' else 'devices', language=self.lang)
                count_label = f'({self.n(count, 0)} {unit})'
                name += ' ' + count_label
                name_html += f' <span class="tx2" style="font:400 12px/16px {FONT};color:{L["tx2"]}">{E(count_label)}</span>'
            values = [self.tr('double') if blocked else self.energy(value), '—' if blocked else self.share(i.get('share')), '—' if blocked else self.money(i.get('cost'))]
            cells = f'<td class="tx bd rank-name" style="width:34%;padding:8px 8px 8px 0;border-top:1px solid {L["bd"]};font:400 14px/20px {FONT};color:{L["tx"]}">{name_html}</td><td class="bd bar" style="width:24%;padding:8px 8px 8px 0;border-top:1px solid {L["bd"]}">{bar}</td>'
            for idx, (value, width_, extra, weight) in enumerate(zip(values, (18, 9, 15), (' rank-value', ' hm', ' rank-cost'), (700, 400, 400))):
                text_color = 'tx2' if idx == 1 else 'tx'
                padding = '8px 0' if idx == 0 else '8px 0 8px 8px'
                size = 12 if idx == 1 else 14
                nowrap = ';white-space:nowrap' if idx != 1 else ''
                cells += f'<td class="{text_color} bd{extra}" align="right" style="width:{width_}%;padding:{padding};border-top:1px solid {L["bd"]};font:{weight} {size}px/20px {FONT};color:{L[text_color]}{nowrap}">{E(value)}</td>'
            rows.append(('<tr data-energy-included="true">' if included else '<tr>') + cells + '</tr>')
            if included:
                self.lines.append(f'  {name}: {self.energy(i.get("kwh"))}')
            else:
                self.lines.append(f'- {name}: {" · ".join(values)}')
        return section('<table role="presentation" width="100%" style="table-layout:fixed">' + ''.join(rows) + '</table>') if rows else section(paragraph(self.tr('no_data')))

    def cost_block(self):
        rows = []
        for key, label, bold in (('energy_cost', 'active', False), ('variable_cost', 'variable', False), ('usage_cost', 'usage', True), ('fixed_share', 'fixed', False), ('total_with_fixed', 'total', True)):
            sub = ''
            fixed_period_label = None
            if key == 'usage_cost':
                sub = f'{self.tr("effective")} {self.rate(self.totals.get("effective_rate"))}'
            if key == 'fixed_share':
                days = self.tariff.get('days_in_month')
                if self.kind == 'day' and self.period.get('complete') is True and _numeric(days):
                    fixed_period_label = f'1/{self.n(days, 0)} ' + ('miesiąca' if self.lang == 'pl' else 'of month')
                    sub = fixed_period_label
                else:
                    sub = self.tr('period_share') + ': ' + self.period_label()
                sub += f' · {self.money(self.tariff.get("fixed_monthly"))} {self.tr("per_month")}'
                details = ' + '.join(f'{self.fixed_label(fixed)} {self.money(fixed.get("monthly"))}' for fixed in _items(self.tariff.get('fixed')))
                if details:
                    sub += ': ' + details
            value = self.money(self.totals.get(key))
            display_label = self.tr(label) + (f' — {fixed_period_label}' if fixed_period_label else '')
            display_sub = sub.partition(' · ')[2] if fixed_period_label else sub
            rows.append(self.row(display_label, value, bold=bold, sub=display_sub))
            self.lines.append(f'- {self.tr(label)}: {value}' + (f' · {sub}' if sub else ''))
        rate_rows = []
        for zone in _items(self.tariff.get('zones')):
            zone_label = self.zone_label(zone)
            label = f'{zone_label[:1].upper() + zone_label[1:]} ({zone.get("hours_label") or ""})'
            components = f'{self.n(zone.get("energy_rate"), 2)} + {self.n(zone.get("variable_rate"), 2)} = '
            total_rate = self.rate(zone.get('rate'))
            value = components + total_rate
            label_padding = '4px 8px 4px 0' if self.lang == 'en' else '4px 0'
            rate_rows.append(f'<tr><td class="tx2" style="padding:{label_padding};font:400 12px/16px {FONT};color:{L["tx2"]}">{E(label)}</td><td class="tx2" align="right" style="padding:4px 0;font:400 12px/16px {FONT};color:{L["tx2"]}">{E(components)}<b class="rate-total">{E(total_rate)}</b></td></tr>')
            self.lines.append(f'- {label}: {value}')
        basis = self.tariff.get('price_basis', self.m.get('price_basis'))
        basis_label = self.tr(basis) if basis in ('gross', 'net') else self.tr('basis_unknown')
        rate_note = f'{basis_label}: {"energia + dystrybucja zmienna" if self.lang == "pl" else "energy + variable distribution"} · {self.tr("tariff")} {self.tariff.get("name") or self.tr("no_data")} {self.tr("from")} {self.tariff.get("valid_from") or self.tr("no_data")}'
        extras = paragraph(rate_note, line_height=16)
        self.lines.append(rate_note)
        for alt in _items(self.m.get('alternatives')):
            delta = alt.get('delta')
            difference = f' — {"o " if self.lang == "pl" else ""}{self.money(abs(delta))} {self.tr("more" if delta > 0 else "less")}' if _numeric(delta) and delta != 0 else ''
            note = f'{self.tr("alternative")} {alt.get("tariff") or self.tr("no_data")} ({self.rate(alt.get("rate"))}): {self.money(alt.get("cost"))}{difference}. {self.tr("informational")}'
            extras += paragraph(note, margin='8px 0 0')
            self.lines.append(note)
        return section('<table role="presentation" width="100%">' + ''.join(rows) + '</table><table role="presentation" width="100%" style="margin-top:8px">' + ''.join(rate_rows) + '</table>' + extras)


def render(model, *, language, timezone_name, hour_cycle, date_language=None, external_url=None):
    """Return subject/preheader/html/text/notify; render only backend-provided facts."""
    if not isinstance(model, dict) or model.get('schema') != 'ha_tools.energy_report/1':
        raise ValueError('Expected ha_tools.energy_report/1')
    v = _View(model, language, timezone_name, hour_cycle, date_language)
    title = f'{v.tr("energy")} — {v.period_label()}'
    report_label = _REPORTS.get(v.kind, _REPORTS['day'])[v.lang == 'en']
    subtitle = f'{v.clock(v.start)}–{v.clock(v.end, end=True)} · {v.tr("tariff")} {v.tariff.get("name") or v.tr("no_data")} · {v.coverage_label()}'
    summary = f'{v.energy(v.totals.get("import_kwh"), "tile")} · {v.money(v.totals.get("usage_cost"))}'
    first = v.comparisons[0] if v.comparisons else {}
    change = v.change(first)
    comparison = f'{change} vs {v.comparison_label(first)}' if change else v.coverage_label() if not v.complete else ''
    cheap = next((z for z in v.zones if z.get('classification') == 'cheap' or
        'classification' not in z and z.get('id') == 'cheap'), None)
    if cheap is None:
        cheap = v.zones[0] if len(v.zones)==1 else {}
    zone_label = v.tr('cheap') if len(v.zones) > 1 else f'{v.tr("zone")}: {v.zone_label(cheap)}'
    pre_parts = [summary, comparison]
    if cheap and v.complete and _numeric(cheap.get('share')):
        pre_parts.append(f'{zone_label.lower()} {v.share(cheap.get("share"))}')
    rankings = _items(model.get('ranking'))
    top = next((i for i in rankings if not v.is_unmeasured(i) and i.get('color') != 'other'), None)
    if top:
        pre_parts.append(f'{v.tr("top")}: {v.item_name(top)} {v.energy(top.get("kwh"), "tile")}')
    if not v.complete and v.coverage_label() not in pre_parts:
        pre_parts.append(v.coverage_label())
    if v.double:
        pre_parts.append(v.tr('double'))
    preheader = ' · '.join(p for p in pre_parts if p)
    subject = f'{v.tr("energy")}: {v.subject_label()} — {summary}' + (f' ({change})' if change else '')
    kpis = (kpi_cell(v.tr('import'), v.n(v.totals.get('import_kwh')), 'kWh', comparison, 'warn' if _numeric(first.get('delta_kwh')) and first['delta_kwh'] > 0 and change else 'good' if change else 'tx2')
            + '<td class="kpigap" style="width:2%"></td>'
            + kpi_cell(v.tr('usage'), v.n(v.totals.get('usage_cost'), 2), 'zł' if v.lang == 'pl' and v.currency == 'PLN' else str(v.currency or ''), v.rate(v.totals.get('effective_rate')))
            + '<td class="kpigap" style="width:2%"></td>'
            + kpi_cell(zone_label, v.n(_pct_ratio(cheap.get('share')), 0) if v.complete else '—', '%' if v.complete and _numeric(cheap.get('share')) else '', v.energy(cheap.get('kwh'))))
    v.lines = [title, f'{report_label} · {subtitle}', '', summary]
    if comparison:
        v.lines.append(comparison)
    v.lines.append(f'{zone_label}: {v.share(cheap.get("share"))} · {v.energy(cheap.get("kwh"))}')
    blocks = ['<!-- block:3 -->' + section('<table role="presentation" width="100%"><tr>' + kpis + '</tr></table>', padding='16px 24px 0')]
    sections = [(4, 'comparison', 'comparison_note', v.comparison_block),
                (5, 'hour_chart' if v.kind == 'day' else 'daily_chart', 'chart_note', v.chart),
                (6, 'zones', None, v.zone_block),
                (7, 'devices', 'devices_note', lambda: v.rank_block(rankings)),
                (8, 'rooms', 'rooms_note', lambda: v.rank_block(model.get('rooms'), rooms=True)),
                (9, 'costs', None, v.cost_block)]
    for index, heading, note, renderer in sections:
        v.lines += ['', v.tr(heading)]
        note_text = v.chart_note() if note == 'chart_note' else v.tr(note) if note else ''
        blocks.append(f'<!-- block:{index} -->' + h2(v.tr(heading), note_text) + renderer())
    explanation = f'{v.tr("explain")} {v.tr("timezone")}: {timezone_name}.'
    if v.double:
        explanation += ' ' + v.tr('double') + '.'
    safe_url = safe_external_url(external_url)
    reading_note = (f'<p class="tx2" style="margin:12px 0 0;font:400 12px/18px {FONT};color:{L["tx2"]}">'
                    f'<b>{E(v.tr("read"))}</b> {E(explanation)}</p>')
    closing = '<!-- block:10 -->' + hr(padding='24px 24px 0') + section(reading_note, padding='0 24px')
    closing += '<!-- block:11 -->' + section(button(safe_url, v.tr('open')))
    closing += '<!-- block:12 -->' + section(paragraph(v.tr('footer'), kind='tx3', margin='16px 0 0', line_height=16), padding='0 24px 24px')
    v.lines += ['', v.tr('read') + ' ' + explanation]
    if safe_url:
        v.lines.append(v.tr('open') + ': ' + safe_url)
    v.lines.append(v.tr('footer'))
    html = render_frame(language=v.lang, title=title, preheader=preheader, eyebrow=f'Home Assistant · HA Tools · {report_label}', subtitle=subtitle, content=''.join(blocks), closing=closing)
    notify_data = {'url': '/ha-tools?tab=energy', 'clickAction': '/ha-tools?tab=energy', 'group': 'ha-tools-energy', 'tag': 'ha-tools-energy-' + {'day': 'daily', 'week': 'weekly', 'month': 'monthly', 'year': 'yearly'}.get(v.kind, 'report')}
    notify = {'title': f'{v.tr("energy")}: {v.period_label()} · {summary}', 'message': preheader, 'data': notify_data}
    if v.lang == 'en':
        html = html.replace('</style>', _EN_ZONE_MOBILE_CSS + '</style>', 1)
    return {'subject': subject, 'preheader': preheader, 'html': html, 'text': '\n'.join(v.lines), 'notify': notify}
