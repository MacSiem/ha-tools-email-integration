"""Mail-only outage line uses authorized report gaps and shares formatting."""
import copy
import importlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from tests.test_energy_partial_sources_20261104 import model, mail, sample
from tests.test_energy_report import REPORT
from tests.test_energy_analysis_service import Harness, service

helper = importlib.import_module(model.__package__ + '.energy_stale_mail')
NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)

class StaleMailTest(unittest.TestCase):
    def setUp(self):
        self.report = model.build_report(sample())
        self.status = {'b': {'stale': True, 'since': '2026-11-01T12:00:00+00:00'},
                       'secret': {'stale': True, 'since': '2026-10-01T12:00:00+00:00'}}

    def line(self, report=None, status=None, language='pl'):
        return helper.stale_mail_line(report or self.report, self.status if status is None else status,
            language=language, timezone_name='UTC', now=NOW)

    def test_localized_single_line_is_escaped_and_precedes_missing_list(self):
        for lang, expected, missing in [('pl','<script>B</script> — brak aktualizacji od 1 lis (17 dni)','Brakujące źródła:'),
                                         ('en','<script>B</script> — no updates since Nov 1 (17 days)','Missing sources:')]:
            line = self.line(language=lang)
            self.assertEqual(expected,line)
            out=mail.render(self.report,language=lang,timezone_name='UTC',hour_cycle='24',stale_line=line)
            self.assertEqual(1,out['text'].count(line))
            self.assertLess(out['text'].index(line),out['text'].index(missing))
            self.assertLess(out['html'].index('data-role="stale-source"'),out['html'].index(missing))
            self.assertIn('&lt;script&gt;B&lt;/script&gt;',out['html'])
            self.assertNotIn('<script>B</script>',out['html'])

    def test_only_intersecting_missing_ids_and_oldest(self):
        other=copy.deepcopy(self.report)
        other['coverage']['missing_sources'].append({'id':'c','name':'Older authorized',
            'ranges':[{'start':'2026-11-03T00:00:00+00:00','end':'2026-11-04T00:00:00+00:00'}]})
        statuses={**self.status,'c':{'stale':True,'since':'2026-10-25T00:00:00+00:00'}}
        self.assertTrue(self.line(other,statuses).startswith('Older authorized'))
        statuses['c']['since']='2026-11-17T13:00:00+00:00'
        self.assertTrue(self.line(other,statuses).startswith('<script>B</script>'))
        self.assertIsNone(self.line(status={'secret':self.status['secret']}))
        before=copy.deepcopy(self.report);before['period']['end']='2026-10-31T00:00:00+00:00';before['period']['start']='2026-10-30T00:00:00+00:00'
        self.assertIsNone(self.line(before))
        after_gap=copy.deepcopy(self.report)
        after_gap['coverage']['missing_sources'][0]['ranges'][0]['end']='2026-10-31T00:00:00+00:00'
        after_gap['coverage']['missing_devices']=[]
        self.assertIsNone(self.line(after_gap))

    def test_schema2_actual_string_ids_require_full_outage_overlap(self):
        snapshot=json.loads((Path(__file__).parent/'fixtures/energy-report-schema2-v53.json').read_text())
        snapshot['coverage']['missing_sources']=['sensor.device']
        status={'sensor.device':{'stale':True,'since':'2026-10-01T00:00:00+00:00'},
                'sensor.secret':{'stale':True,'since':'2026-09-01T00:00:00+00:00'}}
        line=self.line(snapshot,status,language='en')
        self.assertEqual('Desk — no updates since Oct 1 (48 days)',line)
        rendered=REPORT.render_energy_analysis(snapshot,language='en',timezone_name='UTC',hour_cycle='24',stale_line=line)
        self.assertIn(line,rendered['text']);self.assertIn(line,rendered['html'])
        # A missing bucket elsewhere in the window is not proof that it
        # overlaps a later outage, even though the ID itself is authorized.
        status['sensor.device']['since']='2026-10-10T12:00:00+00:00'
        self.assertIsNone(self.line(snapshot,status,language='en'))
        status['sensor.device']['since']='2026-10-01T00:00:00+00:00'
        snapshot['period']['end']='2026-11-19T00:00:00+00:00'
        self.assertIsNone(self.line(snapshot,status,language='en'))
        snapshot['period']['end']='2026-10-11T00:00:00+00:00'
        snapshot['groups']['devices'].pop('sensor.device')
        self.assertTrue(self.line(snapshot,status,language='en').startswith('sensor.device —'))
        snapshot['coverage']['missing_sources']=['sensor.other']
        self.assertIsNone(self.line(snapshot,status,language='en'))

    def test_legacy_renderer_uses_same_line_without_csv_change(self):
        snapshot=json.loads((Path(__file__).parent/'fixtures/energy-report-schema2-v53.json').read_text())
        for lang in ('pl','en'):
            opts={'language':lang,'timezone_name':'UTC','hour_cycle':'24'}
            line=self.line(language=lang)
            plain=REPORT.render_energy_analysis(snapshot,**opts)
            alert=REPORT.render_energy_analysis(snapshot,stale_line=line,**opts)
            self.assertEqual(plain['csv'],alert['csv'])
            self.assertEqual(1,alert['text'].count(line))
            self.assertIn('data-role="stale-source"',alert['html'])
            self.assertIn('&lt;script&gt;B&lt;/script&gt;',alert['html'])

class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_render_passes_same_optional_line_to_both_renderers(self):
        h=Harness()
        # Isolate transport/renderer boundaries while exercising the actual bridge.
        snapshot={'totals':{},'report_model':{'schema':'ha_tools.energy_report/1'},
                  'snapshot_id':'id','context':{}}
        legacy=importlib.import_module(service.__package__+'.energy_report')
        native=importlib.import_module(service.__package__+'.mail_energy')
        selector=importlib.import_module(service.__package__+'.energy_stale_mail')
        captured=[]
        def a(s,**kw):
            captured.append(kw.get('stale_line'));return {'csv':'csv'}
        def b(s,**kw):
            captured.append(kw.get('stale_line'));return {k:k for k in ('subject','preheader','html','text','notify')}
        async def valid(*args):return True
        with h.patches(),patch.object(selector,'stale_mail_line',return_value='verified'), \
                patch.object(legacy,'render_energy_analysis',a),patch.object(native,'render',b), \
                patch.object(legacy,'append_partial_model_csv',return_value='csv'), \
                patch.object(h.subject,'async_validate_result',valid):
            await h.subject._async_render_energy_artifact(snapshot,'user',{'language':'en','timezone_name':'UTC','hour_cycle':'24'})
        self.assertEqual(['verified','verified'],captured)
