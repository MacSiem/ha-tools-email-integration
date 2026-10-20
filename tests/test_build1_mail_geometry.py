"""Geometry contracts only; pixel/Gmail/stress rendering belongs to owner QA."""
import copy
import importlib
import json
from pathlib import Path
import re
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_build1_mail_geometry'
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / 'custom_components/ha_tools_email')]
    sys.modules[PACKAGE] = package
MAIL = importlib.import_module(PACKAGE + '.mail_energy')
FRAME = importlib.import_module(PACKAGE + '.mail_frame')
GOLDEN = json.loads((ROOT / 'tests/fixtures/build1/golden-energy-2026-10-17.json').read_text())


def block(html, number):
    return html.split(f'<!-- block:{number} -->', 1)[1].split('<!-- block:', 1)[0]


class Build1MailGeometryTests(unittest.TestCase):
    def setUp(self):
        self.model = copy.deepcopy(GOLDEN['model'])

    def render(self, language='pl', **settings):
        return MAIL.render(self.model, language=language, timezone_name='Europe/Warsaw',
                           hour_cycle='24', **settings)

    def test_mobile_reference_geometry_and_separate_narrow_fallback(self):
        css = FRAME.css()
        ordinary = css.split('@media screen and (max-width: 480px)', 1)[1].split('@media screen and (max-width: 360px)', 1)[0]
        narrow = css.split('@media screen and (max-width: 360px)', 1)[1].split('@media (prefers-color-scheme', 1)[0]
        self.assertIn('.bar { width:20%', ordinary)
        self.assertNotIn('.zone-hours { display:block', css)
        self.assertNotIn('font-size:12px', ordinary)
        self.assertIn('overflow-wrap:anywhere', ordinary)
        self.assertIn('.rank-value', narrow)
        self.assertIn('white-space:normal', narrow)

    def test_zone_rows_keep_hours_inline_secondary_and_have_reference_bar_gap(self):
        html = block(self.render()['html'], 6)
        self.assertIn('class="tx bd zone-label"', html)
        self.assertRegex(html, r'<span class="tx2 zone-hours"[^>]*font:400 12px/16px')
        self.assertIn('class="tx2 bd zone-value"', html)
        self.assertIn('<td width="4" style="width:4px;', html)
        self.assertIn('6,21 kWh', html)
        self.assertIn('33%', html)

    def test_ranking_uses_secondary_count_and_share_without_altering_text(self):
        result = self.render()
        html = block(result['html'], 7)
        self.assertRegex(html, r'<span class="tx2"[^>]*font:400 12px/16px[^>]*>\(3 urządzenia\)</span>')
        self.assertRegex(html, r'class="tx2 bd hm"[^>]*font:400 12px/20px')
        self.assertRegex(html, r'class="tx bd rank-value"[^>]*padding:8px 0;[^>]*white-space:nowrap')
        self.assertIn('Pozostałe zmierzone (3 urządzenia)', result['text'])
        self.assertIn('6,90 kWh', html)

    def test_fixed_fee_allocation_moves_to_heading_only_for_complete_day(self):
        result = self.render()
        self.assertIn('Opłaty stałe — 1/31 miesiąca', block(result['html'], 9))
        self.assertIn('Opłaty stałe: 1,10 zł · 1/31 miesiąca · 34,10 zł', result['text'])
        self.model['period']['kind'] = 'month'
        self.assertNotIn('Opłaty stałe — 1/31', self.render()['html'])
        self.assertIn('udział za okres', self.render()['text'])

    def test_note_spacing_footer_and_frame_defaults_are_local(self):
        result = self.render()
        closing = block(result['html'], 10)
        self.assertIn('padding:24px 24px 0', closing)
        self.assertIn('<b>Jak czytać te liczby.</b>', closing)
        footer = block(result['html'], 12)
        self.assertIn('font:400 12px/16px', footer)
        self.assertIn('padding:0 24px', FRAME.hr())
        self.assertIn('font:400 12px/18px', FRAME.paragraph('default'))
        self.assertIn('font:400 12px/16px', FRAME.paragraph('note', line_height=16))

    def test_stress_strings_children_and_safe_cta_stay_escaped(self):
        long_name = '<unsafe>' + 'LongName' * 60 + '& quote'
        self.model['ranking'][0]['name'] = long_name
        self.model['ranking'][0]['children'] = [dict(name='<child>', kwh=1.235)]
        self.model['zones'][0]['label'] = long_name
        before = copy.deepcopy(self.model)
        for language in ('pl', 'en'):
            html = self.render(language, external_url='https://example.test/?a=1&b=2')['html']
            self.assertIn('&lt;unsafe&gt;', html)
            self.assertNotIn('<unsafe>', html)
            self.assertIn('&lt;child&gt;', html)
            self.assertIn('href="https://example.test/?a=1&amp;b=2"', html)
        self.assertEqual(before, self.model)
        self.assertNotIn('href=', self.render(external_url='javascript:bad()')['html'])

    def test_numeric_semantics_coverage_and_gmail_marker_survive_geometry(self):
        result = self.render()
        self.assertEqual(result['subject'], GOLDEN['display']['mail.subject'])
        for value in ('18,8 kWh', '15,66 zł', '5,47 kWh', '16,76 zł'):
            self.assertIn(value, result['text'])
        ticks = re.findall(r'<span class="tick-ink"[^>]*>(█+)</span>', result['html'])
        self.assertGreaterEqual(len(ticks), 25)
        self.assertTrue(all(len(tick) == 128 for tick in ticks))
        self.assertEqual(result['html'].count('data-energy-bar='), 24)
        self.model['coverage']['complete'] = False
        for surface in ('text', 'subject', 'preheader'):
            self.assertNotRegex(self.render()[surface], r'\d[\d,.]*%')

    def test_included_child_uses_full_row_and_own_metadata_without_a_bar_or_aggregation(self):
        child = dict(name='<Included child>', kwh=5.100005, share=.271710442194992, cost=4.1896479, count=2)
        self.model['ranking'][0]['children'] = [child]
        before = copy.deepcopy(self.model)
        for language, included, quantity, cost, share in (
                ('pl', 'w tym', '5,10 kWh', '4,19 zł', '27%'),
                ('en', 'included', '5.10 kWh', '4.19 PLN', '27%')):
            html = block(self.render(language)['html'], 7)
            row = html.split('<tr data-energy-included="true">', 1)[1].split('</tr>', 1)[0]
            self.assertEqual(row.count('<td '), 5)
            self.assertIn(f'↳ &lt;Included child&gt; ({included})', row)
            for value in (quantity, cost, share):
                self.assertIn(value, row)
            self.assertIn('rank-name', row)
            self.assertIn('font:400 14px/20px', row)
            self.assertNotIn('height="10"', row)
            self.assertNotIn('colspan=', row)
            self.assertIn('18,8 kWh' if language == 'pl' else '18.8 kWh', self.render(language)['subject'])
        self.assertEqual(before, self.model)

    def test_included_child_partial_and_missing_values_never_borrow_parent_metadata(self):
        self.model['ranking'][0]['children'] = [dict(name='Missing child', kwh=None, cost=None, share=.99)]
        self.model['coverage']['complete'] = False
        html = block(self.render('en')['html'], 7)
        row = html.split('<tr data-energy-included="true">', 1)[1].split('</tr>', 1)[0]
        self.assertNotIn('99%', row)
        self.assertNotIn('6.90 kWh', row)
        self.assertNotIn('5.74 PLN', row)
        self.assertNotIn('0.00', row)
        self.assertNotIn('height="10"', row)

    def test_chart_copy_uses_period_unit_and_actual_comparison_and_keeps_gap_evidence(self):
        result = self.render()
        chart = block(result['html'], 5)
        self.assertIn('kWh na godzinę, kolor = strefa taryfy, kreska = piątek, 16 paź', chart)
        self.assertIn('>00</td>', chart)
        self.assertIn('>06</td>', chart)
        self.assertIn('00:00:', result['text'])
        self.model['hourly'][0]['prev_kwh'] = None
        self.assertIn('luka = brak pomiaru', block(self.render()['html'], 5))
        self.model['period']['kind'] = 'week'
        self.model['daily'] = [dict(start='2026-10-17T00:00:00+02:00', kwh=None, prev_kwh=2)]
        chart = block(self.render('en')['html'], 5)
        self.assertIn('kWh per day', chart)
        self.assertIn('gap = missing reading', chart)

    def test_unknown_comparison_is_compact_but_neutral_and_qualified(self):
        self.model['comparisons'] = [dict(key='previous_day', kwh=None, delta_kwh=None, delta_pct=99, coverage_ok=False)]
        html = block(self.render()['html'], 4)
        self.assertIn('— (dane niepełne)', html)
        self.assertIn('class="tx2 bd" align="right"', html)
        self.assertNotIn('99', html)
        self.assertNotIn('▲', html)
        self.assertNotIn('▼', html)
        self.assertNotIn('0,00 kWh', html)

    def test_tariff_initial_capitalization_preserves_acronyms_and_alternate_sentence(self):
        self.model['zones'][0]['label'] = 'my G12 <label>'
        self.model['tariff']['zones'][0]['label'] = 'my G12 <label>'
        html = self.render()['html']
        self.assertIn('My G12 &lt;label&gt;', block(html, 6))
        self.assertIn('My G12 &lt;label&gt;', block(html, 9))
        self.assertNotIn('My g12', html)
        self.assertIn('Stawki brutto: energia + dystrybucja zmienna', html)
        self.assertIn(' — o 1,05 zł więcej.', html)
