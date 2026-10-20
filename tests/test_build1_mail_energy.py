"""Focused presentation contracts. Install in tests/ and run through owner's FIFO.

The isolated package below avoids importing HA or integration __init__ during tests.
"""
import copy
import importlib
import json
from pathlib import Path
import re
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'custom_components/ha_tools_email'
PACKAGE = '_build1_mail_renderer_contract'
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE)]
    sys.modules[PACKAGE] = package
MAIL = importlib.import_module(PACKAGE + '.mail_energy')
GOLDEN = json.loads((ROOT / 'tests/fixtures/build1/golden-energy-2026-10-17.json').read_text())


class Build1MailEnergyTests(unittest.TestCase):
    def test_previous_period_ticks_keep_a_visible_edge_when_gmail_retains_dark_fill(self):
        from html.parser import HTMLParser
        class Markers(HTMLParser):
            def __init__(self):super().__init__();self.styles=[]
            def handle_starttag(self,tag,attrs):
                a=dict(attrs)
                if 'tick' in a.get('class','').split():self.styles.append(a.get('style',''))
        doc=Markers();doc.feed(self.render(language='en')['html'])
        self.assertGreaterEqual(len(doc.styles),25)  # actual 24-hour chart and legend
        for style in doc.styles:
            self.assertIn('border-top:1px solid currentColor',style)
            self.assertIn('color:#111827',style)
            self.assertIn('box-sizing:border-box',style)
            self.assertIn('height:2px',style)
        # Gmail Android can retain #111827 cells while inverting the page.
        # Native dark CSS also binds currentColor to the readable foreground.
        def lum(value):
            rgb=[int(value[i:i+2],16)/255 for i in (1,3,5)]
            rgb=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgb]
            return sum(a*b for a,b in zip(rgb,(.2126,.7152,.0722)))
        edge=lum('#e5e7eb');dark=lum('#111214')
        self.assertGreater((edge+.05)/(dark+.05),3)

    def test_unknown_zone_classification_never_infers_cheap_from_rate_or_order(self):
        for row in self.model['zones']:
            row['classification'] = None
        result = self.render(language='en')
        self.assertNotIn('33%',result['preheader'])
        # The zone's measured share remains valid even when its cheapness is unknown.
        self.assertIn('33%',result['text'])
        self.assertNotIn('class="cheap" bgcolor=',result['html'])

    def setUp(self):
        self.model = copy.deepcopy(GOLDEN['model'])

    def render(self, **kwargs):
        settings = dict(language='pl', timezone_name='Europe/Warsaw', hour_cycle='24')
        settings.update(kwargs)
        return MAIL.render(self.model, **settings)

    def test_golden_r12_and_backend_numbers_are_preserved(self):
        before = copy.deepcopy(self.model)
        result = self.render()
        self.assertEqual(result['subject'], GOLDEN['display']['mail.subject'])
        for value in ('18,8 kWh', '15,66 zł', '5,47 kWh', '16,76 zł', '1,10 zł', '7,22 zł'):
            self.assertIn(value, result['text'])
            self.assertIn(value, result['html'])
        self.assertEqual(self.model, before)
        self.assertEqual(set(result), {'subject', 'preheader', 'html', 'text', 'notify'})

    def test_frame_and_fixed_block_order(self):
        html = self.render()['html']
        markers = re.findall(r'<!-- block:(\d+) -->', html)
        self.assertEqual(markers, [str(i) for i in range(13)])
        for text in ('width="600"', 'max-width:600px', 'max-width: 480px', 'prefers-color-scheme: dark', 'supported-color-schemes', 'mso-hide:all', 'height="4"'):
            self.assertIn(text, html)
        self.assertNotIn('<img', html)
        self.assertNotIn('<svg', html)
        self.assertEqual(html.count('Jak czytać te liczby.'), 1)
        self.assertNotIn('href=', html)

    def test_date_tariff_zones_and_currency_are_dynamic(self):
        self.model['period'].update(start='2027-02-01T00:00:00+01:00', end='2027-02-02T00:00:00+01:00')
        self.model['tariff']['name'] = 'My tariff'
        self.model['currency'] = 'EUR'
        self.model['zones'] = [dict(id='flat', label='Single zone', kwh=18.77, share=1, cost=10, rate=.5)]
        result = self.render(language='en', hour_cycle='12')
        self.assertIn('Monday 1 Feb', result['subject'])
        self.assertIn('My tariff', result['html'])
        self.assertIn('EUR', result['text'])
        self.assertIn('12:00 AM', result['text'])
        self.assertIn('Single zone', result['text'])
        self.assertNotIn('In the cheaper zone', result['text'])
        self.assertNotIn('sobota', result['subject'])
        self.assertNotIn('17 Oct', result['subject'])
        self.assertIn('Comparison', result['text'])

    def test_date_language_independent_of_number_language(self):
        result = self.render(language='en', date_language='pl')
        self.assertIn('sobota 17 paź', result['subject'])
        self.assertIn('18.8 kWh', result['subject'])
        self.assertIn('Usage cost', result['text'])

    def test_timezone_controls_period_and_hour_labels(self):
        result = self.render(language='en', timezone_name='America/New_York')
        self.assertIn('Saturday 17 Oct', result['subject'])
        self.assertIn('18:00', result['text'])
        self.assertIn('America/New_York', result['text'])

    def test_partial_coverage_suppresses_all_share_and_change_percentages(self):
        self.model['coverage'].update(complete=False, measured_hours=8, expected_hours=24)
        result = self.render()
        for surface in ('text', 'subject', 'preheader'):
            self.assertNotRegex(result[surface], r'\d[,.\d]*%')
        self.assertIn('dane niepełne (8/24 h)', result['text'])
        self.assertIn('15,66 zł', result['text'])
        self.assertNotIn('width="33.0847%"', result['html'])

    def test_incomplete_comparison_cannot_publish_a_supplied_delta_pct(self):
        self.model['comparisons'][0]['coverage_ok'] = False
        result = self.render()
        self.assertNotIn('+6,6%', result['subject'])
        self.assertNotIn('+6,6%', result['preheader'])
        self.assertIn('dane niepełne', result['text'])

    def test_nulls_stay_unknown_and_do_not_make_zero_bars(self):
        for key in self.model['totals']:
            self.model['totals'][key] = None
        self.model['coverage'].update(complete=False, measured_hours=0)
        self.model['hourly'] = [dict(start='2026-10-17T00:00:00+02:00', hour=0, kwh=None, prev_kwh=None, zone=None)]
        self.model['zones'] = [dict(id='one', label='Missing zone', kwh=None, share=None, cost=None)]
        self.model['ranking'] = [dict(name='<Unknown>', kwh=None, share=None, cost=None)]
        self.model['rooms'] = [dict(name='Unknown room', kwh=None, share=None, cost=None)]
        self.model['comparisons'] = [dict(key='previous_day', kwh=None, delta_kwh=None, delta_pct=None, coverage_ok=False)]
        self.model['tariff'] = None
        self.model['alternatives'] = [dict(tariff='Unknown', rate=None, cost=None, delta=None)]
        result = self.render(language='en')
        self.assertIn('No data', result['text'])
        self.assertNotIn('0.00 kWh', result['text'])
        self.assertNotIn('None', result['html'])
        self.assertNotIn('class="tick" bgcolor=', result['html'])
        self.assertEqual(result['html'].count('data-energy-bar='), 1)

    def test_double_counting_hides_unmeasured_energy_cost_and_share(self):
        self.model['unmeasured']['status'] = 'double_counting'
        result = self.render()
        self.assertIn('Suma urządzeń przekracza import', result['text'])
        self.assertNotIn('5,47 kWh', result['text'])
        self.assertNotIn('4,98 zł', result['text'])
        self.assertNotIn('29%', result['text'])

    def test_escape_all_untrusted_names_and_safe_optional_cta(self):
        unsafe = '<script>alert("x")</script>'
        self.model['ranking'][0]['name'] = unsafe
        self.model['tariff']['name'] = unsafe
        self.model['zones'][0]['label'] = unsafe
        result = self.render(external_url='https://example.test/energy?a=1&b="two"')
        self.assertNotIn('<script>', result['html'])
        self.assertIn('&lt;script&gt;', result['html'])
        self.assertIn('href="https://example.test/energy?a=1&amp;b=&quot;two&quot;"', result['html'])
        self.assertIn(unsafe, result['text'])
        for url in ('javascript:alert(1)', 'data:text/html,a', '//example.test', 'https://user:pass@example.test', 'https://example.test\n/x', 'https://example.test:bad'):
            with self.subTest(url=url):
                result = self.render(external_url=url)
                self.assertNotIn('href=', result['html'])
                self.assertNotIn('Otwórz panel Energia:', result['text'])

    def test_dst_preserves_both_repeated_hours_and_real_coverage(self):
        self.model['period'].update(start='2026-10-25T00:00:00+02:00', end='2026-10-26T00:00:00+01:00', hours=25)
        self.model['coverage'].update(expected_hours=25, measured_hours=25)
        self.model['hourly'] = [dict(start='2026-10-25T02:00:00+02:00', hour=2, kwh=.2, prev_kwh=.1, zone='cheap'), dict(start='2026-10-25T02:00:00+01:00', hour=2, kwh=.3, prev_kwh=.2, zone='cheap')]
        result = self.render()
        self.assertIn('(25/25 h)', result['text'])
        self.assertIn('02:00 UTC+02:00', result['text'])
        self.assertIn('02:00 UTC+01:00', result['text'])
        self.assertEqual(result['html'].count('data-energy-bar='), 2)

    def test_longer_period_requires_daily_facts_and_never_aggregates_hourly(self):
        self.model['period'].update(kind='week', start='2026-10-12T00:00:00+02:00', end='2026-10-19T00:00:00+02:00')
        result = self.render(language='en')
        self.assertIn('Daily chart data unavailable.', result['text'])
        self.assertNotIn('data-energy-bar=', result['html'])
        self.assertIn('18.8 kWh', result['text'])
        self.assertNotIn('1/31', result['text'])
        self.model['daily'] = [dict(start='2026-10-12T00:00:00+02:00', kwh=4, prev_kwh=3, zones=[dict(id='cheap', kwh=1), dict(id='exp', kwh=3)])]
        result = self.render(language='en')
        self.assertEqual(result['html'].count('data-energy-bar='), 1)
        self.assertIn('ha-tools-energy-weekly', str(result['notify']))
        self.assertIn('class="cheap" bgcolor=', result['html'])
        self.assertIn('class="exp" bgcolor=', result['html'])

    def test_r12_rounding_is_presentation_only_not_component_summing(self):
        self.model['totals'].update(usage_cost=7.225, total_with_fixed=90.005)
        self.model['zones'][0]['cost'] = 1.111
        self.model['zones'][1]['cost'] = 1.111
        result = self.render(language='en')
        self.assertIn('7.23 PLN', result['text'])
        self.assertIn('90.01 PLN', result['text'])
        self.assertEqual(self.model['totals']['usage_cost'], 7.225)

    def test_month_title_and_optional_alternatives(self):
        self.model['period'].update(kind='month', start='2027-02-01T00:00:00+01:00', end='2027-03-01T00:00:00+01:00')
        self.model['alternatives'] = []
        result = self.render(language='en')
        self.assertIn('February 2027', result['subject'])
        self.assertNotIn('For information only', result['text'])
        self.assertIn('period share', result['text'])
