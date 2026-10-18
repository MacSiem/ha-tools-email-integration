"""Report route derives caller identity and independent purpose server-side."""
from copy import deepcopy
import unittest
from tests import test_tariff_book_websocket as fixtures


class EnergyReportWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws,hass,storage,lane,connection,results,errors=fixtures.BookWebsocketTest().fixture()
        calls=[]
        class Report:
            valid=True
            validate_result=True
            hook=None
            validate_hook=None
            purpose='report_read'
            async def async_get_report(self,kind,user_id,**options):
                calls.append((kind,user_id,options))
                if self.hook:self.hook()
                return {'snapshot_id':'synthetic','context':{'access_purpose':self.purpose},'period':{},'html':'<table>synthetic</table>','text':'synthetic','csv':'synthetic'}
            async def async_validate_result(self,result,user_id):
                if self.validate_hook:self.validate_hook()
                user=await hass.auth.async_get_user(user_id)
                state=await storage.async_get_feature_access()
                if not ws.feature_access_allowed(state['policy'],'energy_reports','read',user_id=user_id,is_active=user.is_active,is_admin=user.is_admin):
                    raise PermissionError('private-policy-error')
                return self.validate_result
            def context_is_current(self,result):return self.valid
        service=Report();hass.data['ha_tools_email']['energy_analysis']=service
        msg={'id':1,'period':'week','language':'pl','timezone':'Europe/Warsaw','hour_cycle':'24','user_id':'forged','purpose':'analysis'}
        return ws,hass,storage,service,connection,results,errors,calls,msg

    async def test_admin_report_needs_no_smtp_and_ignores_forged_identity_or_purpose(self):
        ws,hass,storage,service,connection,results,errors,calls,msg=self.fixture()
        await ws._ws_energy_report(hass,connection,msg)
        self.assertEqual([],errors);self.assertEqual(1,len(results))
        self.assertEqual([('week','synthetic',{'language':'pl','timezone_name':'Europe/Warsaw','hour_cycle':'24'})],calls)
        self.assertNotIn('load_config',hass.data['ha_tools_email'])
        self.assertFalse(getattr(ws._ws_energy_report,'tariff_requires_admin',False))

    async def test_household_default_denied_before_service(self):
        ws,hass,storage,service,connection,results,errors,calls,msg=self.fixture()
        connection.user.is_admin=False
        await ws._ws_energy_report(hass,connection,msg)
        self.assertEqual([],calls);self.assertEqual([],results);self.assertEqual('unauthorized',errors[-1][1])

    async def test_explicit_read_delegation_keeps_execute_and_settings_independent(self):
        ws,hass,storage,service,connection,results,errors,calls,msg=self.fixture()
        connection.user.is_admin=False
        await storage.async_save_feature_access({'schema':1,'features':{'energy_reports':{'read':{'mode':'household'}}}},expected_revision=0)
        await ws._ws_energy_report(hass,connection,msg)
        self.assertEqual([],errors);self.assertEqual(1,len(results))
        self.assertTrue(getattr(ws._ws_get_config,'tariff_requires_admin',False))

    async def test_revoked_permission_before_final_send_never_exposes_report(self):
        ws,hass,storage,service,connection,results,errors,calls,msg=self.fixture()
        service.hook=lambda:setattr(connection.user,'is_admin',False)
        await ws._ws_energy_report(hass,connection,msg)
        self.assertEqual([],results);self.assertEqual('unauthorized',errors[-1][1]);self.assertNotIn('private',errors[-1][2])

    async def test_replaced_service_wrong_purpose_or_stale_result_never_sent(self):
        for failure in ('replace','purpose','stale','invalid'):
            ws,hass,storage,service,connection,results,errors,calls,msg=self.fixture()
            if failure=='replace':service.validate_hook=lambda:hass.data['ha_tools_email'].update(energy_analysis=object())
            elif failure=='purpose':service.purpose='analysis'
            elif failure=='stale':service.valid=False
            else:service.validate_result=False
            await ws._ws_energy_report(hass,connection,msg)
            self.assertEqual([],results);self.assertEqual('energy_report_unavailable',errors[-1][1])

    async def test_private_failure_is_safe_and_no_send_occurs(self):
        ws,hass,storage,service,connection,results,errors,calls,msg=self.fixture()
        def fail():raise RuntimeError('private smtp path')
        service.hook=fail
        await ws._ws_energy_report(hass,connection,msg)
        self.assertEqual([],results);self.assertNotIn('private',errors[-1][2]);self.assertEqual('energy_report_unavailable',errors[-1][1])


    async def test_date_language_forwards_as_optional_preference(self):
        ws,hass,storage,service,connection,results,errors,calls,msg=self.fixture()
        msg['date_language']='en'
        await ws._ws_energy_report(hass,connection,msg)
        self.assertEqual([],errors)
        self.assertEqual('en',calls[0][2]['date_language'])
        self.assertEqual('pl',calls[0][2]['language'])
        self.assertEqual(1,len(results))
