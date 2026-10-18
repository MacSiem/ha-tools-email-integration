"""Calendar sharing and usage-only contracts; runnable before implementation as RED.

No Home Assistant imports or implementation-text assertions.
"""
import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import importlib
from math import fsum
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('comparison_core_test_package')
package.__path__ = [str(ROOT)]
with patch.dict(sys.modules, {package.__name__: package}):
    stream = importlib.import_module(package.__name__ + '.energy_comparison_stream')
    core = importlib.import_module(package.__name__ + '.energy_comparison_core')
    history = importlib.import_module(package.__name__ + '.tariff_history')


def period(start, end):
    return dict(start=start, end=end)


def profile(energy=.5, variable=.2, **changes):
    result = dict(schema=1, currency='PLN', timezone='UTC', clock={'mode': 'civil'},
                  priceBasis='gross', defaultZone='day',
                  zones=[dict(id='day', energyRate=energy, variableRate=variable)], rules=[])
    result.update(changes)
    return result


def version(start='2025-01-01', vid='old', energy=.4, variable=.1, **changes):
    result = dict(id=vid, valid_from=start, profile=profile(energy, variable),
                  rate_semantics='components', fixed_monthly=dict(subscription=31,
                  capacity=0, distribution=0, other=0), legacy_combined_monthly=None)
    result.update(changes)
    return result


def tariff(*versions):
    return dict(id='active', name='Active', origin={'kind': 'user'}, versions=list(versions)
        if versions else [version(), version('2026-01-02', 'new', .5, .2)])


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp()*1000)


BOUNDS = (period('2026-01-02T00:00Z', '2026-01-02T10:00Z'),
          period('2026-01-01T00:00Z', '2026-01-01T10:00Z'))


def run_pair(periods=BOUNDS, *, plan=None, book=None, split=3, missing=(), kind='daily', zone='UTC'):
    model = stream.PairedComparisonAccumulator(*periods, timezone_name=zone,
        calendar_kind=kind, tariff=book, calendar_plan=plan)
    cursors = [datetime.fromisoformat(p['start']) for p in periods]
    ends = [datetime.fromisoformat(p['end']) for p in periods]
    while (side := model.needs_side()) is not None:
        i = 0 if side == 'current' else 1
        start, end = cursors[i], min(cursors[i]+timedelta(hours=split), ends[i])
        rows = [dict(**{'from': t, 'to': t+3600000}, kwh=i+1)
                for t in range(int(start.timestamp()*1000), int(end.timestamp()*1000), 3600000)
                if (i, t) not in missing]
        data = dict(source_ids=['sensor.a'], source_buckets={'sensor.a': rows},
            measurement_provenance={'sensor.a': dict(method='recorder_hourly_change_v1',
                unit='kWh', source_unit='kWh', has_sum=True)})
        model.push(side, data, start, end)
        cursors[i] = end
    return model.finish(), model


class CalendarPlanTest(unittest.TestCase):
    def test_read_only_detached_and_shared_without_rebuilding(self):
        source = copy.deepcopy(BOUNDS)
        source[0]['unrelated'] = {'editable': []}
        plan = core.CalendarComparisonPlan(*source, timezone_name='UTC', calendar_kind='daily')
        source[0]['start'] = 'invalid'
        source[0]['unrelated']['editable'].append('changed')
        self.assertEqual(datetime(2026, 1, 2, tzinfo=timezone.utc), plan.starts[0])
        self.assertEqual({'start', 'end'}, set(plan.periods[0]))
        for mapping in (plan.periods[0], plan.grids[0], plan.ranks):
            with self.assertRaises(TypeError): mapping['x'] = 1
        with self.assertRaises((FrozenInstanceError, AttributeError)): plan.aligned = False
        with self.assertRaises((FrozenInstanceError, AttributeError)): del plan.grids
        with patch.object(core, 'calendar_grid', side_effect=AssertionError('rebuilt shared grid')):
            models = [stream.PairedComparisonAccumulator(*BOUNDS, timezone_name='UTC',
                calendar_kind='daily', calendar_plan=plan) for _ in range(3)]
            plan.validate_matches(BOUNDS, 'UTC', 'daily')
        for model in models:
            self.assertIs(plan, model.calendar_plan)
            self.assertIs(plan.grids, model.calendar_plan.grids)
            self.assertIs(plan.grids[0], model.calendar_plan.grids[0])
            with self.assertRaises(AttributeError): model.calendar_plan = None
        self.assertEqual((10, 10), tuple(len(g) for g in plan.grids))

    def test_mismatch_kind_zone_bounds_and_fake_proof_rejected(self):
        plan = core.CalendarComparisonPlan(*BOUNDS, timezone_name='UTC', calendar_kind='daily')
        changes = [(BOUNDS, 'UTC', 'weekly'), (BOUNDS, 'Europe/Warsaw', 'daily'),
                   ((period('2026-01-02T01:00Z', BOUNDS[0]['end']), BOUNDS[1]), 'UTC', 'daily')]
        for periods, zone, kind in changes:
            with self.subTest(zone=zone, kind=kind, periods=periods), self.assertRaises(ValueError):
                stream.PairedComparisonAccumulator(*periods, timezone_name=zone,
                    calendar_kind=kind, calendar_plan=plan)
        for fake in ({'validated': True}, types.SimpleNamespace(validated=True)):
            with self.assertRaises(ValueError):
                stream.PairedComparisonAccumulator(*BOUNDS, timezone_name='UTC',
                    calendar_kind='daily', calendar_plan=fake)

    def test_boundaries_hour_alignment_and_year_limit(self):
        bad = [('2026-01-01T00:00', '2026-01-02T00:00Z'),
               ('2026-01-01T00:30Z', '2026-01-02T00:00Z'),
               ('2026-01-01T00:00Z', '2026-01-01T00:00Z'),
               ('2026-01-02T00:00Z', '2026-01-01T00:00Z'),
               ('2024-01-01T00:00Z', '2025-01-01T01:00Z')]
        for bounds in bad:
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                core.CalendarComparisonPlan(period(*bounds), BOUNDS[1],
                    timezone_name='UTC', calendar_kind='yearly')
        for zone, kind in (('UTC', 'bad'), ('not/a/zone', 'daily')):
            with self.assertRaises((ValueError, KeyError)):
                core.CalendarComparisonPlan(*BOUNDS, timezone_name=zone, calendar_kind=kind)
        leap = period('2024-01-01T00:00Z', '2025-01-01T00:00Z')
        normal = period('2023-01-01T00:00Z', '2024-01-01T00:00Z')
        plan = core.CalendarComparisonPlan(leap, normal, timezone_name='UTC', calendar_kind='yearly')
        self.assertEqual((8784, 8760), tuple(len(g) for g in plan.grids))
        self.assertTrue(plan.aligned)
        self.assertEqual(8760, len(plan.ranks))
        self.assertNotIn(((2, 29), 0, 0, 0), plan.ranks)
        # A 366-day window through a nonleap year repeats Jan 1 yearly frames.
        with self.assertRaises(ValueError):
            core.CalendarComparisonPlan(period('2025-01-01T00:00Z', '2026-01-02T00:00Z'),
                normal, timezone_name='UTC', calendar_kind='yearly')

    def test_troll_actual_order_keeps_two_hour_fold(self):
        bounds = period('2026-10-24T22:00Z', '2026-10-26T00:00Z')
        plan = core.CalendarComparisonPlan(bounds, bounds,
            timezone_name='Antarctica/Troll', calendar_kind='daily')
        first = ((0, 0, 0, 0), (0, 1, 0, 0), (0, 2, 0, 0), (0, 1, 0, 1), (0, 2, 0, 1))
        self.assertEqual(first, plan.common_order[:5])
        self.assertEqual(tuple(range(5)), tuple(plan.ranks[k] for k in first))
        self.assertTrue(plan.aligned)
        self.assertEqual(26, len(plan.grids[0]))

    def test_optional_plan_preserves_full_partial_and_unknown_results(self):
        for partial in (False, True):
            missing = tuple((i, stamp(p['start'])) for i, p in enumerate(BOUNDS)) if partial else ()
            plan = core.CalendarComparisonPlan(*BOUNDS, timezone_name='UTC', calendar_kind='daily')
            for book in (None, tariff()):
                for split in (1, 3, 168):
                    plain, _ = run_pair(book=book, split=split, missing=missing)
                    shared, model = run_pair(plan=plan, book=book, split=split, missing=missing)
                    self.assertEqual(plain, shared)
                    self.assertEqual((9, 18) if partial else (10, 20),
                        (shared['current_kwh'], shared['baseline_kwh']))
                    if book:
                        a = shared['costs']['current']
                        self.assertEqual('6.30' if partial else '7.00', a['usage_cost'])
                        self.assertEqual('0.42', a['fixed_cost'])
                        self.assertEqual(None if partial else '7.42', a['total_cost'])
                    self.assertEqual(0, model.retained_cells)
        missing = ((0, stamp(BOUNDS[0]['start'])), (1, stamp(BOUNDS[1]['start'])+3600000))
        shared, _ = run_pair(plan=plan, book=tariff(), missing=missing)
        self.assertEqual('unknown', shared['status'])
        self.assertEqual(.8, shared['coverage']['common_current'])
        self.assertIsNone(shared['costs']['current']['usage_cost'])

    def test_matched_hook_observes_before_advance_and_preserves_totals(self):
        seen = []
        class Observe(stream.PairedComparisonAccumulator):
            def _matched_frame(self, left, right, frame):
                seen.append((frame, left['frames'][left['head']], right['frames'][right['head']]))
                return super()._matched_frame(left, right, frame)
        model = Observe(*BOUNDS, timezone_name='UTC', calendar_kind='daily')
        for i, side in enumerate(('current', 'baseline')):
            a, b = stamp(BOUNDS[i]['start']), stamp(BOUNDS[i]['end'])
            data = dict(source_ids=['sensor.a'], source_buckets={'sensor.a': [
                {'from': t, 'to': t+3600000, 'kwh': 1} for t in range(a,b,3600000)]},
                measurement_provenance={'sensor.a': dict(method='recorder_hourly_change_v1',
                    unit='kWh', source_unit='kWh', has_sum=True)})
            model.push(side, data, BOUNDS[i]['start'], BOUNDS[i]['end'])
        self.assertEqual(10, len(seen))
        self.assertTrue(all(a == b == c for a,b,c in seen))
        self.assertEqual(10, model.finish()['current_kwh'])


class UsageCostCoreTest(unittest.TestCase):
    def price(self, book, hours, *, eligible=True, constraints=()):
        rates = history.HistoricalTariffRates(book)
        state = core.new_usage_cost_state(rates)
        for when, values in hours:
            core.add_usage_cost(state, stamp(when), values, rates)
        return core.finish_usage_cost(state, eligible=eligible, identity_constraints=constraints), state

    def test_historical_literal_one_point_four_and_one_without_fees(self):
        for day, expected in (('2026-01-02', '1.40'), ('2026-01-01', '1.00')):
            result, state = self.price(tariff(), [(day+'T00:00Z', [1]), (day+'T01:00Z', [1])])
            self.assertEqual(expected, result['usage_cost'])
            self.assertEqual(7200, result['priced_source_seconds']['usage_cost'])
            self.assertEqual(Decimal(2), state['kwh'])
            self.assertFalse(set(result) & {'fees', 'fixed_cost', 'fixed_scope', 'total_cost', 'effective_price'})
        with self.assertRaises(ValueError): core.new_usage_cost_state({'validated': True})
        with self.assertRaises(ValueError): history.HistoricalTariffRates(tariff(version(energy=-1)))

    def test_same_selected_mask_and_partial_price_known_sum(self):
        dynamic = {'intervals': [{'from': '2026-01-02T01:00Z', 'to': '2026-01-02T10:00Z',
                                 'energyRate': .5, 'variableRate': .2}]}
        book = tariff(version(), version('2026-01-02', 'new', profile=profile(dynamic=dynamic)))
        hours = [('2026-01-02T%02d:00Z' % h, [1, 1]) for h in range(1, 10)]
        result, _ = self.price(book, hours)
        self.assertEqual(('9.00','3.60','12.60'), tuple(result[k] for k in ('energy_cost','variable_cost','usage_cost')))
        result, _ = self.price(book, [('2026-01-02T00:00Z', [1,1])]+hours[:-1])
        self.assertIsNone(result['usage_cost'])
        self.assertEqual('8.00', result['known_costs']['energy_cost'])
        self.assertEqual(16*3600, result['priced_source_seconds']['usage_cost'])
        self.assertTrue(result['issues'])
        result, _ = self.price(tariff(), hours, eligible=False)
        self.assertIsNone(result['energy_cost']); self.assertEqual('9.00', result['known_costs']['energy_cost'])

    def test_legacy_mixed_rounds_parts_once_not_hours(self):
        book = tariff(version(energy=.005, variable=None, rate_semantics='legacy_combined'),
                      version('2026-01-02', 'new', .005, .005))
        result, _ = self.price(book, [('2026-01-01T23:00Z', [1]), ('2026-01-02T00:00Z', [1])])
        self.assertEqual('0.03', result['usage_cost'])
        self.assertIsNone(result['energy_cost']); self.assertIsNone(result['variable_cost'])
        self.assertEqual(dict(energy_cost='0.01', variable_cost='0.01', legacy_combined_cost='0.01'), result['known_costs'])
        self.assertEqual(['components', 'legacy_combined'], result['rate_semantics'])
        result, _ = self.price(tariff(version(energy=.005, variable=.005)),
            [('2026-01-01T00:00Z',[1]), ('2026-01-01T01:00Z',[1])])
        self.assertEqual('0.02', result['usage_cost'])

    def test_unknown_variable_zero_negative_and_identity_constraints(self):
        for value in (0, 1):
            result, _ = self.price(tariff(version(variable=None)), [('2026-01-01T00:00Z',[value])])
            self.assertEqual('0.00' if value == 0 else '0.40', result['energy_cost'])
            self.assertIsNone(result['variable_cost']); self.assertIsNone(result['usage_cost'])
            self.assertIn('unknown_variable_cost', result['issues'])
        negative = tariff(version(profile=profile(-.2,.05,allowNegativeEnergy=True)))
        result, _ = self.price(negative, [('2026-01-01T00:00Z',[1,1])])
        self.assertEqual('-0.30', result['usage_cost'])
        book = tariff(version(), version('2026-01-02', 'new', profile=profile(currency='EUR')))
        result, _ = self.price(book, [('2026-01-01T23:00Z',[1]),('2026-01-02T00:00Z',[1])])
        self.assertFalse(result['compatible']); self.assertIsNone(result['usage_cost'])
        self.assertTrue(all(x is None for x in result['known_costs'].values()))
        for constraint in (('EUR','gross'), ('PLN','net')):
            result, _ = self.price(tariff(), [('2026-01-01T00:00Z',[1])], constraints=[constraint])
            self.assertFalse(result['compatible']); self.assertIsNone(result['usage_cost'])

    def test_resolver_validation_once_detaches_and_unknown_history_has_no_price(self):
        book = tariff()
        with patch.object(history, 'validate_tariff_book', wraps=history.validate_tariff_book) as validate:
            rates = history.HistoricalTariffRates(book)
            book['versions'][1]['profile']['zones'][0]['energyRate'] = 99
            states = [core.new_usage_cost_state(rates) for _ in range(3)]
            for state in states:
                for h in range(2):
                    core.add_usage_cost(state, stamp('2026-01-02T%02d:00Z'%h), [1], rates)
                self.assertEqual('1.40', core.finish_usage_cost(state, eligible=True)['usage_cost'])
            self.assertEqual(1, validate.call_count)
        result, _ = self.price(tariff(), [('2024-01-01T00:00Z', [1])])
        self.assertIsNone(result['usage_cost'])
        self.assertIn('unknown_historical_version', result['issues'])
        self.assertEqual(0, result['priced_source_seconds']['usage_cost'])
        result, _ = self.price(tariff(), [('2026-01-02T00:00Z', [])])
        self.assertEqual(0, result['mask_source_seconds'])
        self.assertIsNone(result['usage_cost'])

    def test_large_decimal_period_amounts_ignore_ambient_precision(self):
        with localcontext() as context:
            context.prec = 6
            result, _ = self.price(tariff(version(energy=123456.125, variable=.125)),
                [('2026-01-01T00:00Z', [1]), ('2026-01-01T01:00Z', [1])])
            self.assertEqual(6, context.prec)
        self.assertEqual(('246912.25','0.25','246912.50'),
            tuple(result[k] for k in ('energy_cost','variable_cost','usage_cost')))

    def test_fsum_decimal40_and_detached_final_result(self):
        rates = history.HistoricalTariffRates(tariff(version(energy=.005,variable=.005)))
        state = core.new_usage_cost_state(rates)
        with localcontext() as context:
            context.prec = 6
            for h in range(2):
                core.add_usage_cost(state, stamp('2026-01-01T%02d:00Z'%h), [.1,.2], rates)
            result = core.finish_usage_cost(state, eligible=True)
            self.assertEqual(6, context.prec)
        self.assertEqual(Decimal(str(fsum([.1,.2])))*2, state['kwh'])
        self.assertEqual('0.00', result['usage_cost'])
        result['known_costs'].clear(); result['priced_source_seconds'].clear(); result['issues'].append('mutated')
        again = core.finish_usage_cost(state, eligible=True)
        self.assertEqual('0.00', again['known_costs']['energy_cost'])
        self.assertEqual(4*3600, again['priced_source_seconds']['usage_cost'])
        self.assertNotIn('mutated', again['issues'])


    def test_one_validated_hour_resolves_once_for_many_projected_groups(self):
        rates = history.HistoricalTariffRates(tariff())
        when = stamp('2026-01-02T00:00Z')
        with patch.object(rates, 'resolve_interval', wraps=rates.resolve_interval) as resolve:
            hour = core.UsageCostHour(when, rates)
            states = [core.new_usage_cost_state(rates) for _ in range(514)]
            for state in states:
                hour.add(state, [1])
                self.assertEqual('0.70', core.finish_usage_cost(state, eligible=True)['usage_cost'])
            self.assertEqual(1, resolve.call_count)
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            hour.stamp = 0
        with self.assertRaises(ValueError): core.UsageCostHour(when, {'validated': True})
        for invalid in (True, float('inf'), when+1, '2026-01-02T00:00Z'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                core.UsageCostHour(invalid, rates)

    def test_hour_helper_and_scalar_use_identical_legacy_and_missing_price_arithmetic(self):
        for book in (tariff(), tariff(version(variable=None)),
                     tariff(version(energy=.005, variable=None, rate_semantics='legacy_combined'))):
            rates = history.HistoricalTariffRates(book)
            a, b = core.new_usage_cost_state(rates), core.new_usage_cost_state(rates)
            for h in range(2):
                when = stamp('2026-01-02T%02d:00Z'%h)
                core.add_usage_cost(a, when, [.1,.2], rates)
                core.UsageCostHour(when, rates).add(b, [.1,.2])
            self.assertEqual(a, b)
            self.assertEqual(core.finish_usage_cost(a, eligible=True), core.finish_usage_cost(b, eligible=True))


if __name__ == '__main__':
    unittest.main()
