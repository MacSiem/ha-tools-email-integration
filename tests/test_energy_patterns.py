"""Patterns tests use real hourly evidence, accumulator and historical prices."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from tests.test_energy_analysis import analysis
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version

resolve_patterns_window = analysis.resolve_patterns_window
UTC = timezone.utc


class PatternsProducerTest(unittest.TestCase):
    def build(self, now='2026-01-08T00:00Z', *, days=7, zone='UTC', tariff=None,
              value=1, adjust=None, sources=('sensor.grid',)):
        window = resolve_patterns_window(days, datetime.fromisoformat(now), zone)
        self.assertEqual('ready', window['status'])
        acc = analysis.EnergyAnalysisAccumulator(tariff, window, {}, generated_at=now,
                                               patterns_window=window)
        start, end = (datetime.fromisoformat(window[k]) for k in ('start', 'end'))
        cursor = start
        while cursor < end:
            stop = min(cursor+timedelta(hours=168), end)
            snapshot = readings(cursor.isoformat(), stop.isoformat(), sources=sources, value=value)
            if adjust:
                for sid, rows in snapshot['source_buckets'].items():
                    for row in rows:
                        row['kwh'] = adjust(sid, datetime.fromtimestamp(row['from']/1000, UTC), row['kwh'])
            acc.add_chunk(snapshot, cursor, stop)
            cursor = stop
        return acc.finish()

    def test_last_completed_interval_defines_seven_and_thirty_local_dates(self):
        for days, first, hours in [(7, '2026-04-29', 168), (30, '2026-04-06', 720)]:
            with self.subTest(days=days):
                window = resolve_patterns_window(days, datetime(2026,5,6,tzinfo=UTC), 'UTC')
                self.assertEqual((first, '2026-05-05', hours),
                    (window['first_date'], window['last_date'], window['hours']))
                self.assertEqual('2026-05-06T00:00:00+00:00', window['query_end'])
                result = self.build('2026-05-06T00:00Z', days=days)
                self.assertEqual('patterns', result['kind'])
                self.assertEqual((days, hours), (len(result['patterns']['days']), len(result['patterns']['hours'])))
                self.assertEqual(Decimal(hours), Decimal(result['totals']['kwh']))
                self.assertNotIn('buckets', result); self.assertNotIn('groups', result)
                self.assertNotIn('alternatives', result)

    def test_strict_days_and_aware_clock_reject_without_fabricated_period(self):
        for bad in (True, 7.0, '7', 1, 31, None):
            self.assertEqual('invalid_patterns_days', resolve_patterns_window(bad,
                datetime(2026,5,6,tzinfo=UTC), 'UTC')['reason'])
        self.assertEqual('aware_server_clock_required', resolve_patterns_window(7,
            datetime(2026,5,6), 'UTC')['reason'])
        self.assertEqual('measurement_timezone_unavailable', resolve_patterns_window(7,
            datetime(2026,5,6,tzinfo=UTC), 'bad/zone')['reason'])

    def test_partial_today_keeps_measured_hours_but_withholds_full_day(self):
        result = self.build('2026-01-08T12:34Z', tariff=book())
        day = result['patterns']['days'][-1]
        self.assertEqual('2026-01-08', day['local_date'])
        self.assertFalse(day['complete']); self.assertIsNone(day['kwh'])
        self.assertIsNone(day['energy_cost'])
        self.assertEqual(Decimal(12), Decimal(day['recorded_kwh']))
        self.assertEqual('12.00', day['known_costs']['energy_cost'])
        self.assertEqual((86400,43200), (day['expected_seconds'], day['measured_seconds']))
        self.assertEqual(Decimal(1), Decimal(result['patterns']['hours'][-1]['kwh']))
        self.assertTrue(result['patterns']['partial_window'])

    def test_hierarchical_cent_ties_use_same_day_and_hour_targets(self):
        stamps = {'2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00',
                  '2026-01-02T00:00:00+00:00'}
        result = self.build(tariff=book(), value=0,
            adjust=lambda sid,t,v: .005 if t.isoformat() in stamps else 0)
        days, hours = result['patterns']['days'], result['patterns']['hours']
        self.assertEqual('0.02', result['totals']['energy_cost'])
        self.assertEqual(['0.01','0.01'], [row['energy_cost'] for row in days[:2]])
        self.assertEqual(['0.01','0.00'], [row['energy_cost'] for row in hours[:2]])
        self.assertEqual('0.005', hours[0]['raw_costs']['energy_cost'])
        self.assertEqual(Decimal('.02'), sum(Decimal(row['energy_cost']) for row in hours))
        for day in days:
            self.assertEqual(Decimal(day['known_costs']['energy_cost']),
                sum(Decimal(row['known_costs']['energy_cost']) for row in hours if row['local_date']==day['local_date']))
        self.assertTrue(all('fixed_cost' not in row for row in hours))

    def test_zero_missing_and_partial_source_do_not_create_household_measurement(self):
        result = self.build(value=0, sources=('sensor.a','sensor.b'), tariff=book(),
            adjust=lambda sid,t,v: None if sid=='sensor.b' and t.day==1 and t.hour==0 else v)
        first, zero = result['patterns']['hours'][:2]
        self.assertIsNone(first['kwh']); self.assertIsNone(first['energy_cost'])
        self.assertEqual(Decimal(0), Decimal(zero['kwh']))
        self.assertEqual('0.00', zero['energy_cost'])
        self.assertIsNone(result['patterns']['days'][0]['kwh'])
        self.assertEqual(Decimal(0), Decimal(result['patterns']['days'][0]['recorded_kwh']))

    def test_dst_hours_and_half_hour_boundaries_preserve_real_identities(self):
        for now, zone, last_date, count in [('2026-03-29T22:00Z','Europe/Warsaw','2026-03-29',23),
                ('2026-10-25T23:00Z','Europe/Warsaw','2026-10-25',25)]:
            with self.subTest(now=now):
                result = self.build(now, zone=zone)
                day = result['patterns']['days'][-1]
                selected = [row for row in result['patterns']['hours'] if row['local_date']==last_date]
                self.assertTrue(day['complete']); self.assertEqual(Decimal(count), Decimal(day['kwh']))
                self.assertEqual(count,len(selected))
                repeated = [row for row in selected if row['local_start'][11:13]=='02']
                if count==25:
                    self.assertEqual([0,1],[row['fold'] for row in repeated])
                    self.assertNotEqual(repeated[0]['id'], repeated[1]['id'])
        india = self.build('2026-01-08T12:00Z', zone='Asia/Kolkata')
        self.assertTrue(all(not day['complete'] and day['kwh'] is None for day in india['patterns']['days']))
        self.assertEqual(Decimal(1),Decimal(india['patterns']['hours'][0]['kwh']))
        self.assertTrue(any(row['crosses_local_date'] for row in india['patterns']['hours']))

    def test_history_variable_legacy_and_currency_keep_component_unknowns(self):
        for semantics, variable, energy, combined in [('components',None,'24.00','0.00'),
                                                      ('legacy_combined',None,None,'24.00')]:
            v = version(rate_semantics=semantics)
            v['profile']['zones'][0]['variableRate'] = variable
            result = self.build(tariff=book([v]))
            day = result['patterns']['days'][0]
            self.assertEqual((energy,None,combined), tuple(day[k] for k in
                ('energy_cost','variable_cost','legacy_combined_cost')))
            self.assertEqual(None if semantics=='components' else '24.00',day['usage_cost'])
        va,vb = version(),version('2026-01-04',2,'b')
        changed = self.build(tariff=book([va,vb]))
        self.assertEqual(['24.00','48.00'], [changed['patterns']['days'][i]['energy_cost'] for i in (2,3)])
        vb['profile']['currency']='PLN'
        mixed = self.build(tariff=book([va,vb]))
        self.assertTrue(all(row['energy_cost'] is None for row in mixed['patterns']['hours']))
        self.assertEqual(Decimal(1),Decimal(mixed['patterns']['hours'][0]['kwh']))

    def test_mixed_version_hour_and_unknown_history_are_not_split(self):
        va,vb=version(),version('2026-01-02',2,'b')
        for v in (va,vb): v['profile']['timezone']='Asia/Kathmandu'
        result = self.build(tariff=book([va,vb]))
        row = next(row for row in result['patterns']['hours'] if row['from']==1767290400000)
        self.assertEqual('mixed_version_bucket',row['pricing_reason'])
        self.assertIsNone(row['energy_cost']); self.assertEqual(Decimal(1),Decimal(row['kwh']))
        later = self.build(tariff=book([version('2026-01-04')]))
        self.assertEqual('unknown_historical_version',later['patterns']['hours'][0]['pricing_reason'])

    def test_weekday_means_use_only_full_days_and_include_zero_days(self):
        def observed(sid,t,v):
            if t.weekday()==0:
                return 1 if t.day==5 else 2 if t.day==12 else 3 if t.day==19 else None
            return 0
        result = self.build('2026-01-19T12:00Z', days=30, adjust=observed)
        monday = result['patterns']['weekdays'][1]
        self.assertEqual((2,'36'), (monday['count'], monday['kwh']))
        tuesday = result['patterns']['weekdays'][2]
        self.assertGreater(tuesday['count'],0); self.assertEqual('0',tuesday['kwh'])

    def test_negative_prices_and_mixed_zones_use_historical_priced_rows(self):
        v=version(rate=-.5)
        v['profile']['allowNegativeEnergy']=True
        negative=self.build(tariff=book([v]))
        self.assertEqual('-84.00',negative['totals']['energy_cost'])
        self.assertEqual('-12.00',negative['patterns']['days'][0]['energy_cost'])
        self.assertEqual('-0.50',negative['patterns']['hours'][0]['energy_cost'])
        for rate in (1,2):
            v=version();p=v['profile']
            p['zones'].append({'id':'night','energyRate':rate,'variableRate':0})
            p['rules']=[{'zone':'night','dates':['2026-01-01'],
                         'intervals':[{'from':'00:30','to':'01:00'}]}]
            result=self.build(tariff=book([v]))
            row=result['patterns']['hours'][0]
            self.assertEqual(Decimal(1),Decimal(row['kwh']))
            if rate==1:
                self.assertEqual('1.00',row['energy_cost'])
                self.assertEqual([],row['zone_breakdown']['zones'])
                self.assertEqual(Decimal(1),Decimal(row['zone_breakdown']['unattributed']['recorded_kwh']))
            else:
                self.assertEqual('mixed_rate_bucket',row['pricing_reason'])
                self.assertIsNone(row['energy_cost'])
                self.assertEqual('167.00',result['totals']['known_costs']['energy_cost'])

    def test_basis_mismatch_and_zero_with_unknown_variable_never_invent_money(self):
        va,vb=version(),version('2026-01-04',2,'b')
        vb['profile']['priceBasis']='net'
        result=self.build(tariff=book([va,vb]))
        self.assertTrue(all(row['energy_cost'] is None for row in result['patterns']['hours']))
        v=version();v['profile']['zones'][0]['variableRate']=None
        zero=self.build(value=0,tariff=book([v]))
        day=zero['patterns']['days'][0]
        self.assertEqual('0.00',day['energy_cost'])
        self.assertIsNone(day['variable_cost']);self.assertIsNone(day['usage_cost'])
        self.assertIsNone(zero['patterns']['hours'][0]['usage_cost'])
