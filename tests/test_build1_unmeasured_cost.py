"""R4/R6 monetary residuals require a common verified hour mask.

No HA IO; reuse the existing priced-input fixture, never reprice or allocate.
"""
import copy
import unittest

from tests.test_build1_energy_model import inputs_for, model


def residual_copies(result):
    return [result['unmeasured'],
            next(row for row in result['ranking'] if row['id'] == '_unmeasured'),
            next(row for row in result['rooms'] if row['area_id'] == '_unmeasured'),
            next(row for row in result['floors'] if row['floor_id'] == '_unmeasured')]


class Build1UnmeasuredCostTests(unittest.TestCase):
    def build_unchanged(self, inputs):
        before = copy.deepcopy(inputs)
        result = model.build_report(inputs)
        self.assertEqual(before, inputs)
        return result

    def assert_cost_unknown_everywhere(self, result):
        for row in residual_copies(result):
            self.assertIsNone(row['cost'])

    def test_actual_partial_grid_and_independently_valid_device_cannot_produce_negative_cost(self):
        inputs = inputs_for()
        inputs['projection']['currency'] = 'EUR'
        for row in inputs['projection']['hours']:
            row['currency'] = 'EUR'
            row['kwh'] = None
            row['devices']['parent'] = {'kwh': None, 'costs': {}}
        hours = inputs['projection']['hours']
        hours[-1]['kwh'] = '25'
        hours[-1]['costs'] = {'energy_cost': '12.5', 'variable_cost': '2.5', 'usage_cost': '15'}
        hours[0]['devices']['parent'] = {'kwh': '44', 'costs': {'usage_cost': '40.4'}}
        result = self.build_unchanged(inputs)
        self.assertEqual(1, result['coverage']['measured_hours'])
        self.assertEqual(25, result['totals']['import_kwh'])
        self.assertEqual(15, result['totals']['usage_cost'])
        self.assertEqual(44, result['devices'][0]['kwh'])
        self.assertEqual(40.4, result['devices'][0]['cost'])
        self.assertEqual(-19, result['unmeasured']['raw_balance_kwh'])
        self.assertEqual('unknown', result['unmeasured']['status'])
        self.assertIsNone(result['unmeasured']['kwh'])
        self.assert_cost_unknown_everywhere(result)

    def test_partial_residual_quantity_subtotal_does_not_authorize_period_residual_cost(self):
        inputs = inputs_for()
        inputs['projection']['hours'][0]['kwh'] = None
        result = self.build_unchanged(inputs)
        self.assertEqual('unknown', result['unmeasured']['status'])
        self.assertAlmostEqual(9.2, result['unmeasured']['kwh'])
        self.assertAlmostEqual(14.4, result['devices'][0]['kwh'])
        self.assertAlmostEqual(7.2, result['devices'][0]['cost'])
        self.assert_cost_unknown_everywhere(result)

    def test_known_quantity_with_missing_grid_or_root_usage_price_stays_unknown_money(self):
        for missing in ('grid', 'root'):
            with self.subTest(missing=missing):
                inputs = inputs_for()
                row = inputs['projection']['hours'][0]
                if missing == 'grid':
                    row['costs']['usage_cost'] = None
                else:
                    row['devices']['parent']['costs']['usage_cost'] = None
                result = self.build_unchanged(inputs)
                self.assertEqual('ok', result['unmeasured']['status'])
                self.assertAlmostEqual(9.6, result['unmeasured']['kwh'])
                self.assert_cost_unknown_everywhere(result)

    def test_usage_only_native_price_is_sufficient_without_inventing_components(self):
        inputs = inputs_for()
        for row in inputs['projection']['hours']:
            row['costs'] = {'usage_cost': '.5'}
        result = self.build_unchanged(inputs)
        self.assertEqual('ok', result['unmeasured']['status'])
        self.assertAlmostEqual(4.8, result['unmeasured']['cost'])
        self.assertIsNone(result['totals']['energy_cost'])
        self.assertIsNone(result['totals']['variable_cost'])

    def test_double_counting_hides_cost_even_if_total_period_balance_is_positive(self):
        inputs = inputs_for()
        inputs['projection']['hours'][0]['devices']['parent']['kwh'] = '1.050001'
        result = self.build_unchanged(inputs)
        self.assertEqual('double_counting', result['unmeasured']['status'])
        self.assertGreater(result['unmeasured']['raw_balance_kwh'], 0)
        self.assert_cost_unknown_everywhere(result)
        self.assertAlmostEqual(7.2, result['devices'][0]['cost'])

    def test_unsupported_consumer_domain_cannot_authorize_a_residual(self):
        inputs = inputs_for()
        inputs['consumer_domain'] = 'site_consumption'
        result = self.build_unchanged(inputs)
        self.assertEqual('unknown', result['unmeasured']['status'])
        self.assertIsNone(result['unmeasured']['kwh'])
        self.assert_cost_unknown_everywhere(result)

    def test_unsupported_energy_unit_remains_unknown_without_destroying_other_devices(self):
        # Exercise the existing unit normalizer, then pass its unknown cell to
        # model/1. The model consumes verified kWh; it must not normalize again.
        from tests.test_build1_energy_native_short import short, SID, raw_at
        from tests.test_energy_snapshot import at
        cells = short.normalized_cells(raw_at('2026-10-18T19:00Z', (100,)),
            {SID: {'unit_of_measurement': 'W', 'has_sum': True}}, [SID],
            at('2026-10-18T19:00Z'), at('2026-10-18T19:05Z'))
        self.assertIsNone(cells[0]['kwh'])
        inputs = inputs_for()
        inputs['consumers'].append({'stat_consumption': 'unsupported'})
        for row in inputs['projection']['hours']:
            row['devices']['unsupported'] = {'kwh': cells[0]['kwh'], 'costs': {'usage_cost': '99'}}
        result = self.build_unchanged(inputs)
        valid = next(row for row in result['devices'] if row['id'] == 'parent')
        self.assertAlmostEqual(14.4, valid['kwh'])
        self.assertAlmostEqual(7.2, valid['cost'])
        self.assert_cost_unknown_everywhere(result)

    def test_complete_and_zero_balances_keep_exact_existing_arithmetic(self):
        for zero in (False, True):
            with self.subTest(zero=zero):
                inputs = inputs_for()
                if zero:
                    for row in inputs['projection']['hours']:
                        row['devices']['parent'] = {'kwh': '1', 'costs': {'usage_cost': '.5'}}
                result = self.build_unchanged(inputs)
                self.assertTrue(result['coverage']['complete'])
                self.assertEqual('ok', result['unmeasured']['status'])
                for row in residual_copies(result):
                    self.assertAlmostEqual(0 if zero else 9.6, row['kwh'])
                    self.assertAlmostEqual(0 if zero else 4.8, row['cost'])

    def test_complete_sofar_window_does_not_require_finished_calendar_day(self):
        inputs = inputs_for()
        inputs['measured_until'] = inputs['projection']['hours'][1]['to']
        inputs['projection']['hours'] = inputs['projection']['hours'][:2]
        result = self.build_unchanged(inputs)
        self.assertFalse(result['period']['complete'])
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual('ok', result['unmeasured']['status'])
        self.assertAlmostEqual(.8, result['unmeasured']['kwh'])
        self.assertAlmostEqual(.4, result['unmeasured']['cost'])

    def test_mixed_currency_and_absent_grid_keep_money_unknown(self):
        for mode in ('mixed', 'absent'):
            with self.subTest(mode=mode):
                inputs = inputs_for()
                if mode == 'mixed':
                    inputs['projection']['hours'][0]['currency'] = 'EUR'
                else:
                    for row in inputs['projection']['hours']:
                        row['kwh'] = None
                result = self.build_unchanged(inputs)
                self.assert_cost_unknown_everywhere(result)
