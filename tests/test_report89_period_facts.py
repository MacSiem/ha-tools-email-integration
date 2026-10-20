"""Report89: proved period facts, non-overlapping groups, readable mail numbers.

The actual accumulator supplies 250/247/58/192/0.232. Group metadata reproduces
saved native88 precision and missing-room conditions. Owner executes via FIFO.
"""
from copy import deepcopy
import csv
from io import StringIO
import json
from pathlib import Path
import unittest

from tests import test_energy_report as report_fixture
from tests import test_energy_period_summary as period_fixture

REPORT=report_fixture.REPORT


class Report89PeriodFactsTests(unittest.TestCase):
    def fixture(self):
        # Actual classification, priced stream and household period proof.
        helper=period_fixture.EnergyPeriodSummaryTest()
        book=helper.classified_book(cheap_rate=0.5,expensive_rate=1)
        version=book['tariffs'][0]['versions'][0]
        version['fixed_monthly']['subscription']=31
        for zone in version['profile']['zones']:zone['variableRate']=0.1
        value=helper.build([58,192]+[0]*22,book=book).finish()
        captured=json.loads((Path(__file__).parent/'fixtures/energy-report-schema2-v53.json').read_text())
        groups=deepcopy(captured['groups'])
        item=groups['devices']['sensor.device']
        item.update(name='QA80 Office',kwh='49.9999999999995450',recorded_kwh='49.9999999999995450',
                    area_id=None,area_name=None,floor_name=None,usage_cost='49.20')
        groups['areas']={}
        for row in [groups['unassigned'],groups['floors']['upstairs']]:
            row.update(kwh='49.9999999999995450',recorded_kwh='49.9999999999995450',usage_cost='49.20')
        groups['unmeasured'].update(kwh='200.0000000000004550',recorded_kwh='200.0000000000004550',usage_cost='196.80')
        value['groups']=groups
        # Fixture identity covers these captured synthetic consumer facts.
        unsigned=deepcopy(value);unsigned.pop('snapshot_id')
        value['snapshot_id']=period_fixture.analysis._digest(unsigned)
        return value

    def render(self,value,language='en'):
        return REPORT.render_energy_analysis(value,language=language,timezone_name='UTC',hour_cycle='24')

    def section(self,result,name):
        return report_fixture.ReportDOM(result['html']).root.find(data_section=name)[0]

    def kpi(self,result):
        return self.section(result,'kpis').find(data_role='kpi-number')[2].text()

    def test_actual_period_share_and_source_quantity_replace_literal_unknown_in_pl_and_en(self):
        value=self.fixture();before=deepcopy(value)
        self.assertEqual('0.232',value['period_summary']['cheap_zone_share']['ratio'])
        self.assertEqual('247.00',value['totals']['total_cost'])
        for language,percent,energy in [('en','23.2','58.0'),('pl','23,2','58,0')]:
            with self.subTest(language=language):
                result=self.render(value,language);card=self.section(result,'kpis')
                self.assertEqual(percent,self.kpi(result))
                self.assertIn('%',card.text());self.assertIn(energy+' kWh',card.text())
                self.assertIn(energy+' kWh',card.text())
                self.assertIn('period_summary.cheap_zone_share.known_cheap_kwh',result['csv'])
                self.assertIn(percent+' %',result['text'])
                self.assertIn('250',card.text());self.assertIn('247',card.text())
        self.assertEqual(before,value)

    def test_zero_cheap_share_is_known_but_zero_total_and_missing_proof_are_not(self):
        value=self.fixture();share=value['period_summary']['cheap_zone_share']
        share.update(known_cheap_kwh='0',ratio='0')
        self.assertEqual('0',self.kpi(self.render(value)))
        for defect in ['zero_total','missing','window','zone','scope','classification','measurement','coverage',
                       'reason','missing_reason','classified','total','negative','over_one','nonfinite','reversedwindow']:
            with self.subTest(defect=defect):
                candidate=self.fixture();s=candidate['period_summary']['cheap_zone_share']
                if defect=='zero_total':s.update(total_kwh='0',classified_kwh='0',known_cheap_kwh='0',ratio=None,status='unknown',reason='zero_total_kwh');candidate['totals']['kwh']='0'
                elif defect=='missing':candidate.pop('period_summary')
                elif defect=='window':candidate['period_summary']['window']['end']='2026-01-03T00:00:00+00:00'
                elif defect=='zone':candidate['period_summary']['window']['timezone']='Europe/Warsaw'
                elif defect=='scope':s['scope']='chart_bins'
                elif defect=='classification':s['classification_source']='guessed_by_price'
                elif defect=='measurement':candidate['totals']['measurement_complete']=False
                elif defect=='coverage':candidate['coverage']['complete']=False
                elif defect=='reason':s['reason']='incomplete_classification'
                elif defect=='missing_reason':s.pop('reason')
                elif defect=='reversedwindow':
                    candidate['period']['end']='2025-12-31T00:00:00+00:00'
                    candidate['period_summary']['window']['end']=candidate['period']['end']
                elif defect=='classified':s['classified_kwh']='249'
                elif defect=='total':s['total_kwh']='251'
                elif defect=='negative':s['ratio']='-0.1'
                elif defect=='over_one':s['ratio']='1.1'
                else:s['ratio']='NaN'
                self.assertEqual('Unknown',self.kpi(self.render(candidate)))

    def test_partial_and_unknown_keep_status_and_known_subtotals_without_promoting_ratio(self):
        for status,reason in [('partial','incomplete_classification'),('unknown','classification_unavailable')]:
            value=self.fixture();value['period_summary']['cheap_zone_share'].update(status=status,reason=reason,ratio=None)
            result=self.render(value)
            self.assertEqual('Unknown',self.kpi(result))
            detail=self.section(result,'period-share-details').text()
            self.assertIn('58.0',detail);self.assertIn('250.0',detail)
            self.assertIn('Incomplete data' if status=='partial' else 'Unknown',detail)
            self.assertIn('Incomplete zone classification' if status=='partial' else 'Zone classification unavailable',detail)
            self.assertIn('58.0',result['text'])

    def test_energy_share_stays_ready_when_monetary_compatibility_is_unknown(self):
        value=self.fixture();value['totals'].update(currency=None,total_cost=None)
        value['pricing_status']='partial'
        value['period_summary']['zone_breakdown'].update(status='partial',reason='currency_or_basis_mismatch')
        self.assertEqual('23.2',self.kpi(self.render(value)))

    def test_share_is_not_inferred_from_zone_names_prices_or_chart_bin_sums(self):
        value=self.fixture();value.pop('period_summary')
        value['chart']['bins'][0]['zone_breakdown']['zones'][0]['label']='Cheap night'
        value['chart']['bins'][0]['kwh']='999999'
        result=self.render(value)
        self.assertEqual('Unknown',self.kpi(result))

    def test_devices_and_room_aggregate_are_separate_partitions_with_no_double_count(self):
        value=self.fixture();result=self.render(value)
        devices=self.section(result,'device-ranking').text()
        self.assertIn('QA80 Office',devices);self.assertIn('Unmeasured',devices)
        self.assertNotIn('Unassigned',devices)
        rooms=self.section(result,'areas').text()
        self.assertIn('Unassigned',rooms);self.assertIn('Unmeasured',rooms)
        floors=self.section(result,'floors').text()
        self.assertIn('Unmeasured',floors)
        self.assertEqual(1,devices.count('QA80 Office'))

    def test_compact_main_and_detail_rankings_keep_exact_source_only_in_csv(self):
        value=self.fixture();before=deepcopy(value)
        for language,raw in [('en','49.9999999999995450'),('pl','49,9999999999995450')]:
            result=self.render(value,language)
            main=self.section(result,'device-ranking').text()
            self.assertIn('50',main);self.assertIn('200',main);self.assertNotIn(raw,main)
            self.assertFalse(report_fixture.ReportDOM(result['html']).root.find(data_section='ranking-values'))
            self.assertIn('50.0' if language=='en' else '50,0', self.section(result,'device-ranking').text())
            self.assertNotIn(raw,result['text'])
            rows={r['field']:r for r in csv.DictReader(StringIO(result['csv']))}
            self.assertEqual(raw,rows['groups.devices.sensor.device.kwh']['value'])
            self.assertEqual(before['snapshot_id'],result['snapshot_id'])
            self.assertTrue(all(r['snapshot_id']==before['snapshot_id'] for r in rows.values()))
        self.assertEqual(before,value)

    def test_parent_residual_device_is_already_exclusive_no_additional_measured_kwh_inference(self):
        value=self.fixture();item=value['groups']['devices']['sensor.device']
        item.update(attribution='parent_residual',measured_kwh='99',kwh='49.9999999999995450')
        result=self.render(value)
        self.assertNotIn('99',[node.text() for node in self.section(result,'device-ranking').find() if node.tag=='td'])
        self.assertIn('50.0',self.section(result,'device-ranking').text())
        rows={r['field']:r for r in csv.DictReader(StringIO(result['csv']))}
        self.assertEqual('49.9999999999995450',rows['groups.devices.sensor.device.kwh']['value'])

    def test_unknown_small_and_negative_rank_values_remain_distinct_and_labels_are_escaped(self):
        for quantity,expected in [(None,'Unknown'),('0','0'),('0.00001','<0.01'),('-0.00001','>−0.01')]:
            value=self.fixture();value['groups']['devices']['sensor.device'].update(kwh=quantity,name='QA <img src=x>&')
            result=self.render(value);section=self.section(result,'device-ranking')
            self.assertIn(expected,section.text());self.assertIn('QA <img src=x>&',section.text())
            self.assertIn('QA &lt;img src=x&gt;&amp;',result['html']);self.assertNotIn('<img',result['html'])
        for forbidden in ('<svg','<canvas','<script','<iframe','display:flex','display:grid','url('):
            self.assertNotIn(forbidden,result['html'])
