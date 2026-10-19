"""Report93: semantic HTML colours and no-floor label; owner runs RED/GREEN."""
from copy import deepcopy
import csv
from io import StringIO
import re
import unittest

from tests import test_energy_report as report_fixture
from tests import test_report89_period_facts as report89_fixture

REPORT = report_fixture.REPORT


class Report93PresentationTests(unittest.TestCase):
    def fixture(self):
        # Real accumulator DTO, including public rates and measured zone proofs.
        return report89_fixture.Report89PeriodFactsTests().fixture()

    def render(self, value, language='en'):
        return REPORT.render_energy_analysis(value, language=language,
            timezone_name='UTC', hour_cycle='24')

    def section(self, result, name):
        return report_fixture.ReportDOM(result['html']).root.find(data_section=name)[0]

    def colours(self, value):
        bars = self.section(self.render(value), 'timeline').find(data_role='bar')
        return [set(bar.attrs['class'].split()) - {'bar-fill'} for bar in bars]

    def set_proof(self, value, index, version, zone, *, complete=True, status='ready'):
        value['chart']['bins'][index]['zone_breakdown'] = {
            'status': status, 'zones': [{'version_id': version, 'zone_id': zone,
                'complete': complete, 'kwh': '1', 'label': 'Misleading cheap label'}]}

    def test_actual_dto_and_reversed_profile_and_bin_order_use_classification(self):
        value = self.fixture()
        value['chart']['bins'] = value['chart']['bins'][:2]
        rate = value['rates'][0]
        self.assertTrue(rate['version_id'])
        expected = {zone['id']: 'zone-' + zone['classification']
                    for zone in rate['profile']['zones']}
        self.assertEqual({'zone-cheap', 'zone-expensive'}, set(expected.values()))
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                candidate = deepcopy(value)
                if reverse:
                    candidate['rates'][0]['profile']['zones'].reverse()
                    candidate['chart']['bins'].reverse()
                actual = self.colours(candidate)
                for index, item in enumerate(candidate['chart']['bins']):
                    zone = item['zone_breakdown']['zones'][0]
                    self.assertEqual({expected[zone['zone_id']]}, actual[index])

    def test_same_zone_id_in_different_versions_and_reversed_rates(self):
        value = self.fixture()
        rate = deepcopy(value['rates'][0])
        rate['profile']['zones'] = [{'id': 'same', 'classification': 'cheap'}]
        rate['version_id'] = 'v-cheap'
        other = deepcopy(rate)
        other['version_id'] = 'v-expensive'
        other['profile']['zones'][0]['classification'] = 'expensive'
        value['rates'] = [rate, other]
        self.set_proof(value, 0, 'v-expensive', 'same')
        self.set_proof(value, 1, 'v-cheap', 'same')
        for reverse in (False, True):
            if reverse:
                value['rates'].reverse()
            self.assertEqual([{'zone-expensive'}, {'zone-cheap'}], self.colours(value)[:2])

    def test_unknown_missing_versions_partial_and_mixed_bins_stay_neutral(self):
        for defect in ('unknown', 'missing', 'unclassified', 'mixed', 'partial',
                       'incomplete', 'dynamic', 'conflict', 'missing_version'):
            with self.subTest(defect=defect):
                value = self.fixture()
                rate = value['rates'][0]
                zone = rate['profile']['zones'][0]
                self.set_proof(value, 0, rate['version_id'], zone['id'])
                if defect == 'unknown':
                    zone['classification'] = 'unknown'
                elif defect == 'missing':
                    value['rates'] = []
                elif defect == 'unclassified':
                    zone.pop('classification')
                    zone.update(label='Cheap night', energyRate='-999')
                elif defect == 'mixed':
                    proof = value['chart']['bins'][0]['zone_breakdown']
                    proof['zones'].append(deepcopy(proof['zones'][0]))
                elif defect == 'partial':
                    value['chart']['bins'][0]['zone_breakdown']['status'] = 'partial'
                elif defect == 'incomplete':
                    value['chart']['bins'][0]['zone_breakdown']['zones'][0]['complete'] = False
                elif defect == 'dynamic':
                    rate['profile']['dynamic'] = {'intervals': []}
                elif defect == 'missing_version':
                    rate.pop('version_id')
                    value['chart']['bins'][0]['zone_breakdown']['zones'][0].pop('version_id')
                else:
                    duplicate = deepcopy(rate)
                    duplicate['profile']['zones'][0]['classification'] = (
                        'expensive' if zone['classification'] == 'cheap' else 'cheap')
                    value['rates'].append(duplicate)
                self.assertEqual({'zone-unknown'}, self.colours(value)[0])

    def test_light_and_dark_css_map_semantic_classes_to_green_blue_and_neutral(self):
        html = self.render(self.fixture())['html']
        css = re.search(r'<style>(.*?)</style>', html, re.S).group(1)
        light, dark = css.split('@media(prefers-color-scheme:dark)', 1)
        for styles, green, blue, neutral in ((light, '#25824b', '#2877cb', '#7b8595'),
                                           (dark, '#65ce8c', '#68aaff', '#a6afbc')):
            self.assertIn('.zone-cheap{background:' + green + '}', styles)
            self.assertIn('.zone-expensive{background:' + blue + '}', styles)
            self.assertIn('.zone-unknown,.unmeasured{background:' + neutral + '}', styles)
        self.assertNotIn('zone-0', css)

    def floor_fixture(self):
        value = self.fixture()
        row = deepcopy(next(iter(value['groups']['floors'].values())))
        row.pop('name', None)
        value['groups']['floors'] = {'__no_floor__': row}
        for device in value['groups']['devices'].values():
            device.update(floor_id=None, floor_name=None)
        return value

    def test_no_floor_is_localized_only_in_html_preserving_source_export_and_snapshot(self):
        for language, caption, old_caption in (('pl', 'Bez piętra', 'Piętra 1'),
                                               ('en', 'No floor', 'Floors 1')):
            with self.subTest(language=language):
                value = self.floor_fixture()
                before = deepcopy(value)
                result = self.render(value, language)
                self.assertIn(caption, self.section(result, 'floors').text())
                self.assertNotIn(old_caption, self.section(result, 'floors').text())
                self.assertIn(old_caption + ':', result['text'])
                rows = {row['field']: row for row in csv.DictReader(StringIO(result['csv']))}
                key = 'groups.floors.__no_floor__.kwh'
                self.assertIn(key, rows)
                self.assertIn('__no_floor__', rows[key]['label'])
                self.assertEqual(before, value)
                self.assertEqual(value['snapshot_id'], result['snapshot_id'])

    def test_custom_no_floor_label_and_real_floor_name_are_preserved_and_escaped(self):
        for identity, source in (('__no_floor__', 'aggregate'), ('__no_floor__', 'registry'),
                                 ('real-floor', 'aggregate'), ('real-floor', 'registry')):
            with self.subTest(identity=identity, source=source):
                value = self.floor_fixture()
                row = value['groups']['floors'].pop('__no_floor__')
                value['groups']['floors'][identity] = row
                name = 'Piętra 1 <custom>'
                if source == 'aggregate':
                    row['name'] = name
                else:
                    next(iter(value['groups']['devices'].values())).update(
                        floor_id=identity, floor_name=name)
                for language in ('pl', 'en'):
                    result = self.render(value, language)
                    self.assertIn(name, self.section(result, 'floors').text())
                    self.assertIn('&lt;custom&gt;', result['html'])
                    self.assertNotIn('No floor', self.section(result, 'floors').text())
                    self.assertNotIn('Bez piętra', self.section(result, 'floors').text())


if __name__ == '__main__':
    unittest.main()
