"""Fixture-only specification for static mail charts; owner runs these tests."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'custom_components/ha_tools_email' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

m = load('mail_charts_fixture', 'mail_charts.py')
c = load('mail_composer_fixture', 'composer.py')
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

def series(start, hours):
    return [{'from': (start + timedelta(hours=i)).timestamp() * 1000,
             'to': (start + timedelta(hours=i+1)).timestamp() * 1000,
             'kwh': i + 1} for i in range(hours)]

def summary(start, end):
    return {'devices': [], 'period': {'start': start.isoformat(), 'end': end.isoformat()},
            'currency': 'PLN', 'priceBasis': 'gross', 'status': 'ready'}

def chart(model, key):
    return next(item for item in model['charts'] if item['id'] == key)

class MailChartSpecification(unittest.TestCase):
    def test_signed_zero_unknown_tiny_and_exact_numeric_labels(self):
        model = m.build_log_chart_model({'level_counts': {'A': 0, 'B': None, 'C': -2, 'D': 1e-12},
            'logger_counts': {}, 'unique_problems': 4})
        data = chart(model, 'log-levels')
        self.assertEqual([0, None, -2, 1e-12], [r['value'] for r in data['rows']])
        self.assertEqual(['0', 'Unknown', '-2', '1e-12'], [r['display'] for r in data['rows']])
        html = m.render_charts_html(model)
        self.assertIn('Zero axis: 0', html)
        self.assertIn('-2 occurrences', html)
        self.assertIn('1e-12 occurrences', html)
        # No minimum-pixel bar that invents positive usage for zero or missing.
        zero_model = m.build_log_chart_model({'level_counts': {'A': 0, 'B': None}, 'logger_counts': {}})
        self.assertNotIn('background:#2563eb', m.render_charts_html(zero_model))
        self.assertNotIn('min-width:', html)

    def test_all_unknown_has_unavailable_scale_and_no_measured_zero(self):
        model = m.build_log_chart_model({'level_counts': {'A': None}, 'logger_counts': {}})
        data = chart(model, 'log-levels')
        self.assertIsNone(data['scale_min'])
        self.assertIsNone(data['scale_max'])
        self.assertIn('Scale unavailable', m.render_charts_html(model))
        self.assertNotIn('Scale: 0', m.render_charts_plain(model))
        self.assertNotIn('measured zero</span>', m.render_charts_html(model))

    def test_hourly_data_and_tariff_costs_use_same_intervals_and_values(self):
        start = NOW - timedelta(hours=3)
        buckets = series(start, 3)
        costs = [{**r, 'energyCost': r['kwh'] * .5, 'variableCost': r['kwh'] * .2} for r in buckets]
        s = summary(start, NOW)
        s.update(energyCost=3, variableCost=1.2, usageCost=4.2, fixedMonthly=99)
        before = copy.deepcopy((s, buckets, costs))
        model = m.build_energy_chart_model(s, buckets, costs, timezone_name='Europe/Warsaw')
        for key, values in [('energy-usage', [1, 2, 3]), ('energy-cost', [.5, 1, 1.5]),
                            ('variable-fees', [.2, .4, .6000000000000001])]:
            data = chart(model, key)
            self.assertEqual(values, [r['value'] for r in data['rows']])
            for row in data['rows']:
                self.assertIn(row['display'] + ' ' + data['unit'], m.render_charts_plain(model))
                self.assertIn(row['display'] + ' ' + data['unit'], m.render_charts_html(model))
        self.assertIn('+02:00', chart(model, 'energy-usage')['rows'][0]['label'])
        self.assertEqual(before, (s, buckets, costs))
        self.assertNotIn('99 PLN', m.render_charts_plain(model))

    def test_hour_gap_duplicate_and_unknown_do_not_become_zero(self):
        start = NOW - timedelta(hours=3)
        buckets = series(start, 3)
        for broken in [buckets[:1] + buckets[2:], buckets + [buckets[1]],
                       [buckets[0], {**buckets[1], 'kwh': None}, buckets[2]]]:
            data = chart(m.build_energy_chart_model(summary(start, NOW), broken), 'energy-usage')
            self.assertEqual([1, None, 3], [r['value'] for r in data['rows']])

    def test_long_period_uses_completed_calendar_days_with_dst_23_and_25_hours(self):
        tz = ZoneInfo('Europe/Warsaw')
        for day, hours in [(datetime(2026, 3, 29, tzinfo=tz), 23),
                           (datetime(2026, 10, 25, tzinfo=tz), 25)]:
            start = (day - timedelta(days=1)).astimezone(timezone.utc)
            end = (day + timedelta(days=1)).astimezone(timezone.utc)
            buckets = series(start, int((end-start).total_seconds()/3600))
            data = chart(m.build_energy_chart_model(summary(start, end), buckets,
                         timezone_name='Europe/Warsaw'), 'energy-usage')
            self.assertEqual('calendar_day', data['granularity'])
            self.assertEqual([24, hours], [r['hours'] for r in data['rows']])
            self.assertEqual(sum(r['kwh'] for r in buckets), sum(r['value'] for r in data['rows']))
            self.assertIn(f'{hours}h', data['rows'][1]['label'])
            gap = buckets[:25] + buckets[26:]
            missing = chart(m.build_energy_chart_model(summary(start, end), gap,
                            timezone_name='Europe/Warsaw'), 'energy-usage')
            self.assertIsNone(missing['rows'][1]['value'])

    def test_partial_calendar_days_unknown_and_display_bounded_without_fake_other(self):
        start = NOW.replace(hour=1)
        end = start + timedelta(days=40)
        model = m.build_energy_chart_model(summary(start, end), series(start, 960))
        data = chart(model, 'energy-usage')
        self.assertEqual(len(data['rows']), 31)
        self.assertGreater(data['omitted_rows'], 0)
        self.assertIsNone(data['rows'][0]['value'])
        self.assertIn('displayed', data['note'])
        self.assertNotIn('Other', m.render_charts_plain(model))

    def test_whole_calendar_month_is_visible_and_binary_float_tail_is_readable(self):
        start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        end = datetime(2026, 11, 1, tzinfo=timezone.utc)
        data = chart(m.build_energy_chart_model(summary(start, end), series(start, 744)), 'energy-usage')
        self.assertEqual(31, len(data['rows']))
        self.assertEqual(0, data['omitted_rows'])
        model = m.build_energy_chart_model({'devices': [{'name': 'Meter', 'kwh': .1 + .2}]})
        self.assertEqual('0.3', model['charts'][0]['rows'][0]['display'])
        self.assertEqual(.1 + .2, model['charts'][0]['rows'][0]['value'])

    def test_distributions_are_bounded_and_grid_sources_are_not_appliances(self):
        devices = [{'name': f'Meter {i}', 'kwh': i, 'share': 0, 'cost': None} for i in range(20)]
        model = m.build_energy_chart_model({'devices': devices, 'source_ids': ['sensor.grid'],
                                           'status': 'partial'}, source_kind='grid')
        data = chart(model, 'energy-sources')
        self.assertEqual(10, len(data['rows']))
        self.assertEqual(10, data['omitted_rows'])
        self.assertIn('Grid import sources', data['title'])
        self.assertIn('period total withheld', data['note'])
        self.assertEqual([r['kwh'] for r in devices[:10]], [r['value'] for r in data['rows']])

    def test_logs_occurrences_unknown_and_unique_metric_remain_separate(self):
        payload = c.build_log_digest_payload([
            {'id':'a','name':'fixture','level':'ERROR','count':0,'timestamp':NOW.timestamp()-1},
            {'id':'b','name':'other','level':'WARNING','count':None,'timestamp':NOW.timestamp()-2},
        ], cadence='daily', now=NOW)
        s = payload['summary']
        self.assertEqual(2, s['unique_problems'])
        self.assertIsNone(s['total'])
        data = chart(s['visualization'], 'log-levels')
        self.assertEqual([0, None], [r['value'] for r in data['rows']])
        self.assertIn('retained aggregates', payload['html'])
        self.assertIn('Unique problems: 2', payload['body'])
        self.assertIn('Unique problems: 2', payload['html'])

    def test_escape_long_labels_inline_table_mobile_and_no_active_content(self):
        label = '<b onclick="x">' + 'x'*400 + '</b>'
        model = m.build_energy_chart_model({'devices':[{'name':label,'kwh':1}]})
        html = m.render_charts_html(model)
        self.assertIn('&lt;b onclick=&quot;x&quot;&gt;', html)
        self.assertIn('table-layout:fixed', html)
        self.assertIn('word-break:break-word', html)
        self.assertIn('font-size:14px', html)
        self.assertIn('<caption', html)
        for forbidden in ['<script', '<canvas', '<svg', '<img', '<link', '<iframe', 'src=', '@import', 'url(']:
            self.assertNotIn(forbidden, html.lower())
        self.assertIn(label, m.render_charts_plain(model))

    def test_optional_link_only_known_safe_config_url_and_same_plain_link(self):
        for unsafe in ['javascript:alert(1)', '//example.test', 'https://u:p@example.test',
                       'https://example.test/?token=secret', 'https://example.test/#secret',
                       'https://example.test/\nenergy', 'https://example.test:bad',
                       'https://exa\\mple.test', 'https://example.test/%0atoken']:
            self.assertIsNone(m.safe_energy_url(unsafe), unsafe)
        payload = c.build_energy_report_payload([{'name':'Meter','kwh':0}],cadence='daily',
                        interactive_url='https://ha.example.test/base')
        url = 'https://ha.example.test/energy'
        self.assertEqual(url, payload['summary']['visualization']['interactive_url'])
        self.assertIn(f'href="{url}"', payload['html'])
        self.assertIn(url, payload['body'])
        self.assertNotIn('href=', c.build_energy_report_payload([],cadence='daily')['html'])

    def test_unknown_variable_fees_and_signed_cost_are_separate_from_kwh(self):
        start = NOW - timedelta(hours=1)
        buckets = series(start, 1)
        costs = [{**buckets[0], 'energyCost':-.15, 'variableCost':None}]
        model = m.build_energy_chart_model(summary(start, NOW), buckets, costs)
        self.assertEqual(-.15, chart(model, 'energy-cost')['rows'][0]['value'])
        self.assertIsNone(chart(model, 'variable-fees')['rows'][0]['value'])
        self.assertEqual('kWh', chart(model, 'energy-usage')['unit'])
        self.assertEqual('PLN', chart(model, 'energy-cost')['unit'])
        self.assertIn('-0.15 PLN', m.render_charts_html(model))
        self.assertIn('Unknown PLN', m.render_charts_plain(model))

    def test_pure_known_costs_share_values_without_changing_existing_summary(self):
        payload = c.build_energy_report_payload([{'name':'Meter','kwh':3}],cadence='daily',
                                               currency='PLN',price_per_kwh=.8)
        data = chart(payload['summary']['visualization'], 'energy-source-cost')
        self.assertEqual(payload['summary']['devices'][0]['cost'], data['rows'][0]['value'])
        self.assertNotIn('currency', payload['summary'])
        self.assertIn('2.4 PLN', payload['body'])

    def test_dst_hour_offsets_are_not_collapsed_or_renamed(self):
        start = datetime(2026,10,25,0,tzinfo=timezone.utc)
        end = start + timedelta(hours=3)
        data = chart(m.build_energy_chart_model(summary(start,end),series(start,3),
                         timezone_name='Europe/Warsaw'), 'energy-usage')
        self.assertIn('02:00 → 02:00 (+02:00 → +01:00)', data['rows'][0]['label'])
        self.assertIn('02:00 → 03:00 (+01:00)', data['rows'][1]['label'])
        self.assertNotEqual(data['rows'][0]['from'], data['rows'][1]['from'])
        self.assertEqual([1,2,3], [r['value'] for r in data['rows']])

    def test_pure_financial_fields_are_exactly_unchanged(self):
        payload = c.build_energy_report_payload([{'name':'Meter','entity_id':'sensor.grid','kwh':3},
                     {'name':'Zero','entity_id':'sensor.zero','kwh':0}],cadence='daily',price_per_kwh=.8)
        self.assertEqual({'total_kwh':3.0,'total_cost':2.4000000000000004,'devices':[
          {'name':'Meter','entity_id':'sensor.grid','kwh':3.0,'cost':2.4000000000000004,'share':100.0},
          {'name':'Zero','entity_id':'sensor.zero','kwh':0.0,'cost':0.0,'share':0.0}]},
          {k:v for k,v in payload['summary'].items() if k != 'visualization'})

    def test_async_tariff_fields_and_zone_breakdown_unchanged_fixture(self):
        start = NOW - timedelta(hours=2)
        buckets = series(start, 2)
        cost_rows = [{**r,'energyCost':r['kwh']*.5,'variableCost':r['kwh']*.2,
                      'energyRate':.5,'variableRate':.2,'zone':'flat','zones':['flat'],
                      'zoneAttribution':'single'} for r in buckets]
        costs = {'complete':True,'issues':[],'rows':cost_rows,'currency':'PLN','priceBasis':'gross',
                 'energyCost':1.5,'variableCost':.6000000000000001,'usageCost':2.1,'fixedMonthly':99}
        class Storage:
            async def async_get_tariff(self): return {'profile':{'schema':1}}
        async def snapshot(*args):
            return {'status':'ready','source_ids':['sensor.grid'], 'hourly_buckets':buckets,
                    'devices':[{'name':'Meter','entity_id':'sensor.grid','kwh':3}]}
        package_module = ModuleType('chart_fixture_pkg')
        package_module.__path__ = []
        tariff_module = ModuleType('chart_fixture_pkg.tariff')
        tariff_module.price_measurements = lambda *args, **kwargs: copy.deepcopy(costs)
        hass = SimpleNamespace(data={'ha_tools_email':{'storage':Storage()}},
                               config=SimpleNamespace(currency='PLN',time_zone='Europe/Warsaw',
                                                      external_url='https://ha.example.test'))
        with patch.object(c, '__package__', 'chart_fixture_pkg'), patch.object(c, 'async_get_energy_usage', snapshot), \
             patch.dict(sys.modules, {'chart_fixture_pkg':package_module, 'chart_fixture_pkg.tariff':tariff_module}):
            payload = asyncio.run(c.async_build_energy_report_payload(hass,cadence='daily',
                                     window_start=start,window_end=NOW))
        s = payload['summary']
        for key in ['energyCost','variableCost','usageCost','fixedMonthly','priceBasis']:
            self.assertEqual(costs[key], s[key])
        self.assertEqual(2.1, s['total_cost'])
        self.assertEqual([{'zone':'flat','zones':['flat'],'zoneAttribution':'single','kwh':3,
                          'energyRate':.5,'variableRate':.2,'energyCost':1.5,
                          'variableCost':.6000000000000001,'usageCost':2.1}], s['zoneBreakdown'])
        self.assertEqual([1,2],[r['value'] for r in chart(s['visualization'],'energy-usage')['rows']])
        self.assertIn('Fixed monthly fee: 99.00 PLN', payload['body'])
        self.assertIn('https://ha.example.test/energy',payload['body'])
        self.assertEqual(1,payload['body'].count('Static chart data'))

    def test_calendar_chart_uses_report_timezone_without_changing_measured_totals(self):
        start = datetime(2026, 10, 5, 4, tzinfo=timezone.utc)
        end = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
        buckets = [{**r, 'kwh': 1} for r in series(start, 168)]
        async def snapshot(*args):
            return {'status': 'ready', 'source_ids': ['sensor.grid'],
                    'hourly_buckets': buckets, 'devices': [{'name': 'Grid', 'kwh': 168}]}
        hass = SimpleNamespace(data={}, config=SimpleNamespace(currency='PLN', time_zone='Asia/Tokyo'))
        with patch.object(c, 'async_get_energy_usage', snapshot):
            payload = asyncio.run(c.async_build_energy_report_payload(hass, cadence='weekly',
                window_start=start, window_end=end, report_timezone='America/New_York'))
            old = asyncio.run(c.async_build_energy_report_payload(hass, cadence='weekly',
                window_start=start, window_end=end))
        model = payload['summary']['visualization']
        self.assertEqual('America/New_York', model['timezone'])
        rows = chart(model, 'energy-usage')['rows']
        self.assertEqual(7, len(rows))
        self.assertEqual([24] * 7, [r['hours'] for r in rows])
        self.assertEqual([24] * 7, [r['value'] for r in rows])
        self.assertTrue(all(r['complete'] for r in rows))
        self.assertIn('2026-10-05', rows[0]['label'])
        self.assertIn('America/New_York', payload['body'])
        self.assertEqual('Asia/Tokyo', hass.config.time_zone)
        self.assertEqual({k:v for k,v in old['summary'].items() if k not in {'visualization', 'comparison'}},
                         {k:v for k,v in payload['summary'].items() if k not in {'visualization', 'comparison'}})
        self.assertEqual('rolling_or_custom_window', old['summary']['comparison']['reason'])
        self.assertEqual('incomplete_statistics', payload['summary']['comparison']['reason'])

if __name__ == '__main__':
    unittest.main()
