from datetime import timedelta
import unittest
from tests.test_build1_energy_model import inputs_for, model

class ComparisonMaskTests(unittest.TestCase):
    def test_complete_current_and_partial_baseline_pair_the_same_hours(self):
        inputs=inputs_for(); start=model._instant(inputs['period']['start']); prior=start-timedelta(days=1)
        for row in inputs['projection']['hours']: row['kwh']='3'
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':(prior+timedelta(hours=i+1)).isoformat(),'kwh':None if i==0 else ('9' if i==1 else '5')} for i in range(24)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':start.isoformat()},'hours':hours,'scope':'full_period'}]
        report=model.build_report(inputs); c=report['comparisons'][0]
        self.assertEqual(report['totals']['import_kwh'],72)
        self.assertEqual(c['kwh'],119)
        self.assertEqual(c['compared_current_kwh'],69)
        self.assertEqual(c['compared_baseline_kwh'],119)
        self.assertEqual(c['delta_kwh'],-50)
        self.assertAlmostEqual(c['delta_pct'],100*-50/119)
        self.assertEqual(c['comparison_scope'],'common_measurements')

    def test_common_mask_below_threshold_is_not_a_comparison(self):
        inputs=inputs_for();start=model._instant(inputs['period']['start']);prior=start-timedelta(days=1)
        for i,row in enumerate(inputs['projection']['hours']):
            if i<2: row['kwh']=None
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':(prior+timedelta(hours=i+1)).isoformat(),'kwh':None if i>=22 else '2'} for i in range(24)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':start.isoformat()},'hours':hours,'scope':'full_period'}]
        c=model.build_report(inputs)['comparisons'][0]
        self.assertFalse(c['coverage_ok']);self.assertIsNone(c['delta_kwh']);self.assertEqual(c['reason'],'insufficient_common_coverage')

    def test_real_mail_shows_separate_matched_values_and_short_hour_labels(self):
        from tests.test_build1_mail_energy import Build1MailEnergyTests
        inputs=inputs_for();start=model._instant(inputs['period']['start']);prior=start-timedelta(days=1)
        for row in inputs['projection']['hours']: row['kwh']='3'
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':(prior+timedelta(hours=i+1)).isoformat(),'kwh':None if i==0 else ('9' if i==1 else '5')} for i in range(24)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':start.isoformat()},'hours':hours,'scope':'full_period'}]
        inputs['tariff']={'defaultZone':'flat','rules':[],'zones':[{'id':'flat','energyRate':'.5','rate':'.5'}]}
        for row in inputs['projection']['hours']:row['zone']='flat'
        rendered=Build1MailEnergyTests();rendered.setUp();rendered.model=model.build_report(inputs)
        for lang,matched,short,note in [('pl','Wspólne godziny','Godziny pomiarów:','Godziny nie rozróżniają dat'),('en','Matched hours','Reading hours:','Hours do not distinguish dates')]:
            result=rendered.render(language=lang)
            for body in (result['html'],result['text']):
                self.assertIn(matched,body);self.assertIn(short,body);self.assertIn(note,body)
                self.assertIn('42,0' if lang=='pl' else '42.0',body)
                self.assertEqual(body.count(note),1)

    def test_incomplete_current_displays_matched_values_and_percent(self):
        from tests.test_build1_mail_energy import Build1MailEnergyTests
        inputs=inputs_for();start=model._instant(inputs['period']['start']);prior=start-timedelta(days=1)
        inputs['projection']['hours'][0]['kwh']=None
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':(prior+timedelta(hours=i+1)).isoformat(),'kwh':'2'} for i in range(24)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':start.isoformat()},'hours':hours,'scope':'full_period'}]
        m=model.build_report(inputs);c=m['comparisons'][0]
        self.assertEqual((c['kwh'],c['compared_current_kwh'],c['compared_baseline_kwh']),(48,23,46))
        self.assertEqual(c['delta_pct'],-50)
        mail=Build1MailEnergyTests();mail.setUp();mail.model=m
        self.assertIn('−50.0%',mail.render(language='en')['text'])

    def test_short_final_intervals_are_paired_only_with_same_duration(self):
        inputs=inputs_for();start=model._instant(inputs['period']['start']);prior=start-timedelta(days=1)
        inputs['measured_until']=(start+timedelta(hours=5,minutes=30)).isoformat()
        inputs['projection']['hours']=inputs['projection']['hours'][:6]
        inputs['projection']['hours'][-1]['to']=inputs['measured_until']
        end=prior+timedelta(hours=5,minutes=30)
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':min(prior+timedelta(hours=i+1),end).isoformat(),'kwh':'2'} for i in range(6)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':end.isoformat()},'hours':hours,'scope':'full_period'}]
        c=model.build_report(inputs)['comparisons'][0];self.assertTrue(c['coverage_ok']);self.assertEqual(c['delta_kwh'],-6)

    def test_dst_fold_is_not_paired_with_the_other_occurrence(self):
        start='2026-10-25T00:00:00+02:00'; end='2026-10-26T00:00:00+01:00'
        inputs=inputs_for(start,end);current=model._instant(start);prior=current-timedelta(days=1)
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':(prior+timedelta(hours=i+1)).isoformat(),'kwh':None if i==4 else '2'} for i in range(24)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':current.isoformat()},'hours':hours,'scope':'full_period'}]
        c=model.build_report(inputs)['comparisons'][0]
        self.assertTrue(c['coverage_ok']);self.assertEqual(c['comparison_scope'],'common_measurements')
        self.assertEqual(c['compared_current_kwh'],23);self.assertEqual(c['compared_baseline_kwh'],46)

    def test_fixed_labels_translate_only_generated_canonical_id_and_default(self):
        from tests.test_build1_mail_energy import Build1MailEnergyTests
        mail=Build1MailEnergyTests();mail.setUp()
        mail.model['tariff']['fixed']=[{'id':x,'label':x,'monthly':1} for x in ('subscription','capacity','distribution','other')]+[{'id':'subscription','label':'My custom name','monthly':1}]
        pl=mail.render(language='pl')['text'];self.assertIn('Abonament',pl);self.assertIn('Opłata mocowa',pl);self.assertIn('My custom name',pl);self.assertNotIn('subscription 1,00',pl)
        mail.model['tariff'].update(fixedFeeSemantics='legacy_combined',fixed=[{'id':'fixed','label':'opłaty stałe','monthly':31}])
        en=mail.render(language='en')['text'];self.assertIn('Fixed charges',en);self.assertNotIn('opłaty stałe 31',en)

    def test_common_coverage_is_weighted_by_actual_short_tail_duration(self):
        inputs=inputs_for();start=model._instant(inputs['period']['start']);prior=start-timedelta(days=1)
        end=start+timedelta(hours=5,minutes=1);previous_end=prior+timedelta(hours=5,minutes=1)
        inputs['measured_until']=end.isoformat();inputs['projection']['hours']=inputs['projection']['hours'][:6];inputs['projection']['hours'][-1]['to']=end.isoformat()
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':min(prior+timedelta(hours=i+1),previous_end).isoformat(),'kwh':None if i==5 else '2'} for i in range(6)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':previous_end.isoformat()},'hours':hours,'scope':'full_period'}]
        c=model.build_report(inputs)['comparisons'][0]
        self.assertTrue(c['coverage_ok']);self.assertEqual(c['comparison_scope'],'common_measurements')
        self.assertEqual((c['compared_current_kwh'],c['compared_baseline_kwh']),(5,10))

    def test_unequal_complete_short_windows_do_not_claim_full_periods(self):
        from tests.test_build1_mail_energy import Build1MailEnergyTests
        inputs=inputs_for();start=model._instant(inputs['period']['start']);prior=start-timedelta(days=1)
        end=start+timedelta(hours=5,minutes=30);prev_end=prior+timedelta(hours=6)
        inputs['measured_until']=end.isoformat();inputs['projection']['hours']=inputs['projection']['hours'][:6];inputs['projection']['hours'][-1]['to']=end.isoformat()
        hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':(prior+timedelta(hours=i+1)).isoformat(),'kwh':'2'} for i in range(6)]
        inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':prev_end.isoformat()},'hours':hours,'scope':'full_period'}]
        report=model.build_report(inputs);c=report['comparisons'][0]
        self.assertFalse(c['coverage_ok']);self.assertIsNone(c['delta_kwh']);self.assertNotEqual(c['comparison_scope'],'calendar_totals')
        mail=Build1MailEnergyTests();mail.setUp();mail.model=report
        for lang,label in [('en','Full periods'),('pl','Pełne okresy')]:
            body=mail.render(language=lang)['text'];self.assertNotIn(' · '+label,body)

    def test_full_dst_days_are_explicit_calendar_totals(self):
        from tests.test_build1_mail_energy import Build1MailEnergyTests
        for start,end in [('2026-03-29T00:00:00+01:00','2026-03-30T00:00:00+02:00'),('2026-10-25T00:00:00+02:00','2026-10-26T00:00:00+01:00')]:
            inputs=inputs_for(start,end);begin=model._instant(start);prior=begin-timedelta(days=1)
            hours=[{'from':(prior+timedelta(hours=i)).isoformat(),'to':(prior+timedelta(hours=i+1)).isoformat(),'kwh':'2'} for i in range(24)]
            inputs['comparisons']=[{'key':'previous_day','period':{'start':prior.isoformat(),'end':begin.isoformat()},'hours':hours,'scope':'full_period'}]
            report=model.build_report(inputs);self.assertEqual(report['comparisons'][0]['comparison_scope'],'calendar_totals')
            mail=Build1MailEnergyTests();mail.setUp();mail.model=report
            self.assertIn('Full periods',mail.render(language='en')['text'])
