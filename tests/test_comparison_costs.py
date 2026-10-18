"""Literal comparison-cost contracts. Apply independently before implementation.

Pure module loading avoids Home Assistant; no Recorder IO or client cost flags.
"""
import copy
from datetime import datetime, timedelta
from decimal import localcontext
import importlib
from math import fsum
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('comparison_costs_test_package')
package.__path__ = [str(ROOT)]
with patch.dict(sys.modules, {package.__name__: package}):
    stream = importlib.import_module(package.__name__ + '.energy_comparison_stream')
    history = importlib.import_module(package.__name__ + '.tariff_history')


def profile(energy=.5, variable=.2, **changes):
    result = dict(schema=1, currency='PLN', timezone='UTC', clock={'mode': 'civil'},
                  priceBasis='gross', defaultZone='day',
                  zones=[dict(id='day', energyRate=energy, variableRate=variable)], rules=[])
    result.update(changes)
    return result


def version(start='2025-01-01', vid='old', energy=.4, variable=.1, monthly=0, **changes):
    result = dict(id=vid, valid_from=start, profile=profile(energy, variable),
                  rate_semantics='components',
                  fixed_monthly=dict(subscription=monthly, capacity=0, distribution=0, other=0),
                  legacy_combined_monthly=None)
    result.update(changes)
    return result


def tariff(*versions):
    return dict(id='active', name='Active', origin={'kind': 'user'},
                versions=list(versions) if versions else [version(), version('2026-01-02', 'new', .5, .2)])


def snapshot(start, end, sources, value, missing=(), offsets=None):
    a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    stamps = range(int(a.timestamp()*1000), int(b.timestamp()*1000), 3600000)
    return {'source_ids': list(sources), 'source_buckets': {
        sid: [{'from': stamp, 'to': stamp+3600000,
               'kwh': offsets[sid] if offsets else value}
              for stamp in stamps if (sid, stamp) not in missing] for sid in sources},
        'measurement_provenance': {sid: {'method': 'recorder_hourly_change_v1', 'unit': 'kWh',
            'source_unit': 'kWh', 'has_sum': True} for sid in sources}}


class ComparisonCostsTest(unittest.TestCase):
    bounds = (('2026-01-02T00:00Z', '2026-01-02T02:00Z'),
              ('2026-01-01T00:00Z', '2026-01-01T02:00Z'))

    def model(self, book, bounds=None, *, kind='daily', zone='UTC', configure=True):
        bounds = bounds or self.bounds
        kwargs = {'tariff': book} if configure else {}
        return stream.PairedComparisonAccumulator(
            dict(zip(('start', 'end'), bounds[0])), dict(zip(('start', 'end'), bounds[1])),
            timezone_name=zone, calendar_kind=kind, **kwargs)

    def run_pair(self, book, bounds=None, *, split=168, sources=('sensor.a',), missing=None,
                 values=(1, 1), model=None, kind='daily', zone='UTC', offsets=None):
        bounds = bounds or self.bounds
        model = model or self.model(book, bounds, kind=kind, zone=zone)
        ends = [datetime.fromisoformat(p[1]) for p in bounds]
        cursors = [datetime.fromisoformat(p[0]) for p in bounds]
        missing = missing or {}
        while (side := model.needs_side()) is not None:
            i = 0 if side == 'current' else 1
            start, end = cursors[i], min(cursors[i] + timedelta(hours=split), ends[i])
            data = snapshot(start.isoformat(), end.isoformat(), sources, values[i], missing.get(side, ()), offsets)
            model.push(side, data, start, end)
            cursors[i] = end
        return model.finish(), model

    def test_literal_costs_use_each_sides_historical_dates(self):
        for split in (1, 2, 168):
            result, model = self.run_pair(tariff(), split=split)
            costs = result['costs']
            self.assertEqual((2, 2), (result['current_kwh'], result['baseline_kwh']))
            self.assertEqual(('1.40', '1.00', '0.40', '40.0'),
                (costs['current']['usage_cost'], costs['baseline']['usage_cost'],
                 costs['delta_cost'], costs['percent']))
            self.assertEqual(('1.00', '0.40'), (costs['current']['energy_cost'], costs['current']['variable_cost']))
            self.assertEqual(('1.40', '0.40'), (costs['current']['total_cost'], costs['delta_total_cost']))
            self.assertEqual(7200, costs['current']['priced_source_seconds']['usage_cost'])
            self.assertEqual(0, model.retained_cells)

    def test_none_preserves_entire_energy_contract_and_results_are_detached(self):
        bare = self.model(None, configure=False)
        plain, _ = self.run_pair(None, model=bare)
        optional, _ = self.run_pair(None)
        priced, model = self.run_pair(tariff())
        self.assertEqual(plain, optional)
        self.assertNotIn('costs', optional)
        self.assertEqual(plain, {k: v for k, v in priced.items() if k != 'costs'})
        priced['costs']['current']['known_costs'].clear()
        self.assertEqual('1.00', model.finish()['costs']['current']['known_costs']['energy_cost'])

    def test_validate_detach_once_never_trust_flag(self):
        book = tariff()
        with patch.object(history, 'validate_tariff_book', wraps=history.validate_tariff_book) as validate:
            model = self.model(book)
            book['versions'][1]['profile']['zones'][0]['energyRate'] = 50
            result, _ = self.run_pair(book, model=model, split=1)
            self.assertEqual(1, validate.call_count)
            self.assertEqual('1.40', result['costs']['current']['usage_cost'])
        bad = tariff(); bad['validated'] = True
        bad['versions'][0]['profile']['zones'][0]['energyRate'] = 'bad'
        with self.assertRaises(ValueError): self.model(bad)

    def test_partial_prices_same_source_mask_and_ignores_unrelated_missing_rate(self):
        bounds = (('2026-01-02T00:00Z', '2026-01-02T10:00Z'),
                  ('2026-01-01T00:00Z', '2026-01-01T10:00Z'))
        # Only the first own-source observation on each side is unmatched.
        # The current tariff has no rate at 00:00, outside the common mask.
        dynamic = {'intervals': [{'from': '2026-01-02T01:00Z', 'to': '2026-01-02T10:00Z',
                                 'energyRate': .5, 'variableRate': .2}]}
        book = tariff(version(), version('2026-01-02', 'new', profile=profile(dynamic=dynamic)))
        current0 = int(datetime.fromisoformat(bounds[0][0]).timestamp()*1000)
        baseline0 = int(datetime.fromisoformat(bounds[1][0]).timestamp()*1000)
        missing = {'current': [('sensor.a', current0)], 'baseline': [('sensor.b', baseline0)]}
        for split in (1, 3, 168):
            result, _ = self.run_pair(book, bounds, sources=('sensor.a', 'sensor.b'), missing=missing, split=split)
            self.assertEqual((18, 18), (result['current_kwh'], result['baseline_kwh']))
            self.assertEqual('common_measurements', result['comparison_scope'])
            a = result['costs']['current']
            self.assertEqual('12.60', a['usage_cost'])
            self.assertEqual('3.60', result['costs']['delta_cost'])
            self.assertEqual(18*3600, a['mask_source_seconds'])
            self.assertEqual(a['mask_source_seconds'], a['priced_source_seconds']['usage_cost'])
            self.assertIsNone(a['total_cost']); self.assertIsNone(a['effective_price'])
        # Moving the missing measurements leaves 00:00 inside the same 90% mask.
        missing = {'current': [('sensor.a', current0+9*3600000)],
                   'baseline': [('sensor.b', baseline0+9*3600000)]}
        result, _ = self.run_pair(book, bounds, sources=('sensor.a', 'sensor.b'), missing=missing)
        self.assertEqual('ready', result['status'])
        a = result['costs']['current']
        self.assertIsNone(a['usage_cost']); self.assertIsNone(result['costs']['delta_cost'])
        self.assertEqual(16*3600, a['priced_source_seconds']['energy_cost'])
        self.assertEqual('8.00', a['known_costs']['energy_cost'])

    def test_variable_missing_and_zero_never_invent_usage(self):
        for value in (0, 1):
            result, _ = self.run_pair(tariff(version(variable=None)), values=(value, 0))
            a = result['costs']['current']
            self.assertEqual('0.00' if value == 0 else '0.80', a['energy_cost'])
            self.assertIsNone(a['variable_cost']); self.assertIsNone(a['usage_cost'])
            self.assertEqual(0, a['priced_source_seconds']['usage_cost'])
            self.assertIsNone(result['costs']['percent'])
        result, _ = self.run_pair(tariff(), values=(1, 0))
        self.assertEqual('1.40', result['costs']['delta_cost'])
        self.assertIsNone(result['costs']['percent'])

    def test_legacy_and_mixed_named_parts_have_one_period_rounding(self):
        bounds = (('2026-01-01T23:00Z', '2026-01-02T01:00Z'),
                  ('2025-12-31T23:00Z', '2026-01-01T01:00Z'))
        old = version(energy=.005, variable=None, rate_semantics='legacy_combined')
        new = version('2026-01-02', 'new', .005, .005)
        result, _ = self.run_pair(tariff(old, new), bounds, split=1)
        a = result['costs']['current']
        self.assertEqual(['components', 'legacy_combined'], a['rate_semantics'])
        self.assertIsNone(a['energy_cost']); self.assertIsNone(a['variable_cost'])
        self.assertEqual({'energy_cost': '0.01', 'variable_cost': '0.01',
                          'legacy_combined_cost': '0.01'}, a['known_costs'])
        self.assertEqual('0.03', a['usage_cost']); self.assertIsNone(a['total_cost'])
        self.assertEqual('0.01', result['costs']['baseline']['legacy_combined_cost'])

    def test_no_hour_or_chunk_rounding_and_local_decimal_context(self):
        for split in (1, 2):
            with localcontext() as context:
                context.prec = 6
                result, _ = self.run_pair(tariff(version(energy=.005, variable=.005)), split=split)
                self.assertEqual(6, context.prec)
            self.assertEqual('0.02', result['costs']['current']['usage_cost'])
        with patch.object(stream, 'calculate_energy_amounts', wraps=stream.calculate_energy_amounts) as calculate:
            self.run_pair(tariff(), sources=('sensor.a', 'sensor.b'),
                          offsets={'sensor.a': .1, 'sensor.b': .2})
            self.assertTrue(calculate.call_count)
            self.assertTrue(all(call.args[0] == fsum([.1, .2]) for call in calculate.call_args_list))

    def test_currency_basis_must_match_sides_and_be_uniform_inside_side(self):
        for changes in ({'currency': 'EUR'}, {'priceBasis': 'net'}):
            book = tariff(version(), version('2026-01-02', 'new', profile=profile(**changes)))
            result, _ = self.run_pair(book)
            costs = result['costs']
            self.assertEqual('currency_or_basis_mismatch', costs['reason'])
            self.assertIsNone(costs['delta_cost']); self.assertIsNone(costs['delta_total_cost'])
            self.assertEqual('1.40', costs['current']['usage_cost'])
            bounds = (('2026-01-01T23:00Z', '2026-01-02T01:00Z'), self.bounds[1])
            result, _ = self.run_pair(book, bounds)
            self.assertFalse(result['costs']['current']['compatible'])
            self.assertIsNone(result['costs']['current']['usage_cost'])
            self.assertTrue(all(v is None for v in result['costs']['current']['known_costs'].values()))

    def test_mixed_hour_versions_unknown_even_zero_no_interpolation(self):
        bounds = (('2026-01-01T18:00Z', '2026-01-01T19:00Z'),
                  ('2025-12-31T18:00Z', '2025-12-31T19:00Z'))
        book = tariff(version(profile=profile(timezone='Asia/Kolkata')),
                      version('2026-01-02', 'new', profile=profile(timezone='Asia/Kolkata')))
        for value in (0, 1):
            result, _ = self.run_pair(book, bounds, values=(value, value))
            a = result['costs']['current']
            self.assertIn('mixed_version_bucket', a['issues'])
            self.assertIsNone(a['energy_cost']); self.assertIsNone(a['usage_cost'])
            self.assertEqual(0, a['priced_source_seconds']['usage_cost'])

    def test_negative_rates_only_with_validated_engine_permission(self):
        with self.assertRaises(ValueError): self.model(tariff(version(energy=-.2)))
        book = tariff(version(profile=profile(-.2, .05, allowNegativeEnergy=True)))
        result, _ = self.run_pair(book)
        self.assertEqual('-0.30', result['costs']['current']['usage_cost'])

    def test_january_week_fees_once_despite_sources_partial_coverage_and_chunks(self):
        bounds = (('2026-01-08T00:00Z', '2026-01-15T00:00Z'),
                  ('2026-01-01T00:00Z', '2026-01-08T00:00Z'))
        for count in (2, 512):
            sources = tuple(f'sensor.s{i}' for i in range(count))
            for partial in (False, True):
                missing = {'current': [(sources[0], int(datetime.fromisoformat(bounds[0][0]).timestamp()*1000))]} if partial else None
                result, model = self.run_pair(tariff(version(monthly=31)), bounds,
                    sources=sources, missing=missing, kind='weekly', split=73)
                for side in ('current', 'baseline'):
                    row = result['costs'][side]
                    self.assertEqual('7.00', row['fixed_cost'])
                    self.assertEqual('7.00', row['fees']['components']['subscription'])
                    self.assertEqual('household_period', row['fixed_scope'])
                    if partial:
                        self.assertIsNone(row['total_cost']); self.assertIsNone(row['effective_price'])
                self.assertLessEqual(model.peak_retained_cells, 2*73*count)
                self.assertEqual(0, model.retained_cells)

    def test_complete_dst_uses_own_hours_partial_below_threshold_stays_unknown(self):
        bounds = (('2026-10-24T22:00Z', '2026-10-25T23:00Z'),
                  ('2026-10-23T22:00Z', '2026-10-24T22:00Z'))
        for split in (1, 7, 168):
            result, _ = self.run_pair(tariff(version(energy=.5, variable=.2)), bounds,
                                      zone='Europe/Warsaw', split=split)
            self.assertEqual((25, 24), (result['current_kwh'], result['baseline_kwh']))
            self.assertEqual(('17.50', '16.80', '0.70'),
                (result['costs']['current']['usage_cost'], result['costs']['baseline']['usage_cost'],
                 result['costs']['delta_cost']))
        bounds = (('2026-01-02T00:00Z', '2026-01-02T10:00Z'),
                  ('2026-01-01T00:00Z', '2026-01-01T10:00Z'))
        missing = {side: [('sensor.a', int(datetime.fromisoformat(bounds[i][0]).timestamp()*1000)
                          + i*3600000)] for i, side in enumerate(('current', 'baseline'))}
        result, _ = self.run_pair(tariff(), bounds, missing=missing)
        self.assertEqual('unknown', result['status'])
        self.assertIsNone(result['costs']['delta_cost'])
        self.assertIsNone(result['costs']['current']['usage_cost'])
        self.assertEqual(8*3600, result['costs']['current']['mask_source_seconds'])

    def test_year_stream_with_costs_keeps_two_week_source_bound(self):
        bounds = (('2024-01-01T00:00Z', '2025-01-01T00:00Z'),
                  ('2023-01-01T00:00Z', '2024-01-01T00:00Z'))
        sources = tuple(f'sensor.s{i}' for i in range(512))
        result, model = self.run_pair(tariff(version('2023-01-01', energy=.5, variable=.2)),
                                      bounds, kind='yearly', sources=sources)
        self.assertEqual((8784*512, 8760*512), (result['current_kwh'], result['baseline_kwh']))
        self.assertEqual('3148185.60', result['costs']['current']['usage_cost'])
        self.assertEqual(8784*512*3600, result['costs']['current']['priced_source_seconds']['usage_cost'])
        self.assertLessEqual(model.peak_retained_cells, 2*168*512)
        self.assertEqual(0, model.retained_cells)


if __name__ == '__main__':
    unittest.main()
