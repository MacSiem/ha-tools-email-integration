"""Scoped EN mobile tariff table; browser/Gmail geometry belongs to owner QA."""
import copy
from html import unescape
import re
import unittest
from tests.test_build1_mail_energy import MAIL, GOLDEN


def block(html, number):
    return html.split(f'<!-- block:{number} -->', 1)[1].split('<!-- block:', 1)[0]


class Build1EnglishZoneLayoutTests(unittest.TestCase):
    def setUp(self):
        self.model = copy.deepcopy(GOLDEN['model'])

    def render(self, language='en'):
        return MAIL.render(self.model, language=language, timezone_name='Europe/Warsaw', hour_cycle='24')

    def test_en_has_one_real_table_and_only_scoped_mobile_overrides(self):
        html = self.render()['html']
        zones = block(html, 6)
        self.assertEqual(zones.count('class="energy-zones-en"'), 1)
        rows = re.findall(r'<tr>(<td class="tx bd zone-label".*?)</tr>', zones)
        self.assertEqual(len(rows), len(self.model['zones']))
        for row in rows:
            self.assertEqual(row.count('<td '), 4)
            self.assertEqual(row.count('align="right"'), 3)
            self.assertNotRegex(row, r'<td[^>]*style="[^"]*display:')
        css = html.split('<style>', 1)[1].split('</style>', 1)[0]
        self.assertIn('@media screen and (max-width: 500px)', css)
        added = css.split('@media screen and (max-width: 500px)', 1)[1]
        # Every new selector is local to this EN table and inside one media rule.
        selectors = re.findall(r'^\s*(\.[^{]+)\{', added, re.M)
        self.assertTrue(selectors)
        self.assertTrue(all(selector.startswith('.energy-zones-en') for selector in selectors))
        self.assertIn('width:100%', added)
        self.assertIn('width:40%', added)
        self.assertIn('width:20%', added)
        self.assertIn('padding:0 8px 8px 0', added)
        self.assertNotIn('font-size:', added)
        self.assertNotIn('nowrap', added)
        self.assertNotIn('overflow:hidden', added)
        self.assertNotIn('display:none', added)
        self.assertNotIn('min-width:', added)
        self.assertEqual(html.count('>6.21 kWh</td>'), 1)
        self.assertEqual(html.count('>11.81 PLN</td>'), 1)

    def test_polish_keeps_four_cell_markup_and_has_no_en_styles_or_class(self):
        html = self.render('pl')['html']
        self.assertNotIn('energy-zones-en', html)
        self.assertNotIn('max-width: 500px', html)
        zones = block(html, 6)
        rows = re.findall(r'<tr>(<td class="tx bd zone-label".*?)</tr>', zones)
        self.assertEqual(len(rows), len(self.model['zones']))
        for row in rows:
            self.assertEqual(row.count('<td '), 4)
            self.assertEqual(row.count('align="right"'), 3)
            self.assertIn('padding:8px 0;', row)
        self.assertIn('6,21 kWh', zones)
        self.assertIn('11,81 zł', zones)

    def test_long_numbers_and_names_preserve_formatted_data_and_escape(self):
        self.model['zones'][0].update(label='<LongName>' + 'Energy' * 80,
            hours_label='22–6, <13–15>', kwh=123456789.125, cost=987654321.125)
        before = copy.deepcopy(self.model)
        result = self.render()
        zones = block(result['html'], 6)
        self.assertEqual(before, self.model)
        self.assertNotIn('<LongName>', zones)
        self.assertIn('&lt;LongName&gt;', zones)
        self.assertIn('&lt;13–15&gt;', zones)
        self.assertIn('123,456,789.13 kWh', unescape(zones))
        self.assertIn('987,654,321.13 PLN', unescape(zones))
        self.assertIn('overflow-wrap:anywhere', MAIL._EN_ZONE_MOBILE_CSS)
        self.assertNotIn('text-overflow:ellipsis', MAIL._EN_ZONE_MOBILE_CSS)

    def test_unknown_partial_and_zero_do_not_change_semantics(self):
        self.model['coverage']['complete'] = False
        self.model['zones'][0].update(kwh=None, share=.99, cost=None)
        self.model['zones'][1].update(kwh=0, share=0, cost=0)
        zones = block(self.render()['html'], 6)
        first = zones.split('<td class="tx bd zone-label"', 1)[1].split('</tr>', 1)[0]
        self.assertNotIn('99%', first)
        self.assertNotIn('0.00', first)
        self.assertIn('—', unescape(first))
        self.assertIn('0.00 kWh', zones)
        self.assertIn('0.00 PLN', zones)

    def test_r12_model_text_notifications_and_marker_are_unchanged(self):
        before = copy.deepcopy(self.model)
        result = self.render()
        self.assertEqual(before, self.model)
        for value in ('6.21 kWh', '33%', '3.85 PLN', '12.56 kWh', '11.81 PLN'):
            self.assertIn(value, result['text'])
            self.assertIn(value, result['html'])
        self.assertNotIn('energy-zones-en', result['text'])
        self.assertNotIn('energy-zones-en', str(result['notify']))
        self.assertIn('█' * 128, block(result['html'], 5))
