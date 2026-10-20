"""Recorded native cost facts and current-only prices."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
MODULE = ROOT / 'native_energy_pricing.py'
spec = importlib.util.spec_from_file_location('native_pricing_fixture', MODULE)
pricing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pricing)
START = datetime(2026, 10, 19, tzinfo=timezone.utc)
END = START + timedelta(hours=24)


def source(sid='sensor.grid', cost='sensor.cost', **extra):
    return {'type': 'grid', 'stat_energy_from': sid, 'stat_cost': cost, **extra}


def config(*sources):
    sources = sources or (source(),)
    return pricing.capture_native_pricing({'energy_sources': list(sources)},
        [s['stat_energy_from'] for s in sources], 'EUR')


def metadata(*ids):
    return {sid: {'has_sum': True, 'unit_of_measurement': 'EUR', 'unit_class': 'monetary'}
            for sid in (ids or ('sensor.cost',))}


def rows(value=2.4375, start=START, hours=24):
    return [{'start': start + timedelta(hours=i), 'change': value} for i in range(hours)]


def accumulator(configuration=None, end=END):
    return pricing.NativeCostAccumulator(configuration or config(), {'start': START, 'end': end})


class CaptureTest(unittest.TestCase):
    def test_flat_legacy_selection_and_detachment(self):
        prefs = {'energy_sources': [source(number_energy_price=.4),
            {'type': 'grid', 'flow_from': [source('sensor.second', 'external:cost')]},
            {'type': 'gas', 'stat_energy_from': 'sensor.gas', 'stat_cost': 'gas:cost'},
            {'type': 'grid', 'stat_energy_to': 'sensor.export', 'stat_cost': 'export:cost'}]}
        result = pricing.capture_native_pricing(prefs, ['sensor.grid', 'sensor.second'], 'EUR')
        self.assertTrue(result['valid'])
        self.assertEqual(['sensor.cost', 'external:cost'], [s['cost_statistic_id'] for s in result['sources']])
        self.assertEqual('0.4', result['sources'][0]['number_price'])
        prefs['energy_sources'][0]['stat_cost'] = 'tampered'
        self.assertEqual('sensor.cost', result['sources'][0]['cost_statistic_id'])

    def test_flat_takes_precedence_and_no_cost_suffix_is_guessed(self):
        s = source(cost=None, flow_from=[source('sensor.second', 'other:cost')])
        result = pricing.capture_native_pricing({'energy_sources': [s]}, ['sensor.grid', 'sensor.second'], 'EUR')
        self.assertEqual([None, None], [s['cost_statistic_id'] for s in result['sources']])
        self.assertTrue(result['valid'])

    def test_duplicate_energy_and_shared_cost_fail_closed(self):
        for sources in [(source(), source()), (source(), source('sensor.second'))]:
            result = config(*sources)
            self.assertFalse(result['valid'])
            builder = accumulator(result)
            builder.add_chunk({'sensor.cost': rows()}, metadata(), START, END)
            cost = builder.finish()['period_cost']
            self.assertEqual('unavailable', cost['status'])
            self.assertIsNone(cost['recorded_amount'])

    def test_limits_and_invalid_prices_currency(self):
        for value in (True, float('nan'), float('inf'), 10**100, '1e99999', '1e999999999'):
            self.assertFalse(config(source(number_energy_price=value))['valid'])
        for value in (0, -.25):
            self.assertTrue(config(source(number_energy_price=value))['valid'])
        many = [source(f'sensor.e{i}', f'cost:{i}') for i in range(513)]
        self.assertFalse(config(*many)['valid'])
        self.assertFalse(pricing.capture_native_pricing({}, ['sensor.grid'], 'eur')['valid'])
        self.assertFalse(pricing.capture_native_pricing({}, ['sensor.grid', 'sensor.grid'], 'EUR')['valid'])


class RecordedCostTest(unittest.TestCase):
    def test_full_day_native_cost_is_independent_of_current_rate(self):
        cfg = config(source(number_energy_price=99))
        builder = accumulator(cfg)
        builder.add_chunk({'sensor.cost': rows()}, metadata(), START, END)
        result = builder.finish()
        self.assertEqual('ha_energy', result['source'])
        self.assertEqual(1, result['schema'])
        cost = result['period_cost']
        self.assertEqual('ready', cost['status'])
        self.assertEqual(Decimal('58.5'), Decimal(cost['amount']))
        self.assertEqual(cost['amount'], cost['recorded_amount'])
        self.assertNotEqual(Decimal(234) * 99, Decimal(cost['amount']))
        self.assertEqual({'complete': True, 'expected_source_hours': 24, 'measured_source_hours': 24}, cost['coverage'])
        self.assertEqual('unknown', cost['components_status'])
        self.assertEqual('unknown', cost['fixed_fees_status'])
        self.assertEqual([], result['current_prices'])
        result['period_cost']['amount'] = 'bad'
        self.assertEqual(Decimal('58.5'), Decimal(builder.finish()['period_cost']['amount']))

    def test_partial_two_sources_keeps_only_recorded_subset(self):
        builder = accumulator(config(source(), source('sensor.second', 'second:cost')))
        builder.add_chunk({'sensor.cost': rows()}, metadata('sensor.cost', 'second:cost'), START, END)
        cost = builder.finish()['period_cost']
        self.assertEqual('partial', cost['status'])
        self.assertIsNone(cost['amount'])
        self.assertEqual('58.5', cost['recorded_amount'])
        self.assertEqual(48, cost['coverage']['expected_source_hours'])
        self.assertEqual(24, cost['coverage']['measured_source_hours'])

    def test_zero_and_negative_are_measured(self):
        for value in (0, -2):
            builder = accumulator()
            builder.add_chunk({'sensor.cost': rows(value)}, metadata(), START, END)
            cost = builder.finish()['period_cost']
            self.assertEqual('ready', cost['status'])
            self.assertEqual(Decimal(value * 24), Decimal(cost['amount']))

    def test_duplicates_invalidate_hour_even_after_later_valid_duplicate(self):
        builder = accumulator()
        points = rows() + [rows()[0], rows()[0]]
        builder.add_chunk({'sensor.cost': points}, metadata(), START, END)
        cost = builder.finish()['period_cost']
        self.assertEqual('partial', cost['status'])
        self.assertEqual(23, cost['coverage']['measured_source_hours'])
        self.assertEqual(Decimal('56.0625'), Decimal(cost['recorded_amount']))

    def test_invalid_values_and_timestamps_do_not_count_or_round_into_hour(self):
        for value in (True, None, '2', float('nan'), float('inf'), 10**100):
            with self.subTest(value=str(value)):
                builder = accumulator()
                builder.add_chunk({'sensor.cost': rows(value)}, metadata(), START, END)
                cost = builder.finish()['period_cost']
                self.assertEqual('unavailable', cost['status'])
                self.assertIsNone(cost['recorded_amount'])
        builder = accumulator()
        points = rows(); points[0]['start'] = START + timedelta(microseconds=1)
        builder.add_chunk({'sensor.cost': points}, metadata(), START, END)
        self.assertEqual(23, builder.finish()['period_cost']['coverage']['measured_source_hours'])

    def test_metadata_required_exact_currency_and_sum(self):
        for field, value in [('has_sum', False), ('has_sum', 1), ('unit_of_measurement', 'USD'),
                             ('unit_of_measurement', 'EUR/kWh'), ('unit_class', 'energy')]:
            meta = metadata(); meta['sensor.cost'][field] = value
            builder = accumulator(); builder.add_chunk({'sensor.cost': rows()}, meta, START, END)
            self.assertIsNone(builder.finish()['period_cost']['recorded_amount'])
        builder = accumulator()
        builder.add_chunk({'sensor.cost': rows()}, {'sensor.cost': (42, metadata()['sensor.cost'])}, START, END)
        self.assertEqual('ready', builder.finish()['period_cost']['status'])

    def test_native_timestamp_seconds_and_aware_iso_supported(self):
        builder = accumulator()
        points = rows()
        for i, row in enumerate(points):
            row['start'] = row['start'].timestamp() if i % 2 else row['start'].isoformat()
        builder.add_chunk({'sensor.cost': points}, metadata(), START, END)
        self.assertEqual('58.5', builder.finish()['period_cost']['amount'])

    def test_no_native_cost_is_not_configured_even_with_current_price(self):
        builder = accumulator(config(source(cost=None, number_energy_price=.25)))
        builder.add_chunk({}, {}, START, END)
        cost = builder.finish()['period_cost']
        self.assertEqual('not_configured', cost['status'])
        self.assertIsNone(cost['amount'])
        self.assertIsNone(cost['recorded_amount'])
        self.assertFalse(cost['coverage']['complete'])

    def test_streamed_eight_days_and_maximum_source_capture(self):
        many = [source(f'sensor.e{i}', f'cost:{i}') for i in range(512)]
        self.assertTrue(config(*many)['valid'])
        stop = START + timedelta(days=8)
        split = START + timedelta(days=7)
        builder = accumulator(end=stop)
        builder.add_chunk({'sensor.cost': rows(1, hours=168)}, metadata(), START, split)
        builder.add_chunk({'sensor.cost': rows(-1, start=split, hours=24)}, metadata(), split, stop)
        cost = builder.finish()['period_cost']
        self.assertEqual('144', cost['amount'])
        self.assertEqual(192, cost['coverage']['measured_source_hours'])

    def test_missing_period_tail_and_unconfigured_second_import_stay_partial(self):
        builder = accumulator()
        mid = START + timedelta(hours=12)
        builder.add_chunk({'sensor.cost': rows(1, hours=12)}, metadata(), START, mid)
        self.assertEqual('partial', builder.finish()['period_cost']['status'])
        builder = accumulator(config(source(), source('sensor.second', None)))
        builder.add_chunk({'sensor.cost': rows()}, metadata(), START, END)
        cost = builder.finish()['period_cost']
        self.assertEqual('partial', cost['status'])
        self.assertIsNone(cost['amount'])
        self.assertEqual('58.5', cost['recorded_amount'])

    def test_contiguous_chunks_limits_and_detached_constructor(self):
        cfg = config(); builder = accumulator(cfg)
        cfg['sources'][0]['cost_statistic_id'] = 'tampered'
        mid = START + timedelta(hours=12)
        builder.add_chunk({'sensor.cost': rows(hours=12)}, metadata(), START, mid)
        with self.assertRaises(ValueError):
            builder.add_chunk({}, {}, START, mid)
        with self.assertRaises(ValueError):
            builder.add_chunk({}, {}, mid + timedelta(hours=1), END)
        builder.add_chunk({'sensor.cost': rows(start=mid, hours=12)}, metadata(), mid, END)
        self.assertEqual('58.5', builder.finish()['period_cost']['amount'])
        for end in (START + timedelta(days=367), START + timedelta(minutes=1), START.replace(tzinfo=None)):
            with self.assertRaises(ValueError):
                accumulator(end=end)
        builder = accumulator(end=START + timedelta(days=8))
        with self.assertRaises(ValueError):
            builder.add_chunk({}, {}, START, START + timedelta(days=8))


class CurrentPriceTest(unittest.TestCase):
    def test_fixed_and_entity_prices_are_current_only(self):
        cfg = config(source(number_energy_price=.25), source('sensor.second', 'cost:second', entity_energy_price='sensor.price'))
        prices = pricing.project_current_prices(cfg, {'sensor.price': {'state': '0.0003', 'unit_of_measurement': 'EUR/Wh'}}, END)
        self.assertEqual(['0.25', '0.3'], [p['price'] for p in prices])
        self.assertEqual(['number_energy_price', 'entity_energy_price'], [p['origin'] for p in prices])
        self.assertTrue(all(p['historical'] is False and p['energy_unit'] == 'kWh' for p in prices))
        cfg = config(source(entity_energy_price='sensor.price'))
        for unit, state, expected in [('EUR/MWh', '200', '0.2'), ('EUR/kWh', '-0.2', '-0.2'), ('EUR/kWh', '0', '0')]:
            result = pricing.project_current_prices(cfg, {'sensor.price': {'state': state, 'unit_of_measurement': unit}}, END)
            self.assertEqual(expected, result[0]['price'])

    def test_bad_entity_state_unit_and_currency_remain_unknown(self):
        cfg = config(source(entity_energy_price='sensor.price'))
        for unit, value in [('USD/kWh', '1'), ('EUR', '1'), ('EUR/kWh', 'unknown'), ('EUR/kWh', 'NaN'), ('EUR/kWh', True)]:
            result = pricing.project_current_prices(cfg, {'sensor.price': {'state': value, 'unit_of_measurement': unit}}, END)[0]
            self.assertEqual('unavailable', result['status'])
            self.assertIsNone(result['price'])
        self.assertEqual('unavailable', pricing.project_current_prices(cfg, {}, END)[0]['status'])
        with self.assertRaises(ValueError):
            pricing.project_current_prices(cfg, {}, END.replace(tzinfo=None))


if __name__ == '__main__':
    unittest.main()
