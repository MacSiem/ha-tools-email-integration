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

    def test_complete_mail_reads_as_a_report_and_keeps_audit_in_csv(self):
        result = self.render()
        visible = fixture.ReportDOM(result['html']).root.text()
        for technical in ('Own current measurements', 'Full baseline period energy', 'Measurement 24',
                          'Consumer calendar period', 'Details · Comparisons'):
            self.assertNotIn(technical, visible)
            self.assertNotIn(technical, result['text'])
        for title in ('Measured timeline', 'From', 'To', 'Selected comparisons', 'Rooms', 'Cost and rates'):
            self.assertIn(title, visible)
        for path in ('comparisons.previous.full_baseline_kwh', 'chart.bins.23.start'):
            self.assertIn(path, result['csv'])

    def test_mail136_kpi_uses_full_producer_comparison_and_effective_price(self):
        self.s = json.loads((fixture.ROOT / 'tests/fixtures/energy-report-schema2-v53.json').read_text())
        c = self.s['comparisons']['previous']
        c.update(delta_kwh='12.3456', percent='5.4321')
        result = self.render()
        kpis = self.section(result, 'kpis').text()
        for value in ('Previous period', '+12.35 kWh', '+5.4%', '1.00 EUR/kWh'):
            self.assertIn(value, kpis)
            self.assertIn(value, result['text'])
        self.assertIn('Effective price', result['text'])

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

    def test_lower_tables_keep_phone_reading_width_and_avoid_empty_zone_audit_rows(self):
        result = self.render()
        zones = self.section(result, 'measured-zone-values')
        rows = [node for node in zones.children if isinstance(node, fixture.ReportNode) and node.tag == 'tr']
        self.assertLessEqual(len(rows), 49)
        self.assertEqual(24, len(zones.find(data_role='zone-heading')))
        self.assertNotIn('Unattributed to zone', zones.text())
        self.assertIn('10.0 kWh', zones.text())
        groups = self.section(result, 'comparison-groups')
        for row in [n for n in groups.children if isinstance(n, fixture.ReportNode) and n.tag == 'tr']:
            self.assertLessEqual(len(row.children), 3)

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
        self.assertIn('0.0 kWh', kpis)
        self.assertIn('No change', self.render()['text'])

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
        self.assertIn('-123.45 kWh', kpis)
        self.assertIn('Decrease -123.45 kWh', self.render()['text'])
        self.assertNotIn('Previous period', kpis)

    def test_mail137_all_mail_values_are_brief_and_csv_keeps_exact_producer_values(self):
        device = next(iter(self.s['groups']['devices'].values()))
        raw_device, raw_residual = '49.9999999999995450', '200.0000000000004550'
        device.update(kwh=raw_device, recorded_kwh=raw_device)
        self.s['groups']['unmeasured'].update(kwh=raw_residual, recorded_kwh=raw_residual)
        result = self.render()
        visual = self.section(result, 'flow-destination-composition').text()
        self.assertIn('50 kWh', visual)
        self.assertIn('200 kWh', visual)
        for raw in (raw_device, raw_residual):
            self.assertNotIn(raw, visual)
            self.assertNotIn(raw, result['text'])
            self.assertIn(raw, result['csv'])

    def test_mail137_flow_swatch_width_is_an_explicit_column_not_an_equal_table_share(self):
        result = self.render()
        for side in ('source', 'destination'):
            section = self.section(result, 'flow-' + side + '-composition')
            columns = [n for n in section.find(width='12') if n.tag == 'col']
            self.assertEqual(1, len(columns))
            self.assertIn('width:12px', columns[0].attrs.get('style', ''))
