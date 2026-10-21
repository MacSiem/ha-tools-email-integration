"""Focused HTML presentation contracts; execution belongs to the owner's FIFO.

These tests inspect semantic groups, escaping and formatting, not pixel parity or
Gmail layout. No Home Assistant integration imports or external services needed.
"""
import copy
from html.parser import HTMLParser
import importlib
import json
import re
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = '_build1_mail_polish_contract'
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / 'custom_components/ha_tools_email')]
    sys.modules[PACKAGE] = package
MAIL = importlib.import_module(PACKAGE + '.mail_energy')
GOLDEN = json.loads((ROOT / 'tests/fixtures/build1/golden-energy-2026-10-17.json').read_text())


class Element:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.text = ''
        self.children = []

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Element()
        self.stack = [self.root]
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Element(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in ('meta', 'br', 'hr', 'img', 'input', 'link'):
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        for node in self.stack:
            node.text += value

    def by_class(self, name):
        return [node for node in self.nodes if node.has_class(name)]


def block(html, number):
    return html.split(f'<!-- block:{number} -->', 1)[1].split('<!-- block:', 1)[0]


class Build1MailPolishTests(unittest.TestCase):
    def setUp(self):
        self.model = copy.deepcopy(GOLDEN['model'])

    def render(self, language='pl', **settings):
        return MAIL.render(self.model, language=language, timezone_name='Europe/Warsaw',
                           hour_cycle='24', **settings)

    def test_previous_marker_ink_fits_its_clip_and_has_a_text_sized_cell(self):
        # Gmail iOS clipped the 8px glyph inside a 2px line to a faint hairline.
        # This is an HTML precondition; actual client contrast still needs QA.
        for language in ('pl', 'en'):
            doc = Document(block(self.render(language)['html'], 5))
            ticks = doc.by_class('tick-ink')
            self.assertGreater(len(ticks), 1)
            for tick in ticks:
                style = tick.attrs['style']
                height = float(re.search(r'height:([\d.]+)px', style)[1])
                size, line = map(float, re.search(r'font:700 ([\d.]+)px/([\d.]+)px', style).groups())
                self.assertLessEqual(size, height)
                self.assertEqual(line, height)
                if tick.parent.tag == 'td':
                    self.assertNotIn('font-size:0', tick.parent.attrs['style'])
                    self.assertNotRegex(tick.parent.attrs['style'], r'line-height:0(?:;|$)')

    def test_english_rate_labels_have_a_real_gap_before_the_equation(self):
        result = self.render('en')
        doc = Document(block(result['html'], 9))
        totals = doc.by_class('rate-total')
        self.assertEqual(len(totals), len(self.model['tariff']['zones']))
        for total in totals:
            label = total.parent.parent.children[0]
            padding = re.search(r'padding:([^;]+)', label.attrs['style'])[1].split()
            right = padding[1] if len(padding) > 1 else padding[0]
            self.assertGreaterEqual(float(right.removesuffix('px')), 8)
            self.assertNotIn('white-space:nowrap', label.attrs['style'])
            self.assertIn(total.parent.text, result['text'])

    def test_comparison_baselines_are_secondary_atomic_quantities_in_both_languages(self):
        for language, baseline, delta in (('pl', '17,6 kWh', '+1,17 kWh'),
                                           ('en', '17.6 kWh', '+1.17 kWh')):
            with self.subTest(language=language):
                result = self.render(language)
                doc = Document(block(result['html'], 4))
                baselines = doc.by_class('comparison-baseline')
                self.assertEqual(len(baselines), len(self.model['comparisons']))
                first = baselines[0]
                self.assertEqual(first.text, baseline)
                self.assertTrue(first.has_class('tx2'))
                self.assertIn('12px/16px', first.attrs['style'])
                self.assertIn('white-space:nowrap', first.attrs['style'])
                cell = first.parent
                self.assertEqual(cell.tag, 'td')
                self.assertIn('400 14px/20px', cell.attrs['style'])
                self.assertNotIn('white-space:nowrap', cell.attrs['style'])
                result_cell = cell.parent.children[1]
                self.assertIn('700 14px/20px', result_cell.attrs['style'])
                self.assertIn(delta, result_cell.text)
                self.assertIn('white-space:nowrap', result_cell.attrs['style'])
                self.assertIn(baseline, result['text'])
                self.assertIn(delta, result['text'])

    def test_long_custom_comparison_description_is_escaped_and_can_wrap(self):
        label = 'Bardzo długi opis / A long comparison <img src=x onerror="bad()"> & "quote"'
        self.model['comparisons'] = [dict(label=label, kwh=17.6, delta_kwh=1.17,
                                          delta_pct=6.6, coverage_ok=True)]
        for language in ('pl', 'en'):
            with self.subTest(language=language):
                result = self.render(language)
                html = block(result['html'], 4)
                doc = Document(html)
                cell = doc.by_class('comparison-baseline')[0].parent
                self.assertIn(label, cell.text)
                self.assertNotIn('white-space:nowrap', cell.attrs['style'])
                self.assertFalse(any(node.tag == 'img' for node in doc.nodes))
                self.assertIn('&lt;img', html)
                self.assertIn(label, result['text'])

    def test_legend_groups_keep_dates_and_markers_together_with_spacing(self):
        for language, date in (('pl', 'piątek, 16 paź'), ('en', 'Friday, 16 Oct')):
            with self.subTest(language=language):
                doc = Document(block(self.render(language)['html'], 5))
                groups = doc.by_class('legend-group')
                self.assertEqual(len(groups), len(self.model['zones']) + 1)
                for group in groups:
                    self.assertIn('display:inline-block', group.attrs['style'])
                    self.assertIn('max-width:100%', group.attrs['style'])
                    self.assertIn('white-space:normal', group.attrs['style'])
                    self.assertIn('overflow-wrap:anywhere', group.attrs['style'])
                    self.assertIn('margin-right:', group.attrs['style'])
                previous = groups[-1]
                self.assertIn(date, previous.text)
                ticks = [node for node in doc.by_class('tick-ink') if node.parent is previous]
                self.assertEqual(len(ticks), 1)
                self.assertEqual(ticks[0].text, '█' * 128)
                self.assertEqual(ticks[0].attrs['aria-hidden'], 'true')
                self.assertIn('overflow:hidden', ticks[0].attrs['style'])
                self.assertTrue(any(node.attrs.get('style') == 'table-layout:fixed' for node in doc.nodes))
                self.assertTrue(all(len(node.text) == 128 for node in doc.by_class('tick-ink')))

    def test_long_zone_labels_hours_and_previous_labels_are_escaped_in_legend(self):
        label = 'Dłuższa strefa / Longer zone <script>alert(1)</script> & more'
        hours = '22–6, 13–15 "<hours>"'
        previous = 'Poprzedni okres / Earlier period <img src=x>'
        self.model['zones'][0].update(label=label, hours_label=hours)
        self.model['comparisons'] = [dict(label=previous, kwh=10, coverage_ok=False)]
        for language in ('pl', 'en'):
            with self.subTest(language=language):
                html = block(self.render(language)['html'], 5)
                doc = Document(html)
                groups = doc.by_class('legend-group')
                self.assertIn(label, groups[0].text)
                self.assertIn(hours, groups[0].text)
                self.assertIn(previous, groups[-1].text)
                self.assertFalse(any(node.tag in ('script', 'img', 'hours') for node in doc.nodes))
                self.assertIn('&lt;script&gt;', html)

    def test_partial_comparison_keeps_reason_and_unknowns_do_not_become_zero(self):
        self.model['coverage']['complete'] = False
        self.model['comparisons'] = [dict(label='Earlier', kwh=None, delta_kwh=None,
                                          delta_pct=99, coverage_ok=False)]
        for language, reason in (('pl', 'dane niepełne'), ('en', 'partial data')):
            with self.subTest(language=language):
                result = self.render(language)
                doc = Document(block(result['html'], 4))
                self.assertEqual(len(doc.by_class('comparison-baseline')), 1)
                self.assertIn(reason, doc.root.text)
                self.assertNotIn('99', doc.root.text)
                self.assertNotIn('0,0 kWh', doc.root.text)
                self.assertNotIn('0.0 kWh', doc.root.text)
                self.assertNotIn('None', doc.root.text)
                self.assertNotIn('▲', doc.root.text)
                self.assertNotIn('▼', doc.root.text)
                self.assertNotIn('%', doc.by_class('comparison-baseline')[0].parent.parent.text)

    def test_no_comparisons_and_no_chart_data_remain_honest_empty_states(self):
        self.model['comparisons'] = []
        for language, empty, previous in (('pl', 'Brak danych', 'poprzedni okres'),
                                           ('en', 'No data', 'previous period')):
            with self.subTest(language=language):
                result = self.render(language)
                self.assertIn(empty, Document(block(result['html'], 4)).root.text)
                self.assertEqual(Document(block(result['html'], 4)).by_class('comparison-baseline'), [])
                chart = Document(block(result['html'], 5))
                self.assertIn(previous, chart.by_class('legend-group')[-1].text)
        self.model['hourly'] = []
        html = block(self.render()['html'], 5)
        self.assertIn('Brak danych', Document(html).root.text)
        self.assertEqual(Document(html).by_class('legend-group'), [])

    def test_rate_only_emphasizes_final_total_without_changing_text_or_precision(self):
        self.model['tariff']['zones'] = [dict(label='<Zone> & name', hours_label='22–6',
                                             energy_rate=.4, variable_rate=.22, rate=.62)]
        for language, total, equation in (('pl', '0,62 zł/kWh', '0,40 + 0,22 = 0,62 zł/kWh'),
                                          ('en', '0.62 PLN/kWh', '0.40 + 0.22 = 0.62 PLN/kWh')):
            with self.subTest(language=language):
                result = self.render(language)
                doc = Document(block(result['html'], 9))
                totals = doc.by_class('rate-total')
                self.assertEqual(len(totals), 1)
                self.assertEqual(totals[0].tag, 'b')
                self.assertEqual(totals[0].text, total)
                self.assertEqual(totals[0].parent.text, equation)
                self.assertIn(equation, result['text'])
                self.assertIn('<Zone> & name', doc.root.text)
                self.assertFalse(any(node.tag == 'zone' for node in doc.nodes))
        self.model['tariff']['zones'][0].update(energy_rate=None, variable_rate=None, rate=None)
        doc = Document(block(self.render('en')['html'], 9))
        self.assertNotIn('0.00', doc.by_class('rate-total')[0].text)
        self.assertNotIn('None', doc.by_class('rate-total')[0].text)

    def test_polish_is_presentation_only_and_keeps_children_cta_and_r12(self):
        self.model['ranking'][0]['children'] = [dict(name='<Child> & device', kwh=1.235)]
        before = copy.deepcopy(self.model)
        result = self.render(external_url='https://example.test/energy?a=1&b=2')
        self.assertEqual(self.model, before)
        self.assertEqual(result['subject'], GOLDEN['display']['mail.subject'])
        for value in ('18,8 kWh', '15,66 zł', '5,47 kWh', '16,76 zł'):
            self.assertIn(value, result['text'])
        self.assertIn('&lt;Child&gt; &amp; device', result['html'])
        self.assertIn('1,24 kWh', result['text'])
        self.assertIn('href="https://example.test/energy?a=1&amp;b=2"', result['html'])
        self.assertNotIn('█', result['text'])
        self.assertNotIn('comparison-baseline', str(result['notify']))
