"""Preview74: actual legacy handlers, fixed safe errors and final publication fence.

Only composition and service IO boundaries are mocked. Do not bypass decorators
or copy TestCase classes into discovery. Owner executes RED/GREEN through FIFO.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_schedule_timezone_v3 import load_websocket

NOW = datetime(2026, 10, 18, 12, 30, tzinfo=timezone.utc)
PRIVATE = 'private SMTP credential/source path'


class Preview74SafeErrorsTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws = load_websocket(self)
        results, errors = [], []
        forbidden = Mock(side_effect=AssertionError('preview SMTP/history/write'))
        storage = SimpleNamespace(async_append_attempt=forbidden, async_save=forbidden)
        service = SimpleNamespace(async_validate_result=AsyncMock(return_value=True),
            context_is_current=Mock(return_value=True))
        hass = SimpleNamespace(config=SimpleNamespace(time_zone='UTC'),
            data={'ha_tools_email': {'storage':storage, 'energy_analysis':service,
                'load_config':forbidden, 'send_email':forbidden}})
        conn = SimpleNamespace(user=SimpleNamespace(id='authenticated-admin', is_admin=True, is_active=True),
            send_result=lambda *args:results.append(args), send_error=lambda *args:errors.append(args))
        payload = {'snapshot_id':'one-prepared-artifact', 'subject':'Energy report',
            'body':'Energy body', 'html':'<p>Energy</p>', 'csv':'snapshot_id,one-prepared-artifact',
            'period':{'timezone':'UTC','start':(NOW-timedelta(days=1)).isoformat(),'end':NOW.isoformat()},
            'summary':{'total_kwh':'24','source':{'name':'recorder','complete':True}},
            'context':{'access_purpose':'report_read','service_generation':'generation-one'}}
        build = AsyncMock(return_value=payload)
        for boundary in (patch.object(ws,'_utcnow',lambda:NOW),
                         patch.object(ws,'async_build_report_payload',build)):
            boundary.start(); self.addCleanup(boundary.stop)
        return SimpleNamespace(ws=ws,hass=hass,conn=conn,service=service,storage=storage,
            payload=payload,build=build,forbidden=forbidden,results=results,errors=errors)

    async def energy(self, f, **extra):
        await f.ws._ws_preview_energy_report(f.hass,f.conn,{'id':7,'cadence':'daily',**extra})

    def safe_error(self, f, code='energy_report_unavailable'):
        self.assertEqual([],f.results)
        message='Report or source access required.' if code=='unauthorized' else 'Energy report is unavailable.'
        self.assertEqual([(7,code,message)],f.errors)
        self.assertNotIn(PRIVATE,repr(f.errors))
        f.forbidden.assert_not_called()

    async def test_energy_composition_denial_and_private_failures_have_fixed_safe_errors(self):
        for failure,code in [(PermissionError(PRIVATE),'unauthorized'),
                             (ValueError(PRIVATE),'energy_report_unavailable'),
                             (RuntimeError(PRIVATE),'energy_report_unavailable')]:
            with self.subTest(error=type(failure).__name__):
                f=self.fixture();f.build.side_effect=failure
                await self.energy(f,format='mail')
                self.safe_error(f,code)
                f.service.async_validate_result.assert_not_awaited()
                self.assertEqual('authenticated-admin',f.build.await_args.kwargs['actor'])
                self.assertEqual('report_read',f.build.await_args.kwargs['purpose'])

    async def test_energy_validation_denial_exception_false_or_non_boolean_never_publish(self):
        for failure,value,code in [(PermissionError(PRIVATE),None,'unauthorized'),
                                   (RuntimeError(PRIVATE),None,'energy_report_unavailable'),
                                   (None,False,'energy_report_unavailable'),
                                   (None,1,'energy_report_unavailable'),
                                   (None,None,'energy_report_unavailable')]:
            with self.subTest(error=type(failure).__name__,value=value):
                f=self.fixture();f.service.async_validate_result.side_effect=failure
                f.service.async_validate_result.return_value=value
                await self.energy(f)
                self.safe_error(f,code)
                f.service.context_is_current.assert_not_called()

    async def test_final_context_denial_or_private_exception_is_safe(self):
        for failure,value,code in [(PermissionError(PRIVATE),None,'unauthorized'),
                                   (RuntimeError(PRIVATE),None,'energy_report_unavailable'),
                                   (None,False,'energy_report_unavailable')]:
            with self.subTest(error=type(failure).__name__,value=value):
                f=self.fixture();f.service.context_is_current.side_effect=failure
                f.service.context_is_current.return_value=value
                await self.energy(f)
                self.safe_error(f,code)

    async def test_missing_service_never_composes_or_publishes(self):
        f=self.fixture();f.hass.data['ha_tools_email'].pop('energy_analysis')
        await self.energy(f)
        self.safe_error(f)
        f.build.assert_not_awaited()

    async def test_service_replacement_during_composition_or_validation_cannot_publish(self):
        for boundary in ('composition','validation'):
            with self.subTest(boundary=boundary):
                f=self.fixture()
                replacement=SimpleNamespace(async_validate_result=AsyncMock(return_value=True),
                    context_is_current=Mock(return_value=True))
                async def compose(*args,**kwargs):
                    f.hass.data['ha_tools_email']['energy_analysis']=replacement
                    return f.payload
                async def validate(*args,**kwargs):
                    f.hass.data['ha_tools_email']['energy_analysis']=replacement
                    return True
                if boundary=='composition':f.build.side_effect=compose
                else:f.service.async_validate_result.side_effect=validate
                await self.energy(f,format='mail')
                self.safe_error(f)
                replacement.async_validate_result.assert_not_awaited()
                replacement.context_is_current.assert_not_called()

    async def test_wrong_purpose_and_service_generation_change_cannot_publish(self):
        for boundary in ('purpose','generation'):
            with self.subTest(boundary=boundary):
                f=self.fixture();generation=['generation-one']
                f.service.context_is_current.side_effect=lambda payload:payload['context']['service_generation']==generation[0]
                async def validate(*args,**kwargs):
                    if boundary=='purpose':f.payload['context']['access_purpose']='report_execute'
                    else:generation[0]='generation-two'
                    return True
                f.service.async_validate_result.side_effect=validate
                await self.energy(f)
                self.safe_error(f)

    async def test_energy_success_summary_and_mail_shapes_and_final_fence_order(self):
        for mail in (False,True):
            with self.subTest(mail=mail):
                f=self.fixture();events=[];before=deepcopy(f.payload)
                async def validate(*args,**kwargs):events.append('validate');return True
                def current(payload):events.append('context');return True
                f.service.async_validate_result.side_effect=validate
                f.service.context_is_current.side_effect=current
                f.conn.send_result=lambda *args:(events.append('publish'),f.results.append(args))
                await self.energy(f,**({'format':'mail'} if mail else {}))
                self.assertEqual(['validate','context','publish'],events)
                self.assertEqual([],f.errors);self.assertEqual(before,f.payload)
                result=f.results[0][1]
                expected={**{key:before[key] for key in ('snapshot_id','subject','body','html','csv','period')},
                    'cadence':'daily','generated_at':NOW.isoformat(),'effective_timezone':'UTC',
                    'source':before['summary']['source']} if mail else before['summary']
                self.assertEqual([(7,expected)],f.results)
                f.service.async_validate_result.assert_awaited_once_with(f.payload,'authenticated-admin')
                f.forbidden.assert_not_called()

    async def test_invalid_parameters_remain_invalid_payload_before_composition(self):
        f=self.fixture()
        await self.energy(f,format='mail',period_mode='calendar',calendar_period=False)
        self.assertEqual([],f.results)
        self.assertEqual('invalid_payload',f.errors[0][1])
        self.assertEqual('calendar_period conflicts with period_mode',f.errors[0][2])
        f.build.assert_not_awaited();f.forbidden.assert_not_called()

    async def test_admin_decorator_denies_before_any_composition(self):
        f=self.fixture();f.conn.user.is_admin=False
        await self.energy(f)
        self.assertEqual([],f.results);self.assertEqual('unauthorized',f.errors[0][1])
        f.build.assert_not_awaited();f.forbidden.assert_not_called()

    async def test_log_mail_output_payload_and_exception_semantics_are_unchanged(self):
        f=self.fixture();f.payload['summary']['period']={'from':'start','to':'end'}
        before=deepcopy(f.payload)
        await f.ws._ws_preview_log_digest(f.hass,f.conn,{'id':7,'cadence':'daily','format':'mail','max_entries':3})
        expected={'subject':before['subject'],'body':before['body'],'html':before['html'],
            'cadence':'daily','period':before['summary']['period'],'source':before['summary']['source'],
            'generated_at':NOW.isoformat()}
        self.assertEqual([(7,expected)],f.results);self.assertEqual([],f.errors)
        self.assertNotIn('actor',f.build.await_args.kwargs);self.assertNotIn('purpose',f.build.await_args.kwargs)
        self.assertEqual(3,f.build.await_args.kwargs['max_entries'])
        f.service.async_validate_result.assert_not_awaited();f.service.context_is_current.assert_not_called()
        f.forbidden.assert_not_called()
        for error in (PermissionError(PRIVATE),ValueError(PRIVATE)):
            f.build.side_effect=error;f.results.clear()
            with self.assertRaises(type(error)) as raised:
                await f.ws._ws_preview_log_digest(f.hass,f.conn,{'id':7,'cadence':'daily','format':'mail'})
            self.assertIs(error,raised.exception)
            self.assertEqual([],f.results);self.assertEqual([],f.errors)
