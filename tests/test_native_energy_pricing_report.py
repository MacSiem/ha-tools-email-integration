"""One native HA recorded cost in HTML/text/CSV, without a tariff bill claim."""
import copy
import csv
from io import StringIO
import json
from pathlib import Path
import unittest
from tests.test_energy_report import REPORT, ReportDOM


class NativeCostReportTest(unittest.TestCase):
    def test_native_cost_primary_and_csv_preserve_value_currency_and_unknown_components(self):
        for language in ('en','pl'):
            for amount in ('58.50','0','-12.50'):
                s = json.loads((Path(__file__).parent/'fixtures/energy-report-schema2-v53.json').read_text())
                s['active_tariff_id'] = None
                for key in ('energy_cost','variable_cost','usage_cost','fixed_cost','total_cost','effective_price'):
                    s['totals'][key] = None
                s['totals']['currency'] = None
                s['rates'] = []; s['fees'] = {}
                s['native_pricing'] = {'schema':1,'source':'ha_energy','period_cost':{
                    'status':'ready','amount':amount,'recorded_amount':amount,'currency':'EUR',
                    'scope':'grid_import_recorded_cost','coverage':{'complete':True,'expected_source_hours':24,'measured_source_hours':24},
                    'components_status':'unknown','fixed_fees_status':'unknown','reason':None},
                    'current_prices':[{'source_id':'sensor.grid','origin':'number_energy_price','price':'999',
                    'currency':'EUR','energy_unit':'kWh','observed_at':'2026-10-19T12:00:00+00:00',
                    'historical':False,'status':'ready','reason':None}],
                    'provenance':[{'energy_source_id':'sensor.grid','cost_statistic_id':'sensor.cost'}]}
                before = copy.deepcopy(s)
                r = REPORT.render_energy_analysis(s,language=language,timezone_name='Europe/Warsaw',hour_cycle='24')
                root = ReportDOM(r['html']).root
                primary = root.find(data_section='kpis')[0].text()
                label = 'Koszt zapisany w HA' if language=='pl' else 'Cost recorded in HA'
                value = amount.replace('.',',') if language=='pl' else amount
                self.assertIn(label,primary)
                self.assertIn(value,primary)
                self.assertIn(label,r['text'])
                self.assertIn('Rozbicie kosztu i opłaty stałe są nieznane' if language=='pl' else 'Cost breakdown and fixed fees are unknown',r['text'])
                self.assertIn('Bieżące ceny · teraz' if language=='pl' else 'Current prices · now',r['text'])
                records = {row['field']:row for row in csv.DictReader(StringIO(r['csv']))}
                row = records['native_pricing.period_cost.amount']
                self.assertEqual(value,row['value'])
                self.assertEqual('EUR',row['unit'])
                self.assertEqual('EUR/kWh',records['native_pricing.current_prices.0.price']['unit'])
                self.assertEqual(before,s)
