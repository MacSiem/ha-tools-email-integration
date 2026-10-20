"""The model wire is additive; document users retain their exact route."""
import types
import unittest
from unittest.mock import AsyncMock,Mock,patch
from tests.test_tariff_profile import load_websocket

class ModelWebsocketTest(unittest.IsolatedAsyncioTestCase):
 async def test_model_returns_only_canonical_model_after_final_revalidation(self):
  ws,_=load_websocket();model={'schema':'ha_tools.energy_report/1'};envelope={'period':{'kind':'day'},'context':{'private':'internal'},'report_model':model}
  service=types.SimpleNamespace(async_get_build1_model=AsyncMock(return_value=envelope),async_validate_result=AsyncMock(return_value=True),context_is_current=Mock(return_value=True))
  results=[];hass=types.SimpleNamespace(data={'ha_tools_email':{'energy_analysis':service}})
  connection=types.SimpleNamespace(user=types.SimpleNamespace(id='owner'),send_result=lambda *a:results.append(a),send_error=Mock())
  with patch.object(ws,'_feature_authorized',AsyncMock(return_value=True)):
   await ws._ws_energy_report(hass,connection,{'id':1,'period':'day','date':'2026-10-17','mode':'full','representation':'model'})
  self.assertEqual([(1,model)],results);service.async_get_build1_model.assert_awaited_once_with('day','owner',date='2026-10-17',mode='full',purpose='analysis')
  service.async_validate_result.assert_awaited_once_with(envelope,'owner')
 async def test_late_revocation_or_stale_context_never_releases_model(self):
  for revalidated,current in [(False,True),(True,False)]:
   ws,_=load_websocket();service=types.SimpleNamespace(async_get_build1_model=AsyncMock(return_value={'report_model':{'schema':'ha_tools.energy_report/1'}}),async_validate_result=AsyncMock(return_value=revalidated),context_is_current=Mock(return_value=current))
   results=[];errors=[];hass=types.SimpleNamespace(data={'ha_tools_email':{'energy_analysis':service}});connection=types.SimpleNamespace(user=types.SimpleNamespace(id='owner'),send_result=lambda *a:results.append(a),send_error=lambda *a:errors.append(a))
   with patch.object(ws,'_feature_authorized',AsyncMock(return_value=True)):
    await ws._ws_energy_report(hass,connection,{'id':1,'representation':'model','period':'day','mode':'so_far'})
   self.assertEqual([],results);self.assertTrue(errors)
 async def test_legacy_document_request_keeps_language_and_exact_report_route(self):
  ws,_=load_websocket();report={'html':'<p>document</p>','text':'document','csv':'raw'};service=types.SimpleNamespace(async_get_report=AsyncMock(return_value=report),async_validate_result=AsyncMock(return_value=True),context_is_current=Mock(return_value=True))
  results=[];hass=types.SimpleNamespace(data={'ha_tools_email':{'energy_analysis':service}});connection=types.SimpleNamespace(user=types.SimpleNamespace(id='owner'),send_result=lambda *a:results.append(a),send_error=Mock())
  with patch.object(ws,'_feature_authorized',AsyncMock(return_value=True)):
   await ws._ws_energy_report(hass,connection,{'id':2,'period':'day','language':'en','timezone':'UTC','hour_cycle':'24'})
  self.assertEqual([(2,report)],results);service.async_get_report.assert_awaited_once()
