"""Mail67: actual schema2 service and renderer; only external IO is stubbed."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_energy_analysis_service import Harness, service

periods = importlib.import_module(service.__package__ + '.report_window')
scheduler = importlib.import_module(service.__package__ + '.scheduler')
composer = importlib.import_module(service.__package__ + '.composer')
UTC = timezone.utc


class ReportWindowTest(unittest.TestCase):
    def test_completed_rolling_hours_are_not_local_dates(self):
        now = datetime(2026, 3, 30, 0, 34, tzinfo=UTC)
        for cadence, hours in [('daily',24),('weekly',168),('monthly',720)]:
            request = periods.resolve_report_window(cadence, now, 'Europe/Warsaw')
            result = request.period(now)
            self.assertEqual(hours, result['hours'])
            self.assertEqual('2026-03-30T00:00:00+00:00', result['end'])
            self.assertEqual('rolling_completed_hours', result['semantics'])
            self.assertEqual('report_window', result['kind'])
        calendar = periods.resolve_report_window('daily', now, 'Europe/Warsaw', mode='calendar')
        result = calendar.period(now)
        self.assertEqual('2026-03-28T23:00:00+00:00', result['start'])
        self.assertEqual('2026-03-29T22:00:00+00:00', result['end'])
        self.assertEqual(23, result['hours'])
        self.assertEqual(result, calendar.period(now + timedelta(days=1)))

    def test_fall_iso_and_leap_boundaries(self):
        cases = [('daily','2026-10-26T12:00:00+00:00','Europe/Warsaw',25),
                 ('weekly','2026-01-05T12:00:00+00:00','UTC',168),
                 ('monthly','2024-03-01T12:00:00+00:00','UTC',696)]
        for cadence, stamp, zone, hours in cases:
            now = datetime.fromisoformat(stamp)
            self.assertEqual(hours, periods.resolve_report_window(cadence, now, zone, mode='calendar').period(now)['hours'])

    def test_half_hour_and_untrusted_boundaries_fail_before_io(self):
        now = datetime(2026, 1, 2, 12, tzinfo=UTC)
        with self.assertRaisesRegex(ValueError, 'calendar_boundary_not_recorder_hour'):
            periods.resolve_report_window('daily', now, 'Asia/Kolkata', mode='calendar').period(now)
        for bad in [datetime(2026,1,2), None]:
            with self.assertRaises(ValueError):
                periods.resolve_report_window('daily', bad, 'UTC')
        with self.assertRaises(ValueError):
            periods.ReportWindow('rolling_completed_hours','daily','UTC',now.isoformat(),(now+timedelta(hours=1)).isoformat()).period(now)


class ActualDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        h = Harness(); h.user.is_admin = True; h.user.id = 'real-admin'
        h.now = datetime(2026,1,2,2,34,tzinfo=UTC)
        h.storage.current_smtp_revision = 0
        h.storage.current_schedule_revision = 0
        original_patches=h.patches
        def patches():
            stack=original_patches()
            stack.enter_context(patch.object(composer,"__package__",service.__package__))
            return stack
        h.patches=patches
        h.storage.async_append_attempt = AsyncMock()
        h.storage.async_get_schedule = AsyncMock(return_value=None)
        sent=[]
        def send(*args): sent.append(args)
        async def executor(job,*args): return job(*args)
        h.hass.async_add_executor_job=executor
        async def load(): return {'default_recipient':'synthetic@example.invalid'}
        h.hass.data[service.DOMAIN].update(load_config=load,send_email=send,schedule_generation=1)
        return h,sent

    async def build(self,h,purpose='report_execute',actor='real-admin'):
        window=periods.resolve_report_window('daily',h.now,'UTC')
        return await h.subject.async_prepare_report(window,actor,purpose=purpose,
            language='en',timezone_name='UTC',hour_cycle='24')

    async def test_actual_renderer_one_snapshot_and_purpose_bound_ids(self):
        h,sent=self.fixture()
        with h.patches(), patch.object(composer,'async_get_energy_usage',side_effect=AssertionError('old engine')):
            read=await self.build(h,'report_read')
            execute=await self.build(h)
            self.assertNotEqual(read['snapshot_id'],execute['snapshot_id'])
            self.assertIsInstance(execute['summary']['total_kwh'],str)
            self.assertEqual(Decimal('240'),Decimal(execute['summary']['total_kwh']))
            self.assertEqual(execute['snapshot_id'],execute['summary']['snapshot_id'])
            self.assertIn(execute['snapshot_id'],execute['csv'])
            self.assertIn(execute['snapshot_id'],execute['html'])
            self.assertIn('240',execute['body'])
            self.assertNotIn('real-admin',execute['html'])
            self.assertNotIn('source_signature',execute['body'])
        self.assertEqual([],sent)

    async def test_real_send_and_history_failure_never_retry(self):
        h,sent=self.fixture();h.storage.async_append_attempt.side_effect=OSError('synthetic')
        with h.patches(),patch.object(composer,'async_get_energy_usage',side_effect=AssertionError('old engine')):
            result=await scheduler.async_send_report(h.hass,h.storage,kind='energy_report',cadence='daily',
                recipients=[],now=h.now,actor='real-admin')
        self.assertEqual('accepted_by_smtp',result['status']);self.assertEqual(1,len(sent))
        self.assertEqual('unavailable',result['history_status'])
        self.assertIn('240',sent[0][4]);self.assertIn('240',sent[0][5])

    async def test_denied_native_or_feature_has_no_private_io(self):
        for mode in ['feature','native','missing_actor']:
            h,_=self.fixture();h.user.is_admin=False
            h.policy['features']={'energy_reports':{'read':{'mode':'household'}}}
            if mode=='feature':h.policy['features']['energy_reports']['read']={'mode':'admin'}
            if mode=='native':
                h.permissions.access_all_entities=lambda _:False
                h.permissions.check_entity=lambda *_:False
            with h.patches(),self.assertRaises(PermissionError):
                await self.build(h,'report_read',None if mode=='missing_actor' else 'real-admin')
            self.assertEqual([],h.queries)

    async def test_revocation_during_io_and_cpu_blocks_publication(self):
        for point in ['io','cpu']:
            h,_=self.fixture()
            async def revoke(*_):h.user.is_active=False
            if point=='io':h.on_read=revoke
            else:h.on_cpu=lambda _:setattr(h.user,'is_active',False)
            with h.patches(),self.assertRaises(PermissionError):await self.build(h)
            self.assertIsNone(h.subject._cache)

    async def test_server_actor_is_private_bound_and_strict_policy_fails_before_io(self):
        h,_=self.fixture();schedule={'id':'stable','kind':'energy_report','cadence':'daily','enabled':True}
        actor=h.subject._schedule_actor(schedule,1)
        with self.assertRaises(TypeError):json.dumps(actor)
        with h.patches():
            h.hass.auth.async_get_user=AsyncMock(side_effect=AssertionError('no invented user'))
            report=await self.build(h,actor=actor)
            self.assertEqual('report_execute',report['context']['access_purpose'])
        h,_=self.fixture();actor=h.subject._schedule_actor(schedule,1)
        h.policy['features']={'energy_reports':{'execute':{'mode':'selected_users','user_ids':['real-admin']}}}
        with h.patches(),self.assertRaises(PermissionError):await self.build(h,actor=actor)
        self.assertEqual([],h.queries)

    async def test_bound_author_native_reauthorization_and_generation(self):
        for change in ['author','generation','unload']:
            h,_=self.fixture();schedule={'id':'s','kind':'energy_report','author_user_id':'real-admin'}
            actor=h.subject._schedule_actor(schedule,1)
            async def mutate(*_):
                if change=='author':h.user.is_active=False
                elif change=='generation':h.hass.data[service.DOMAIN]['schedule_generation']+=1
                else:h.subject.async_stop()
            h.on_read=mutate
            with h.patches(),self.assertRaises((PermissionError,RuntimeError)):await self.build(h,actor=actor)

    async def test_dispatch_fences_config_schedule_service_and_callable_changes(self):
        for change in ['smtp','schedule','service','send','load']:
            h,sent=self.fixture();schedule={'id':'s','kind':'energy_report','cadence':'daily'}
            h.storage.async_get_schedule=AsyncMock(return_value=deepcopy(schedule))
            original=h.subject.async_prepare_report
            async def prepare(*args,**kwargs):
                value=await original(*args,**kwargs)
                bucket=h.hass.data[service.DOMAIN]
                if change=='smtp':h.storage.current_smtp_revision+=1
                elif change=='schedule':h.storage.async_get_schedule.return_value={**schedule,'enabled':False}
                elif change=='service':bucket['energy_analysis']=object()
                elif change=='send':bucket['send_email']=lambda *_:None
                else:bucket['load_config']=AsyncMock()
                return value
            with h.patches(),patch.object(h.subject,'async_prepare_report',prepare),self.assertRaises((RuntimeError,ValueError)):
                await scheduler.async_send_report(h.hass,h.storage,kind='energy_report',cadence='daily',
                    recipients=[],now=h.now,actor='real-admin',schedule_id='s',expected_schedule=schedule)
            self.assertEqual([],sent)


    async def test_actual_send_denial_has_zero_private_and_smtp_reads_raw_counts(self):
        # Count real send-path boundaries, not merely a standalone service call.
        from unittest.mock import Mock
        provider=importlib.import_module(service.__package__+'.analysis_provider')
        for mode in ['feature','native','missing_actor','strict_authorless']:
            h,sent=self.fixture();actor='real-admin';expected=None
            load=AsyncMock(return_value={'default_recipient':'synthetic@example.invalid'})
            h.hass.data[service.DOMAIN]['load_config']=load
            if mode=='feature':h.policy={'schema':0,'features':{}}
            if mode=='native':
                calls=[]
                def revoke_on_native_lookup():
                    calls.append(1)
                    if len(calls)==2:h.user.is_active=False
                h.on_auth=revoke_on_native_lookup
            if mode=='missing_actor':actor=None
            if mode=='strict_authorless':
                actor=None  # Exercise the real private schedule capability, not an authenticated admin.
                expected={'id':'s','kind':'energy_report','cadence':'daily','enabled':True}
                h.policy['features']={'energy_reports':{'execute':{
                    'mode':'selected_users','user_ids':['real-admin']}}}
                h.storage.async_get_schedule.return_value=deepcopy(expected)
            with self.subTest(mode=mode),h.patches(), \
                    patch.object(service,'async_get_energy_metadata',wraps=service.async_get_energy_metadata) as metadata, \
                    patch.object(provider,'async_get_energy_metadata',wraps=provider.async_get_energy_metadata) as provider_metadata, \
                    patch.object(provider,'async_get_energy_usage',wraps=provider.async_get_energy_usage) as readings, \
                    patch.object(h.registry,'capture',wraps=h.registry.capture) as registry, \
                    patch.object(h.storage,'async_get_tariff_book',wraps=h.storage.async_get_tariff_book) as book_read, \
                    patch.object(h.hass.auth,'async_get_user',wraps=h.hass.auth.async_get_user) as auth:
                with self.assertRaises(PermissionError):
                    await scheduler.async_send_report(h.hass,h.storage,kind='energy_report',cadence='daily',
                        recipients=[],now=h.now,actor=actor,
                        schedule_id='s' if expected else None,expected_schedule=expected)
                raw_counts={'smtp_config':load.await_count,'metadata':metadata.await_count,
                    'provider_metadata':provider_metadata.await_count,'recorder':readings.await_count,
                    'registry':registry.call_count,'tariff_book':book_read.await_count,
                    'private_queries':len(h.queries),'registry_captures':len(h.registry_ids),
                    'schedule_read':h.storage.async_get_schedule.await_count,
                    'native_user_reads':auth.await_count,'sent':len(sent)}
                self.assertEqual({'smtp_config':0,'metadata':0,'provider_metadata':0,
                    'recorder':0,'registry':0,'tariff_book':0,'private_queries':0,
                    'registry_captures':0,'schedule_read':0,
                    'native_user_reads':2 if mode=='native' else 0,'sent':0},raw_counts)
                # Preparation failure history is retained; it is not a private Energy row read.
                self.assertEqual(0 if mode=='missing_actor' else 1,
                                 h.storage.async_append_attempt.await_count)

    async def test_revoke_during_smtp_load_reauthorizes_actual_send(self):
        import asyncio
        h,sent=self.fixture();events=[]
        original=h.subject.async_prepare_report
        async def prepare(*args,**kwargs):
            value=await original(*args,**kwargs)
            events.append('authorized_composition_finished')
            return value
        async def load():
            events.append('smtp_load')
            await asyncio.sleep(0)
            h.user.is_active=False
            return {'default_recipient':'synthetic@example.invalid'}
        cfg=AsyncMock(side_effect=load)
        h.hass.data[service.DOMAIN]['load_config']=cfg
        with h.patches(),patch.object(h.subject,'async_prepare_report',side_effect=prepare) as prepared:
            with self.assertRaises(PermissionError):
                await scheduler.async_send_report(h.hass,h.storage,kind='energy_report',cadence='daily',
                    recipients=[],now=h.now,actor='real-admin')
        self.assertEqual(['authorized_composition_finished','smtp_load'],events)
        self.assertEqual({'prepared':1,'smtp_config':1,'private_queries':2,'registry_captures':1,'sent':0},
            {'prepared':prepared.await_count,'smtp_config':cfg.await_count,
             'private_queries':len(h.queries),'registry_captures':len(h.registry_ids),'sent':len(sent)})

    async def test_changes_during_smtp_await_block_handoff_with_raw_counts(self):
        import asyncio
        changes=['smtp_revision','schedule_revision','schedule_generation','service','storage',
                 'coordinator','entry','send','load','policy_revision','tariff_revision','source','unit']
        for change in changes:
            h,sent=self.fixture()
            async def load():
                await asyncio.sleep(0)
                bucket=h.hass.data[service.DOMAIN]
                if change=='smtp_revision':h.storage.current_smtp_revision+=1
                elif change=='schedule_revision':h.storage.current_schedule_revision+=1
                elif change=='schedule_generation':bucket['schedule_generation']+=1
                elif change=='service':bucket['energy_analysis']=object()
                elif change=='storage':bucket['storage']=object()
                elif change=='coordinator':bucket['energy_snapshot']=object()
                elif change=='entry':h.entry=object()
                elif change=='send':bucket['send_email']=lambda *_:None
                elif change=='load':bucket['load_config']=AsyncMock()
                elif change=='policy_revision':h.storage.current_feature_access_revision+=1
                elif change=='tariff_revision':h.storage.current_tariff_revision+=1
                elif change=='source':h.manager.data['device_consumption'][0]['name']='Changed'
                else:h.metadata['sensor.grid']['unit_of_measurement']='Wh'
                return {'default_recipient':'synthetic@example.invalid'}
            cfg=AsyncMock(side_effect=load)
            h.hass.data[service.DOMAIN]['load_config']=cfg
            with self.subTest(change=change),h.patches(), \
                    patch.object(h.subject,'async_prepare_report',wraps=h.subject.async_prepare_report) as prepared:
                with self.assertRaises((RuntimeError,PermissionError)):
                    await scheduler.async_send_report(h.hass,h.storage,kind='energy_report',cadence='daily',
                        recipients=[],now=h.now,actor='real-admin')
                raw_counts={'prepared':prepared.await_count,'smtp_config':cfg.await_count,
                    'private_queries':len(h.queries),'registry_captures':len(h.registry_ids),'sent':len(sent)}
                self.assertEqual({'prepared':1,'smtp_config':1,'private_queries':2,'registry_captures':1,'sent':0},raw_counts)

    async def test_scheduled_reread_then_native_validation_after_smtp_load(self):
        h,sent=self.fixture();events=[]
        async def before_read(ids, start, end):
            self.assertNotIn('smtp_load', events)
        h.on_read = before_read
        expected={'id':'s','kind':'energy_report','cadence':'daily','enabled':True}
        async def load():
            events.append('smtp_load')
            return {'default_recipient':'synthetic@example.invalid'}
        async def schedule_read(_):
            events.append('schedule_reread')
            return deepcopy(expected)
        original=h.subject.async_validate_result
        async def validate(*args,**kwargs):
            if 'smtp_load' in events:events.append('native_validation')
            return await original(*args,**kwargs)
        h.hass.data[service.DOMAIN]['load_config']=AsyncMock(side_effect=load)
        h.storage.async_get_schedule=AsyncMock(side_effect=schedule_read)
        with h.patches(),patch.object(h.subject,'async_validate_result',side_effect=validate):
            result=await scheduler.async_send_report(h.hass,h.storage,kind='energy_report',cadence='daily',
                recipients=[],now=h.now,actor='real-admin',schedule_id='s',expected_schedule=expected)
        self.assertEqual(['smtp_load','schedule_reread','native_validation'],events)
        self.assertEqual('accepted_by_smtp',result['status'])
        self.assertEqual({'smtp_config':1,'schedule_read':1,'sent':1},
            {'smtp_config':h.hass.data[service.DOMAIN]['load_config'].await_count,
             'schedule_read':h.storage.async_get_schedule.await_count,'sent':len(sent)})
        # R9 and qualified 30-day colors add authorized historical reads. Every
        # read remains bounded and is completed before SMTP configuration loads.
        self.assertGreaterEqual(len(h.queries), 4)
        self.assertTrue(all(0 < (end-start).total_seconds() <= 168*3600 for _,start,end in h.queries))


    async def test_log_path_keeps_config_before_composer_without_energy_io(self):
        h,sent=self.fixture();events=[]
        async def load():
            events.append('smtp_config')
            return {'default_recipient':'synthetic@example.invalid'}
        async def compose(*args,**kwargs):
            events.append('log_composer')
            self.assertNotIn('actor',kwargs)
            return {'subject':'Synthetic log','body':'Synthetic body','html':'<p>Synthetic</p>','dedup_keys':[]}
        h.hass.data[service.DOMAIN]['load_config']=AsyncMock(side_effect=load)
        h.storage.async_set_digest_state=AsyncMock()
        with h.patches(),patch.object(composer,'async_build_report_payload',side_effect=compose) as prepared, \
                patch.object(h.subject,'async_prepare_report',wraps=h.subject.async_prepare_report) as energy_prepared:
            result=await scheduler.async_send_report(h.hass,h.storage,kind='log_digest',cadence='daily',
                recipients=[],now=h.now)
        self.assertEqual(['smtp_config','log_composer'],events)
        self.assertEqual({'smtp_config':1,'log_prepared':1,'energy_prepared':0,
                          'private_queries':0,'registry_captures':0,'sent':1,'digest_write':1},
            {'smtp_config':h.hass.data[service.DOMAIN]['load_config'].await_count,
             'log_prepared':prepared.await_count,'energy_prepared':energy_prepared.await_count,
             'private_queries':len(h.queries),'registry_captures':len(h.registry_ids),
             'sent':len(sent),'digest_write':h.storage.async_set_digest_state.await_count})
        self.assertEqual('accepted_by_smtp',result['status'])


class StoreAndReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_author_fields_rejected_and_actual_author_preserves_store(self):
        from tests.test_schedule_timezone_v3 import store, schedule
        for key in ['author_user_id','actor','actor_id','purpose','server_actor']:
            _,storage=store()
            with self.assertRaises(ValueError):
                await storage.async_upsert_schedule({**schedule(),key:'forged'})
            self.assertEqual([],storage._store.saved)
        _,storage=store()
        row={**schedule(kind='energy_report'),'future':{'keep':True}}
        storage._store.loaded={'schedules':[row],'future':{'keep':True},
            'scheduler_state':{'one':{'last_fired_period':'daily:2026-01-01'}}}
        initial=storage.current_schedule_revision
        saved=await storage.async_upsert_schedule({'id':'one','time':'08:30'},author_user_id='real-admin')
        self.assertEqual('real-admin',saved['author_user_id'])
        self.assertEqual({'keep':True},saved['future'])
        self.assertEqual(['test@example.invalid'],saved['recipients'])
        self.assertEqual(initial+1,storage.current_schedule_revision)
        self.assertEqual('daily:2026-01-01',await storage.async_get_last_fired('one'))
        storage._store.fail=True
        with self.assertRaises(OSError):
            await storage.async_upsert_schedule({'id':'one','enabled':False},author_user_id='real-admin')
        self.assertEqual(initial+1,storage.current_schedule_revision)
        self.assertTrue((await storage.async_get_schedule('one'))['enabled'])

    async def test_energy_arming_waits_for_service_logs_are_independent(self):
        import sys, types
        callbacks=[]
        def track(hass,callback,**kwargs):
            callbacks.append(callback)
            return lambda:None
        fake=types.SimpleNamespace(async_track_time_change=track)
        storage=types.SimpleNamespace(async_list_schedules=AsyncMock(return_value=[
            {'id':'e','kind':'energy_report','time':'07:30','enabled':True},
            {'id':'l','kind':'log_digest','time':'07:30','enabled':True}]))
        hass=types.SimpleNamespace(data={service.DOMAIN:{'storage':storage}})
        with patch.dict(sys.modules,{'homeassistant.helpers.event':fake}):
            await scheduler.async_reload_schedules(hass)
            self.assertEqual(1,len(callbacks))
            old_generation=hass.data[service.DOMAIN]['schedule_generation']
            hass.data[service.DOMAIN]['energy_analysis']=object()
            await scheduler.async_reload_schedules(hass)
            self.assertEqual(3,len(callbacks))
            scheduler.async_stop_scheduler(hass)
            self.assertGreater(hass.data[service.DOMAIN]['schedule_generation'],old_generation)
            self.assertEqual([],hass.data[service.DOMAIN]['scheduler_unsubs'])


class SMTPRevisionTest(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_save_only_publishes_secret_free_generation(self):
        from tests.test_schedule_timezone_v3 import store
        _,storage=store()
        storage._data={'smtp_config':{},'security':{}}
        self.assertEqual(0,storage.current_smtp_revision)
        await storage.async_save_smtp_config({'server':'synthetic.invalid'})
        self.assertEqual(1,storage.current_smtp_revision)
        storage._store.fail=True
        with self.assertRaises(OSError):
            await storage.async_save_smtp_config({'server':'other.invalid'})
        self.assertEqual(1,storage.current_smtp_revision)
        self.assertEqual('synthetic.invalid',storage._data['smtp_config']['server'])
        self.assertIs(type(storage.current_smtp_revision),int)
