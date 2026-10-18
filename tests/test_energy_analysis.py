"""A single immutable household calculation assembled from bounded chunks."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version, tariff

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('analysis_fixture'); package.__path__ = [str(ROOT)]
with patch.dict(sys.modules, {package.__name__: package}):
    spec = importlib.util.spec_from_file_location(package.__name__ + '.energy_analysis', ROOT / 'energy_analysis.py')
    analysis = importlib.util.module_from_spec(spec); spec.loader.exec_module(analysis)


class EnergyAnalysisTest(unittest.TestCase):
    def builder(self, b='configured', context=None, period_extra=None):
        if b == 'configured':
            b = book([version(monthly=31)])
            b['tariffs'].append(tariff([version(rate=.5, monthly=31)], 'alternative'))
        period = {'start': '2026-01-01T00:00Z', 'end': '2026-01-01T02:00Z', 'timezone': 'UTC', 'kind': 'day'}
        period.update(period_extra or {})
        return analysis.EnergyAnalysisAccumulator(b, period,
            context if context is not None else {'entry_id': 'synthetic', 'tariff_revision': 4, 'registry_revision': 2},
            generated_at=datetime(2026, 1, 1, 2, 34, tzinfo=timezone.utc))

    def add(self, builder, start, end, value):
        return builder.add_chunk(readings(start, end, value=value, sources=('sensor.grid',)), start, end)

    def test_two_chunks_price_once_with_identical_alternatives_and_calendar_fees(self):
        builder = self.builder()
        self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        self.add(builder, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 2)
        result = builder.finish()
        self.assertEqual(2, result['schema'])
        self.assertEqual('ready', result['status'])
        self.assertEqual(Decimal(3), Decimal(result['totals']['kwh']))
        self.assertEqual('3.00', result['totals']['energy_cost'])
        self.assertEqual('0.08', result['totals']['fixed_cost'])
        self.assertEqual('3.08', result['totals']['total_cost'])
        self.assertEqual('active', result['active_tariff_id'])
        self.assertEqual('0.08', result['fees']['components']['subscription'])
        other = next(row for row in result['alternatives'] if row['tariff_id'] == 'alternative')
        self.assertEqual('1.58', other['total_cost'])
        self.assertEqual('-1.50', other['delta_cost'])
        self.assertEqual(result['measurement_fingerprint'], other['measurement_fingerprint'])
        self.assertEqual(7200, result['coverage']['expected_source_seconds'])
        self.assertEqual(7200, result['coverage']['measured_source_seconds'])
        original_id = result['snapshot_id']
        result['totals']['energy_cost'] = 'tampered'
        again = builder.finish()
        self.assertEqual('3.00', again['totals']['energy_cost'])
        self.assertEqual(original_id, again['snapshot_id'])
        with self.assertRaises(ValueError):
            self.add(builder, '2026-01-01T02:00Z', '2026-01-01T03:00Z', 1)

    def test_missing_stays_unknown_but_calendar_fees_and_known_costs_remain(self):
        builder = self.builder()
        self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', None)
        self.add(builder, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 2)
        result = builder.finish()
        self.assertEqual('partial', result['status'])
        self.assertIsNone(result['totals']['kwh'])
        self.assertIsNone(result['totals']['total_cost'])
        self.assertEqual('0.08', result['totals']['fixed_cost'])
        self.assertEqual('2.00', result['totals']['known_costs']['energy_cost'])
        self.assertEqual(.5, result['coverage']['ratio'])
        self.assertIsNone(result['buckets'][0]['kwh'])

    def test_no_tariff_keeps_measured_energy_without_a_fictional_zero_bill(self):
        builder = self.builder(None)
        self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        self.add(builder, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 2)
        result = builder.finish()
        self.assertEqual(Decimal(3), Decimal(result['totals']['kwh']))
        self.assertIsNone(result['totals']['total_cost'])
        self.assertEqual('not_configured', result['pricing_status'])
        self.assertEqual([], result['alternatives'])

    def test_gap_overlap_or_changed_sources_reject_without_poisoning_prior_chunks(self):
        builder = self.builder()
        self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        with self.assertRaises(ValueError):
            builder.finish()
        with self.assertRaises(ValueError):
            self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        changed = readings('2026-01-01T01:00Z', '2026-01-01T02:00Z', sources=('sensor.other',))
        with self.assertRaisesRegex(ValueError, 'energy_sources_changed'):
            builder.add_chunk(changed, '2026-01-01T01:00Z', '2026-01-01T02:00Z')
        self.add(builder, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 2)
        self.assertEqual('3.08', builder.finish()['totals']['total_cost'])

    def test_unknown_context_fields_are_not_reflected_to_household_readers(self):
        builder = self.builder(context={'entry_id': 'synthetic', 'tariff_revision': 4,
            'registry_revision': 2, 'smtp_config': {'password': '!secret synthetic_smtp'},
            'unrelated_private_state': ['not a public analysis field']})
        self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        self.add(builder, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 2)
        result = builder.finish()
        self.assertNotIn('smtp_config', result['context'])
        self.assertNotIn('unrelated_private_state', result['context'])
        self.assertEqual('synthetic', result['context']['entry_id'])

    def test_rate_and_period_extensions_are_preserved_in_config_but_not_reflected(self):
        b = book([version(monthly=31)])
        p = b['tariffs'][0]['versions'][0]['profile']
        p['future_internal'] = {'reference': '!secret synthetic'}
        p['zones'][0]['future_internal'] = {'keep_in_config': True}
        p['clock']['future_internal'] = {'keep_in_config': True}
        p['rules'] = [{'zone': 'day', 'intervals': [{'from': '00:00', 'to': '24:00',
                                                     'future_internal': {'keep': True}}]}]
        original = copy.deepcopy(b)
        builder = self.builder(b, period_extra={'unrelated_private_state': {'keep_in_config': True}})
        self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        self.add(builder, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 2)
        result = builder.finish()
        rate = result['rates'][0]
        self.assertNotIn('future_internal', rate['profile'])
        self.assertNotIn('future_internal', rate['profile']['zones'][0])
        self.assertNotIn('future_internal', rate['profile']['rules'][0]['intervals'][0])
        self.assertNotIn('future_internal', rate['clock'])
        self.assertNotIn('unrelated_private_state', result['period'])
        self.assertEqual(original, b)

    def test_chunk_boundaries_do_not_round_costs_or_fees_early(self):
        b = book([version(rate=.005, monthly=62)])
        b['tariffs'][0]['versions'][0]['profile']['zones'][0]['variableRate'] = .005
        builder = self.builder(b)
        self.add(builder, '2026-01-01T00:00Z', '2026-01-01T01:00Z', 1)
        self.add(builder, '2026-01-01T01:00Z', '2026-01-01T02:00Z', 1)
        result = builder.finish()
        self.assertEqual('0.01', result['totals']['energy_cost'])
        self.assertEqual('0.01', result['totals']['variable_cost'])
        self.assertEqual('0.17', result['totals']['fixed_cost'])
        self.assertEqual('0.19', result['totals']['total_cost'])

    def test_actual_year_is_assembled_from_fifty_three_bounded_chunks(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2027, 1, 1, tzinfo=timezone.utc)
        builder = analysis.EnergyAnalysisAccumulator(book([version(monthly=31)]),
            {'start': start.isoformat(), 'end': end.isoformat(), 'timezone': 'UTC', 'kind': 'year'},
            {'entry_id': 'synthetic', 'tariff_revision': 4}, generated_at=end)
        cursor, chunks = start, 0
        while cursor < end:
            stop = min(cursor + timedelta(days=7), end)
            self.add(builder, cursor.isoformat(), stop.isoformat(), 1)
            cursor, chunks = stop, chunks + 1
        result = builder.finish()
        self.assertEqual(53, chunks)
        self.assertEqual(8760 * 3600, result['coverage']['expected_source_seconds'])
        self.assertEqual('8760.00', result['totals']['energy_cost'])
        self.assertEqual('372.00', result['totals']['fixed_cost'])
        self.assertEqual('9132.00', result['totals']['total_cost'])
