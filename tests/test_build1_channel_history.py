"""Additive history metadata with real dispatch/Store and fake external handoff."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from tests import test_attempt_history as fixtures
from tests.test_energy_analysis_service import service

history = importlib.import_module(service.__package__ + '.history')
delivery = importlib.import_module(service.__package__ + '.delivery')
composer = importlib.import_module(service.__package__ + '.composer')
scheduler = importlib.import_module(service.__package__ + '.scheduler')
NOW = datetime(2026,10,20,12,tzinfo=timezone.utc)
PERIOD = {'start':'2026-10-16T22:00:00+00:00','end':'2026-10-17T22:00:00+00:00',
          'timezone':'Europe/Warsaw','semantics':'previous_completed_calendar_period'}


def legacy_record():return fixtures.record(timestamp=NOW.isoformat())


def environment():
    storage = fixtures.storage()
    native = types.SimpleNamespace(_actor_signature=Mock(),_check_running=Mock(),
        context_is_current=Mock(return_value=True),async_validate_result=AsyncMock(return_value=True))
    sent = Mock()
    async def executor(fn,*args):return fn(*args)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone='Europe/Warsaw'),
        services=types.SimpleNamespace(async_call=AsyncMock()),async_add_executor_job=executor)
    hass.data={service.DOMAIN:{'storage':storage,'energy_analysis':native,'schedule_generation':0,
        'load_config':AsyncMock(return_value={'default_recipient':'PRIVATE@example.invalid'}),'send_email':sent}}
    payload={'subject':'PRIVATE SUBJECT','body':'PRIVATE BODY','html':'<p>PRIVATE HTML</p>',
             'period':deepcopy(PERIOD),'notify':{'title':'PRIVATE TITLE','message':'PRIVATE MESSAGE'}}
    return hass,storage,native,sent,payload


class CleanHistoryTests(unittest.TestCase):
    def test_old_attempts_remain_byte_equivalent_without_optional_fields(self):
        expected=legacy_record()
        for extra in ({},{'recipients':['PRIVATE@example.invalid'],'body':'PRIVATE'},
                      {'channel':'light.turn_on','batch_id':'PRIVATE','report_period':{'content':'PRIVATE'}}):
            raw={**expected,**extra}
            cleaned=history.clean_attempt(raw)
            self.assertEqual(json.dumps(expected,separators=(',',':')),json.dumps(cleaned,separators=(',',':')))
            self.assertEqual([expected],history.retained_attempts({'schema':0,'attempts':[raw]},NOW))
        self.assertEqual(1,history.HISTORY_SCHEMA)

    def test_channel_allowlist_canonical_uuid_and_bounded_batch_are_additive(self):
        batch=str(uuid.uuid4())
        raw={**legacy_record(),'channel':'notify.mobile_app_owner','batch_id':batch,'batch_size':2,'batch_index':1,
             'report_period':deepcopy(PERIOD),'user_id':'PRIVATE','subject':'PRIVATE'}
        clean=history.clean_attempt(raw)
        self.assertEqual(raw['channel'],clean['channel']);self.assertEqual(batch,clean['batch_id'])
        self.assertEqual(PERIOD,clean['report_period'])
        self.assertNotIn('PRIVATE',repr(clean));self.assertNotIn('user_id',clean)
        class HaText(str):pass
        yaml_text=history.clean_attempt({**raw,'channel':HaText('notify.mobile_app_owner'),
            'report_period':{**PERIOD,'timezone':HaText('Europe/Warsaw')}})
        self.assertIs(type(yaml_text['channel']),str);self.assertIs(type(yaml_text['report_period']['timezone']),str)
        for channel in ('notify:mobile_app_owner','notify.bad.dot','notify.'+'a'*129,'email@example.invalid',[],None):
            with self.subTest(channel=channel):
                value=history.clean_attempt({**legacy_record(),'channel':channel})
                self.assertNotIn('channel',value)
        for size,index in ((True,1),(33,1),(2,0),(2,3),(1,1)):
            with self.subTest(size=size,index=index):
                value=history.clean_attempt({**legacy_record(),'batch_id':batch,'batch_size':size,'batch_index':index})
                self.assertNotIn('batch_id',value)

    def test_report_bounds_are_aware_aligned_complete_bounded_and_drop_private_fields(self):
        invalid=[{**PERIOD,'timezone':'PRIVATE@example.invalid'},
            {**PERIOD,'start':'2026-10-16T22:00:00'},
            {**PERIOD,'end':'2026-10-21T00:00:00+00:00'},
            {**PERIOD,'end':'2026-10-16T22:00:00+00:00'},
            {**PERIOD,'start':'2026-10-16T22:01:00+00:00'},
            {**PERIOD,'semantics':'delivered'}, {**PERIOD,'recipients':['PRIVATE@example.invalid']}]
        for period in invalid:
            with self.subTest(period=period):
                self.assertNotIn('report_period',history.clean_attempt({**legacy_record(),'report_period':period}))
        metadata=history.report_attempt_metadata('daily',PERIOD,now=NOW)
        self.assertEqual('daily:2026-10-17',metadata['period'])
        self.assertEqual(PERIOD,metadata['report_period'])


class DispatcherHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_native_dispatch_persists_validated_channel_and_unknown_once(self):
        hass,storage,native,sent,payload=environment()
        async def validate():pass
        result=await delivery.async_dispatch_notification(hass,storage,'notify:mobile_app_owner',
            AsyncMock(return_value=payload['notify']),validate,now=NOW,cadence='daily',period='daily:2026-10-17')
        self.assertEqual('accepted_by_notify',result['status'])
        rows=(await storage.async_get_attempt_history(now=NOW))['attempts']
        self.assertEqual('notify.mobile_app_owner',rows[0]['channel'])
        self.assertEqual(NOW.isoformat(),rows[0]['timestamp'])
        self.assertNotIn('PRIVATE',repr(rows));self.assertNotIn('@',repr(rows))
        hass.services.async_call.side_effect=TimeoutError('unknown outcome')
        with self.assertRaises(TimeoutError):
            await delivery.async_dispatch_notification(hass,storage,'persistent_notification',
                AsyncMock(return_value=payload['notify']),validate,now=NOW+timedelta(seconds=1))
        rows=(await storage.async_get_attempt_history(now=NOW+timedelta(seconds=1)))['attempts']
        self.assertEqual('transport_outcome_unknown',rows[0]['outcome'])
        self.assertEqual('persistent_notification',rows[0]['channel'])
        self.assertEqual(2,hass.services.async_call.await_count)
        sent.assert_not_called()

    async def test_selected_historical_period_and_real_attempt_clock_share_one_mixed_target_batch(self):
        hass,storage,native,sent,payload=environment()
        with patch.object(composer,'async_build_report_payload',AsyncMock(return_value=payload)) as prepare:
            result=await delivery.async_send_energy_channels(hass,storage,channels=['persistent_notification','email'],
                cadence='daily',actor='real-admin',now=NOW,report_window=tuple(datetime.fromisoformat(PERIOD[k]) for k in ('start','end')),
                report_timezone='Europe/Warsaw',period_semantics=PERIOD['semantics'])
        self.assertEqual(1,prepare.await_count);self.assertEqual(1,sent.call_count)
        self.assertEqual(1,hass.services.async_call.await_count)
        rows=(await storage.async_get_attempt_history(now=NOW))['attempts']
        self.assertEqual(2,len(rows));self.assertEqual({'persistent_notification','email'},{row['channel'] for row in rows})
        self.assertEqual(1,len({row['batch_id'] for row in rows}))
        for row in rows:
            self.assertEqual(2,row['batch_size']);self.assertIn(row['batch_index'],(1,2))
            self.assertEqual('daily:2026-10-17',row['period'])
            self.assertEqual(PERIOD,row['report_period']);self.assertEqual(NOW.isoformat(),row['timestamp'])
        self.assertEqual({'accepted_by_notify','accepted_by_smtp'},{row['outcome'] for row in rows})
        self.assertNotIn('PRIVATE',repr(rows));self.assertNotIn('@',repr(rows))
        self.assertEqual(2,len(result['channels']))

    async def test_single_channel_has_no_batch_and_legacy_smtp_has_no_new_fields(self):
        hass,storage,native,sent,payload=environment()
        with patch.object(composer,'async_build_report_payload',AsyncMock(return_value=payload)):
            await delivery.async_send_energy_channels(hass,storage,channels=['persistent_notification'],
                cadence='daily',actor='real-admin',now=NOW)
        row=(await storage.async_get_attempt_history(now=NOW))['attempts'][0]
        self.assertNotIn('batch_id',row)
        await history.async_dispatch_attempt(hass,storage,sent,
            AsyncMock(return_value=({},'PRIVATE@example.invalid','subject','body',None)),now=NOW+timedelta(seconds=1))
        row=(await storage.async_get_attempt_history(now=NOW+timedelta(seconds=1)))['attempts'][0]
        self.assertTrue({'channel','batch_id','batch_size','batch_index','report_period'}.isdisjoint(row))

    async def test_mixed_batch_stops_at_unknown_and_never_replays_earlier_or_remaining_channel(self):
        hass,storage,native,sent,payload=environment()
        hass.services.async_call.side_effect=[None,TimeoutError('unknown')]
        with patch.object(composer,'async_build_report_payload',AsyncMock(return_value=payload)),self.assertRaises(TimeoutError):
            await delivery.async_send_energy_channels(hass,storage,
                channels=['persistent_notification','notify.mobile_app_owner','email'],cadence='daily',actor='real-admin',now=NOW)
        rows=(await storage.async_get_attempt_history(now=NOW))['attempts']
        self.assertEqual(2,len(rows));self.assertEqual(1,len({row['batch_id'] for row in rows}))
        self.assertEqual({1,2},{row['batch_index'] for row in rows})
        self.assertEqual({3},{row['batch_size'] for row in rows})
        self.assertEqual(2,hass.services.async_call.await_count);sent.assert_not_called()
        hass.data[service.DOMAIN]['load_config'].assert_not_awaited()

    async def test_invalid_dispatch_metadata_rejects_before_prepare_or_handoff(self):
        hass,storage,native,sent,payload=environment()
        prepare=AsyncMock(return_value=payload['notify']);handoff=AsyncMock()
        with self.assertRaises(ValueError):
            await history.async_dispatch_channel_attempt(storage,prepare,handoff,channel='light.turn_on',now=NOW)
        prepare.assert_not_awaited();handoff.assert_not_awaited()
        self.assertEqual([], (await storage.async_get_attempt_history(now=NOW))['attempts'])

    async def test_actual_legacy_calendar_smtp_records_selected_bounds_without_adding_channel(self):
        hass,storage,native,sent,payload=environment()
        bounds=tuple(datetime.fromisoformat(PERIOD[key]) for key in ('start','end'))
        with patch.object(composer,'async_build_report_payload',AsyncMock(return_value=payload)):
            await scheduler.async_send_report(hass,storage,kind='energy_report',cadence='daily',
                recipients=[],now=NOW,actor='real-admin',report_window=bounds,report_timezone='Europe/Warsaw',
                period_semantics='previous_completed_calendar_period')
        row=(await storage.async_get_attempt_history(now=NOW))['attempts'][0]
        self.assertEqual('daily:2026-10-17',row['period']);self.assertEqual(PERIOD,row['report_period'])
        self.assertEqual(NOW.isoformat(),row['timestamp']);self.assertNotIn('channel',row)
        self.assertNotIn('batch_id',row);sent.assert_called_once()

    async def test_week_and_month_keys_use_selected_local_window_not_attempt_date(self):
        week={**PERIOD,'start':'2025-12-28T23:00:00+00:00','end':'2026-01-04T23:00:00+00:00'}
        month={**PERIOD,'start':'2026-08-31T22:00:00+00:00','end':'2026-09-30T22:00:00+00:00'}
        self.assertEqual('weekly:2026-W01',history.report_attempt_metadata('weekly',week,now=NOW)['period'])
        self.assertEqual('monthly:2026-09',history.report_attempt_metadata('monthly',month,now=NOW)['period'])
        rolling={**PERIOD,'semantics':'rolling_completed_hours'}
        self.assertNotIn('period',history.report_attempt_metadata('daily',rolling,now=NOW))
