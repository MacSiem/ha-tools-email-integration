"""Partial source totals are real observations, never a household balance."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from tests.test_energy_analysis import analysis
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version
from tests.test_energy_partial_sources_20261104 import model, mail, sample


class KnownProjectionTest(unittest.TestCase):
    def test_43_of_44_through_analysis_projection_and_mail(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        ids = tuple('sensor.s%d' % i for i in range(44))
        data = readings(start, end, sources=ids, missing=[(ids[-1], 0), (ids[-1], 1)])
        acc = analysis.EnergyAnalysisAccumulator(book([version(rate=1, monthly=31)]),
            {'start': start, 'end': end, 'timezone': 'UTC', 'kind': 'day'}, {},
            generated_at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
            consumers=[], membership={}, include_report_hours=True)
        acc.add_chunk(data, start, end)
        result = acc.finish()
        self.assertFalse(result['coverage']['complete'])
        self.assertIsNone(result['totals']['kwh'])
        self.assertEqual(Decimal('86'), Decimal(result['known_totals']['kwh']))
        self.assertEqual('86.00', result['known_totals']['usage_cost'])
        self.assertEqual('86.08', result['known_totals']['total_cost'])
        self.assertEqual('43.0', result['known_chart']['bins'][0]['kwh'])
        self.assertFalse(result['known_chart']['bins'][0]['complete'])
        projection = acc.report_projection()
        projection['hours'][0]['recorded_cost'] = '43'
        projection['hours'][1]['recorded_cost'] = '43'
        report = model.build_report({'period': {'kind': 'day', 'start': start, 'end': end, 'timezone': 'UTC'},
            'projection': projection, 'consumers': [], 'membership': {}})
        self.assertEqual(86, report['totals']['import_kwh'])
        self.assertEqual(86, report['totals']['usage_cost'])
        self.assertAlmostEqual(86 + 1/12, report['totals']['total_with_fixed'])
        self.assertIsNone(report['unmeasured']['kwh'])
        self.assertEqual(86, report['zones'][0]['kwh'])
        for lang in ('pl', 'en'):
            out = mail.render(report, language=lang, timezone_name='UTC', hour_cycle='24')
            self.assertIn('86', out['text'])
            self.assertNotIn(' ·  · ', out['text'])

    def test_comparison_uses_paired_source_mask_and_matching_chart(self):
        data = sample(mode='partial')
        start = data['period']['start']
        from datetime import datetime
        stamp = int(datetime.fromisoformat(start).timestamp() * 1000)
        paired = {'status': 'ready', 'comparison_scope': 'common_measurements',
            'current_kwh': 24, 'baseline_kwh': 12, 'coverage': {'common_current': .5},
            'chart': {'markers': [{'status': 'ready', 'current_from': stamp, 'baseline_kwh': '0.5'}]}}
        data['comparisons'] = [{'key': 'previous_day', 'paired': paired}]
        result = model.build_report(data)
        self.assertEqual(100, result['comparisons'][0]['delta_pct'])
        self.assertEqual(.5, result['hourly'][0]['prev_kwh'])
        self.assertIsNone(result['hourly'][1]['prev_kwh'])

    def test_partial_period_subject_never_says_zero_sources_are_missing(self):
        report = model.build_report(sample(mode='intermittent'))
        report['coverage'].update(measured_sources=2, expected_sources=2)
        for lang in ('pl', 'en'):
            result = mail.render(report, language=lang, timezone_name='UTC', hour_cycle='24')
            self.assertIn('niepełne pokrycie' if lang == 'pl' else 'partial coverage', result['subject'])
            self.assertNotIn('bez 0' if lang == 'pl' else '0 missing', result['subject'])

    def test_device_purchase_price_is_not_a_grid_residual(self):
        data = sample(mode='full')
        data['consumer_domain'] = 'site_consumption'
        for row in data['projection']['hours']:
            row['devices']['a']['costs'] = {'usage_cost': '.5'}
        result = model.build_report(data)
        self.assertEqual('purchase_price_excluding_pv', result['device_cost_basis'])
        self.assertEqual(12, next(row for row in result['devices'] if row['id'] == 'a')['cost'])
        self.assertIsNone(result['unmeasured']['kwh'])
        for lang in ('pl', 'en'):
            out = mail.render(result, language=lang, timezone_name='UTC', hour_cycle='24')
            for part in ('html', 'text'):
                self.assertIn('szacunkowe' if lang == 'pl' else 'estimates', out[part])
                self.assertIn('bez uwzględnienia PV' if lang == 'pl' else 'excluding solar', out[part])

    def test_source_lifecycle_from_recorder_first_observation(self):
        from tests.test_energy_measurements import measurements
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        data = readings(start, end, sources=('sensor.a', 'sensor.b'), missing=[('sensor.b', 0)])
        first = int(datetime(2026, 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        data['source_first'] = {'sensor.a': 0, 'sensor.b': first}
        evidence = measurements.measurement_evidence(data, start, end)
        self.assertTrue(evidence['coverage']['complete'])
        self.assertEqual([1, 2], [row['expected_sources'] for row in evidence['hourly_buckets']])
        self.assertEqual([1, 2], [row['kwh'] for row in evidence['hourly_buckets']])
        self.assertEqual(3 * 3600, evidence['coverage']['expected_source_seconds'])
        data = readings(start, end, sources=('sensor.a',))
        self.assertEqual(1, len(measurements.measurement_evidence(data, start, end)['source_ids']))

    def test_report_cutoff_retains_a_recorder_cycle(self):
        from tests.test_energy_analysis_service import service
        from importlib import import_module
        inputs = import_module(service.__package__ + '.energy_inputs')
        self.assertEqual('2026-01-01T11:00:00+00:00',
            inputs.report_clock(datetime(2026, 1, 1, 11, 5, tzinfo=timezone.utc), 'UTC')['cutoff'])
        self.assertEqual('2026-01-01T00:00:00+00:00',
            inputs.report_clock(datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc), 'UTC')['cutoff'])

    def test_no_observations_and_measured_zero_are_distinct(self):
        start, end = '2026-01-01T00:00Z', '2026-01-01T02:00Z'
        for value in (None, 0):
            acc = analysis.EnergyAnalysisAccumulator(None,
                {'start': start, 'end': end, 'timezone': 'UTC', 'kind': 'day'}, {},
                generated_at=datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
            acc.add_chunk(readings(start, end, value=value), start, end)
            result = acc.finish()
            self.assertEqual(None if value is None else Decimal(0),
                None if result['known_totals']['kwh'] is None else Decimal(result['known_totals']['kwh']))
            self.assertIsNone(result['known_totals']['usage_cost'])

    def test_incomplete_residual_never_claims_complete_balance(self):
        report = model.build_report(sample(mode='intermittent'))
        self.assertEqual(36, report['totals']['import_kwh'])
        self.assertEqual('unknown', report['unmeasured']['status'])
        self.assertIsNone(report['unmeasured']['share'])

class ActualComposerKnownProjectionTest(unittest.TestCase):
    def test_43_of_44_real_composer_calendar_and_rolling_pl_en(self):
        import asyncio
        from datetime import timedelta
        from tests.helper_composer_schema2 import ComposerFixture
        from tests.test_tariff_history import book, version
        ids = ['sensor.s%d' % i for i in range(44)]
        prefs = {'energy_sources': [{'type': 'grid', 'stat_energy_from': sid} for sid in ids],
                 'device_consumption': []}
        rows = {sid: {'unit': 'kWh'} for sid in ids}
        def points(start, end, selected):
            return {sid: [{'start': (start + timedelta(hours=i)).timestamp(), 'change': 1}
                          for i in range(int((end-start).total_seconds()/3600))]
                    for sid in selected if sid != ids[-1]}
        async def run():
            for lang in ('pl', 'en'):
                for calendar in (False, True):
                    fx = ComposerFixture(datetime(2026, 1, 4, 12, tzinfo=timezone.utc),
                        preferences=prefs, rows=rows, points=points,
                        book=book([version(rate=1, monthly=31)]), language=lang)
                    with fx.patches():
                        start = datetime(2026, 1, 2, tzinfo=timezone.utc)
                        report = await fx.compose(start=start if calendar else None,
                            end=start + timedelta(days=1) if calendar else None, rolling=not calendar)
                    self.assertEqual(1032, float(report['summary']['total_kwh']))
                    self.assertIn('1032', report['text'].replace(' ', '').replace('\xa0', '').replace(',', ''))
                    self.assertIn('1032', report['html'].replace(' ', '').replace('\xa0', '').replace(',', ''))
                    self.assertIn(ids[-1], report['csv'])
                    self.assertFalse(report['summary']['coverage']['complete'])
                    if calendar:
                        for part in ('text', 'html'):
                            body = report[part]
                            self.assertIn('znane odczyty' if lang == 'pl' else 'known readings', body.lower())
                            self.assertIn('wspólne odczyty' if lang == 'pl' else 'matched readings', body.lower())
                            self.assertNotIn('godziny z kompletem źródeł' if lang == 'pl' else 'complete-source hours', body.lower())
        asyncio.run(run())

    def test_device_only_real_composer_keeps_device_measurement_without_grid_import(self):
        import asyncio
        from datetime import timedelta
        from tests.helper_composer_schema2 import ComposerFixture
        prefs = {'energy_sources': [], 'device_consumption': [
            {'stat_consumption': 'sensor.device', 'name': 'Device'}]}
        async def run():
            for lang in ('pl', 'en'):
                for calendar in (False, True):
                    fx = ComposerFixture(datetime(2026, 1, 4, 12, tzinfo=timezone.utc),
                        preferences=prefs, rows={'sensor.device': {'unit': 'kWh'}},
                        book=book([version(rate=1, monthly=31)]), language=lang)
                    with fx.patches():
                        start = datetime(2026, 1, 2, tzinfo=timezone.utc)
                        report = await fx.compose(start=start if calendar else None,
                            end=start + timedelta(days=1) if calendar else None, rolling=not calendar)
                    self.assertIsNone(report['summary']['total_kwh'])
                    self.assertFalse(report['summary']['coverage']['complete'])
                    device = report['summary']['groups']['devices']['sensor.device']
                    self.assertEqual(Decimal(24), Decimal(device['kwh']))
                    self.assertEqual(Decimal(24), Decimal(device['usage_cost']))
                    self.assertIn('Device', report['text'])
                    self.assertIn('Device', report['html'])
        asyncio.run(run())
