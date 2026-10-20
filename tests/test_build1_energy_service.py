"""Focused K4 regressions. Owner executes through the authorized FIFO lane."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_energy_analysis_service import Harness, service

inputs = importlib.import_module(service.__package__ + '.energy_inputs')
native_model = importlib.import_module(service.__package__ + '.native_energy_pricing')
UTC = timezone.utc


class Build1ServiceTest(unittest.TestCase):
    def test_grid_with_solar_keeps_import_but_does_not_price_site_consumers(self):
        async def run():
            h = Harness(); h.now = datetime(2026,1,11,12,tzinfo=UTC)
            h.manager.data['energy_sources'].append({'type':'solar','stat_energy_from':'sensor.pv'})
            with h.patches():
                result = await h.subject.async_get_build1_model('day','user',date='2026-01-10')
                model = result['report_model']
                self.assertEqual(240,model['totals']['import_kwh'])
                self.assertEqual(48,model['devices'][0]['kwh'])
                self.assertIsNone(model['unmeasured']['kwh'])
                self.assertIsNone(model['devices'][0]['cost'])
                self.assertTrue(await h.subject.async_validate_result(result,'user'))
        asyncio.run(run())

    def test_collector_supplies_actual_previous_ghosts_without_counting_them_as_import(self):
        async def run():
            h = Harness(); h.now = datetime(2026,1,11,12,tzinfo=UTC)
            with h.patches():
                result = await h.subject.async_get_build1_model('day','user')
                model = result['report_model']
                self.assertEqual(120,model['totals']['import_kwh'])
                self.assertEqual(10,model['hourly'][12]['prev_kwh'])
                self.assertIsNone(model['hourly'][12]['kwh'])
                self.assertTrue(model['hourly'][12]['future'])
                self.assertEqual(120,model['daily'][0]['kwh'])
                self.assertEqual(240,model['daily'][0]['prev_kwh'])
                self.assertTrue(await h.subject.async_validate_result(result,'user'))
        asyncio.run(run())

    def test_default_dto_unchanged_opt_in_envelope_calendar_and_actor(self):
        async def run():
            h = Harness(); h.now = datetime(2026, 1, 11, 12, tzinfo=UTC)
            with h.patches():
                legacy = await h.subject.async_get_analysis('day', 'user', anchor='2026-01-10')
                self.assertNotIn('report_model', legacy)
                result = await h.subject.async_get_build1_model('day', 'user', date='2026-01-10', mode='full')
                self.assertEqual(legacy['period'], result['period'])
                self.assertEqual('ha_tools.energy_report/1', result['report_model']['schema'])
                self.assertEqual(240, result['report_model']['totals']['import_kwh'])
                self.assertEqual(legacy['totals'], result['totals'])
                self.assertTrue(h.subject.context_is_current(result))
                self.assertTrue(await h.subject.async_validate_result(result, 'user'))
                with self.assertRaises(PermissionError):
                    await h.subject.async_validate_result(result, 'different-user')
                self.assertNotEqual(legacy['snapshot_id'], result['snapshot_id'])
        asyncio.run(run())

    def test_current_cutoff_real_partial_cell_full_calendar_and_no_synthetic_history(self):
        async def run():
            h = Harness(); h.now = datetime(2026, 1, 11, 12, 7, tzinfo=UTC)
            async def short(subject, configuration, start, end, guard):
                await guard()
                self.assertEqual(300, (end-start).total_seconds())
                return {'sensor.grid': '0.7', 'sensor.device': '0.2'}
            with h.patches(), patch.object(inputs, 'async_short_hour', short):
                result = await h.subject.async_get_build1_model('day', 'user')
                model = result['report_model']
                self.assertEqual('2026-01-11T12:00:00+00:00', result['period']['end'])
                self.assertEqual('2026-01-12T00:00:00+00:00', model['period']['end'])
                self.assertEqual(120.7, model['totals']['import_kwh'])
                self.assertTrue(model['hourly'][12]['partial'])
                self.assertTrue(model['hourly'][13]['future'])
                self.assertEqual(7, next(c for c in model['comparisons'] if c['key'] == 'avg_7d')['denominator'])
                self.assertTrue(h.subject.context_is_current(result))
                h.now += timedelta(minutes=5)
                self.assertFalse(h.subject.context_is_current(result))
        asyncio.run(run())

    def test_permission_and_revision_during_extra_history_reads_fail_closed(self):
        async def run():
            for mutate in ('permission', 'revision'):
                h = Harness(); h.now = datetime(2026, 1, 11, 12, tzinfo=UTC)
                async def on_read(ids, start, end):
                    # Extra 30-day history begins before the selected Jan 10.
                    if start < datetime(2026, 1, 1, tzinfo=UTC):
                        if mutate == 'permission':
                            h.user.is_active = False
                        else:
                            h.registry.revision += 1
                h.on_read = on_read
                with self.subTest(mutate=mutate), h.patches():
                    with self.assertRaises((RuntimeError, PermissionError)):
                        await h.subject.async_get_build1_model('day', 'user', date='2026-01-10')
                    self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_colors_daily_actual_today_historical_period_and_cache_detached(self):
        async def run():
            h = Harness(); h.now = datetime(2026, 1, 11, 12, tzinfo=UTC)
            with h.patches():
                first = await h.subject.async_get_build1_model('day', 'user', date='2026-01-10')
                self.assertEqual('2026-01-11', first['report_model']['provenance']['color_as_of'])
                before = len(h.queries)
                first['report_model']['totals']['import_kwh'] = -1
                again = await h.subject.async_get_build1_model('day', 'user', date='2026-01-10')
                self.assertEqual(before, len(h.queries))
                self.assertEqual(240, again['report_model']['totals']['import_kwh'])
                # A second historical period reuses the daily identity colors;
                # it must not perform a new 30-day scan.
                before = len([q for q in h.queries if (q[2]-q[1]).days == 7 and 'sensor.device' in q[0]])
                await h.subject.async_get_build1_model('day', 'user', date='2026-01-09')
                after = len([q for q in h.queries if (q[2]-q[1]).days == 7 and 'sensor.device' in q[0]])
                self.assertEqual(before, after)
        asyncio.run(run())

    def test_first_five_minutes_work_without_inventing_a_completed_hour(self):
        async def run():
            h = Harness(); h.now = datetime(2026, 1, 11, 0, 7, tzinfo=UTC)
            async def short(subject, configuration, start, end, guard):
                await guard()
                self.assertEqual(300, (end-start).total_seconds())
                return {'sensor.grid':'0.7','sensor.device':'0.2'}
            with h.patches(), patch.object(inputs,'async_short_hour',short):
                with self.assertRaises(ValueError):
                    await h.subject.async_get_analysis('day','user')
                result = await h.subject.async_get_build1_model('day','user')
                model = result['report_model']
                self.assertEqual(0,result['period']['hours'])
                self.assertEqual(.7,model['totals']['import_kwh'])
                self.assertEqual(1,model['coverage']['measured_hours'])
                self.assertTrue(model['hourly'][0]['partial'])
                self.assertTrue(model['hourly'][1]['future'])
                self.assertTrue(h.subject.context_is_current(result))
                self.assertTrue(await h.subject.async_validate_result(result,'user'))
        asyncio.run(run())

    def test_invalid_mode_and_external_report_window_rejected_before_io(self):
        async def run():
            h = Harness()
            with h.patches():
                with self.assertRaises(ValueError):
                    await h.subject.async_get_build1_model('day', 'user', mode='arbitrary')
                self.assertEqual([], h.queries)
        asyncio.run(run())

    def test_colors_qualified_root_identity_uses_30day_evidence(self):
        totals = {'small': [Decimal(1), 720], 'large': [Decimal(4), 720],
                  'hole': [Decimal(10000), 647], 'third': [Decimal(3), 720]}
        colors = inputs.identity_colors(totals, 720, '2026-10-20')
        self.assertEqual({'large': 'd1', 'third': 'd2', 'small': 'd3'}, colors['devices'])

    def test_short_statistics_units_duplicate_holes_and_actual_values(self):
        start = datetime(2026, 1, 11, 12, tzinfo=UTC)
        end = start + timedelta(minutes=10)
        metadata = {'sensor.grid': {'has_sum': True, 'unit_class': 'energy', 'unit_of_measurement': 'Wh'}}
        points = [{'start': start.timestamp(), 'change': 100},
                  {'start': (start+timedelta(minutes=5)).timestamp(), 'change': 250}]
        normalize = lambda raw: inputs.normalize_short_hour(raw, metadata, ['sensor.grid'], start, end)
        self.assertEqual({'sensor.grid': '0.350'}, normalize({'sensor.grid': points}))
        self.assertEqual({'sensor.grid': None}, normalize({'sensor.grid': points+points[:1]}))
        self.assertEqual({'sensor.grid': None}, normalize({'sensor.grid': points[:1]}))
        self.assertEqual({'sensor.grid': None}, normalize({'sensor.grid': [{'start': start.timestamp(), 'change': -1}]}))

    def test_native_facts_reuse_accumulator_duplicates_and_default_contract(self):
        start = datetime(2026, 1, 11, tzinfo=UTC); end = start+timedelta(hours=2)
        configuration = {'valid': True, 'currency': 'PLN', 'sources': [
            {'energy_id': 'sensor.grid', 'cost_statistic_id': 'sensor.cost'}]}
        period = {'start': start.isoformat(), 'end': end.isoformat()}
        metadata = {'sensor.cost': {'has_sum': True, 'unit_class': 'monetary', 'unit_of_measurement': 'PLN'}}
        points = [{'start': start.timestamp(), 'change': .12345},
                  {'start': (start+timedelta(hours=1)).timestamp(), 'change': .98765}]
        plain = native_model.NativeCostAccumulator(configuration, period)
        projected = native_model.NativeCostAccumulator(configuration, period, include_report_hours=True)
        for subject in (plain, projected):
            subject.add_chunk({'sensor.cost': points}, metadata, start, end)
        self.assertEqual(plain.finish(), projected.finish())
        facts = projected.report_hours()
        self.assertEqual('0.12345', facts['hours'][0]['usage_cost'])
        facts['hours'][0]['usage_cost'] = 'mutated'
        self.assertEqual('0.12345', projected.report_hours()['hours'][0]['usage_cost'])
        duplicate = native_model.NativeCostAccumulator(configuration, period, include_report_hours=True)
        duplicate.add_chunk({'sensor.cost': points+points[:1]}, metadata, start, end)
        self.assertIsNone(duplicate.report_hours()['hours'][0]['usage_cost'])
        self.assertEqual('unknown', projected.finish()['period_cost']['fixed_fees_status'])
