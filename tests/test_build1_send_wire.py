"""K7 wiring and private-principal contracts; owner runs through FIFO."""
import ast
from copy import deepcopy
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import pickle
import types
import unittest
from unittest.mock import AsyncMock, patch
from tests import test_mail67_delivery as mail67
from tests.test_energy_analysis_service import service

scheduler = importlib.import_module(service.__package__ + '.scheduler')
periods = importlib.import_module(service.__package__ + '.report_window')
ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
UTC = timezone.utc


def extract(path, name, namespace):
    node = next(n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []; node.returns = None
    for arg in (*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs):arg.annotation = None
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    return namespace[name]


def harness():
    h, _ = mail67.ActualDeliveryTest().fixture()
    h.now = datetime(2026,1,11,12,tzinfo=UTC)
    return h


def request(h):return periods.resolve_report_window('daily',h.now,'UTC',mode='calendar')


class PrivateActorTest(unittest.IsolatedAsyncioTestCase):
    async def test_authored_calendar_model_binds_original_capability_and_rechecks_author(self):
        h = harness()
        actor = h.subject._schedule_actor({'id':'s','kind':'energy_report','author_user_id':'real-admin'},1)
        with h.patches():
            result = await h.subject.async_get_analysis('report_window',actor,purpose='report_execute',_report_window=request(h),_report_model=True)
            self.assertEqual('ha_tools.energy_report/1',result['report_model']['schema'])
            self.assertEqual(h.subject._actor_signature(actor),result['context']['actor_signature'])
            self.assertTrue(await h.subject.async_validate_result(result,actor))
            h.user.is_active = False
            with self.assertRaises(PermissionError):await h.subject.async_validate_result(result,actor)
        self.assertIsNone(h.subject._cache)

    async def test_authored_schedule_rechecks_admin_before_io_with_explicit_allowlist(self):
        # Canonical feature policy intentionally grants active administrators.
        # A schedule must still lose access when its real author loses admin.
        h = harness();h.policy['features']={'energy_reports':{'execute':{'mode':'selected_users','user_ids':[]}}}
        actor=h.subject._schedule_actor({'id':'s','kind':'energy_report','author_user_id':'real-admin'},1)
        with h.patches():
            result=await h.subject.async_get_analysis('report_window',actor,purpose='report_execute',_report_window=request(h),_report_model=True)
            self.assertTrue(await h.subject.async_validate_result(result,actor))
            h.user.is_admin=False;h.queries.clear()
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('report_window',actor,purpose='report_execute',_report_window=request(h),_report_model=True)
        self.assertEqual([],h.queries)

    async def test_trusted_automation_inherits_server_policy_without_user_or_fake_schedule(self):
        h = harness();actor=h.subject._automation_actor()
        h.hass.auth.async_get_user=AsyncMock(side_effect=AssertionError('no synthetic HA user'))
        with h.patches():
            result=await h.subject.async_get_analysis('report_window',actor,purpose='report_execute',_report_window=request(h),_report_model=True)
            self.assertTrue(await h.subject.async_validate_result(result,actor))
        h.hass.auth.async_get_user.assert_not_awaited()
        self.assertFalse(hasattr(actor,'schedule'))
        with self.assertRaises(TypeError):pickle.dumps(actor)
        with self.assertRaises(TypeError):json.dumps({'actor':actor})
        h.hass.data[service.DOMAIN]['schedule_generation'] += 1
        with self.assertRaises(RuntimeError):actor.check()

    async def test_authorless_automation_fails_closed_for_explicit_non_admin_policy(self):
        for rule in ({'mode':'household'},{'mode':'selected_users','user_ids':[]},{'mode':'selected_users','user_ids':['real-admin']}):
            h=harness();h.policy['features']={'energy_reports':{'execute':rule}};actor=h.subject._automation_actor()
            with self.subTest(rule=rule),h.patches(),self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('report_window',actor,purpose='report_execute',_report_window=request(h),_report_model=True)
            self.assertEqual([],h.queries)

    async def test_explicit_admin_policy_permits_trusted_automation_without_principal_lookup(self):
        h=harness();h.policy['features']={'energy_reports':{'execute':{'mode':'admin'}}}
        actor=h.subject._automation_actor()
        h.hass.auth.async_get_user=AsyncMock(side_effect=AssertionError('no synthetic user'))
        config,period,context=h.subject._capture_report(request(h),'report_execute',h.subject._actor_signature(actor),report_model=True)
        with h.patches():await h.subject._require_access(context,config,actor)
        h.hass.auth.async_get_user.assert_not_awaited()
        self.assertEqual([],h.queries)

    async def test_automation_cannot_be_reused_for_read_purpose(self):
        h=harness();actor=h.subject._automation_actor()
        with h.patches(),self.assertRaises(PermissionError):
            await h.subject.async_get_analysis('report_window',actor,purpose='report_read',_report_window=request(h),_report_model=True)
        self.assertEqual([],h.queries)


class DateWindowTest(unittest.TestCase):
    def test_date_selects_real_closed_ha_calendar_and_dst(self):
        now=datetime(2026,10,26,12,tzinfo=UTC)
        a,b=scheduler.resolve_energy_date_window('daily','2026-10-25',now,'Europe/Warsaw')['report_window']
        self.assertEqual('2026-10-24T22:00:00+00:00',a.isoformat())
        self.assertEqual('2026-10-25T23:00:00+00:00',b.isoformat())
        self.assertEqual(25*3600,(b-a).total_seconds())
        a,b=scheduler.resolve_energy_date_window('daily','2026-03-29',now,'Europe/Warsaw')['report_window']
        self.assertEqual(23*3600,(b-a).total_seconds())
        a,b=scheduler.resolve_energy_date_window('weekly','2026-10-14',now,'Europe/Warsaw')['report_window']
        self.assertEqual('2026-10-12',a.astimezone(scheduler.ZoneInfo('Europe/Warsaw')).date().isoformat())
        for value in ('2026-10-26','2026-10-27','2026-02-30','26-10-2026',{},True):
            with self.subTest(value=value),self.assertRaises(ValueError):scheduler.resolve_energy_date_window('daily',value,now,'Europe/Warsaw')


class SendWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_ws_channels_additive_energy_only_and_unknown_no_replay(self):
        send=AsyncMock(return_value={'status':'accepted_by_notify'})
        ns={'async_send_now':send,'__package__':service.__package__}
        handler=extract(ROOT/'websocket_api.py','_ws_send_now',ns)
        results,errors=[],[]
        conn=types.SimpleNamespace(user=types.SimpleNamespace(id='real-admin'),send_result=lambda *a:results.append(a),send_error=lambda *a:errors.append(a))
        base={'id':1,'kind':'energy_report','cadence':'daily'}
        await handler(object(),conn,base)
        self.assertNotIn('channels',send.await_args.kwargs)
        await handler(object(),conn,{**base,'channels':['notify:mobile_app_owner','persistent_notification']})
        self.assertEqual(['notify.mobile_app_owner','persistent_notification'],send.await_args.kwargs['channels'])
        self.assertEqual('real-admin',send.await_args.kwargs['actor'])
        count=send.await_count
        await handler(object(),conn,{**base,'kind':'log_digest','channels':['persistent_notification']})
        self.assertEqual(count,send.await_count);self.assertEqual('send_failed',errors[-1][1])
        send.side_effect=TimeoutError('unknown handoff')
        await handler(object(),conn,{**base,'channels':['persistent_notification']})
        self.assertEqual(count+1,send.await_count)

    async def test_action_context_uses_private_actor_and_response_is_serializable(self):
        h=harness();send=AsyncMock(return_value={'status':'accepted_by_notify','channel':'persistent_notification','attempt_id':'a'})
        ns={'hass':h.hass,'DOMAIN':service.DOMAIN,'ServiceValidationError':ValueError,'__package__':service.__package__}
        handler=extract(ROOT/'__init__.py','handle_send_energy_report',ns)
        call=types.SimpleNamespace(context=types.SimpleNamespace(user_id=None),data={'date':'2026-01-10','targets':['persistent_notification']})
        with patch.object(scheduler,'async_send_now',send):result=await handler(call)
        actor=send.await_args.kwargs['actor']
        self.assertIs(actor.service,h.subject);self.assertFalse(hasattr(actor,'schedule'))
        self.assertEqual('2026-01-10',send.await_args.kwargs['date'])
        self.assertEqual('accepted_by_notify',result['result']);self.assertEqual('persistent_notification',result['channel'])
        self.assertIsNotNone(datetime.fromisoformat(result['at']).utcoffset());json.dumps(result)
        call.context.user_id='real-admin'
        with patch.object(scheduler,'async_send_now',send):await handler(call)
        self.assertEqual('real-admin',send.await_args.kwargs['actor'])
        send.reset_mock();send.side_effect=TimeoutError('unknown native handoff')
        with patch.object(scheduler,'async_send_now',send),self.assertRaises(TimeoutError):await handler(call)
        self.assertEqual(1,send.await_count)

    async def test_dated_send_forwards_window_and_real_clock_once_without_config_change(self):
        now=datetime(2026,10,26,12,tzinfo=UTC)
        class FixedDateTime(datetime):
            @classmethod
            def now(cls,tz=None):return now.astimezone(tz) if tz else now.replace(tzinfo=None)
        hass=types.SimpleNamespace(config=types.SimpleNamespace(time_zone='Europe/Warsaw'),data={service.DOMAIN:{'storage':object()}})
        before=deepcopy(hass.config.__dict__);send=AsyncMock(return_value={'status':'accepted_by_notify'})
        with patch.object(scheduler,'datetime',FixedDateTime),patch.object(scheduler,'async_send_report',send):
            await scheduler.async_send_now(hass,kind='energy_report',cadence='daily',actor='real-admin',channels=['persistent_notification'],date='2026-10-25',period_mode='calendar')
        self.assertEqual(1,send.await_count);self.assertEqual(now,send.await_args.kwargs['now'])
        a,b=send.await_args.kwargs['report_window'];self.assertEqual(25*3600,(b-a).total_seconds())
        self.assertEqual(before,hass.config.__dict__)
        self.assertEqual('previous_completed_calendar_period',send.await_args.kwargs['period_semantics'])
