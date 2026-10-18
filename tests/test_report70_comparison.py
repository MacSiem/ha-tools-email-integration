"""Full calendar reports reuse the real comparison lane; rolling stays unknown.

Fixture modules are imported as modules, never as TestCase class globals.
"""
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email import message_from_string
from io import StringIO
import unittest
from unittest.mock import AsyncMock, patch
from tests import test_mail67_delivery as mail67
from tests import test_tariff_history as history
from tests import test_send_semantics as smtp_fixture
from tests import test_energy_analysis_service as fixtures

UTC = timezone.utc
service, periods, scheduler, composer = mail67.service, mail67.periods, mail67.scheduler, mail67.composer


class Report70ComparisonTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self, now='2026-03-30T12:34:00+00:00'):
        h, sent = mail67.ActualDeliveryTest().fixture()
        h.now = datetime.fromisoformat(now)
        h.hass.data[service.DOMAIN]['load_config'] = AsyncMock(return_value={
            'default_recipient': 'synthetic@example.invalid'})
        return h, sent

    def request(self, h, cadence='daily', zone='UTC', mode='calendar'):
        return periods.resolve_report_window(cadence, h.now, zone, mode=mode)

    async def prepare(self, h, request, purpose='report_execute', actor='real-admin'):
        return await h.subject.async_prepare_report(request, actor, purpose=purpose,
            language='en', timezone_name='UTC', hour_cycle='24')

    def side_values(self, h, request, current=1, baseline=.5):
        original = h.usage
        current_start = datetime.fromisoformat(request.start)
        async def measured(hass, sensors, start, end, cadence, *, metadata):
            # Keep real fixture query/hook accounting; replace only synthetic IO.
            await original(hass, sensors, start, end, cadence, metadata=metadata)
            ids = tuple(row['entity_id'] for row in sensors)
            value = current if start >= current_start else baseline
            value = value / 10 if ids == ('sensor.device',) else value
            return fixtures.readings(start.isoformat(), end.isoformat(), value=value, sources=ids)
        h.usage = measured

    def previous(self, report):
        return report['summary']['comparisons']['previous']

    async def send(self, h, request, actor='real-admin'):
        return await scheduler.async_send_report(h.hass, h.storage, kind='energy_report',
            cadence=request.cadence, recipients=[], now=h.now, actor=actor,
            report_window=(datetime.fromisoformat(request.start), datetime.fromisoformat(request.end)),
            period_semantics=request.semantics, report_timezone=request.timezone)

    async def test_literal_dst_energy_historical_money_and_household_fees(self):
        h, _ = self.fixture()
        request = self.request(h, zone='Europe/Warsaw')
        self.side_values(h, request)
        h.book = history.book([history.version(rate=2, monthly=31),
            history.version('2026-03-29', rate=1, version_id='b', monthly=31)])
        for version in h.book['tariffs'][0]['versions']:
            version['profile']['timezone'] = 'Europe/Warsaw'
        with h.patches(), patch.object(composer, 'async_get_energy_usage', side_effect=AssertionError('old engine')):
            report = await self.prepare(h, request)
        comparison = self.previous(report)
        self.assertEqual('report_window', report['period']['kind'])
        self.assertEqual('2026-03-28T23:00:00+00:00', report['period']['start'])
        self.assertEqual(23, report['period']['hours'])
        prior = comparison['baseline_periods'][0]
        self.assertEqual('2026-03-27T23:00:00+00:00', prior['start'])
        self.assertEqual('2026-03-28T23:00:00+00:00', prior['end'])
        self.assertEqual(24, prior['hours'])
        self.assertEqual((23, 12, 11), tuple(comparison[key] for key in ('current_kwh','baseline_kwh','delta_kwh')))
        self.assertEqual('calendar_totals', comparison['comparison_scope'])
        costs = comparison['costs']
        self.assertEqual(('23.00','24.00','-1.00'),
            (costs['current']['usage_cost'],costs['baseline']['usage_cost'],costs['delta_cost']))
        self.assertEqual(('1.00','1.00'), (costs['current']['fixed_cost'], costs['baseline']['fixed_cost']))
        self.assertEqual(('24.00','25.00','-1.00'),
            (costs['current']['total_cost'],costs['baseline']['total_cost'],costs['delta_total_cost']))
        self.assertIsNone(comparison['groups']['devices']['sensor.device']['delta_fixed_cost'])
        chart = comparison['chart']
        self.assertFalse(chart['reconciles_comparison_energy'])
        self.assertEqual('partial', chart['status'])
        self.assertEqual('incomplete_marker_mapping', chart['reason'])
        self.assertTrue(any(row['status'] == 'unknown' for row in chart['markers']))
        self.assertEqual('Europe/Warsaw', report['context']['comparison_timezone'])
        self.assertEqual('UTC', report['context']['measurement_timezone'])
        self.assertEqual('daily', report['context']['comparison_calendar_kind'])
        self.assertEqual('previous', report['context']['comparison_kind'])
        self.assertEqual(12, report['summary']['comparison']['previous_kwh'])
        rows = {row['field']: row for row in csv.DictReader(StringIO(report['csv']))}
        self.assertEqual('11.0', rows['comparisons.previous.delta_kwh']['value'])
        self.assertEqual('-1.00', rows['comparisons.previous.costs.delta_cost']['value'])
        for kind in ('html','text'):
            self.assertIn('Previous period', report[kind])
            self.assertIn('11.0', report[kind])
            self.assertIn('-1.00', report[kind])
            self.assertNotIn('real-admin', report[kind])
        self.assertIn('period_summary.peak_hour', report['csv'])

    async def test_week_iso_leap_month_and_fall_keep_actual_previous_windows(self):
        cases = [
            ('weekly','2026-01-05T12:00:00+00:00','UTC',168,168,'2025-12-22'),
            ('monthly','2024-03-01T12:00:00+00:00','UTC',696,744,'2024-01-01'),
            ('daily','2026-10-26T12:00:00+00:00','Europe/Warsaw',25,24,'2026-10-23T22:00')]
        for cadence, now, zone, hours, prior_hours, prior_start in cases:
            h, _ = self.fixture(now); h.book = None
            request = self.request(h, cadence, zone)
            self.side_values(h, request, current=1, baseline=1)
            with h.patches(): report = await self.prepare(h, request)
            comparison = self.previous(report)
            self.assertEqual(hours, comparison['current_kwh'])
            self.assertEqual(prior_hours, comparison['baseline_kwh'])
            self.assertTrue(comparison['baseline_periods'][0]['start'].startswith(prior_start))
            self.assertEqual('calendar_totals', comparison['comparison_scope'])
            self.assertTrue(all(0 < (b-a).total_seconds() <= 168*3600 for _,a,b in h.queries))

    async def test_partial_common_mask_is_not_replaced_by_full_current_kpi(self):
        for missing_device in (True, False):
            with self.subTest(missing_device=missing_device):
                h, _ = self.fixture('2026-01-03T12:00:00+00:00')
                request = self.request(h)
                original = h.usage
                async def measured(*args, **kwargs):
                    snapshot = await original(*args, **kwargs)
                    ids = tuple(row['entity_id'] for row in args[1])
                    if ids == ('sensor.grid',) or missing_device and ids == ('sensor.device',):
                        for source_id in ids:
                            for row in snapshot['source_buckets'][source_id]:
                                if row['from'] == 1767308400000:  # Jan 1 23:00 UTC, baseline only
                                    row['kwh'] = None
                    return snapshot
                h.usage = measured
                with h.patches(): report = await self.prepare(h, request)
                comparison = self.previous(report)
                self.assertEqual(Decimal('240'), Decimal(report['summary']['total_kwh']))
                self.assertEqual((230,230,0),tuple(comparison[key] for key in ('current_kwh','baseline_kwh','delta_kwh')))
                self.assertEqual('common_measurements', comparison['comparison_scope'])
                self.assertIsNone(comparison['full_baseline_kwh'])
                self.assertEqual(240,comparison['full_current_kwh'])
                self.assertEqual('230.00',comparison['costs']['current']['usage_cost'])
                self.assertIsNone(comparison['costs']['current']['total_cost'])
                device = comparison['groups']['devices']['sensor.device']
                self.assertEqual('common_measurements' if missing_device else 'per_group_calendar_totals',device['comparison_scope'])
                self.assertEqual(Decimal('46') if missing_device else Decimal('48'),Decimal(device['current_kwh']))
                self.assertEqual(Decimal('48'),Decimal(device['own_current_kwh']))
                if not missing_device:
                    self.assertFalse(comparison['groups']['reconciles_household_delta'])
                self.assertIn('Common measurements only',report['html'])

    async def test_zero_baseline_is_measured_zero_with_unknown_percentage(self):
        h, _ = self.fixture('2026-01-03T12:00:00+00:00')
        request = self.request(h); self.side_values(h,request,current=1,baseline=0)
        with h.patches(): report = await self.prepare(h,request)
        comparison = self.previous(report)
        self.assertEqual('ready',comparison['status'])
        self.assertEqual((24,0,24),tuple(comparison[key] for key in ('current_kwh','baseline_kwh','delta_kwh')))
        self.assertIsNone(comparison['percent'])
        self.assertEqual(0,report['summary']['comparison']['previous_kwh'])
        self.assertIsNone(comparison['costs']['percent'])

    async def test_rolling_never_reads_previous_even_when_end_is_midnight(self):
        for cadence,hours in [('daily',24),('weekly',168),('monthly',720)]:
            h, _ = self.fixture('2026-01-03T00:34:00+00:00')
            request = self.request(h,cadence,mode='rolling')
            with h.patches(): report = await self.prepare(h,request)
            self.assertEqual(hours,report['period']['hours'])
            self.assertEqual('unknown',report['summary']['comparison']['status'])
            self.assertEqual('rolling_or_custom_window',report['summary']['comparison']['reason'])
            self.assertEqual({},report['summary']['comparisons'])
            self.assertNotIn('comparison_kind',report['context'])
            self.assertTrue(all(a >= datetime.fromisoformat(request.start) for _,a,_ in h.queries))
            self.assertEqual(hours*10,Decimal(report['summary']['total_kwh']))

    async def test_frozen_calendar_plan_cache_and_purpose_ids_reauthorize(self):
        h, _ = self.fixture('2026-01-03T12:00:00+00:00');request=self.request(h)
        with h.patches():
            first=await self.prepare(h,request)
            count=len(h.queries)
            h.now += timedelta(hours=1)
            again=await self.prepare(h,request)
            self.assertEqual(first,again)
            self.assertEqual(count,len(h.queries))
            self.assertEqual(first['context']['comparison_signature'],again['context']['comparison_signature'])
            read=await self.prepare(h,request,purpose='report_read')
            self.assertNotEqual(first['snapshot_id'],read['snapshot_id'])
            self.assertEqual(self.previous(first)['delta_kwh'],self.previous(read)['delta_kwh'])
            h.user.is_active=False
            with self.assertRaises(PermissionError): await self.prepare(h,request)
        self.assertIsNone(h.subject._cache)

    async def test_mismatched_resolver_current_bounds_fail_before_private_io(self):
        for change in ('timezone','start','end'):
            h, _ = self.fixture('2026-01-03T12:00:00+00:00');request=self.request(h)
            original=service.resolve_analysis_periods
            def broken(*args,**kwargs):
                result=original(*args,**kwargs)
                result['current'][change]='Europe/Warsaw' if change=='timezone' else '2026-01-01T00:00:00+00:00'
                return result
            with h.patches(),patch.object(service,'resolve_analysis_periods',broken),self.assertRaisesRegex(ValueError,'report_comparison_plan_mismatch'):
                await self.prepare(h,request)
            self.assertEqual([],h.queries)
            self.assertEqual([],h.registry_ids)
            self.assertEqual(0,h.subject._pending)

    async def test_baseline_auth_source_unit_and_lifecycle_changes_abort_before_smtp(self):
        for change in ('role','source','unit','policy','entry','stop'):
            h,sent=self.fixture('2026-01-03T12:00:00+00:00');request=self.request(h)
            async def mutate(ids,start,end):
                if start < datetime.fromisoformat(request.start):
                    if change=='role':h.user.is_active=False
                    elif change=='source':h.manager.data['energy_sources'][0]['stat_energy_from']='sensor.changed'
                    elif change=='unit':h.metadata['sensor.grid']['unit_of_measurement']='Wh'
                    elif change=='policy':h.storage.current_feature_access_revision+=1
                    elif change=='entry':h.entry=object()
                    else:h.subject.async_stop()
            h.on_read=mutate
            with h.patches(),self.assertRaises((PermissionError,RuntimeError)):
                await self.send(h,request)
            self.assertEqual([],sent)
            self.assertIsNone(h.subject._cache)
            h.hass.data[service.DOMAIN]['load_config'].assert_not_awaited()

    async def test_render_revocation_and_schedule_capability_expiry_block_handoff(self):
        for change in ('render','schedule'):
            h,sent=self.fixture('2026-01-03T12:00:00+00:00');request=self.request(h)
            actor='real-admin'
            if change=='schedule':
                actor=h.subject._schedule_actor({'id':'s','kind':'energy_report','enabled':True},1)
                async def revoke(ids,start,end):
                    if start < datetime.fromisoformat(request.start):
                        h.hass.data[service.DOMAIN]['schedule_generation']+=1
                h.on_read=revoke
            else:
                def revoke(job):
                    if getattr(getattr(job,'func',None),'__name__','')=='render_energy_analysis':
                        h.user.is_active=False
                h.on_cpu=revoke
            with h.patches(),self.assertRaises((PermissionError,RuntimeError)):
                await self.send(h,request,actor=actor)
            self.assertEqual([],sent)

    async def test_real_composer_mime_contains_one_prepared_comparison_and_no_retry(self):
        h, _ = self.fixture('2026-01-03T12:00:00+00:00');request=self.request(h)
        # Existing transport fixture extracts the unchanged production MIME/SMTP
        # function. Its SMTP connection alone is a Mock: no network is possible.
        native_send,smtp=smtp_fixture.SendSemanticsTests()._send_function({})
        h.hass.data[service.DOMAIN].update(send_email=native_send,load_config=AsyncMock(return_value={
            'server':'synthetic.invalid','username':'synthetic','password':'synthetic-test-value',
            'default_recipient':'synthetic@example.invalid'}))
        h.storage.async_append_attempt.side_effect=OSError('synthetic history failure')
        prepared=[];original=h.subject.async_prepare_report
        async def capture(*args,**kwargs):
            value=await original(*args,**kwargs);prepared.append(deepcopy(value));return value
        with h.patches(),patch.object(h.subject,'async_prepare_report',capture),patch.object(
                composer,'async_get_energy_usage',side_effect=AssertionError('old engine')):
            result=await self.send(h,request)
        self.assertEqual('accepted_by_smtp',result['status'])
        self.assertEqual('unavailable',result['history_status'])
        smtp.sendmail.assert_called_once()
        self.assertEqual(1,len(prepared))
        artifact=prepared[0]
        msg=message_from_string(smtp.sendmail.call_args.args[2])
        parts={part.get_content_type():part.get_payload(decode=True).decode('utf-8') for part in msg.get_payload()}
        self.assertEqual(artifact['text'],parts['text/plain'])
        self.assertEqual(artifact['html'],parts['text/html'])
        for text in parts.values():
            self.assertIn(artifact['snapshot_id'],text)
            self.assertIn('Previous period',text)
        self.assertEqual(artifact['snapshot_id'],artifact['summary']['snapshot_id'])
        self.assertIn(artifact['snapshot_id'],artifact['csv'])


    async def test_unobservable_previous_plan_keeps_current_without_baseline_io(self):
        h,_=self.fixture('2026-01-03T12:00:00+00:00');request=self.request(h)
        original=service.resolve_analysis_periods
        def unavailable(*args,**kwargs):
            resolved=original(*args,**kwargs)
            resolved['comparisons']['previous']={'status':'unknown',
                'reason':'calendar_boundary_not_recorder_hour','baseline_periods':[]}
            return resolved
        with h.patches(),patch.object(service,'resolve_analysis_periods',unavailable):
            report=await self.prepare(h,request)
        previous=self.previous(report)
        self.assertEqual('unknown',previous['status'])
        self.assertEqual('calendar_boundary_not_recorder_hour',previous['reason'])
        self.assertEqual('hour',previous['chart']['grain'])
        self.assertEqual([],previous['chart']['markers'])
        self.assertTrue(all(start >= datetime.fromisoformat(request.start) for _,start,_ in h.queries))
        self.assertEqual(Decimal('240'),Decimal(report['summary']['total_kwh']))

    async def test_plan_context_is_hashed_detached_and_revalidated_for_server_actor(self):
        h,_=self.fixture('2026-01-03T12:00:00+00:00');request=self.request(h)
        actor=h.subject._schedule_actor({'id':'s','kind':'energy_report','enabled':True},1)
        h.hass.auth.async_get_user=AsyncMock(side_effect=AssertionError('no invented user'))
        with h.patches():
            report=await self.prepare(h,request,actor=actor)
            self.assertEqual('previous',report['context']['comparison_kind'])
            self.assertEqual(service._digest(report['context']['report_comparison_plan']),
                             report['context']['comparison_signature'])
            self.assertTrue(await h.subject.async_validate_result(report,actor))
            tampered=deepcopy(report)
            tampered['context']['report_comparison_plan']['baseline_periods'][0]['end']=request.end
            self.assertFalse(h.subject.context_is_current(tampered))
            self.assertFalse(await h.subject.async_validate_result(tampered,actor))
            self.assertTrue(h.subject.context_is_current(report))
        self.assertNotIn('actor_signature',report['text'])
        self.assertNotIn('schedule_capability',report['csv'])
