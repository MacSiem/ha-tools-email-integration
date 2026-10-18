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
        self.assertIn('09.10.2026 05:00:00 PM -0700', rows['period.start']['value'])
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
        self.assertFalse(set(markup.tags) - {'html', 'head', 'meta', 'style', 'body',
            'table', 'caption', 'tr', 'th', 'td'})
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
