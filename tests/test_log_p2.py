"""Synthetic mixed retained aggregates; expected counts are independently fixed."""
import asyncio, json, types, unittest
from datetime import datetime,timezone
from pathlib import Path
from tests.test_composer import composer as c
ROWS=json.loads((Path(__file__).parent/'fixtures/log-p2-mixed.json').read_text())
START=datetime(2026,11,2,tzinfo=timezone.utc);END=datetime(2026,11,3,tzinfo=timezone.utc);NOW=datetime(2026,11,3,1,tzinfo=timezone.utc)
class LogP2(unittest.TestCase):
 def make(self,language):
  hass=types.SimpleNamespace(data={'system_log':types.SimpleNamespace(records=ROWS)},config=types.SimpleNamespace(time_zone='UTC'))
  return asyncio.run(c.async_build_log_digest_payload(hass,object(),cadence='daily',now=NOW,window_start=START,window_end=END,manual_window=True,presentation=c._presentation(language,'UTC')))
 def test_mixed_bounds_known_lifetime_and_diagnostic_survive_both_formats(self):
  for lang in ('pl','en'):
   with self.subTest(lang=lang):
    p=self.make(lang);s=p['summary'];self.assertEqual((3,14),(s['unique_problems'],s['total']));self.assertEqual({'ERROR':9,'WARNING':2,'INFO':3},s['level_counts']);self.assertEqual(40000,len(json.loads(s['recent_errors'][0]['message'][0])['noise']))
    for key in ('html','body'):
     t=p[key];self.assertIn('DEVICE_OFFLINE_SYNTHETIC',t);self.assertLess(len(t),25000);self.assertNotIn("x"*1000,t);self.assertIn('Current only',t);self.assertNotIn('Historical only',t)
     self.assertIn('zachowan' if lang=='pl' else 'retained',t.lower());self.assertNotIn('Total counted warnings/errors',t);self.assertNotIn('Counts by level' if lang=='pl' else 'Liczby według poziomu',t)
 def test_stable_identity_crossing_boundary_is_not_counted_as_old_window(self):
  current={**ROWS[1], 'timestamp':END.timestamp()+20, 'count':12}
  hass=types.SimpleNamespace(data={'system_log':types.SimpleNamespace(records=ROWS+[current])},config=types.SimpleNamespace(time_zone='UTC'))
  p=asyncio.run(c.async_build_log_digest_payload(hass,object(),cadence='daily',now=NOW,window_start=START,window_end=END,manual_window=True))
  self.assertEqual((2,5),(p['summary']['unique_problems'],p['summary']['total']))
  self.assertEqual(16,p['summary']['current_context']['total'])
 def test_undated_errors_are_visible_but_not_attributed_to_period(self):
  rows=ROWS+[{'id':'unknown-time','name':'fixture.undated','level':'ERROR','message':['UNPLACED_SYNTHETIC'],'count':5}]
  hass=types.SimpleNamespace(data={'system_log':types.SimpleNamespace(records=rows)},config=types.SimpleNamespace(time_zone='UTC'))
  for lang in ('pl','en'):
   p=asyncio.run(c.async_build_log_digest_payload(hass,object(),cadence='daily',now=NOW,window_start=START,window_end=END,manual_window=True,presentation=c._presentation(lang,'UTC')))
   self.assertEqual((3,14),(p['summary']['unique_problems'],p['summary']['total']))
   for part in ('body','html'):
    self.assertIn('UNPLACED_SYNTHETIC',p[part]);self.assertIn('Nieznany czas' if lang=='pl' else 'Unknown time',p[part])
 def test_non_json_repr_reason_after_large_prefix_is_preserved_and_bounded(self):
  message="{'message':'one','message':'two','message':'three','noise': '" + 'x'*9000 + "', 'reason': 'REPR_TIMEOUT_SYNTHETIC', 'tail': '" + 'y'*9000 + "'}"
  for language in ('pl','en'):
   p=c.build_log_digest_payload([{'id':'repr','level':'ERROR','name':'fixture','timestamp':NOW.timestamp(),'count':1,'message':[message]}],cadence='daily',now=NOW,presentation=c._presentation(language,'UTC'))
   for key in ('html','body'):
    self.assertIn('REPR_TIMEOUT_SYNTHETIC',p[key]);self.assertNotIn('x'*1000,p[key])
   self.assertEqual(message,p['summary']['entries'][0]['message'][0])
 def test_exception_reason_is_preserved_beyond_message_and_truncated_traceback(self):
  message='Connection failed';exception='Traceback '+('x'*3000)+" {'reason': 'AUTH_EXPIRED_SYNTHETIC', 'tail': '"+('y'*1000)+"'}"
  p=c.build_log_digest_payload([{'id':'trace','level':'ERROR','name':'fixture','timestamp':NOW.timestamp(),'count':1,'message':[message+'\n'+exception],'exception':'PRIVATE_TRACE_SYNTHETIC'}],cadence='daily',now=NOW)
  for part in ('html','body'):
   self.assertIn('AUTH_EXPIRED_SYNTHETIC',p[part]);self.assertIn('Connection failed',p[part]);self.assertNotIn('x'*1000,p[part])
  self.assertIn(exception,p['summary']['entries'][0]['message'][0]);self.assertNotIn('PRIVATE_TRACE_SYNTHETIC',p['body'])
 def test_polish_core_headings_are_localized(self):
  p=self.make('pl')
  for key in ('html','body'):
   for label in ('Liczby według poziomu','Najczęstsze źródła','Ostatnie błędy'):
    self.assertIn(label,p[key])
   self.assertNotIn('Top loggers',p[key])
class LogPreviewP2(unittest.IsolatedAsyncioTestCase):
 async def test_calendar_preview_groups_before_window_and_excludes_unknown_time(self):
  from unittest.mock import patch
  from tests.test_schedule_timezone_v3 import load_websocket
  ws=load_websocket(self);current={**ROWS[1],'timestamp':END.timestamp()+20,'count':12}
  rows=ROWS+[current,{'id':'undated','name':'fixture.undated','level':'ERROR','message':['unplaced'],'count':5}]
  hass=types.SimpleNamespace(data={'system_log':types.SimpleNamespace(records=rows)},config=types.SimpleNamespace(time_zone='UTC'))
  result=[];conn=types.SimpleNamespace(user=types.SimpleNamespace(is_admin=True),send_result=lambda *args:result.append(args[1]),send_error=lambda *args:self.fail(str(args)))
  with patch.object(ws,'_utcnow',lambda:NOW):await ws._ws_preview_log_digest(hass,conn,{'id':1,'cadence':'daily'})
  self.assertEqual((2,5),(result[0]['unique_problems'],result[0]['total']))
  self.assertEqual(1,result[0]['source']['undated_entries'])
