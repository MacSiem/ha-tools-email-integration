"""Mail136 regressions; whole-period fixture is the existing front producer capture."""
import copy
import json
import unittest
from tests import test_energy_report as fixture


class Mail136Test(unittest.TestCase):
    def setUp(self):
        self.s = json.loads((fixture.ROOT / 'tests/fixtures/energy-report-wholeperiod136.json').read_text())

    def render(self):
        return fixture.REPORT.render_energy_analysis(self.s, language='en', timezone_name='Europe/Warsaw', hour_cycle='24')

    def section(self, result, name):
        nodes = fixture.ReportDOM(result['html']).root.find(data_section=name)
        self.assertEqual(1, len(nodes), 'Missing report section: ' + name)
        return nodes[0]

    def test_mail136_kpi_uses_full_producer_comparison_and_effective_price(self):
        self.s = json.loads((fixture.ROOT / 'tests/fixtures/energy-report-schema2-v53.json').read_text())
        c = self.s['comparisons']['previous']
        c.update(delta_kwh='12.3456', percent='5.4321')
        result = self.render()
        kpis = self.section(result, 'kpis').text()
        for value in ('Previous period', '+12.3456 kWh', '+5.4321%', 'Effective price', '1.00 EUR/kWh'):
            self.assertIn(value, kpis)
            self.assertIn(value, result['text'])

    def test_mail136_kpi_rejects_partial_masked_and_unrelated_comparisons(self):
        self.s = json.loads((fixture.ROOT / 'tests/fixtures/energy-report-schema2-v53.json').read_text())
        base = copy.deepcopy(self.s['comparisons']['previous'])
        for change in ({'status': 'partial'}, {'comparison_scope': 'common_measurements'},
                       {'current_kwh': '239'}, {'full_current_kwh': '239'},
                       {'coverage': {'current': 1, 'baseline': 0.9}}):
            with self.subTest(change=change):
                self.s['comparisons']['previous'] = dict(base, **change)
                self.assertIn('No comparable full-period baseline', self.section(self.render(), 'kpis').text())
        self.s['active_tariff_id'] = None
        self.s['native_pricing'] = {'schema': 1, 'source': 'ha_energy', 'period_cost': {
            'status': 'ready', 'coverage': {'complete': True}, 'amount': '57.77', 'currency': 'EUR'}}
        kpis = self.section(self.render(), 'kpis').text()
        self.assertIn('57.77', kpis)
        self.assertNotIn('Effective price', kpis)

    def test_mail136_two_complete_partition_bars_and_accessible_values(self):
        result = self.render()
        source = self.section(result, 'flow-source-composition')
        destination = self.section(result, 'flow-destination-composition')
        self.assertEqual(2, len(source.find(data_role='flow-segment')))
        self.assertEqual(2, len(destination.find(data_role='flow-segment')))
        for text in ('From', 'To', '192.0 kWh', '58.0 kWh', '50.0 kWh', '200.0 kWh', 'Unmeasured'):
            self.assertIn(text, result['text'])
        self.assertNotIn('No area', destination.text())
        self.assertIn('76.8%', source.text())
        self.assertIn('23.2%', source.text())
        for forbidden in ('<svg', '<script', 'display:flex', 'display:grid'):
            self.assertNotIn(forbidden, result['html'])

    def test_mail136_partition_proof_rejects_mismatch_overlap_negative_and_stale(self):
        base = copy.deepcopy(self.s)
        mutations = [
            lambda s: s['groups'].update(partition_complete=False),
            lambda s: s['groups'].update(registry_revision=999),
            lambda s: s['groups']['unmeasured'].update(kwh='-1'),
            lambda s: s['groups']['devices'].update(duplicate=copy.deepcopy(next(iter(s['groups']['devices'].values())))),
            lambda s: s['totals'].update(recorded_household_kwh='249'),
        ]
        for mutate in mutations:
            self.s = copy.deepcopy(base); mutate(self.s)
            part = self.section(self.render(), 'flow-destination-composition')
            self.assertEqual([], part.find(data_role='flow-segment'))
            self.assertIn('Unknown', part.text())
        mutations = [
            lambda s: s['period_summary']['window'].update(timezone='UTC'),
            lambda s: s['period_summary']['zone_breakdown']['zones'][0].update(kwh='191'),
            lambda s: s['period_summary']['zone_breakdown']['unattributed'].update(recorded_kwh='1'),
            lambda s: s['period_summary']['zone_breakdown'].update(reconciles_recorded_kwh=False),
            lambda s: s['period_summary']['zone_breakdown']['zones'][0].update(label='wrong zone'),
        ]
        for mutate in mutations:
            self.s = copy.deepcopy(base); mutate(self.s)
            part = self.section(self.render(), 'flow-source-composition')
            self.assertEqual([], part.find(data_role='flow-segment'))
            self.assertIn('Unknown', part.text())

    def test_mail136_legend_only_chart_zones_escaped_and_mixed_explicit(self):
        zones = self.s['rates'][0]['profile']['zones']
        zones[0]['label'] = '<Day & peak>'
        # The chart legend describes actual plotted bins, not every tariff zone.
        one = copy.deepcopy(self.s['chart']['bins'][0])
        z = one['zone_breakdown']['zones'][0]
        for rate_zone in zones:
            if rate_zone['id'] == z['zone_id']:
                rate_zone['label'] = '<Actual & zone>'
                rate_zone['classification'] = 'cheap'
        z['label'] = '<Actual & zone>'
        mixed = copy.deepcopy(one)
        mixed['zone_breakdown']['zones'].append(dict(z, zone_id='other', label='Other'))
        unknown = copy.deepcopy(one); unknown.pop('zone_breakdown')
        self.s['chart']['bins'] = [one, mixed, unknown]
        result = self.render()
        legend = self.section(result, 'chart-zone-legend')
        self.assertIn('<Actual & zone>', legend.text())
        self.assertIn('Mixed zones', legend.text())
        self.assertIn('Unknown zone', legend.text())
        self.assertNotIn('<Day & peak>', legend.text())
        self.assertIn('&lt;Actual &amp; zone&gt;', result['html'])
        self.assertIn('zone-cheap', [n.attrs.get('class') for n in legend.find(data_role='zone-swatch')])
        self.assertIn('Mixed zones', result['text'])

    def test_mail136_unknown_percent_keeps_delta_without_fake_percentage(self):
        self.s = json.loads((fixture.ROOT / 'tests/fixtures/energy-report-schema2-v53.json').read_text())
        c = self.s['comparisons']['previous']
        c.update(baseline_kwh='0', full_baseline_kwh='0', percent=None)
        kpis = self.section(self.render(), 'kpis').text()
        self.assertNotIn('Unknown%', kpis)
        self.assertIn('No change', kpis)

    def test_mail136_destination_colors_distinguish_device_identities_and_match_legend(self):
        devices = self.s['groups']['devices']
        first = next(iter(devices.values()))
        first.update(kwh='25.0', recorded_kwh='25.0')
        devices['z-second'] = dict(first, name='Second meter')
        result = self.render()
        destination = self.section(result, 'flow-destination-composition')
        colors = [n.attrs.get('class') for n in destination.find(data_role='flow-segment')]
        self.assertEqual({'device-0', 'device-1', 'unmeasured'}, set(colors))
        swatches = [n.attrs.get('class') for n in destination.find(data_role='flow-swatch')]
        self.assertEqual(set(colors), set(swatches))

    def test_mail136_kpi_respects_requested_year_ago_baseline(self):
        self.s = json.loads((fixture.ROOT / 'tests/fixtures/energy-report-schema2-v53.json').read_text())
        self.s['context']['comparison_kind'] = 'year_ago'
        self.s['comparisons']['year_ago'] = dict(self.s['comparisons']['previous'], delta_kwh='-123.45')
        kpis = self.section(self.render(), 'kpis').text()
        self.assertIn('Same period last year', kpis)
        self.assertIn('Decrease -123.45 kWh', kpis)
        self.assertNotIn('Previous period', kpis)
