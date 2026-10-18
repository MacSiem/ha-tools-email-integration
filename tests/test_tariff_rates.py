"""Pure rate lookup/arithmetic contracts; owner runs against the applied patch."""
import copy
from decimal import Decimal, localcontext
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('rates_test_package')
package.__path__ = [str(ROOT)]
with patch.dict(sys.modules, {package.__name__: package}):
    spec = importlib.util.spec_from_file_location(package.__name__ + '.tariff_history', ROOT / 'tariff_history.py')
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)


def profile(rate=1, variable=0, **changes):
    result = dict(schema=1, currency='EUR', timezone='UTC', clock={'mode': 'civil'},
                  priceBasis='gross', defaultZone='day', zones=[{'id': 'day', 'energyRate': rate,
                                                              'variableRate': variable}], rules=[])
    result.update(changes)
    return result


def version(start='2026-01-01', rate=1, version_id='a', **changes):
    result = {'id': version_id, 'valid_from': start, 'profile': profile(rate),
              'rate_semantics': 'components',
              'fixed_monthly': dict(subscription=0, capacity=0, distribution=0, other=0),
              'legacy_combined_monthly': None}
    result.update(changes)
    return result


def tariff(*versions):
    return {'id': 'active', 'name': 'Active', 'origin': {'kind': 'user'},
            'versions': list(versions) if versions else [version()]}


class EnergyAmountsTest(unittest.TestCase):
    def test_decimal_precision_is_local_and_no_chunk_rounding(self):
        with localcontext() as context:
            context.prec = 6
            result = history.calculate_energy_amounts(Decimal('0.1234567890123456789'), .2, .03, 'components')
            self.assertEqual(6, context.prec)
        self.assertEqual(Decimal('0.02469135780246913578'), result['energy_cost'])
        self.assertEqual(Decimal('0.003703703670370370367'), result['variable_cost'])
        self.assertEqual(Decimal('0.028395061472839506147'), result['usage_cost'])
        self.assertEqual(Decimal(0), result['legacy_combined_cost'])
        self.assertEqual(result, history.calculate_energy_amounts('0.1234567890123456789', .2, .03, 'components'))

    def test_zero_unknown_and_legacy_have_distinct_meanings(self):
        self.assertEqual({'energy_cost': Decimal(0), 'variable_cost': Decimal(0),
                          'legacy_combined_cost': Decimal(0), 'usage_cost': Decimal(0)},
                         history.calculate_energy_amounts(0, 2, .5, 'components'))
        unknown = history.calculate_energy_amounts(0, 2, None, 'components')
        self.assertEqual(Decimal(0), unknown['energy_cost'])
        self.assertIsNone(unknown['variable_cost'])
        self.assertIsNone(unknown['usage_cost'])
        legacy = history.calculate_energy_amounts('1.25', .8, None, 'legacy_combined')
        self.assertEqual({'energy_cost': None, 'variable_cost': None,
                          'legacy_combined_cost': Decimal('1.000'), 'usage_cost': Decimal('1.000')}, legacy)
        self.assertIsNone(history.calculate_energy_amounts(1, None, 0, 'components')['usage_cost'])

    def test_invalid_numbers_never_become_zero(self):
        bad = (True, False, None, -1, '-0.1', float('nan'), float('inf'), Decimal('NaN'),
               Decimal('Infinity'), 'NaN', 'Infinity', '', ' 1', '1e2', '1_0',
               '1' * 129, Decimal('1e-129'), 1000000000001, {}, [])
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                history.calculate_energy_amounts(value, 1, 0, 'components')
        for energy, variable in [(True, 0), (float('inf'), 0), ('oops', 0), (1, False), (1, -1)]:
            with self.subTest(energy=energy, variable=variable), self.assertRaises(ValueError):
                history.calculate_energy_amounts(1, energy, variable, 'components')
        with localcontext() as context:
            context.prec = 6
            with self.assertRaises(ValueError):
                history.calculate_energy_amounts(Decimal('1000000000000.0001'), 1, 0, 'components')
        with self.assertRaises(ValueError):
            history.calculate_energy_amounts(1, 1, 0, 'unknown')

    def test_negative_energy_requires_engine_opt_in_before_helper(self):
        with self.assertRaises(history.TariffValidationError):
            history.HistoricalTariffRates(tariff(version(rate=-.2)))
        rates = history.HistoricalTariffRates(tariff(version(profile=profile(-.2, .05, allowNegativeEnergy=True))))
        selected = rates.resolve_interval('2026-01-01T00:00Z', '2026-01-01T01:00Z')
        self.assertTrue(selected['available'])
        amounts = history.calculate_energy_amounts(2, selected['energy_rate'], selected['variable_rate'], selected['rate_semantics'])
        self.assertEqual(Decimal('-.40'), amounts['energy_cost'])
        self.assertEqual(Decimal('-.30'), amounts['usage_cost'])


class HistoricalRatesTest(unittest.TestCase):
    def test_tou_uniform_and_mixed_boundary(self):
        p = profile(zones=[{'id': 'day', 'energyRate': 1, 'variableRate': .2},
                           {'id': 'night', 'energyRate': .3, 'variableRate': .1}],
                    rules=[{'zone': 'night', 'intervals': [{'from': '22:00', 'to': '06:00'}]}])
        rates = history.HistoricalTariffRates(tariff(version(profile=p)))
        night = rates.resolve_interval('2026-01-01T04:00Z', '2026-01-01T06:00Z')
        self.assertTrue(night['available'])
        self.assertEqual((.3, .1, 'night'), (night['energy_rate'], night['variable_rate'], night['zone']))
        day = rates.resolve_interval('2026-01-01T06:00Z', '2026-01-01T07:00Z')
        self.assertEqual((1, .2), (day['energy_rate'], day['variable_rate']))
        mixed = rates.resolve_interval('2026-01-01T05:30Z', '2026-01-01T06:30Z')
        self.assertFalse(mixed['available'])
        self.assertEqual('mixed_rate_bucket', mixed['reason'])
        self.assertNotIn('energy_rate', mixed)

    def test_same_rates_multiple_zones_are_uniform(self):
        p = profile(zones=[{'id': 'day', 'energyRate': 1, 'variableRate': 0},
                           {'id': 'night', 'energyRate': 1, 'variableRate': 0}],
                    rules=[{'zone': 'night', 'intervals': [{'from': '00:00', 'to': '06:00'}]}])
        selected = history.HistoricalTariffRates(tariff(version(profile=p))).resolve_interval(
            '2026-01-01T05:30Z', '2026-01-01T06:30Z')
        self.assertTrue(selected['available'])
        self.assertEqual(['day', 'night'], selected['zones'])
        self.assertIsNone(selected['zone'])
        self.assertEqual('multiple', selected['zone_attribution'])

    def test_historical_version_change_and_unknown_start(self):
        rates = history.HistoricalTariffRates(tariff(version(), version('2026-01-02', 2, 'b')))
        old = rates.resolve_interval('2026-01-01T23:00Z', '2026-01-02T00:00Z')
        new = rates.resolve_interval('2026-01-02T00:00Z', '2026-01-02T01:00Z')
        self.assertEqual(('a', 1), (old['version_id'], old['energy_rate']))
        self.assertEqual(('b', 2), (new['version_id'], new['energy_rate']))
        self.assertEqual('mixed_version_bucket', rates.resolve_interval('2026-01-01T23:30Z', '2026-01-02T00:30Z')['reason'])
        self.assertEqual('unknown_historical_version', rates.resolve_interval('2025-12-31T23:00Z', '2026-01-01T00:00Z')['reason'])
        unknown = history.migrate_tariff_v1(profile(), legacy_combined=True)['tariffs'][0]
        self.assertEqual('unknown_historical_version', history.HistoricalTariffRates(unknown).resolve_interval(
            '2026-01-01T00:00Z', '2026-01-01T01:00Z')['reason'])

    def test_migrated_fixed_clock_boundary_and_legacy_metadata(self):
        migrated = history.migrate_tariff_v1(profile(timezone='Europe/Warsaw',
            clock={'mode': 'fixed', 'offsetMinutes': 0}, validFrom='2026-01-01', validTo='2026-01-01'),
            legacy_combined=True)
        rates = history.HistoricalTariffRates(migrated['tariffs'][0])
        self.assertFalse(rates.resolve_interval('2025-12-31T23:00Z', '2026-01-01T00:00Z')['available'])
        result = rates.resolve_interval('2026-01-01T23:00Z', '2026-01-02T00:00Z')
        self.assertTrue(result['available'])
        self.assertEqual('legacy_profile', result['validity_clock'])
        self.assertEqual('legacy_combined', result['rate_semantics'])
        self.assertEqual('migrated_explicit_start', result['provenance'])
        self.assertFalse(rates.resolve_interval('2026-01-02T00:00Z', '2026-01-02T01:00Z')['available'])

    def test_dynamic_uniformity_and_missing_rate_delegate_to_engine(self):
        p = profile(dynamic={'intervals': [
            {'from': '2026-01-01T00:00Z', 'to': '2026-01-01T00:30Z', 'energyRate': .1, 'variableRate': None},
            {'from': '2026-01-01T00:30Z', 'to': '2026-01-01T01:00Z', 'energyRate': .2, 'variableRate': 0}]})
        rates = history.HistoricalTariffRates(tariff(version(profile=p)))
        self.assertIsNone(rates.resolve_interval('2026-01-01T00:00Z', '2026-01-01T00:30Z')['variable_rate'])
        self.assertEqual('mixed_rate_bucket', rates.resolve_interval('2026-01-01T00:00Z', '2026-01-01T01:00Z')['reason'])
        self.assertEqual('missing_rate', rates.resolve_interval('2026-01-01T01:00Z', '2026-01-01T02:00Z')['reason'])

    def test_invalid_boundaries_and_settings_stay_rejected(self):
        rates = history.HistoricalTariffRates(tariff())
        for start, end in [(True, 1000), (0, float('inf')), ('0', '1000'),
                           ('2026-01-01T00:00', '2026-01-01T01:00Z'),
                           ('2026-01-01T00:00Z', '2026-01-01T00:00Z'),
                           ('2026-01-01T00:00Z', '2026-01-02T00:01Z'),
                           ('1800-01-01T00:00Z', '1800-01-01T01:00Z')]:
            with self.subTest(start=start, end=end):
                self.assertEqual({'available': False, 'reason': 'invalid_interval'}, rates.resolve_interval(start, end))
        for value in ('1', Decimal('1'), True):
            with self.subTest(value=value), self.assertRaises(history.TariffValidationError):
                history.HistoricalTariffRates(tariff(version(rate=value)))
        with self.assertRaises(history.TariffValidationError):
            history.HistoricalTariffRates(tariff(version(profile=profile(energyUnit='MWh'))))
        with self.assertRaises(history.TariffValidationError):
            history.HistoricalTariffRates(tariff(*[version(f'{2026+i}-01-01', version_id=str(i)) for i in range(1, 34)]))

    def test_detached_json_safe_rate_only_results(self):
        source = tariff()
        before = copy.deepcopy(source)
        rates = history.HistoricalTariffRates(source)
        self.assertEqual(before, source)
        source['versions'][0]['profile']['zones'][0]['energyRate'] = 999
        result = rates.resolve_interval('2026-01-01T00:00Z', '2026-01-01T01:00Z')
        self.assertEqual(1, result['energy_rate'])
        self.assertEqual('EUR', result['currency'])
        self.assertEqual('gross', result['price_basis'])
        self.assertEqual(result, json.loads(json.dumps(result, allow_nan=False)))
        forbidden = {'kwh', 'energyCost', 'variableCost', 'usageCost', 'energy_cost',
                     'variable_cost', 'legacy_combined_cost', 'usage_cost', 'costs', 'fixedMonthly', 'fixed_monthly'}
        self.assertFalse(forbidden.intersection(result))
        result['clock']['mode'] = 'changed'
        result['zones'].append('changed')
        fresh = rates.resolve_interval('2026-01-01T00:00Z', '2026-01-01T01:00Z')
        self.assertEqual({'mode': 'civil'}, fresh['clock'])
        self.assertEqual(['day'], fresh['zones'])

    def test_existing_period_rounding_and_row_cost_shape(self):
        a = version(profile=profile(.005, .005))
        source = tariff(a)
        book = {'schema': 2, 'active_tariff_id': 'active', 'tariffs': [source]}
        rows = [{'from': '2026-01-01T00:00Z', 'to': '2026-01-01T01:00Z', 'kwh': 1},
                {'from': '2026-01-01T01:00Z', 'to': '2026-01-01T02:00Z', 'kwh': 1}]
        result = history.price_book(book, rows, {'from': rows[0]['from'], 'to': rows[-1]['to']})['tariffs'][0]
        self.assertEqual(('0.01', '0.01', '0.02', '0.00', '0.02'),
                         (result['energy_cost'], result['variable_cost'], result['usage_cost'], result['fixed_cost'], result['total_cost']))
        self.assertEqual({'energy_cost': '0.005', 'variable_cost': '0.005', 'legacy_combined_cost': '0'}, result['rows'][0]['costs'])
        a['rate_semantics'] = 'legacy_combined'
        a['profile']['zones'][0]['variableRate'] = 0
        legacy = history.price_book(book, rows, {'from': rows[0]['from'], 'to': rows[-1]['to']})['tariffs'][0]
        self.assertEqual('0.01', legacy['usage_cost'])
        self.assertIsNone(legacy['energy_cost'])
        self.assertIsNone(legacy['total_cost'])
        self.assertEqual({'energy_cost': None, 'variable_cost': None, 'legacy_combined_cost': '0.005'}, legacy['rows'][0]['costs'])


if __name__ == '__main__':
    unittest.main()
