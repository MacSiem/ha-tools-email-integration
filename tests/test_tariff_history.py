"""Pure tariff-history tests; owner applies this patch before implementation."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('history_test_package')
package.__path__ = [str(ROOT)]
with patch.dict(sys.modules, {package.__name__: package}):
    spec = importlib.util.spec_from_file_location(package.__name__ + '.tariff_history', ROOT / 'tariff_history.py')
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)
    engine = sys.modules[package.__name__ + '.tariff']


def profile(rate=1, **changes):
    result = dict(schema=1, currency='EUR', timezone='UTC', clock={'mode': 'civil'},
                  priceBasis='gross', defaultZone='day', zones=[{'id': 'day', 'energyRate': rate, 'variableRate': 0}],
                  rules=[], fixedMonthly=7)
    result.update(changes)
    return result


def version(start='2026-01-01', rate=1, version_id='a', monthly=0, **changes):
    result = {'id': version_id, 'valid_from': start, 'profile': profile(rate),
              'rate_semantics': 'components',
              'fixed_monthly': dict(subscription=monthly, capacity=0, distribution=0, other=0),
              'legacy_combined_monthly': None}
    result.update(changes)
    return result


def tariff(versions=None, tariff_id='active'):
    return {'id': tariff_id, 'name': tariff_id, 'origin': {'kind': 'user', 'reference': None},
            'versions': versions if versions is not None else [version()]}


def book(versions=None):
    return {'schema': 2, 'active_tariff_id': 'active', 'tariffs': [tariff(versions)]}


def rows_between(start, end, kwh=1):
    a, b = (datetime.fromisoformat(value.replace('Z', '+00:00')) for value in (start, end))
    rows = []
    while a < b:
        stop = min(a + timedelta(hours=1), b)
        rows.append({'from': a.isoformat(), 'to': stop.isoformat(), 'kwh': kwh})
        a = stop
    return rows


def price(b=None, start='2026-01-01T00:00Z', end='2026-01-01T01:00Z', kwh=1, coverage=None):
    return history.price_book(b or book(), rows_between(start, end, kwh), coverage or {'from': start, 'to': end})['tariffs'][0]


class TariffHistoryTest(unittest.TestCase):
    def test_mixed_legacy_components_round_known_parts_once_then_sum(self):
        a = version(rate=.005, rate_semantics='legacy_combined')
        b = version('2026-01-02', .005, 'b')
        b['profile']['zones'][0]['variableRate'] = .005
        result = price(book([a, b]), start='2026-01-01T23:00Z', end='2026-01-02T01:00Z')
        self.assertEqual({'energy_cost': '0.01', 'variable_cost': '0.01',
                          'legacy_combined_cost': '0.01'}, result['known_costs'])
        self.assertEqual('0.03', result['usage_cost'])
        self.assertIsNone(result['energy_cost'])
        self.assertIsNone(result['total_cost'])

    def test_conflicting_embedded_validity_is_rejected_before_pricing(self):
        v = version()
        v['profile']['validTo'] = '2026-01-01'
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(book([v]))

    def test_migrated_fixed_clock_keeps_schema1_validity_instants(self):
        p = profile(timezone='Europe/Warsaw', clock={'mode': 'fixed', 'offsetMinutes': 0},
                    validFrom='2026-01-01', validTo='2026-01-01')
        migrated = history.migrate_tariff_v1(p, legacy_combined=False)
        for start, end, available in [('2025-12-31T23:00Z', '2026-01-01T00:00Z', False),
                                      ('2026-01-01T23:00Z', '2026-01-02T00:00Z', True)]:
            with self.subTest(start=start):
                result = price(migrated, start=start, end=end)
                self.assertEqual(available, result['rows'][0]['available'])
                original = engine.EnergyTariff(p).price_bucket({'from': start, 'to': end, 'kwh': 1})
                self.assertEqual(original['available'], result['rows'][0]['available'])
        self.assertEqual(p, migrated['source_schema1'])

    def test_rate_details_are_frozen_with_the_priced_result(self):
        b = book([version(monthly=31)])
        p = b['tariffs'][0]['versions'][0]['profile']
        p['zones'][0]['variableRate'] = .25
        p['rules'] = [{'zone': 'day', 'intervals': [{'from': '00:00', 'to': '24:00'}]}]
        result = price(b)
        self.assertEqual('active', result['tariff_name'])
        self.assertEqual({'kind': 'user', 'reference': None}, result['origin'])
        rates = result['rates'][0]
        self.assertEqual(p['zones'], rates['profile']['zones'])
        self.assertEqual(p['rules'], rates['profile']['rules'])
        self.assertEqual(31, rates['fixed_monthly']['subscription'])
        self.assertEqual('1.29', result['total_cost'])
        p['zones'][0]['energyRate'] = 999
        self.assertEqual(1, rates['profile']['zones'][0]['energyRate'])

    def test_outside_measurements_never_inflate_known_costs(self):
        coverage = {'from': '2026-01-01T01:00Z', 'to': '2026-01-01T02:00Z'}
        rows = rows_between('2026-01-01T00:00Z', '2026-01-01T03:00Z', 10)
        result = history.price_book(book(), rows, coverage)['tariffs'][0]
        self.assertFalse(result['measurement_complete'])
        self.assertIsNone(result['total_cost'])
        self.assertEqual('10.00', result['known_costs']['energy_cost'])
        self.assertEqual([False, True, False], [row['available'] for row in result['rows']])
        self.assertEqual('outside_coverage', result['rows'][0]['reason'])
        crossing = [{'from': '2026-01-01T00:30Z', 'to': '2026-01-01T01:30Z', 'kwh': 100}]
        r = history.price_book(book(), crossing, coverage)['tariffs'][0]
        self.assertEqual('0.00', r['known_costs']['energy_cost'])
        self.assertFalse(r['rows'][0]['available'])

    def test_midmonth_versions_actual_energy_and_exclusive_boundary(self):
        b = book([version(monthly=31), version('2026-01-16', 2, 'b', monthly=62)])
        result = price(b, end='2026-02-01T00:00Z')
        self.assertEqual('1128.00', result['energy_cost'])  # 15*24 + 16*24*2
        self.assertEqual('47.00', result['fixed_cost'])
        self.assertEqual('1175.00', result['total_cost'])
        self.assertTrue(result['measurement_complete'])
        self.assertTrue(result['pricing_complete'])
        self.assertEqual(['a', 'b'], [r['version_id'] for r in result['rates']])
        self.assertEqual('2026-01-16', result['rates'][0]['valid_to'])
        self.assertEqual('a', result['rows'][359]['version_id'])
        self.assertEqual('b', result['rows'][360]['version_id'])

    def test_bucket_crossing_version_cannot_be_apportioned(self):
        b = book([version(), version('2026-01-02', 2, 'b')])
        r = price(b, start='2026-01-01T23:30Z', end='2026-01-02T00:30Z')
        self.assertTrue(r['measurement_complete'])
        self.assertFalse(r['pricing_complete'])
        self.assertIsNone(r['total_cost'])
        self.assertEqual('mixed_version_bucket', r['rows'][0]['reason'])

    def test_duplicate_overlap_gap_and_missing_dates_rejected(self):
        invalid = [
            [version(), version('2026-01-02', version_id='a')],
            [version(), version(version_id='b')],
            [version(valid_to='2026-01-03'), version('2026-01-02', version_id='b')],
            [version(valid_to='2026-01-02'), version('2026-01-03', version_id='b')],
            [version(None)],
            [version('2026-02-30')],
        ]
        for versions in invalid:
            with self.subTest(versions=versions), self.assertRaises(history.TariffValidationError):
                history.validate_tariff_book(book(versions))
        b = book()
        b['tariffs'].append(copy.deepcopy(b['tariffs'][0]))
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(b)

    def test_schedule_gaps_ambiguity_overnight_and_priority(self):
        night = {'id': 'night', 'energyRate': .2, 'variableRate': 0}
        overnight = {'zone': 'night', 'intervals': [{'from': '22:00', 'to': '06:00'}]}
        p = profile(zones=[{'id': 'day', 'energyRate': 1, 'variableRate': 0}, night], rules=[overnight])
        v = version(profile=p)
        self.assertEqual('0.20', price(book([v]))['energy_cost'])
        p['defaultZone'] = None
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(book([v]))
        p['defaultZone'] = 'day'
        p['rules'].append({'zone': 'day', 'intervals': [{'from': '23:00', 'to': '24:00'}]})
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(book([v]))
        p['rules'][-1]['priority'] = 2
        history.validate_tariff_book(book([v]))

    def test_season_and_exception_coverage_validation(self):
        p = profile(defaultZone=None, rules=[{'zone': 'day', 'season': {'from': '01-01', 'to': '06-30'},
                                             'intervals': [{'from': '00:00', 'to': '24:00'}]}])
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(book([version(profile=p)]))
        p['defaultZone'] = 'day'
        p['exceptions'] = [{'zone': 'day', 'dates': ['2026-02-03'], 'intervals': [{'from': '00:00', 'to': '24:00'}]}]
        self.assertEqual('1.00', price(book([version(profile=p)]))['energy_cost'])

    def test_nullable_optional_fields_and_hostile_active_id(self):
        v = version(profile=profile(rules=None, exceptions=None))
        self.assertEqual('1.00', price(book([v]))['energy_cost'])
        bad = book()
        bad['active_tariff_id'] = {'invalid': True}
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(bad)
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(book([version('2026-01-01', profile=profile(schema=True))]))

    def test_dynamic_gaps_and_rate_boundary_refusal(self):
        first = {'from': '2026-01-01T00:00Z', 'to': '2026-01-01T00:30Z', 'energyRate': 1, 'variableRate': 0}
        second = dict(first, **{'from': '2026-01-01T00:30Z', 'to': '2026-01-01T01:00Z', 'energyRate': 2})
        v = version(profile=profile(dynamic={'intervals': [first, second]}))
        r = price(book([v]))
        self.assertFalse(r['pricing_complete'])
        self.assertEqual('mixed_rate_bucket', r['rows'][0]['reason'])
        second['from'] = '2026-01-01T00:45Z'
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(book([v]))

    def test_empty_incomplete_bool_nonfinite_and_bounded_json(self):
        coverage = {'from': '2026-01-01T00:00Z', 'to': '2026-01-01T01:00Z'}
        result = history.price_book(book(), [], coverage)['tariffs'][0]
        self.assertFalse(result['measurement_complete'])
        self.assertIsNone(result['energy_cost'])
        row = rows_between(coverage['from'], coverage['to'])[0]
        for value in (True, None, -1):
            bad = dict(row, kwh=value)
            self.assertFalse(history.price_book(book(), [bad], coverage)['tariffs'][0]['measurement_complete'])
        for value in (float('nan'), float('inf')):
            with self.assertRaises(history.TariffValidationError):
                history.price_book(book(), [dict(row, kwh=value)], coverage)
        result = history.price_book(book(), [row], dict(coverage, complete=False))['tariffs'][0]
        self.assertFalse(result['measurement_complete'])
        self.assertTrue(result['pricing_complete'])
        self.assertIsNone(result['total_cost'])
        with self.assertRaises(history.TariffValidationError):
            history.price_book(book(), [row] * 10001, coverage)
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(dict(book(), future='x' * 4000001))
        cyc = book()
        cyc['cycle'] = cyc
        with self.assertRaises(history.TariffValidationError):
            history.validate_tariff_book(cyc)

    def test_missing_and_overlapping_measurement_do_not_make_bill(self):
        coverage = {'from': '2026-01-01T00:00Z', 'to': '2026-01-01T03:00Z'}
        rows = rows_between(coverage['from'], coverage['to'])
        for broken in (rows[:1] + rows[2:], rows + rows[:1]):
            r = history.price_book(book(), broken, coverage)['tariffs'][0]
            self.assertFalse(r['measurement_complete'])
            self.assertIsNone(r['total_cost'])

    def test_migration_explicit_combined_unknown_and_schema1_unchanged(self):
        p = profile(validFrom='2026-01-01', validTo='2026-01-01')
        snapshot = copy.deepcopy(p)
        old = engine.EnergyTariff(p).price_buckets(rows_between('2026-01-01T00:00Z', '2026-01-01T01:00Z', 2))
        b = history.migrate_tariff_v1(p, legacy_combined=True)
        self.assertEqual(snapshot, b['source_schema1'])
        self.assertEqual('2026-01-02', b['tariffs'][0]['versions'][0]['valid_to'])
        r = price(b, kwh=2)
        self.assertEqual('2.00', r['legacy_combined_cost'])
        self.assertEqual('2.00', r['usage_cost'])
        self.assertIsNone(r['energy_cost'])
        self.assertIsNone(r['variable_cost'])
        self.assertFalse(r['components_complete'])
        self.assertEqual(snapshot, p)
        self.assertEqual(old, engine.EnergyTariff(p).price_buckets(rows_between('2026-01-01T00:00Z', '2026-01-01T01:00Z', 2)))
        p['zones'][0].pop('variableRate')
        r = price(history.migrate_tariff_v1(p, legacy_combined=False), kwh=2)
        self.assertEqual('2.00', r['energy_cost'])
        self.assertIsNone(r['variable_cost'])
        self.assertIsNone(r['usage_cost'])
        unknown = history.migrate_tariff_v1(profile(), legacy_combined=False)
        self.assertIn('unknown_historical_start', unknown['migration_issues'])
        self.assertEqual('unknown_historical_version', price(unknown)['rows'][0]['reason'])
        with self.assertRaises(TypeError):
            history.migrate_tariff_v1(profile())

    def test_unknown_component_keeps_known_partial_amount(self):
        v = version()
        v['fixed_monthly'].update(subscription=31, capacity=None)
        v['profile']['zones'][0].pop('variableRate')
        r = price(book([v]), end='2026-01-02T00:00Z')
        self.assertEqual('24.00', r['energy_cost'])
        self.assertIsNone(r['variable_cost'])
        self.assertEqual('1.00', r['fees']['known_components']['subscription'])
        self.assertIsNone(r['fixed_cost'])
        self.assertIsNone(r['total_cost'])

    def test_local_calendar_month_lengths_and_dst_days(self):
        cases = [
            ('2026-02-01T00:00Z', '2026-02-02T00:00Z', 28, 'UTC'),
            ('2024-02-29T00:00Z', '2024-03-01T00:00Z', 29, 'UTC'),
            ('2026-04-01T00:00Z', '2026-04-02T00:00Z', 30, 'UTC'),
            ('2026-01-01T00:00Z', '2026-01-02T00:00Z', 31, 'UTC'),
            ('2026-03-28T23:00Z', '2026-03-29T22:00Z', 31, 'Europe/Warsaw'),
            ('2026-10-24T22:00Z', '2026-10-25T23:00Z', 31, 'Europe/Warsaw'),
        ]
        for start, end, monthly, zone in cases:
            with self.subTest(start=start):
                v = version('2024-01-01', monthly=monthly)
                v['profile']['timezone'] = zone
                r = price(book([v]), start=start, end=end)
                self.assertEqual('1.00', r['fixed_cost'])
                self.assertEqual(1, len(r['fees']['days']))
                self.assertEqual('1', r['fees']['days'][0]['day_fraction'])

    def test_partial_dst_day_month_split_rounding_and_zero_energy(self):
        v = version(monthly=31)
        v['profile']['timezone'] = 'Europe/Warsaw'
        r = price(book([v]), start='2026-03-28T23:00Z', end='2026-03-29T10:30Z', kwh=0)
        self.assertEqual('0.50', r['fixed_cost'])  # 11.5 / 23 actual hours
        self.assertEqual('0.50', r['total_cost'])
        self.assertIsNone(r['effective_price'])
        b = book([version(monthly=31), version('2026-02-01', version_id='b', monthly=28)])
        r = price(b, start='2026-01-31T00:00Z', end='2026-02-02T00:00Z', kwh=0)
        self.assertEqual('2.00', r['fixed_cost'])
        v = version(rate=.005, monthly=0)
        r = price(book([v]), end='2026-01-01T02:00Z')
        self.assertEqual('0.01', r['energy_cost'])  # rounded after accumulation
        self.assertEqual('0.01', r['total_cost'])

    def test_alternatives_identical_rows_fingerprint_no_activation(self):
        b = book()
        b['tariffs'].append(tariff([version(rate=2)], 'alternative'))
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        rows, coverage = rows_between(start, end), {'from': start, 'to': end, 'source_signature': 'synthetic'}
        before = copy.deepcopy((b, rows, coverage))
        result = history.price_book(b, rows, coverage)
        self.assertEqual(before, (b, rows, coverage))
        self.assertEqual('active', result['active_tariff_id'])
        a, other = result['tariffs']
        self.assertEqual(a['measurement_fingerprint'], other['measurement_fingerprint'])
        self.assertEqual('2.00', other['delta_cost'])
        self.assertEqual([(r['from'], r['to'], r['kwh']) for r in a['rows']],
                         [(r['from'], r['to'], r['kwh']) for r in other['rows']])
        self.assertEqual(result, json.loads(json.dumps(result, allow_nan=False)))
        changed = history.price_book(b, rows, dict(coverage, source_signature='changed'))
        self.assertNotEqual(result['measurement_fingerprint'], changed['measurement_fingerprint'])

    def test_alternative_and_historical_currency_basis_mismatch(self):
        for field, value in (('currency', 'PLN'), ('priceBasis', 'net')):
            b = book()
            alternative = tariff([version()], 'other')
            alternative['versions'][0]['profile'][field] = value
            b['tariffs'].append(alternative)
            r = history.price_book(b, rows_between('2026-01-01T00:00Z', '2026-01-01T01:00Z'),
                                   {'from': '2026-01-01T00:00Z', 'to': '2026-01-01T01:00Z'})['tariffs'][1]
            self.assertFalse(r['comparable'])
            self.assertIsNone(r['delta_cost'])
            self.assertEqual('currency_or_basis_mismatch', r['comparison_reason'])
        v = version('2026-01-02', version_id='b')
        v['profile']['currency'] = 'PLN'
        r = price(book([version(), v]), end='2026-01-03T00:00Z')
        self.assertIsNone(r['total_cost'])
        self.assertIsNone(r['energy_cost'])
        self.assertFalse(r['compatible'])


if __name__ == '__main__':
    unittest.main()
