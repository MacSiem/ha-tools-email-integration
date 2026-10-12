"""Retained HA log aggregates: zero, unknown, stable identity and mail parity."""
from datetime import datetime, timedelta, timezone
import copy
import importlib.util
from pathlib import Path
import unittest
spec=importlib.util.spec_from_file_location('log_counts_composer',Path(__file__).resolve().parents[1]/'custom_components/ha_tools_email/composer.py');c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
NOW=datetime(2026,10,12,12,tzinfo=timezone.utc)
def row(**kw):return dict(level='ERROR',name='fixture',source=['fixture.py',10],message=['same'],timestamp=NOW.timestamp()-100,count=1,**kw)
class RetainedCounts(unittest.TestCase):
 def payload(self,rows):return c.build_log_digest_payload(rows,cadence='daily',now=NOW)
 def test_known_zero_stays_zero_in_summary_plain_and_html(self):
  r=row();r['count']=0;p=self.payload([r]);self.assertEqual(0,p['summary']['total']);self.assertIn('x0',p['body']);self.assertIn('x0',p['html']);self.assertNotIn('x1',p['html'])
 def test_unknown_malformed_counts_do_not_become_one_or_measured_total(self):
  for v in [None,True,False,'2','oops',-1,1.5,float('nan'),float('inf'),2**53]:
   with self.subTest(v=v):
    r=row();r['count']=v;p=self.payload([r]);self.assertIsNone(p['summary']['total']);self.assertIsNone(p['summary']['level_counts']['ERROR']);self.assertIn('Unknown',p['body']);self.assertNotIn('None',p['body']);self.assertNotIn('x1',p['html'])
  r=row();r.pop('count');self.assertIsNone(self.payload([r])['summary']['total'])
 def test_stable_id_is_same_aggregate_maximum_and_latest_representation(self):
  a=row(id='a');a['count']=4;b={**a,'count':7,'timestamp':NOW.timestamp()-80,'message':['changed'],'name':'latest'};before=copy.deepcopy([a,b]);p=self.payload([a,b]);self.assertEqual(7,p['summary']['total']);self.assertEqual(1,len(p['summary']['entries']));self.assertEqual('latest',p['summary']['entries'][0]['name']);self.assertEqual(before,[a,b])
 def test_exact_structured_fallback_sums_and_never_flattens_source_identity(self):
  a=row();a['count']=4;b={**a,'count':7,'timestamp':NOW.timestamp()-80};different={**a,'message':'same','count':2};p=self.payload([a,b,different]);self.assertEqual(13,p['summary']['total']);self.assertEqual(2,len(p['summary']['entries']))
 def test_unknown_in_duplicate_or_sum_overflow_remains_unknown(self):
  a=row(id='a');b={**a,'count':None};self.assertIsNone(self.payload([a,b])['summary']['total']);a=row();a['count']=2**53-1;b={**a,'count':1};self.assertIsNone(self.payload([a,b])['summary']['total'])
 def test_future_records_are_excluded_and_malformed_timestamps_do_not_crash(self):
  future=row();future['timestamp']=(NOW+timedelta(days=1)).timestamp();self.assertEqual(0,self.payload([future])['summary']['total'])
  for t in [float('nan'),float('inf'),10**100,True,'garbage']:
   with self.subTest(t=t):
    r=row();r['timestamp']=t;p=self.payload([r]);self.assertTrue(p['summary']['source']['complete'] is False)
 def test_future_duplicate_does_not_hide_a_valid_retained_aggregate(self):
  a=row(id='same');a['count']=3;b={**a,'count':999,'timestamp':(NOW+timedelta(days=1)).timestamp()};self.assertEqual(3,self.payload([a,b])['summary']['total'])
 def test_total_overflow_across_levels_is_not_a_complete_measured_count(self):
  a=row(id='a');a['count']=2**53-1;b={**row(id='b'),'level':'WARNING'};p=self.payload([a,b]);self.assertIsNone(p['summary']['total']);self.assertFalse(p['summary']['counts_complete'])
 def test_epoch_zero_is_a_real_timestamp(self):
  self.assertEqual(datetime(1970,1,1,tzinfo=timezone.utc),c._entry_time({'timestamp':0}))
class ReadonlyCounts(unittest.IsolatedAsyncioTestCase):
 async def test_preview_preserves_zero_and_unknown_and_returns_json_without_sending(self):
  import types,json
  from unittest.mock import patch
  try:
   from tests.test_schedule_timezone_v3 import load_websocket
  except ImportError:
   from test_schedule_timezone_v3 import load_websocket
  ws=load_websocket(self)
  zero=row();zero.update(timestamp=(NOW-timedelta(days=1)).timestamp(),count=0)
  unknown={**zero,'name':'unknown','level':'WARNING','count':None}
  hass=types.SimpleNamespace(data={'system_log':types.SimpleNamespace(records=[zero,unknown])},config=types.SimpleNamespace(time_zone='UTC'))
  results=[]
  conn=types.SimpleNamespace(user=types.SimpleNamespace(is_admin=True),send_result=lambda *a:results.append(a),send_error=lambda *a:self.fail(str(a)))
  with patch.object(ws,'_utcnow',lambda:NOW):
   await ws._ws_preview_log_digest(hass,conn,{'id':1,'cadence':'daily'})
  payload=results[0][1];self.assertIsNone(payload['total']);self.assertFalse(payload['counts_complete']);self.assertEqual(2,payload['unique_problems']);self.assertEqual(0,payload['recent_errors'][0]['count']);self.assertIsNone(payload['recent_warnings'][0]['count']);json.dumps(payload,allow_nan=False)
if __name__=='__main__':unittest.main()
