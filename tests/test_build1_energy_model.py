"""Contract invariants from verified producer facts; no synthetic allocation."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('build1_model', ROOT / 'custom_components/ha_tools_email/energy_model.py')
model = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(model)


def inputs_for(start='2026-10-17T00:00:00+02:00', end='2026-10-18T00:00:00+02:00', zone='Europe/Warsaw'):
    first, last = model._instant(start), model._instant(end)
    rows = []
    while first < last:
        rows.append({'from': first.isoformat(), 'to': (first + timedelta(hours=1)).isoformat(),
            'kwh': '1', 'currency': 'PLN', 'price_basis': 'gross', 'zone': 'custom',
            'costs': {'energy_cost': '.2', 'variable_cost': '.3', 'usage_cost': '.5'},
            'devices': {'parent': {'kwh': '.6', 'costs': {'usage_cost': '.3'}},
                        'child': {'kwh': '.2', 'costs': {'usage_cost': '.1'}}}})
        first += timedelta(hours=1)
    return {'period': {'kind': 'day', 'start': start, 'end': end, 'timezone': zone},
        'projection': {'hours': rows, 'currency': 'PLN', 'price_basis': 'gross', 'fixed_share': None},
        'consumers': [{'stat_consumption': 'parent'}, {'stat_consumption': 'child', 'included_in_stat': 'parent'}],
        'membership': {'sources': {'parent': {'name': 'Parent', 'area_id': 'kitchen', 'area_name': 'Kitchen',
            'floor_id': 'ground', 'floor_name': 'Ground'},
            'child': {'name': 'Child', 'area_id': 'office', 'area_name': 'Office', 'floor_id': 'upper'}}},
        'colors': {'as_of': start[:10], 'window_days': 30, 'devices': {'parent': 'd4'}}}


class Build1ModelTests(unittest.TestCase):
    def test_daily_chart_aggregates_real_dst_hours_and_keeps_missing_day_unknown(self):
        inputs = inputs_for('2026-10-25T00:00:00+02:00','2026-10-27T00:00:00+01:00')
        inputs['period']['kind'] = 'week'
        inputs['projection']['hours'] = inputs['projection']['hours'][:25]
        result = model.build_report(inputs)
        self.assertEqual(2,len(result['daily']))
        self.assertEqual(25,result['daily'][0]['kwh'])
        self.assertEqual(12.5,result['daily'][0]['cost'])
        self.assertTrue(result['daily'][0]['complete'])
        self.assertIsNone(result['daily'][1]['kwh'])
        self.assertFalse(result['daily'][1]['complete'])
        self.assertEqual(25,result['daily'][0]['zones'][0]['kwh'])

    def test_previous_hour_ticks_use_real_civil_matches_without_dst_invention(self):
        inputs = inputs_for('2026-10-25T00:00:00+02:00','2026-10-26T00:00:00+01:00')
        inputs['previous'] = {'start':'2026-10-24T00:00:00+02:00',
            'end':'2026-10-25T00:00:00+02:00','hours':[
                {'from':'2026-10-24T02:00:00+02:00','to':'2026-10-24T03:00:00+02:00','kwh':'1.23456789'}]}
        result = model.build_report(inputs)
        repeated = [r for r in result['hourly'] if r['hour']==2]
        self.assertEqual([1.23456789,None],[r['prev_kwh'] for r in repeated])
        self.assertEqual(25,result['totals']['import_kwh'])

    def test_inclusive_parent_and_root_rooms_do_not_double_count(self):
        inputs = inputs_for()
        original = copy.deepcopy(inputs)
        result = model.build_report(inputs)
        self.assertEqual(original, inputs)
        self.assertAlmostEqual(14.4, result['totals']['measured_kwh'])
        self.assertAlmostEqual(9.6, result['unmeasured']['kwh'])
        self.assertEqual(1, len(result['devices']))
        self.assertAlmostEqual(4.8, result['devices'][0]['children'][0]['kwh'])
        self.assertAlmostEqual(7.2, result['devices'][0]['cost'])
        self.assertEqual(['kitchen', '_unmeasured'], [room['area_id'] for room in result['rooms']])
        self.assertAlmostEqual(14.4, result['rooms'][0]['kwh'])
        self.assertEqual('d4', result['ranking'][0]['color'])
        self.assertIsNone(result['totals']['fixed_share'])
        self.assertIsNone(result['totals']['total_with_fixed'])

    def test_hourly_double_counting_cannot_cancel_in_period_balance(self):
        inputs = inputs_for()
        inputs['projection']['hours'][0]['devices']['parent']['kwh'] = '1.050001'
        result = model.build_report(inputs)
        self.assertEqual('double_counting', result['unmeasured']['status'])
        self.assertGreater(result['unmeasured']['raw_balance_kwh'], 0)
        self.assertIsNone(result['unmeasured']['kwh'])
        self.assertIsNone(result['unmeasured']['cost'])
        self.assertIn('double_counting', [row['code'] for row in result['issues']])

    def test_boundary_minus_005_is_clamped_with_explicit_adjustment(self):
        inputs = inputs_for()
        inputs['projection']['hours'][0]['devices']['parent']['kwh'] = '1.05'
        result = model.build_report(inputs)
        self.assertEqual('ok', result['unmeasured']['status'])
        self.assertAlmostEqual(.05, result['unmeasured']['tolerance_adjustment_kwh'])
        self.assertAlmostEqual(9.2, result['unmeasured']['kwh'])

    def test_missing_import_is_null_and_coverage_is_not_declared(self):
        inputs = inputs_for()
        del inputs['projection']['hours'][2:5]
        inputs['projection']['coverage'] = {'ratio': 1, 'complete': True}
        result = model.build_report(inputs)
        self.assertEqual(.875, result['coverage']['ratio'])
        self.assertIsNone(result['hourly'][2]['kwh'])
        self.assertIsNone(result['devices'][0]['share'])
        self.assertEqual(21, result['totals']['import_kwh'])

    def test_sofar_never_fabricates_five_minute_statistics(self):
        inputs = inputs_for()
        inputs['measured_until'] = '2026-10-17T19:05:00+02:00'
        inputs['projection']['hours'] = inputs['projection']['hours'][:19]
        result = model.build_report(inputs)
        self.assertFalse(result['period']['complete'])
        self.assertEqual(19, result['coverage']['measured_hours'])
        self.assertEqual(20, result['coverage']['expected_hours'])
        self.assertIsNone(result['hourly'][19]['kwh'])
        inputs['projection']['hours'].append({'from': '2026-10-17T19:00:00+02:00',
            'to': inputs['measured_until'], 'kwh': '.1', 'devices': {}})
        result = model.build_report(inputs)
        self.assertTrue(result['hourly'][19]['partial'])
        self.assertAlmostEqual(19.1, result['totals']['import_kwh'])
        self.assertTrue(result['coverage']['complete'])

    def test_dst_grid_uses_elapsed_instants_preserving_both_02_hours(self):
        for start, end, expected in [('2026-03-29T00:00:00+01:00', '2026-03-30T00:00:00+02:00', 23),
                                     ('2026-10-25T00:00:00+02:00', '2026-10-26T00:00:00+01:00', 25)]:
            result = model.build_report(inputs_for(start, end))
            self.assertEqual(expected, result['period']['hours'])
            self.assertEqual(expected, len(result['hourly']))
            self.assertEqual(expected, result['totals']['import_kwh'])
            if expected == 25:
                repeated = [row for row in result['hourly'] if row['hour'] == 2]
                self.assertEqual([0, 1], [row['fold'] for row in repeated])
                self.assertNotEqual(repeated[0]['start'], repeated[1]['start'])

    def test_mean_uses_qualified_N_not_divide_by_seven_and_requires_evidence(self):
        inputs = inputs_for()
        baselines = []
        start = model._instant(inputs['period']['start'])
        for day in range(1, 8):
            first = start - timedelta(days=day)
            hours = [{'from': (first + timedelta(hours=hour)).isoformat(),
                      'to': (first + timedelta(hours=hour + 1)).isoformat(),
                      'kwh': '2' if day < 7 or hour < 20 else None} for hour in range(24)]
            baselines.append({'period': {'start': first.isoformat(), 'end': (first + timedelta(days=1)).isoformat()},
                'hours': hours, 'scope': 'full_period'})
        inputs['comparisons'] = [{'key': 'avg_7d', 'baselines': baselines},
            {'key': 'same_weekday_last_week', 'kwh': 19.4, 'coverage': 1}]
        result = model.build_report(inputs)
        mean, unsupported = result['comparisons']
        self.assertEqual(48, mean['kwh'])
        self.assertEqual(6, mean['qualified_days'])
        self.assertEqual(7, mean['requested_days'])
        self.assertEqual(-50, mean['delta_pct'])
        self.assertIsNone(unsupported['kwh'])
        self.assertIsNone(unsupported['delta_pct'])
        inputs['scope'] = 'elapsed_19h05'
        self.assertIsNone(model.build_report(inputs)['comparisons'][0]['delta_pct'])

    def test_colors_must_be_daily_supplied_identity_and_not_current_rank(self):
        inputs = inputs_for()
        del inputs['colors']
        result = model.build_report(inputs)
        self.assertEqual('other', result['ranking'][0]['color'])
        self.assertIn('device_colors_unavailable', [row['code'] for row in result['issues']])

    def test_native_usage_cost_does_not_invent_named_components_or_fixed_fees(self):
        inputs = inputs_for()
        for row in inputs['projection']['hours']:
            row['costs'] = {'usage_cost': '.1234567'}
        result = model.build_report(inputs)
        self.assertAlmostEqual(2.9629608, result['totals']['usage_cost'])
        self.assertIsNone(result['totals']['energy_cost'])
        self.assertIsNone(result['totals']['variable_cost'])
        self.assertIsNone(result['totals']['fixed_share'])
        self.assertEqual('custom', result['zones'][0]['id'])

    def test_mixed_currencies_never_sum_money(self):
        inputs = inputs_for()
        inputs['projection']['hours'][0]['currency'] = 'EUR'
        result = model.build_report(inputs)
        self.assertIsNone(result['totals']['usage_cost'])
        self.assertIsNone(result['devices'][0]['cost'])
        self.assertIn('incomparable_currency_or_basis', [row['code'] for row in result['issues']])

    def test_duplicate_hours_and_cycles_rejected(self):
        inputs = inputs_for()
        inputs['projection']['hours'].append(copy.deepcopy(inputs['projection']['hours'][0]))
        with self.assertRaisesRegex(ValueError, 'invalid_report_hour'):
            model.build_report(inputs)
        inputs = inputs_for()
        inputs['consumers'][0]['included_in_stat'] = 'child'
        with self.assertRaisesRegex(ValueError, 'invalid_report_hierarchy'):
            model.build_report(inputs)


class ProducerProjectionTests(unittest.TestCase):
    def test_supplied_real_hourly_fixture_reuses_existing_engine_and_keeps_precision(self):
        from tests.test_energy_analysis import analysis
        from tests.test_energy_weighted_comparison import readings
        from tests.test_tariff_history import book, version
        fixture = json.loads((ROOT / 'tests/fixtures/build1/fixture-energy-inputs-2026-10-17.json').read_text())
        tariff = copy.deepcopy(fixture['tariff'])
        tariff.update(schema=1, clock={'mode': 'civil'}, fixedMonthly=34.1)
        for key in ('fixed', 'name'):
            tariff.pop(key, None)
        v = version(start='2026-01-01', profile=tariff,
            fixed_monthly={'subscription': .75, 'capacity': 11.62, 'distribution': 12.4, 'other': 9.33})
        start, end = '2026-10-17T00:00:00+02:00', '2026-10-18T00:00:00+02:00'
        consumers = fixture['energy_prefs']['device_consumption']
        ids = tuple(row['stat_consumption'] for row in consumers)
        sources = {}
        for sid in ids:
            entity = fixture['registries']['entities'][sid]
            area_id = entity.get('area_id')
            area = fixture['registries']['areas'].get(area_id, {})
            sources[sid] = {'name': entity['name'], 'area_id': area_id, 'area_name': area.get('name'),
                'floor_id': area.get('floor_id'), 'floor_name': area.get('floor_id')}
        membership = {'revision': 1, 'captured_at': end, 'sources': sources, 'floor_supported': True}
        grid = readings(start, end, sources=('sensor.licznik_import',))
        devices = readings(start, end, sources=ids)
        for snapshot in (grid, devices):
            for sid in snapshot['source_ids']:
                for row, quantity in zip(snapshot['source_buckets'][sid], fixture['statistics_hourly_change_kwh'][sid]['2026-10-17']):
                    row['kwh'] = quantity
        period = {'kind': 'day', 'start': start, 'end': end, 'timezone': fixture['timezone']}
        def build(enabled):
            builder = analysis.EnergyAnalysisAccumulator(book([v]), period, {},
                generated_at=datetime.fromisoformat(end), consumers=consumers, membership=membership,
                include_report_hours=enabled)
            builder.add_chunk(grid, start, end, consumer_snapshot=devices)
            return builder, builder.finish()
        plain, plain_result = build(False)
        enabled, enabled_result = build(True)
        # New projection must not change any old response byte or bill cent.
        self.assertEqual(plain_result, enabled_result)
        with self.assertRaises(ValueError):
            plain.report_projection()
        projection = enabled.report_projection()
        result = model.build_report({'period': period, 'projection': projection,
            'consumers': consumers, 'membership': membership, 'tariff': fixture['tariff']})
        self.assertAlmostEqual(18.77, result['totals']['import_kwh'], places=9)
        self.assertAlmostEqual(15.6566, result['totals']['usage_cost'], places=9)
        self.assertAlmostEqual(1.1, result['totals']['fixed_share'], places=9)
        by_id = {row['id']: row for row in result['devices']}
        self.assertAlmostEqual(.600004, by_id['sensor.lodowka_energia']['kwh'], places=9)
        self.assertAlmostEqual(2.399998, by_id['sensor.bojler_energia']['kwh'], places=9)
        self.assertEqual(7, len(result['devices']))
        projection['hours'][0]['costs']['usage_cost'] = '999'
        self.assertNotEqual('999', enabled.report_projection()['hours'][0]['costs']['usage_cost'])
