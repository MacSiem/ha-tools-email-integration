"""Composed mail dates are presentation only; owner executes RED then GREEN."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from html import unescape
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_mail_charts_v3 import c, m, chart, series, summary

UTC = timezone.utc


class FriendlyEnergyMailDatesTest(unittest.TestCase):
    def compose(self,start,end,*,language='pl',display='America/Los_Angeles',
                report='Europe/Warsaw',cadence='weekly',gap=False):
        from tests.helper_composer_schema2 import ComposerFixture,service
        import importlib
        scheduler=importlib.import_module(service.__package__+'.scheduler')
        prior_start,_=scheduler.calendar_report_window({'cadence':cadence,'timezone':report},start)
        partial=(end-start).total_seconds()<24*3600
        def points(a,b,ids):
            rows=[]
            for i in range(int((b-a).total_seconds()/3600)):
                at=a+timedelta(hours=i)
                origin=start if start<=at<end else prior_start
                if gap and at==start+timedelta(hours=24):continue
                rows.append({'start':at.timestamp(),'change':0 if at<origin+timedelta(hours=24) else 1})
            return {'sensor.grid':rows,
                'sensor.category':[{'start':(a+timedelta(hours=i)).timestamp(),'change':0}
                    for i in range(int((b-a).total_seconds()/3600))]}
        fx=ComposerFixture(end,measurement_zone=report if partial else display,
            language=language,display_zone=display,points=points,
            rows={'sensor.grid':{},'sensor.category':{}},preferences={
                'energy_sources':[{'type':'grid','stat_energy_from':'sensor.grid'}],
                'device_consumption':[{'stat_consumption':'sensor.category','name':'2026-10-05'}]})
        with fx.patches():
            payload=asyncio.run(fx.analysis('day')) if partial else asyncio.run(
                fx.compose(cadence,start=start,end=end,report_zone=report))
        self.last_fixture=fx
        self.assertEqual(fx.snapshot['snapshot_id'],payload['snapshot_id'])
        return payload

    def test_actual_weekly_mail_keeps_calendar_day_identity_in_report_zone(self):
        from decimal import Decimal
        from tests.helper_composer_schema2 import csv_facts
        start=datetime(2026,10,18,22,tzinfo=UTC);end=datetime(2026,10,25,23,tzinfo=UTC)
        summaries=[]
        for language in ('pl','en'):
            with self.subTest(language=language):
                payload=self.compose(start,end,language=language)
                snapshot=self.last_fixture.snapshot;summaries.append(snapshot)
                rows=snapshot['chart']['bins']
                self.assertEqual('Europe/Warsaw',snapshot['chart']['timezone'])
                self.assertEqual('2026-10-19T00:00:00+02:00',rows[0]['local_start'])
                self.assertEqual('2026-10-25T00:00:00+02:00',rows[-1]['local_start'])
                self.assertEqual('2026-10-26T00:00:00+01:00',rows[-1]['local_end'])
                self.assertEqual((1792360800000,1792447200000),(rows[0]['from'],rows[0]['to']))
                self.assertEqual(25*3600,rows[-1]['expected_seconds'])
                self.assertEqual('2026-10-18T22:00:00+00:00',snapshot['period']['start'])
                authoritative=snapshot['comparisons']['previous']
                self.assertEqual(169,snapshot['period']['hours'])
                self.assertEqual(168,authoritative['baseline_periods'][0]['hours'])
                self.assertEqual((145,144,1),(authoritative['current_kwh'],authoritative['baseline_kwh'],authoritative['delta_kwh']))
                comparison=payload['summary']['comparison'] # report70 structural aliases.
                self.assertEqual('2026-10-11T22:00:00+00:00',comparison['previous_start'])
                self.assertEqual('2026-10-18T22:00:00+00:00',comparison['previous_end'])
                facts=csv_facts(payload)
                self.assertEqual(Decimal('25'),Decimal(str(rows[-1]['kwh'])))
                expected=('18 paź 2026 15:00:00 -0700' if language=='pl' else 'Oct 18, 2026 15:00:00 -0700')
                for field in ('body','html'):
                    body=unescape(payload[field])
                    self.assertIn(expected,body)
                    self.assertIn('America/Los_Angeles',body)
                    self.assertNotIn('2026-10-19 (24h;',body)
                self.assertEqual(expected+' [America/Los_Angeles]',facts['period.start'])
        for key in ('period','coverage','totals','chart'):
            self.assertEqual(summaries[0][key],summaries[1][key])
        for key in ('status','current_kwh','baseline_kwh','delta_kwh','percent','comparison_scope','baseline_periods'):
            self.assertEqual(summaries[0]['comparisons']['previous'][key],
                             summaries[1]['comparisons']['previous'][key])

    def test_actual_hourly_mail_retains_both_dst_fold_offsets_and_epochs(self):
        from tests.helper_composer_schema2 import csv_facts
        start=datetime(2026,10,25,tzinfo=UTC);end=start+timedelta(hours=3)
        for language in ('pl','en'):
            with self.subTest(language=language):
                payload=self.compose(start,end,language=language,display='Europe/Warsaw',report='UTC',cadence='daily')
                snapshot=self.last_fixture.snapshot;rows=snapshot['chart']['bins']
                self.assertTrue(snapshot['period']['partial'])
                self.assertEqual([1792886400000,1792890000000,1792893600000],[row['from'] for row in rows])
                values=csv_facts(payload)
                first=values['chart.bins.0.start'];second=values['chart.bins.1.start']
                prefix='25 paź 2026' if language=='pl' else 'Oct 25, 2026'
                self.assertEqual(prefix+' 02:00:00 +0200 [Europe/Warsaw]',first)
                self.assertEqual(prefix+' 02:00:00 +0100 [Europe/Warsaw]',second)
                self.assertEqual([first,second,prefix+' 03:00:00 +0100 [Europe/Warsaw]'],
                    [values[f'chart.bins.{i}.from'] for i in range(3)])
                for field in ('body','html'):
                    self.assertIn('02:00 +0200',unescape(payload[field]))
                    self.assertIn('02:00 +0100',unescape(payload[field]))
                from tests.helper_composer_schema2 import renderer
                from tests.test_mail_layout import Document
                # Short clocks distinguish the actual instants in both clock
                # modes; full dates, seconds and zone audit remain in CSV.
                for cycle,clock,ordinary in (('24','02:00','03:00'),('12','02:00 AM','03:00 AM')):
                    rendered=renderer.render_energy_analysis(snapshot,language=language,
                        timezone_name='Europe/Warsaw',hour_cycle=cycle)
                    for field in ('text','html'):
                        visible=unescape(rendered[field])
                        self.assertIn(clock+' +0200',visible)
                        self.assertIn(clock+' +0100',visible)
                    doc=Document(rendered['html'])
                    measurements=[n for n in doc.nodes if n.tag=='tr'
                        and n.attrs.get('data-role')=='measurement']
                    clocks=[next(n.text for n in doc.nodes if n.tag=='#text' and n.inside(row))
                            for row in measurements]
                    self.assertEqual([clock+' +0200',clock+' +0100',ordinary],clocks)

    def test_missing_value_and_currency_show_one_unknown_without_changing_csv_facts(self):
        import csv
        from io import StringIO
        from tests.helper_composer_schema2 import renderer
        from tests.test_mail_layout import Document
        start=datetime(2026,10,10,tzinfo=UTC)
        for language,unknown,caption in (('pl','Brak danych','Koszt okresu'),
                                         ('en','Unknown','Period cost')):
            with self.subTest(language=language):
                payload=self.compose(start,start+timedelta(hours=3),language=language,
                    display='Europe/Warsaw',report='UTC',cadence='daily')
                snapshot=copy.deepcopy(self.last_fixture.snapshot)
                self.assertIsNone(snapshot['totals']['total_cost'])
                self.assertIsNone(snapshot['totals']['currency'])
                before=copy.deepcopy(snapshot)
                rendered=renderer.render_energy_analysis(snapshot,language=language,
                    timezone_name='Europe/Warsaw',hour_cycle='24')
                self.assertIn(caption+': '+unknown+'\n',rendered['text'])
                doc=Document(rendered['html'])
                self.assertNotIn(unknown+' '+unknown,rendered['text'])
                # Check individual value cells, not adjacent independent KPIs.
                cells=[n for n in doc.nodes if n.tag=='td' and not any(
                    child is not n and child.tag=='td' and child.inside(n) for child in doc.nodes)]
                for cell in cells:
                    visible=' '.join(n.text for n in doc.nodes if n.tag=='#text' and n.inside(cell))
                    self.assertNotIn(unknown+' '+unknown,visible)
                facts={r['field']:r for r in csv.DictReader(StringIO(rendered['csv']))}
                self.assertEqual((unknown,unknown),
                    (facts['totals.total_cost']['value'],facts['totals.total_cost']['unit']))
                self.assertEqual(before,snapshot)
                # A known currency remains useful beside a missing amount.
                snapshot['totals']['currency']='PLN'
                known=renderer.render_energy_analysis(snapshot,language=language,
                    timezone_name='Europe/Warsaw',hour_cycle='24')
                self.assertIn(caption+': '+unknown+(' zł' if language=='pl' else ' PLN')+'\n',known['text'])

    def test_zero_missing_days_and_date_shaped_categories_keep_their_values(self):
        from decimal import Decimal
        start=datetime(2026,10,18,22,tzinfo=UTC)
        payload=self.compose(start,datetime(2026,10,25,23,tzinfo=UTC),gap=True)
        snapshot=self.last_fixture.snapshot;rows=snapshot['chart']['bins']
        self.assertEqual(Decimal('0'),Decimal(rows[0]['kwh']));self.assertIsNone(rows[1]['kwh'])
        self.assertEqual([True,False],[row['complete'] for row in rows[:2]])
        self.assertIsNone(snapshot['totals']['kwh'])
        self.assertEqual('2026-10-20T00:00:00+02:00',rows[1]['local_start'])
        self.assertEqual('2026-10-05',snapshot['groups']['devices']['sensor.category']['name'])
        for field in ('body','html'):
            body=unescape(payload[field]);self.assertIn('Brak danych',body)
            self.assertIn('2026-10-05',body) # Category is text, not a date bucket.
        # Existing authoritative >=90% comparison uses real common hours; missing
        # root totals remain withheld rather than forcing legacy blanket unknown.
        comparison=snapshot['comparisons']['previous']
        self.assertEqual('common_measurements',comparison['comparison_scope'])
        self.assertEqual('ready',comparison['status'])

    def test_source_and_logger_categories_and_unique_caveats_stay_verbatim(self):
        name = '2026-10-05 (24h; +02:00)'
        for language in ('pl', 'en'):
            energy = c.build_energy_report_payload([{'name': name, 'kwh': 0}], cadence='daily',
                presentation=c._presentation(language, 'Europe/Warsaw'))
            log = c.build_log_digest_payload([{'name': name, 'level': 'ERROR', 'count': 1,
                'timestamp': datetime(2026, 10, 18, tzinfo=UTC).timestamp()}], cadence='daily',
                now=datetime(2026, 10, 18, 1, tzinfo=UTC), presentation=c._presentation(language, 'Europe/Warsaw'))
            for payload in (energy, log):
                for field in ('body', 'html'):
                    self.assertIn(name, unescape(payload[field]))
            for field in ('body', 'html'):
                self.assertEqual(1, unescape(log[field]).count(m.LOG_CAVEAT))
                self.assertIn('Unique problems: 1', log[field])

    def test_direct_energy_html_formats_cost_and_usage_rows_without_mutating_model(self):
        start = datetime(2026, 10, 25, tzinfo=UTC)
        end = start + timedelta(hours=2)
        buckets = series(start, 2)
        costs = [{**r, 'energyCost': -.15 if i == 0 else 0, 'variableCost': None}
                 for i, r in enumerate(buckets)]
        model = m.build_energy_chart_model(summary(start, end), buckets, costs, timezone_name='UTC')
        before = copy.deepcopy(model)
        html = unescape(c.render_energy_report_html('Energy', [], 3, None, 'PLN', end,
            visualization=model, presentation=c._presentation('pl', 'Europe/Warsaw')))
        self.assertEqual(before, model)
        self.assertEqual(3, html.count('<strong>25 paź 2026, 02:00 CEST (+0200) → 25 paź 2026, 02:00 CET (+0100)'))
        self.assertIn('-0.15 PLN', html)
        self.assertIn('Unknown PLN', html)
        for item in model['charts']:
            self.assertIn(item['note'], html)
