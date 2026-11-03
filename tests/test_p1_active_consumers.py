import unittest
from decimal import Decimal
from tests.test_tariff_history import history, version, book, rows_between
from tests.test_build1_energy_model import inputs_for, model
from tests.test_build1_mail_energy import Build1MailEnergyTests

class ActiveP1Tests(Build1MailEnergyTests):
    def test_unlabelled_flat_tariff_from_model_has_named_rate_in_mail(self):
        inputs = inputs_for()
        inputs['tariff'] = {'name':'G11','valid_from':'2026-01-02','defaultZone':'flat','rules':[],'exceptions':[],
            'zones':[{'id':'flat','energyRate':'.5','variableRate':None,'rate':'.5'}]}
        for row in inputs['projection']['hours']: row['zone'] = 'flat'
        self.model = model.build_report(inputs)
        for lang, label in [('pl','Całodobowa'),('en','All-day')]:
            result = self.render(language=lang)
            rates=result['text'].split('Koszty i stawki' if lang=='pl' else 'Costs and rates',1)[1]
            self.assertIn('- '+label+' (', rates)
            self.assertIn(label, result['html'])
            self.assertNotIn('- '+('Brak danych' if lang=='pl' else 'No data')+' (',rates)

class AggregateFeeTests(unittest.TestCase):
    def test_legacy_fee_is_known_without_inventing_components(self):
        v=version(start='2026-11-01',rate=.5,rate_semantics='legacy_combined',
                  fixed_monthly=dict.fromkeys(('subscription','capacity','distribution','other')),
                  legacy_combined_monthly=31)
        start,end='2026-11-02T00:00Z','2026-11-03T00:00Z'
        result=history._price(book([v])['tariffs'][0],rows_between(start,end,3),{'from':start,'to':end},'synthetic',include_report_projection=True)
        self.assertAlmostEqual(float(result['report_fixed_share']),31/30)
        self.assertEqual(result['usage_cost'],'36.00')
        self.assertIsNone(result['fees']['components']['subscription'])
        self.assertFalse(result['components_complete'])
        inputs=inputs_for()
        inputs['projection']['fixed_share']=result['report_fixed_share']
        for row in inputs['projection']['hours']:
            row['kwh']='3'; row['costs'].update(energy_cost=None,variable_cost=None,usage_cost='1.5')
        report=model.build_report(inputs)
        self.assertAlmostEqual(report['totals']['fixed_share'],31/30)
        self.assertAlmostEqual(report['totals']['total_with_fixed'],36+31/30)

    def test_fee_partial_and_history_and_missing_history(self):
        from datetime import datetime
        def stamp(value): return datetime.fromisoformat(value).timestamp()*1000
        a=version(start='2026-11-01',rate=.5,rate_semantics='legacy_combined',fixed_monthly=dict.fromkeys(('subscription','capacity','distribution','other')),legacy_combined_monthly=30)
        a['valid_to']='2026-11-03'
        b=version(start='2026-11-03',version_id='b',monthly=60)
        for start,end,expected in [('2026-11-02T12:00+00:00','2026-11-03T00:00+00:00',.5),('2026-11-02T00:00+00:00','2026-11-04T00:00+00:00',3)]:
            fee,amounts,_,ids=history._fees([a,b],stamp(start),stamp(end))
            self.assertTrue(fee['total_known']);self.assertAlmostEqual(float(sum(amounts.values())),expected)
        fee,_,_,_=history._fees([b],stamp('2026-11-02T00:00+00:00'),stamp('2026-11-03T00:00+00:00'))
        self.assertFalse(fee['total_known'])
        b['profile']['currency']='PLN'
        result=history._price(book([a,b])['tariffs'][0],rows_between('2026-11-02T00:00Z','2026-11-04T00:00Z'),{'from':'2026-11-02T00:00Z','to':'2026-11-04T00:00Z'},'synthetic',include_report_projection=True)
        self.assertIsNone(result['report_fixed_share']);self.assertIsNone(result['total_cost'])

    def test_paired_comparison_legacy_aggregate_and_validation(self):
        import importlib
        from unittest.mock import patch
        from tests.test_energy_analysis_service import service
        stream=importlib.import_module(service.__package__+'.energy_comparison_stream')
        v=version(start='2026-11-01',rate=.5,rate_semantics='legacy_combined',fixed_monthly=dict.fromkeys(('subscription','capacity','distribution','other')),legacy_combined_monthly=30)
        from copy import deepcopy
        invalid=book([deepcopy(v)]);invalid['tariffs'][0]['versions'][0]['fixed_monthly']['subscription']=0
        with self.assertRaises(history.TariffValidationError): history.validate_tariff_book(invalid)
        obj=object.__new__(stream.PairedComparisonAccumulator);obj._versions=[v]
        obj._periods=[{'start':'2026-11-02T00:00Z','end':'2026-11-03T00:00Z'}]
        usage={'compatible':True,'pricing_complete':True,'usage_cost':'36.00','energy_cost':None,'variable_cost':None,'legacy_combined_cost':'36.00'}
        with patch.object(stream,'finish_usage_cost',return_value=usage):
            row=obj._cost_side({'kwh':Decimal(72)},0,True,True)
        self.assertEqual('1.00',row['fixed_cost']);self.assertEqual('37.00',row['total_cost'])
        self.assertIsNone(row['fees']['components']['subscription'])
