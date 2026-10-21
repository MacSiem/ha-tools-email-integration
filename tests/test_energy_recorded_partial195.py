"""Known source readings remain inspectable without becoming a full balance."""
import copy
import unittest
from tests.test_energy_measurements import measurements, readings
from tests.test_build1_energy_model import model, inputs_for
from tests.test_energy_registry import registry


class RecordedPartialTest(unittest.TestCase):
    def test_source_subtotal_retains_a_missing_household_hour(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        snapshot = readings(start, end, missing=(('sensor.b', 1),))
        before = copy.deepcopy(snapshot)
        evidence = measurements.measurement_evidence(snapshot, start, end)
        self.assertFalse(evidence['coverage']['complete'])
        self.assertIsNone(evidence['kwh'])
        self.assertEqual(2, evidence['recorded_household_kwh'])
        self.assertEqual(3, evidence['observed_source_kwh'])
        rows = evidence['hourly_buckets']
        self.assertIsNone(rows[1]['kwh'])
        self.assertEqual([2, 1], [row['recorded_kwh'] for row in rows])
        self.assertEqual([2, 1], [row['measured_sources'] for row in rows])
        self.assertEqual([2, 2], [row['expected_sources'] for row in rows])
        self.assertEqual(before, snapshot)

    def test_no_reading_stays_null_while_measured_zero_stays_zero(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T01:00Z'
        for value in (None, 0):
            with self.subTest(value=value):
                evidence = measurements.measurement_evidence(readings(start, end, value=value), start, end)
                self.assertEqual(value, evidence['hourly_buckets'][0]['recorded_kwh'])

    def test_model_keeps_known_source_subtotal_separate_from_qualified_import(self):
        inputs = inputs_for()
        for row in inputs['projection']['hours']:
            row.update(recorded_kwh='1', measured_sources=2, expected_sources=2)
        row = inputs['projection']['hours'][0]
        row.update(kwh=None, recorded_kwh='.4', measured_sources=1)
        before = copy.deepcopy(inputs)
        result = model.build_report(inputs)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(23, result['totals']['import_kwh'])
        self.assertEqual(23.4, result['totals']['recorded_import_kwh'])
        self.assertIsNone(result['hourly'][0]['kwh'])
        self.assertEqual(.4, result['hourly'][0]['recorded_kwh'])
        self.assertEqual(1, result['hourly'][0]['measured_sources'])
        self.assertIsNone(result['devices'][0]['share'])
        self.assertEqual(before, inputs)

    def test_device_scoped_entity_names_include_the_native_device_name(self):
        data = {'entities': {
            'sensor.a': {'device_id':'a','has_entity_name':True,'original_name':'Energy'},
            'sensor.b': {'device_id':'b','has_entity_name':True,'original_name':'Energy'},
            'sensor.explicit': {'device_id':'a','has_entity_name':True,'name':'My meter','original_name':'Energy'},
            'sensor.legacy': {'device_id':'a','has_entity_name':False,'original_name':'Independent name'}},
            'devices': {'a': {'name':'Factory A','name_by_user':'Kitchen plug'}, 'b': {'name':'Office plug'}}}
        result = registry.resolve_membership(list(data['entities']), data, captured_at='now', revision=1)
        self.assertEqual('Kitchen plug Energy', result['sources']['sensor.a']['name'])
        self.assertEqual('Office plug Energy', result['sources']['sensor.b']['name'])
        self.assertEqual('My meter', result['sources']['sensor.explicit']['name'])
        self.assertEqual('Independent name', result['sources']['sensor.legacy']['name'])
