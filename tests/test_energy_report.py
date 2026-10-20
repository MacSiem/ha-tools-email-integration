"""Report contracts against a real synthetic schema-2 producer snapshot.

Fixture copied byte-for-byte from ha-energy-optimizer at c5b623692abb33e7518d248ab45c07dae8364874.
The export tests deliberately change backend facts: the renderer must echo those
facts, not reconstruct their pricing or assume currencies from active totals.
"""
import copy
import csv
import importlib.util
from io import StringIO
import json
from pathlib import Path
import unittest
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('energy_report_under_test',
    ROOT / 'custom_components/ha_tools_email/energy_report.py')
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.cells = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if any(key.lower().startswith('on') for key, _ in attrs):
            raise AssertionError('Inline event handler in report')

    def handle_data(self, data):
        self.cells.append(data)



class ReportNode:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return ''.join(item if isinstance(item, str) else item.text() for item in self.children)

    def find(self, **attrs):
        result = []
        for item in self.children:
            if isinstance(item, ReportNode):
                if all(item.attrs.get(key.replace('_', '-')) == value for key, value in attrs.items()):
                    result.append(item)
                result.extend(item.find(**attrs))
        return result


class ReportDOM(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.root = ReportNode()
        self.stack = [self.root]
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = ReportNode(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ('meta', 'col'):
            self.stack.append(node)

    def handle_endtag(self, tag):
        if len(self.stack) > 1:
            if self.stack[-1].tag != tag:
                raise AssertionError('Unbalanced email table markup')
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class EnergyReportTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads((ROOT / 'tests/fixtures/energy-report-schema2-v53.json').read_text())

    def render(self, snapshot=None, **kwargs):
        options = {'language': 'en', 'timezone_name': 'Europe/Warsaw', 'hour_cycle': '24'}
        options.update(kwargs)
        return REPORT.render_energy_analysis(self.snapshot if snapshot is None else snapshot, **options)

    def rows(self, result):
        return {row['field']: row for row in csv.DictReader(StringIO(result['csv']))}

    def test_actual_producer_snapshot_has_all_sections_same_identity_and_values(self):
        before = copy.deepcopy(self.snapshot)
        result = self.render()
        rows = self.rows(result)
        sid = self.snapshot['snapshot_id']
        self.assertEqual({'snapshot_id', 'html', 'text', 'csv'}, set(result))
        self.assertEqual(before, self.snapshot)
        self.assertEqual(sid, result['snapshot_id'])
        self.assertTrue(all(row['snapshot_id'] == sid for row in rows.values()))
        for output in ('html', 'text', 'csv'):
            self.assertIn(sid, result[output])
        expected = {
            'totals.total_cost': ('241.00', 'EUR'),
            'totals.fixed_cost': ('1.00', 'EUR'),
            'totals.effective_price': ('1.00', 'EUR/kWh'),
            'totals.kwh': ('240.0', 'kWh'),
            'comparisons.previous.costs.delta_cost': ('0.00', 'EUR'),
            'chart.bins.0.usage_cost': ('10.00', 'EUR'),
            'chart.bins.0.fixed_cost': ('Unknown', 'EUR'),
            'groups.devices.sensor.device.kwh': ('48', 'kWh'),
            'groups.areas.office.usage_cost': ('48.00', 'EUR'),
            'groups.floors.upstairs.usage_cost': ('48.00', 'EUR'),
            'groups.unassigned.kwh': ('0', 'kWh'),
            'groups.unmeasured.kwh': ('192.0', 'kWh'),
            'fees.components.subscription': ('1.00', 'EUR'),
            'rates.0.fixed_monthly.subscription': ('31', 'EUR/month'),
            'fees.days.0.day_fraction': ('1', ''),
            'comparisons.previous.groups.devices.sensor.device.current_kwh': ('46', 'kWh'),
            'comparisons.previous.groups.devices.sensor.device.own_current_kwh': ('48', 'kWh'),
            'comparisons.previous.groups.devices.sensor.device.full_baseline_kwh': ('Unknown', 'kWh'),
        }
        for field, (value, unit) in expected.items():
            with self.subTest(field=field):
                self.assertEqual((value, unit), (rows[field]['value'], rows[field]['unit']))
                self.assertIn(value, result['text'])
                self.assertIn(value, result['html'])
        self.assertIn('Desk', rows['groups.devices.sensor.device.kwh']['label'])
        self.assertIn('Office', rows['groups.areas.office.kwh']['label'])
        self.assertIn('Upstairs', rows['groups.floors.upstairs.kwh']['label'])
        self.assertEqual('common_measurements', rows[
            'comparisons.previous.groups.devices.sensor.device.comparison_scope']['value'])
        self.assertEqual('calendar_totals', rows['comparisons.previous.comparison_scope']['value'])
        self.assertIn('same measurements', result['text'])
        self.assertIn('does not activate', result['text'])

    def test_zero_unknown_negative_and_unrounded_decimal_are_distinct_all_formats(self):
        self.snapshot['totals'].update(energy_cost='0.00', variable_cost=None,
            total_cost='-123456789012345678901234567890.123400', effective_price='1E-20')
        result = self.render()
        rows = self.rows(result)
        literals = {'totals.energy_cost': '0.00', 'totals.variable_cost': 'Unknown',
            'totals.total_cost': '-123456789012345678901234567890.123400',
            'totals.effective_price': '0.00000000000000000001'}
        for field, value in literals.items():
            self.assertEqual(value, rows[field]['value'])
            self.assertIn(value, result['html'])
            self.assertIn(value, result['text'])
        # Valid negative numeric data remains numeric in spreadsheet exports.
        self.assertFalse(rows['totals.total_cost']['value'].startswith("'"))

    def test_user_locale_uses_decimal_comma_and_preserves_date_only(self):
        result = self.render(language='pl', timezone_name='America/Los_Angeles', hour_cycle='12')
        rows = self.rows(result)
        self.assertEqual('241,00', rows['totals.total_cost']['value'])
        self.assertEqual('1 sty 2026', rows['rates.0.valid_from']['value'])
        self.assertEqual('10 paź 2026', rows['fees.days.0.day']['value'])
        self.assertIn('9 paź 2026 05:00:00 PM -0700', rows['period.start']['value'])
        self.assertEqual('Brak danych', rows['chart.bins.0.fixed_cost']['value'])
        self.assertIn('Niezmierzone', result['text'])

    def test_dst_repeated_hour_dynamic_epoch_and_rule_wall_clock_are_unambiguous(self):
        bins = self.snapshot['chart']['bins']
        bins[0]['start'] = '2026-10-25T00:00:00+00:00'
        bins[1]['start'] = '2026-10-25T01:00:00+00:00'
        profile = self.snapshot['rates'][0]['profile']
        profile['dynamic'] = {'intervals': [{'from': 1792886400000, 'to': 1792890000000,
            'energyRate': '-0.123400', 'variableRate': 0, 'classification': 'cheap'}]}
        profile['rules'] = [{'zone': 'day', 'priority': 1, 'dates': ['2026-10-25'],
            'intervals': [{'from': '02:00', 'to': '03:00'}],
            'season': {'from': '10-01', 'to': '10-31'}}]
        result = self.render()
        rows = self.rows(result)
        self.assertIn('02:00:00 +0200', rows['chart.bins.0.start']['value'])
        self.assertIn('02:00:00 +0100', rows['chart.bins.1.start']['value'])
        self.assertIn('02:00:00 +0200', rows['rates.0.profile.dynamic.intervals.0.from']['value'])
        self.assertEqual('-0.123400', rows['rates.0.profile.dynamic.intervals.0.energyRate']['value'])
        self.assertEqual('02:00', rows['rates.0.profile.rules.0.intervals.0.from']['value'])
        self.assertEqual('Oct 25, 2026', rows['rates.0.profile.rules.0.dates.0']['value'])
        self.assertEqual('10-01', rows['rates.0.profile.rules.0.season.from']['value'])

    def test_historical_and_comparison_costs_keep_own_currencies_without_repricing(self):
        pair = self.snapshot['comparisons']['previous']
        pair['costs']['current']['currency'] = 'EUR'
        pair['costs']['baseline']['currency'] = 'PLN'
        pair['costs']['baseline']['usage_cost'] = '907.4310'
        pair['costs'].update(status='unknown', reason='incomparable_currency_or_basis', delta_cost=None)
        baseline = pair['baseline_analysis']
        baseline['totals'].update(currency='PLN', total_cost='999.9900')
        baseline['rates'][0]['currency'] = 'PLN'
        self.snapshot['alternatives'][0].update(comparable=False, currency='USD',
            total_cost='543.2100', delta_cost=None, comparison_reason='currency_mismatch')
        result = self.render()
        rows = self.rows(result)
        self.assertEqual('PLN', rows['comparisons.previous.costs.baseline.usage_cost']['unit'])
        self.assertEqual('907.4310', rows['comparisons.previous.costs.baseline.usage_cost']['value'])
        self.assertEqual('EUR', rows['comparisons.previous.costs.current.usage_cost']['unit'])
        self.assertEqual('PLN', rows['comparisons.previous.baseline_analysis.totals.total_cost']['unit'])
        self.assertEqual('999.9900', rows['comparisons.previous.baseline_analysis.totals.total_cost']['value'])
        self.assertEqual('PLN', rows['comparisons.previous.baseline_analysis.fees.components.subscription']['unit'])
        self.assertEqual('Unknown', rows['comparisons.previous.costs.delta_cost']['value'])
        self.assertEqual('Unknown', rows['comparisons.previous.costs.delta_cost']['unit'])
        self.assertEqual('incomparable_currency_or_basis', rows['comparisons.previous.costs.reason']['value'])
        self.assertEqual('USD', rows['alternatives.0.total_cost']['unit'])
        self.assertEqual('543.2100', rows['alternatives.0.total_cost']['value'])
        self.assertEqual('Unknown', rows['alternatives.0.delta_cost']['value'])

    def test_html_tables_escape_labels_and_csv_neutralizes_text_formulas(self):
        name = ' \t=HYPERLINK("https://invalid", "<script>alert(1)</script>")\nnext'
        self.snapshot['groups']['devices']['sensor.device']['name'] = name
        self.snapshot['alternatives'][0]['tariff_name'] = '@SUM(1,2)'
        result = self.render()
        rows = self.rows(result)
        self.assertEqual("'" + name, rows['groups.devices.sensor.device.name']['value'])
        self.assertEqual("'@SUM(1,2)", rows['alternatives.0.tariff_name']['value'])
        self.assertIn(name, result['text'])
        self.assertNotIn('<script>', result['html'])
        self.assertIn('&lt;script&gt;', result['html'])
        markup = Markup(result['html'])
        self.assertFalse(set(markup.tags) - {'html', 'head', 'title', 'meta', 'style', 'body',
            'table', 'caption', 'colgroup', 'col', 'tr', 'th', 'td'})
        self.assertIn('@media(prefers-color-scheme:dark)', result['html'])
        self.assertNotIn('<svg', result['html'])
        self.assertNotIn('<canvas', result['html'])
        self.assertNotIn('<img', result['html'])
        # The long exact value occurs in normal text nodes, never merely an attribute.
        self.assertIn('241.00', markup.cells)

    def test_missing_optional_sections_is_honest_and_does_not_invent_zones(self):
        minimal = {'schema': 2, 'snapshot_id': 'synthetic-empty', 'totals': {'total_cost': None,
            'currency': None}, 'chart': {'status': 'unknown', 'reason': 'calendar_kind_unavailable',
            'bins': []}, 'groups': {}, 'fees': None, 'rates': [], 'comparisons': {}}
        result = self.render(minimal)
        rows = self.rows(result)
        self.assertEqual('Unknown', rows['totals.total_cost']['value'])
        self.assertEqual('No entries', rows['chart.bins']['value'])
        self.assertEqual('calendar_kind_unavailable', rows['chart.reason']['value'])
        self.assertFalse(any('zone' in key or 'marker' in key for key in rows))

    def test_locale_and_aware_instant_are_required(self):
        for override in ({'language': None}, {'language': 'de'}, {'timezone_name': None},
            {'timezone_name': 'Etc/Invalid'}, {'hour_cycle': None}, {'hour_cycle': True},
            {'hour_cycle': 12.0}, {'hour_cycle': 24}, {'hour_cycle': 'h23'}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.render(**override)
        with self.assertRaises(ValueError):
            self.render({'schema': 1, 'snapshot_id': 'legacy'})
        self.snapshot['period']['start'] = '2026-10-10T00:00:00'
        with self.assertRaisesRegex(ValueError, 'aware_report_instant_required'):
            self.render()

    def test_transport_preflight_is_public_and_operational_context_is_not_exported(self):
        self.assertEqual('Europe/Warsaw', REPORT.validate_report_preferences(
            'pl', 'Europe/Warsaw', '24').key)
        with self.assertRaises(ValueError):
            REPORT.validate_report_preferences('pl', 'UTC', 24)
        self.snapshot['context']['access_purpose'] = 'energy_reports'
        self.snapshot['comparisons']['previous']['baseline_analysis']['context'][
            'access_purpose'] = 'private-operational-marker'
        result = self.render()
        for kind in ('html', 'text', 'csv'):
            self.assertNotIn('access_purpose', result[kind])
            self.assertNotIn('private-operational-marker', result[kind])


    def test_energy_decimal_and_legacy_monthly_fee_have_local_format_and_units(self):
        self.snapshot['totals']['kwh']='-0.1250'
        self.snapshot['rates'][0]['legacy_combined_monthly']='2.00'
        result=self.render(language='pl');rows=self.rows(result)
        self.assertEqual('-0,1250',rows['totals.kwh']['value'])
        self.assertEqual('EUR/miesiąc',rows['rates.0.legacy_combined_monthly']['unit'])
        self.assertIn('-0,1250',result['text']);self.assertIn('-0,1250',result['html'])
        self.assertFalse(rows['totals.kwh']['value'].startswith("'"))

    def test_readable_report_hides_implementation_paths_while_csv_keeps_source_columns(self):
        result=self.render()
        self.assertEqual('Jan 1, 2026',self.rows(result)['rates.0.valid_from']['value'])
        for kind in ('html','text'):
            self.assertNotIn('chart.bins.0',result[kind])
            self.assertNotIn('rates.0.profile',result[kind])
            self.assertNotIn('groups.devices.sensor.device.kwh',result[kind])
        self.assertIn('chart.bins.0',result['csv'])


    def test_pricing_coverage_counters_are_seconds_not_money(self):
        for language in ('en','pl'):
            rows=self.rows(self.render(language=language))
            counters=[row for path,row in rows.items() if '.priced_source_seconds.' in path]
            self.assertTrue(counters)
            self.assertEqual({'s'}, {row['unit'] for row in counters})
            self.assertTrue(any(row['value']=='86400' for row in counters))
            self.assertEqual('EUR',rows['comparisons.previous.costs.current.energy_cost']['unit'])


    def test_approved_sections_precede_details_and_use_email_safe_bars(self):
        result = self.render()
        titles = ['Measured energy', 'Period cost', 'Cheap-zone share',
                  'Measured timeline', 'From the grid to devices',
                  'Selected comparisons', 'Rooms', 'Floors', 'Cost and rates']
        for kind in ('html', 'text'):
            positions = [result[kind].index(title) for title in titles]
            self.assertEqual(sorted(positions), positions)
            self.assertIn('No published period share', result[kind])
            self.assertNotIn('source signature', result[kind])
            self.assertNotIn('calculator version', result[kind])
            self.assertLess(result[kind].index('Cost and rates'), result[kind].index('Details ·'))
        self.assertIn('max-width:640px', result['html'])
        self.assertIn('class="bar-fill', result['html'])
        self.assertIn('width="100%"', result['html'])
        self.assertIn('prefers-color-scheme:dark', result['html'])
        for forbidden in ('<svg', '<canvas', '<img', '<script', 'display:flex', 'display:grid', 'url('):
            self.assertNotIn(forbidden, result['html'])
        rows = self.rows(result)
        for field in ('totals.kwh', 'totals.total_cost', 'chart.bins.0.kwh',
                      'groups.devices.sensor.device.kwh', 'groups.unmeasured.kwh',
                      'fees.components.subscription', 'rates.0.fixed_monthly.subscription'):
            row = rows[field]
            self.assertIn(row['value'], Markup(result['html']).cells)
            self.assertIn(row['value'], result['text'])

    def test_date_language_is_independent_of_ui_language_and_calendar_timezone(self):
        for language, date_language, heading, day, number in (
            ('en', 'pl', 'Energy report', '10 paź 2026', '241.00'),
            ('pl', 'en', 'Raport energii', 'Oct 10, 2026', '241,00')):
            result = self.render(language=language, date_language=date_language,
                                 timezone_name='America/New_York')
            rows = self.rows(result)
            self.assertIn(heading, result['text'])
            self.assertEqual(day, rows['fees.days.0.day']['value'])
            self.assertEqual(number, rows['totals.total_cost']['value'])
            self.assertIn('20:00:00 -0400 [America/New_York]', rows['period.start']['value'])
            utc = self.rows(self.render(language=language, date_language=date_language,
                                       timezone_name='UTC'))
            self.assertEqual(rows['fees.days.0.day']['value'], utc['fees.days.0.day']['value'])
        for invalid in ('de', '', False, 12):
            with self.assertRaisesRegex(ValueError, 'report_date_language_required'):
                REPORT.validate_report_preferences('en', 'UTC', '24', date_language=invalid)
            with self.assertRaises(ValueError):
                self.render(date_language=invalid)

    def test_only_actual_comparison_and_scope_are_presented(self):
        pair = self.snapshot['comparisons'].pop('previous')
        pair['comparison_scope'] = 'common_measurements'
        pair['status'] = 'partial'
        self.snapshot['comparisons']['same_weekday'] = pair
        result = self.render()
        for kind in ('text', 'html'):
            self.assertIn('Same weekday', result[kind])
            self.assertIn('Common measurements only', result[kind])
            self.assertIn('Incomplete data', result[kind])
            self.assertNotIn('Previous period', result[kind])
            self.assertNotIn('7-day mean', result[kind])
        self.snapshot['comparisons']['mean7'] = copy.deepcopy(pair)
        result = self.render()
        self.assertIn('7-day mean', result['text'])
        self.assertIn('Same weekday', result['text'])

    def test_zone_proof_has_labels_without_guessing_cheap_or_summing_cents(self):
        self.snapshot['chart']['bins'][0]['zone_breakdown'] = {
            'status': 'ready', 'currency': 'PLN', 'price_basis': 'gross',
            'zones': [{'zone_id': 'night', 'label': '<Night>', 'kwh': '3.1400',
                       'recorded_kwh': '3.1400', 'usage_cost': '9.9900', 'complete': True}],
            'unattributed': {'recorded_kwh': None, 'reasons': ['missing_measurement']}}
        result = self.render()
        self.assertIn('&lt;Night&gt;', result['html'])
        self.assertIn('<Night>', result['text'])
        self.assertIn('9.9900', Markup(result['html']).cells)
        self.assertEqual('PLN', self.rows(result)['chart.bins.0.zone_breakdown.zones.0.usage_cost']['unit'])
        self.assertIn('No published period share', result['text'])
        self.assertNotIn('class="bar-fill cheap"', result['html'])
        self.assertIn('class="bar-fill zone-unknown"', result['html'])

    def test_unknown_price_basis_does_not_give_comparison_delta_a_currency(self):
        costs = self.snapshot['comparisons']['previous']['costs']
        costs['current']['price_basis'] = None
        costs['baseline']['price_basis'] = None
        rows = self.rows(self.render())
        self.assertEqual('Unknown', rows['comparisons.previous.costs.delta_cost']['unit'])
        self.assertEqual('EUR', rows['comparisons.previous.costs.current.usage_cost']['unit'])

    def test_future_fields_stay_lossless_in_csv_without_becoming_user_prices(self):
        self.snapshot['future_projection'] = {'unclassified_amount': '9.8765432100123456789'}
        result = self.render()
        self.assertEqual('9.8765432100123456789', self.rows(result)[
            'future_projection.unclassified_amount']['value'])
        self.assertNotIn('unclassified amount', result['html'])


    def test_compact_kpi_group_contains_three_cards_without_metadata_rows(self):
        report = self.render()
        dom = ReportDOM(report['html']).root
        group, = dom.find(data_section='kpis')
        row, = [node for node in group.children if isinstance(node, ReportNode) and node.tag == 'tr']
        cards = [node for node in row.children if isinstance(node, ReportNode) and node.tag == 'td']
        self.assertEqual(3, len(cards))
        self.assertEqual(['Measured energy', 'Period cost', 'Cheap-zone share'],
            [card.find(data_role='kpi-label')[0].text() for card in cards])
        self.assertEqual(['240.0', '241.00', 'Unknown'],
            [card.find(data_role='kpi-number')[0].text() for card in cards])
        for card in cards:
            for metadata in ('Complete measurements', 'Complete pricing', 'Gross', 'Price basis'):
                self.assertNotIn(metadata, card.text())
        metadata, = dom.find(data_section='report-status')
        self.assertIn('Complete measurements', metadata.text())
        self.assertLess(report['html'].index('data-section="timeline"'),
                        report['html'].index('data-section="report-status"'))

    def test_timeline_uses_one_row_of_bin_columns_and_separate_full_number_table(self):
        report = self.render()
        dom = ReportDOM(report['html']).root
        timeline, = dom.find(data_section='timeline')
        plot, = timeline.find(data_role='plot')
        columns = [node for node in plot.children if isinstance(node, ReportNode) and node.tag == 'td']
        self.assertEqual(len(self.snapshot['chart']['bins']), len(columns))
        for index, column in enumerate(columns):
            self.assertEqual(str(index), column.attrs['data-bin'])
            self.assertEqual(1, len(column.find(data_role='bar')))
        values, = dom.find(data_section='measurement-values')
        self.assertEqual(len(columns), len(values.find(data_role='measurement')))
        self.assertIn('10.0', values.text())
        self.assertIn('10.00', values.text())
        self.assertLess(report['html'].index('data-section="timeline"'),
                        report['html'].index('data-section="device-ranking"'))
        self.assertLess(report['html'].index('data-section="device-ranking"'),
                        report['html'].index('data-section="measurement-values"'))

    def test_main_period_is_short_but_source_instants_preserve_offset_and_zone(self):
        report = self.render(date_language='pl')
        dom = ReportDOM(report['html']).root
        period, = dom.find(data_section='period')
        self.assertIn('10 paź 2026', period.text())
        self.assertNotIn('Europe/Warsaw', period.text())
        self.assertNotIn('+0200', period.text())
        self.assertNotIn(':00:00', period.text())
        source, = dom.find(data_section='source-times')
        self.assertIn('+0200 [Europe/Warsaw]', source.text())
        self.assertIn('02:00:00', source.text())
        self.assertIn('Oct 10, 2026', self.rows(self.render())['period.start']['value'])

    def test_intentional_details_do_not_duplicate_baseline_hours_or_internal_ids(self):
        report = self.render()
        dom = ReportDOM(report['html']).root
        self.assertEqual(1, len(dom.find(data_section='timeline')))
        self.assertEqual(1, len(dom.find(data_section='device-ranking')))
        self.assertEqual(1, len(dom.find(data_section='measurement-values')))
        self.assertEqual(1, len(dom.find(data_section='comparison-details')))
        for kind in ('html', 'text'):
            self.assertNotIn('sensor.device', report[kind])
            self.assertNotIn('mask source seconds', report[kind])
            self.assertNotIn('priced source seconds', report[kind])
        self.assertIn('sensor.device', report['csv'])
        self.assertIn('comparisons.previous.baseline_analysis.chart.bins.0', report['csv'])
        details, = dom.find(data_section='comparison-groups')
        for value in ('46', '48', 'Unknown'):
            self.assertIn(value, details.text())

    def test_negative_timeline_measurement_stays_textual_without_a_positive_bar(self):
        self.snapshot['chart']['bins'][0]['kwh'] = '-0.0000123400'
        report = self.render()
        dom = ReportDOM(report['html']).root
        timeline, = dom.find(data_section='timeline')
        negative, = timeline.find(data_bin='0')
        bar, = negative.find(data_role='bar')
        self.assertEqual('0%', bar.attrs['height'])
        self.assertIn('-0.0000123400', report['text'])
        values, = dom.find(data_section='measurement-values')
        self.assertIn('-0.0000123400', values.text())

    def test_partial_zone_known_costs_and_unattributed_are_readable_without_invented_full_cost(self):
        self.snapshot['chart']['bins'][0]['zone_breakdown'] = {
            'status': 'partial', 'reason': 'multiple_zones_in_measurement',
            'currency': 'PLN', 'price_basis': 'gross',
            'zones': [{'version_id': 'test-v', 'zone_id': 'test-z', 'label': 'Measured zone',
                       'kwh': None, 'recorded_kwh': '3.1400', 'usage_cost': None,
                       'complete': False, 'known_costs': {'energy_cost': '13.37', 'variable_cost': None,
                                                       'legacy_combined_cost': None}}],
            'unattributed': {'recorded_kwh': '1.23', 'known_costs': {'energy_cost': '2.46'}}}
        result = self.render()
        self.assertIn('13.37', Markup(result['html']).cells)
        self.assertIn('2.46', Markup(result['html']).cells)
        self.assertIn('13.37', result['text'])
        self.assertIn('2.46', result['text'])
        rows = self.rows(result)
        self.assertEqual('Unknown', rows['chart.bins.0.zone_breakdown.zones.0.usage_cost']['value'])
        self.assertEqual('PLN', rows['chart.bins.0.zone_breakdown.zones.0.known_costs.energy_cost']['unit'])

    def test_comparison_changes_show_signs_without_changing_exported_producer_numbers(self):
        compared = self.snapshot['comparisons']['previous']
        compared['delta_kwh'] = '17.2500'
        compared['percent'] = 7.125
        compared['costs']['delta_cost'] = '13.37'
        compared['costs']['delta_total_cost'] = '-2.46'
        result = self.render()
        self.assertIn('+17.2500 kWh', Markup(result['html']).cells)
        self.assertIn('+13.37', Markup(result['html']).cells)
        self.assertIn('-2.46', Markup(result['html']).cells)
        self.assertIn('+7.125', Markup(result['html']).cells)
        self.assertIn('+17.2500 kWh', result['text'])
        rows = self.rows(result)
        self.assertEqual('17.2500', rows['comparisons.previous.delta_kwh']['value'])
        self.assertEqual('13.37', rows['comparisons.previous.costs.delta_cost']['value'])
