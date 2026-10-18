"""Chart/table/export bins come from the same actual household hours and prices."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from tests.test_energy_analysis import analysis
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version


class AnalysisChartBinsTest(unittest.TestCase):
    def build(self, start, end, kind, *, zone='Europe/Warsaw', missing=(), value=1, tariff=None):
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
        builder = analysis.EnergyAnalysisAccumulator(tariff,
            {'start': start, 'end': end, 'kind': kind, 'timezone': zone}, {},
            generated_at=b + timedelta(hours=1))
        cursor = a
        while cursor < b:
            stop = min(cursor+timedelta(hours=168), b)
            offset = int((cursor-a).total_seconds()/3600)
            absent = [('sensor.grid', hour-offset) for hour in missing
                      if offset <= hour < offset + (stop-cursor).total_seconds()/3600]
            builder.add_chunk(readings(cursor.isoformat(), stop.isoformat(), sources=('sensor.grid',),
                                       missing=absent, value=value), cursor, stop)
            cursor = stop
        return builder.finish()

    def test_dst_daily_chart_retains_two_distinct_two_oclock_hours(self):
        result = self.build('2026-10-24T22:00Z', '2026-10-25T23:00Z', 'day')
        chart = result['chart']; bins = chart['bins']
        self.assertEqual('ready', chart['status']); self.assertEqual('hour', chart['grain'])
        self.assertEqual(25, len(bins))
        repeated = [row for row in bins if row['local_start'][11:13] == '02']
        self.assertEqual([0, 1], [row['fold'] for row in repeated])
        self.assertNotEqual(repeated[0]['from'], repeated[1]['from'])
        self.assertEqual(['+02:00', '+01:00'], [row['local_start'][-6:] for row in repeated])
        self.assertEqual(Decimal(result['totals']['kwh']), sum(Decimal(row['kwh']) for row in bins))

    def test_week_bins_cover_actual_twenty_three_hour_day(self):
        result = self.build('2026-03-22T23:00Z', '2026-03-29T22:00Z', 'week')
        bins = result['chart']['bins']
        self.assertEqual('day', result['chart']['grain']); self.assertEqual(7, len(bins))
        self.assertEqual([24]*6+[23], [row['expected_seconds']/3600 for row in bins])
        self.assertEqual([Decimal(24)]*6+[Decimal(23)], [Decimal(row['kwh']) for row in bins])

    def test_leap_year_month_bins_sum_exactly_across_53_chunks(self):
        result = self.build('2024-01-01T00:00Z', '2025-01-01T00:00Z', 'year', zone='UTC')
        bins = result['chart']['bins']
        self.assertEqual('month', result['chart']['grain']); self.assertEqual(12, len(bins))
        self.assertEqual(Decimal(696), Decimal(bins[1]['kwh']))
        self.assertEqual(Decimal(8784), sum(Decimal(row['kwh']) for row in bins))

    def test_missing_hour_withholds_bin_total_but_retains_known_observations_and_zero(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-03T00:00Z', 'month',
                            zone='UTC', missing=(4,), value=0)
        first, second = result['chart']['bins']
        self.assertIsNone(first['kwh']); self.assertEqual('0', first['recorded_kwh'])
        self.assertEqual(23*3600, first['measured_seconds'])
        self.assertEqual(24*3600, first['expected_seconds'])
        self.assertFalse(first['complete']); self.assertTrue(second['complete'])
        self.assertEqual(Decimal(0), Decimal(second['kwh']))
        self.assertIsNone(first['usage_cost']); self.assertIsNone(first['fixed_cost'])

    def test_cost_bins_reconcile_component_cents_without_duplicating_household_fees(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-01T03:00Z', 'day', zone='UTC',
                            value=.005, tariff=book([version(rate=1, monthly=31)]))
        bins = result['chart']['bins']
        self.assertEqual(Decimal('.02'), sum(Decimal(row['energy_cost']) for row in bins))
        self.assertEqual(Decimal(result['totals']['usage_cost']), sum(Decimal(row['usage_cost']) for row in bins))
        self.assertTrue(all(row['fixed_cost'] is None and row['total_cost'] is None for row in bins))
        self.assertEqual('household_period_only', result['chart']['fixed_fee_scope'])
        self.assertEqual('0.13', result['totals']['fixed_cost'])

    def test_unavailable_measurement_zone_is_explicit_not_host_timezone(self):
        result = self.build('2026-01-01T00:00Z', '2026-01-01T02:00Z', 'day', zone='Invalid/Timezone')
        self.assertEqual('unknown', result['chart']['status'])
        self.assertEqual('measurement_timezone_unavailable', result['chart']['reason'])
        self.assertEqual([], result['chart']['bins'])
