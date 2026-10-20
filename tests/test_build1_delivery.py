"""Notify uses the same single-attempt lifecycle without pretending delivery."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
ROOT=Path(__file__).resolve().parents[1]/'custom_components/ha_tools_email'
pkg=types.ModuleType('build1_delivery_fixture');pkg.__path__=[str(ROOT)]
with patch.dict(sys.modules,{pkg.__name__:pkg}):
 spec=importlib.util.spec_from_file_location(pkg.__name__+'.delivery',ROOT/'delivery.py')
 delivery=importlib.util.module_from_spec(spec);spec.loader.exec_module(delivery)

class DeliveryTargetsTest(unittest.TestCase):
 def test_default_uses_only_proven_owner_mobile_or_persistent(self):
  services={'notify':{'mobile_app_owner':{},'mobile_app_guest':{},'telegram':{}},'persistent_notification':{'create':{}}}
  targets=delivery.list_delivery_targets(services)
  self.assertEqual(next(x['id'] for x in targets if x['default']),'persistent_notification')
  self.assertNotIn('email',[x['id'] for x in targets])
  self.assertEqual(next(x['id'] for x in delivery.list_delivery_targets(services,default_mobile_target='notify.mobile_app_owner') if x['default']),'notify.mobile_app_owner')
  self.assertEqual(next(x['id'] for x in delivery.list_delivery_targets(services,default_mobile_target='notify.not_present') if x['default']),'persistent_notification')
 def test_migration_preserves_explicit_email_and_rejects_arbitrary_service(self):
  self.assertEqual(delivery.normalize_channels(None,legacy=True),['email'])
  self.assertEqual(delivery.normalize_channels(None),['persistent_notification'])
  self.assertEqual(delivery.normalize_channels(['notify:mobile_app_owner','email','email']),['notify.mobile_app_owner','email'])
  for invalid in [[],['light.turn_on'],['notify:bad.dot'],['notify:' ],'email']:
   with self.subTest(invalid=invalid),self.assertRaises(ValueError):delivery.normalize_channels(invalid)

class NotifyDispatchTest(unittest.IsolatedAsyncioTestCase):
 async def test_fences_before_handoff_one_attempt_no_smtp(self):
  order=[]
  async def fence():order.append('fence')
  async def compose():order.append('compose');return {'title':'Energy','message':'18.8 kWh','data':{'tag':'energy-daily'}}
  async def call(*args,**kwargs):order.append('call')
  hass=types.SimpleNamespace(services=types.SimpleNamespace(async_call=AsyncMock(side_effect=call)))
  storage=types.SimpleNamespace(async_append_attempt=AsyncMock())
  result=await delivery.async_dispatch_notification(hass,storage,'notify.mobile_app_owner',compose,fence,cadence='daily',period='daily:2026-10-17')
  self.assertEqual(order,['fence','compose','fence','call'])
  self.assertEqual(result['status'],'accepted_by_notify')
  self.assertEqual(storage.async_append_attempt.await_count,1)
  args=hass.services.async_call.call_args
  self.assertEqual(args.args[:2],('notify','mobile_app_owner'))
  self.assertTrue(args.kwargs['blocking'])
  self.assertEqual(storage.async_append_attempt.call_args.args[0]['outcome'],'accepted_by_notify')
 async def test_changed_context_blocks_io_and_records_preparation_failure(self):
  fence=AsyncMock(side_effect=[None,RuntimeError('changed')]);compose=AsyncMock(return_value={'title':'x','message':'x'})
  hass=types.SimpleNamespace(services=types.SimpleNamespace(async_call=AsyncMock()));storage=types.SimpleNamespace(async_append_attempt=AsyncMock())
  with self.assertRaisesRegex(RuntimeError,'changed'):await delivery.async_dispatch_notification(hass,storage,'persistent_notification',compose,fence)
  hass.services.async_call.assert_not_awaited()
  self.assertEqual(storage.async_append_attempt.call_args.args[0]['outcome'],'preparation_failed')
 async def test_unknown_transport_is_recorded_once_without_retry(self):
  hass=types.SimpleNamespace(services=types.SimpleNamespace(async_call=AsyncMock(side_effect=TimeoutError())))
  storage=types.SimpleNamespace(async_append_attempt=AsyncMock())
  with self.assertRaises(TimeoutError):await delivery.async_dispatch_notification(hass,storage,'persistent_notification',AsyncMock(return_value={'title':'x','message':'x'}),AsyncMock())
  self.assertEqual(hass.services.async_call.await_count,1)
  self.assertEqual(storage.async_append_attempt.call_args.args[0]['outcome'],'transport_outcome_unknown')
 async def test_successful_handoff_remains_success_if_history_write_fails(self):
  hass=types.SimpleNamespace(services=types.SimpleNamespace(async_call=AsyncMock()));storage=types.SimpleNamespace(async_append_attempt=AsyncMock(side_effect=OSError()))
  result=await delivery.async_dispatch_notification(hass,storage,'persistent_notification',AsyncMock(return_value={'title':'x','message':'x','data':{'secret':'not_for_persistent'}}),AsyncMock())
  self.assertEqual(result['status'],'accepted_by_notify');self.assertEqual(result['history_status'],'unavailable')
  self.assertEqual(hass.services.async_call.call_args.args[2],{'title':'x','message':'x'})
