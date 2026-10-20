"""Calendar comparisons exercise actual payload rendering and conservative data gates."""
import copy
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from decimal import Decimal
from tests.helper_composer_schema2 import ComposerFixture,csv_facts

from tests.test_composer import composer as c
from tests.test_scheduler import scheduler
from tests import test_energy_grid_provider as grid_fixture

UTC = timezone.utc


def snapshot(kwh=24, sid='sensor.grid'):
    return {'status': 'ready', 'source_ids': [sid],
            'devices': [{'entity_id': sid, 'name': 'Grid <meter>', 'kwh': kwh}],
            'measurement_provenance': {sid: {'method': 'recorder_hourly_change_v1',
                'unit': 'kWh', 'source_unit': 'kWh', 'unit_class': 'energy', 'has_sum': True}}}


class CalendarComparisonTest(unittest.TestCase):
    def test_partial_calendar_period_compares_matching_local_fragment(self):
        cases = [
            ('daily', '2026-10-18T00:00+02:00', '2026-10-18T12:00+02:00',
             '2026-10-17T00:00:00+02:00', '2026-10-17T12:00:00+02:00', 12, 12),
            ('weekly', '2026-10-12T00:00+02:00', '2026-10-17T12:00+02:00',
             '2026-10-05T00:00:00+02:00', '2026-10-10T12:00:00+02:00', 132, 132),
            ('monthly', '2026-10-01T00:00+02:00', '2026-10-18T12:00+02:00',
             '2026-09-01T00:00:00+02:00', '2026-09-18T12:00:00+02:00', 420, 420),
            ('daily', '2026-03-29T00:00+01:00', '2026-03-29T12:00+02:00',
             '2026-03-28T00:00:00+01:00', '2026-03-28T12:00:00+01:00', 11, 12),
            ('weekly', '2026-10-19T00:00+02:00', '2026-10-25T12:00+01:00',
             '2026-10-12T00:00:00+02:00', '2026-10-18T12:00:00+02:00', 157, 156),
            ('yearly', '2024-01-01T00:00+01:00', '2024-02-29T12:00+01:00',
             '2023-01-01T00:00:00+01:00', '2023-02-28T12:00:00+01:00', 1428, 1404),
        ]
        for cadence, start, end, prior_start, prior_end, hours, prior_hours in cases:
            with self.subTest(cadence=cadence, end=end):
                result = c._calendar_comparison_window(cadence,
                    datetime.fromisoformat(start), datetime.fromisoformat(end), 'Europe/Warsaw', True)
                self.assertEqual('pending', result['status'],
                                 'A current calendar fragment must have an equally aligned baseline')
                self.assertTrue(result['partial'])
                self.assertEqual(prior_start, result['previous_local_start'])
                self.assertEqual(prior_end, result['previous_local_end'])
                self.assertEqual(hours, result['current_hours'])
                self.assertEqual(prior_hours, result['previous_hours'])

    def test_partial_month_clamps_previous_fragment_to_its_actual_end(self):
        result = c._calendar_comparison_window('monthly',
            datetime.fromisoformat('2026-03-01T00:00+01:00'),
            datetime.fromisoformat('2026-03-31T12:00+02:00'), 'Europe/Warsaw', True)
        self.assertEqual('pending', result['status'])
        self.assertTrue(result['calendar_clamped'])
        self.assertEqual('2026-02-01T00:00:00+01:00', result['previous_local_start'])
        self.assertEqual('2026-03-01T00:00:00+01:00', result['previous_local_end'])
        self.assertEqual(731, result['current_hours'])
        self.assertEqual(672, result['previous_hours'])

    def window(self, cadence, now, zone='UTC'):
        start, end = scheduler.calendar_report_window({'cadence': cadence}, now, local_timezone=zone)
        return start, end, c._calendar_comparison_window(cadence, start, end, zone, True)

    def test_windows_match_scheduler_across_month_end_leap_day_and_dst(self):
        for cadence, now, zone in [
            ('monthly', datetime(2024, 3, 31, 12, tzinfo=UTC), 'Europe/Warsaw'),
            ('monthly', datetime(2026, 4, 30, 12, tzinfo=UTC), 'UTC'),
            ('daily', datetime(2026, 3, 30, 12, tzinfo=UTC), 'Europe/Warsaw'),
            ('daily', datetime(2026, 10, 26, 12, tzinfo=UTC), 'Europe/Warsaw'),
            ('weekly', datetime(2026, 10, 26, 12, tzinfo=UTC), 'Europe/Warsaw'),
            ('daily', datetime(2026, 4, 25, 12, tzinfo=UTC), 'Africa/Cairo'),
            ('daily', datetime(2026, 3, 9, 12, tzinfo=UTC), 'America/Havana'),
            ('daily', datetime(2026, 11, 2, 12, tzinfo=UTC), 'America/Havana'),
        ]:
            with self.subTest(cadence=cadence, now=now, zone=zone):
                start, end, result = self.window(cadence, now, zone)
                previous_start, previous_end = scheduler.calendar_report_window(
                    {'cadence': cadence}, start, local_timezone=zone)
                self.assertEqual('pending', result['status'])
                self.assertEqual(previous_start.isoformat(), result['previous_start'])
                self.assertEqual(previous_end.isoformat(), result['previous_end'])
                self.assertEqual((end-start).total_seconds()/3600, result['current_hours'])
        self.assertEqual(23, self.window('daily', datetime(2026,3,30,12,tzinfo=UTC), 'Europe/Warsaw')[2]['current_hours'])
        self.assertEqual(25, self.window('daily', datetime(2026,10,26,12,tzinfo=UTC), 'Europe/Warsaw')[2]['current_hours'])
        leap = self.window('monthly', datetime(2024,3,31,12,tzinfo=UTC))[2]
        self.assertEqual(29*24, leap['current_hours'])
        self.assertEqual(31*24, leap['previous_hours'])

    def test_nonhour_boundaries_unknown_and_rolling_never_reinterpreted(self):
        now = datetime(2026, 10, 12, 12, tzinfo=UTC)
        for zone in ('Asia/Kolkata', 'Asia/Kathmandu', 'Australia/Adelaide'):
            with self.subTest(zone=zone):
                result = self.window('daily', now, zone)[2]
                self.assertEqual('unknown', result['status'])
                self.assertEqual('calendar_boundary_not_recorder_hour', result['reason'])
                self.assertIn('previous_local_start', result)
        for explicit in (True, False):
            result = c._calendar_comparison_window('daily', now-timedelta(days=1), now, 'UTC', explicit)
            self.assertEqual('rolling_or_custom_window', result['reason'])
        start, end, _ = self.window('daily', now)
        self.assertEqual('report_timezone_unavailable',
            c._calendar_comparison_window('daily', start, end, 'invalid/zone', True)['reason'])

    def test_delta_percent_and_measured_zero(self):
        for current, previous, delta, percent in [(30,24,6,25),(0,24,-24,-100),(24,0,24,None),(0,0,0,None)]:
            with self.subTest(current=current, previous=previous):
                result = c._compare_energy_snapshots(snapshot(current), snapshot(previous), {})
                self.assertEqual('ready', result['status'])
                self.assertEqual(previous, result['previous_kwh'])
                self.assertEqual(delta, result['delta_kwh'])
                self.assertEqual(percent, result['percent'])
                if previous == 0:
                    self.assertIn('Previous measured grid import: 0.00 kWh.', c._energy_comparison_lines(result))
                    self.assertIn('zero baseline', c._energy_comparison_lines(result)[-1])

    def test_incomplete_changed_sources_or_missing_provenance_withhold_delta(self):
        for which, mutation in [
            ('previous', lambda x: x.update(status='partial')),
            ('current', lambda x: x.update(status='no_data')),
            ('previous', lambda x: x.update(devices=[])),
            ('previous', lambda x: x.pop('measurement_provenance')),
            ('previous', lambda x: x['measurement_provenance']['sensor.grid'].update(source_unit='Wh')),
            ('previous', lambda x: x['devices'][0].update(kwh=float('nan'))),
            ('previous', lambda x: x['devices'][0].update(kwh=True)),
        ]:
            current, previous = snapshot(), snapshot(0)
            mutation(previous if which == 'previous' else current)
            result = c._compare_energy_snapshots(current, previous,
                {'delta_kwh':None, 'percent':None})
            self.assertEqual('unknown', result['status'])
            self.assertIsNone(result['delta_kwh'])
            self.assertIsNone(result['percent'])
        result = c._compare_energy_snapshots(snapshot(), snapshot(0, 'sensor.other'), {})
        self.assertEqual('source_set_changed', result['reason'])
        self.assertEqual(0, result['previous_kwh'])


class RecorderProvenanceTest(unittest.TestCase):
    def test_actual_recorder_adapter_emits_normalized_measurement_provenance(self):
        fixture = grid_fixture.EnergyGridProviderTest()
        for unit, change in [('Wh',1000), ('kWh',1), ('MWh',0.001)]:
            with self.subTest(unit=unit):
                summary, calls = fixture.compose(
                    {'energy_sources':[{'type':'grid', 'stat_energy_from':'sensor.grid'}]},
                    {'sensor.grid':{'unit':unit, 'change':change}})
                self.assertEqual(Decimal('24'),Decimal(summary['total_kwh']))
                self.assertEqual({'sensor.grid':{'method':'recorder_hourly_change_v1',
                    'unit':'kWh','source_unit':unit,'unit_class':'energy','has_sum':True}},
                    calls['snapshot']['measurement_provenance'])


class RuntimeComparisonTest(unittest.IsolatedAsyncioTestCase):
    async def test_partial_payload_queries_only_matching_previous_fragment(self):
        start=datetime(2026,10,12,tzinfo=UTC);end=datetime(2026,10,17,12,tzinfo=UTC)
        baseline_start=datetime(2026,10,5,tzinfo=UTC)
        baseline_end=datetime(2026,10,10,12,tzinfo=UTC)
        # Keep literal 12 vs10 with one measured hour each, all other hours zero.
        # Never spread aggregate snapshot totals across points.
        def points(a,b,ids):
            rows=[]
            for i in range(int((b-a).total_seconds()/3600)):
                at=a+timedelta(hours=i)
                rows.append({'start':at.timestamp(),'change':12 if at==start else 10 if at==baseline_start else 0})
            return {'sensor.grid':rows}
        fx=ComposerFixture(end,points=points)
        with fx.patches():
            # Existing partial analysis path owns these internal fragments.
            payload=await fx.analysis('week',anchor='2026-10-12',comparison='previous')
        self.assertEqual(2,len(fx.statistics_calls),'The partial calendar period needs a measured baseline')
        self.assertEqual((baseline_start,baseline_end),fx.statistics_calls[1][:2])
        previous=fx.snapshot['comparisons']['previous']
        self.assertEqual(Decimal('12'),Decimal(str(previous['current_kwh'])))
        self.assertEqual(Decimal('10'),Decimal(str(previous['baseline_kwh'])))
        self.assertEqual(20,previous['percent'])
        self.assertTrue(fx.snapshot['period']['partial'])
        self.assertIn('Selected comparisons',payload['body'])
        # Public full-window report requests must continue rejecting this shape.
        other=ComposerFixture(end,points=points)
        with other.patches(),self.assertRaisesRegex(ValueError,'invalid_report_calendar_boundaries'):
            await other.compose('weekly',start=start,end=end)
        self.assertEqual([],other.statistics_calls)

    async def test_actual_payload_uses_same_selected_sources_and_renders_both_formats(self):
        now=datetime(2026,10,12,12,tzinfo=UTC)
        start,end=scheduler.calendar_report_window({'cadence':'daily'},now,local_timezone='UTC')
        def points(a,b,ids):
            return {'sensor.grid':[{'start':(a+timedelta(hours=i)).timestamp(),
                'change':1.25 if start<=a+timedelta(hours=i)<end else 1}
                for i in range(int((b-a).total_seconds()/3600))],
                'sensor.solar':[{'start':a.timestamp(),'change':9999}]}
        fx=ComposerFixture(now,preferences={'energy_sources':[
            {'type':'grid','stat_energy_from':'sensor.grid'},
            {'type':'solar','stat_energy_from':'sensor.solar'}]},
            rows={'sensor.grid':{'name':'Grid <meter>'}},points=points)
        with fx.patches():payload=await fx.compose(start=start,end=end)
        self.assertEqual(1,len(fx.manager_reads));self.assertEqual(2,len(fx.statistics_calls))
        self.assertEqual(fx.adapter_reads[0]['sensors'],fx.adapter_reads[1]['sensors'])
        self.assertEqual(['sensor.grid'],[r['entity_id'] for r in fx.adapter_reads[1]['sensors']])
        self.assertEqual((start-timedelta(days=1),start),fx.statistics_calls[1][:2])
        self.assertEqual(Decimal('30'),Decimal(payload['summary']['total_kwh']))
        self.assertIsNone(payload['summary']['total_cost'])
        compared=payload['summary']['comparisons']['previous'] # report70 dependency, no fake adapter.
        self.assertEqual(25,compared['percent']);self.assertEqual(24,compared['baseline_kwh'])
        self.assertEqual(6,compared['delta_kwh'])
        self.assertNotIn('costs',compared) # No configured tariff must not fabricate a monetary comparison.
        self.assertEqual(24,payload['summary']['comparison']['previous_kwh'])
        facts=csv_facts(payload)
        for path,want in [('comparisons.previous.current_kwh','30'),
                          ('comparisons.previous.baseline_kwh','24'),
                          ('comparisons.previous.delta_kwh','6'),('comparisons.previous.percent','25')]:
            self.assertEqual(Decimal(want),Decimal(facts[path]))
        for body in (payload['body'],payload['html']):
            self.assertIn('Selected comparisons',body);self.assertIn('UTC',body)
            self.assertNotIn('9999',body)
        # Preserve the grid-name escaping scenario on the existing pure formatter.
        pure=c.build_energy_report_payload(fx.adapter_reads[0]['result']['devices'],cadence='daily',source_kind='grid')
        self.assertIn('Grid import source',pure['html']);self.assertIn('Grid &lt;meter&gt;',pure['html'])
        self.assertNotIn('Grid <meter>',pure['html'])

    async def test_rolling_payload_fetches_only_current_and_keeps_generic_consumers(self):
        fx=ComposerFixture(datetime(2026,10,12,12,tzinfo=UTC))
        with fx.patches():payload=await fx.compose()
        self.assertEqual(1,len(fx.statistics_calls))
        self.assertEqual('rolling_or_custom_window',payload['summary']['comparison']['reason'])
        self.assertEqual('unknown',payload['summary']['comparison']['status'])
        self.assertEqual({},payload['summary']['comparisons'])
        self.assertEqual(Decimal('24'),Decimal(payload['summary']['total_kwh']))
        generic=c.build_energy_report_payload([{'name':'Appliance','kwh':1}],cadence='daily')
        self.assertIn('Top consumers:',generic['body']);self.assertIn('>Consumer</th>',generic['html'])
        self.assertNotIn('comparison',generic['summary'])


class ActualRecorderComparisonTest(unittest.IsolatedAsyncioTestCase):
    async def test_calendar_mail_composes_both_periods_through_actual_recorder_adapter(self):
        for cadence,now,previous_rate in [
            ('daily',datetime(2026,3,30,12,tzinfo=UTC),0.5),
            ('weekly',datetime(2026,3,30,12,tzinfo=UTC),0.5),
            ('monthly',datetime(2026,4,1,12,tzinfo=UTC),0.5),
            ('daily',datetime(2026,10,12,12,tzinfo=UTC),0),
        ]:
            with self.subTest(cadence=cadence,now=now,previous_rate=previous_rate):
                zone='Europe/Warsaw'
                start,end=scheduler.calendar_report_window({'cadence':cadence},now,local_timezone=zone)
                previous_start,previous_end=scheduler.calendar_report_window({'cadence':cadence},start,local_timezone=zone)
                def points(a,b,ids):
                    return {'sensor.grid':[{'start':(a+timedelta(hours=i)).timestamp(),
                        'change':1 if start<=a+timedelta(hours=i)<end else previous_rate}
                        for i in range(int((b-a).total_seconds()/3600))],
                        'sensor.unrelated':[{'start':a.timestamp(),'change':9999}]}
                fx=ComposerFixture(now,measurement_zone=zone,preferences={'energy_sources':[
                    {'type':'grid','flow_from':[{'stat_energy_from':'sensor.grid'}]},
                    {'type':'solar','stat_energy_from':'sensor.unrelated'}]},points=points)
                with fx.patches():payload=await fx.compose(cadence,start=start,end=end,report_zone=zone)
                fx.assert_queries_cover(self,start,end);fx.assert_queries_cover(self,previous_start,previous_end)
                self.assertTrue(all(ids=={'sensor.grid'} for a,b,ids in fx.statistics_calls))
                self.assertEqual(1,len(fx.manager_reads))
                current=(end-start).total_seconds()/3600
                previous=(previous_end-previous_start).total_seconds()/3600*previous_rate
                comparison=payload['summary']['comparisons']['previous']
                self.assertEqual('ready',comparison['status'])
                self.assertEqual(Decimal(str(current)),Decimal(payload['summary']['total_kwh']))
                self.assertEqual(previous,comparison['baseline_kwh'])
                self.assertEqual(current-previous,comparison['delta_kwh'])
                if previous:self.assertAlmostEqual((current-previous)/previous*100,comparison['percent'])
                else:self.assertIsNone(comparison['percent'])
                self.assertIsNone(payload['summary']['total_cost'])
                if cadence=='daily' and now.month==3:
                    self.assertEqual((23,12,11),(current,previous,comparison['delta_kwh']))
                if cadence=='weekly':self.assertEqual((167,84,83),(current,previous,comparison['delta_kwh']))
                if cadence=='monthly':self.assertEqual((743,336,407),(current,previous,comparison['delta_kwh']))
                for body in (payload['body'],payload['html']):
                    self.assertIn('Selected comparisons',body);self.assertIn('Europe/Warsaw',body)
                    # The random snapshot hash can contain these digits; inspect
                    # rendered measurements, while retaining the exact-source IO proof.
                    numeric_body = body.replace(payload['summary']['snapshot_id'], '')
                    self.assertNotRegex(numeric_body, r'(?<![0-9A-Za-z])(?:9999|9[,\u00a0 ]999)(?![0-9A-Za-z])')
                    self.assertNotIn('sensor.unrelated', body)
