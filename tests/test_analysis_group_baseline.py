"""Baseline rooms use the same actual readings, hierarchy and captured registry."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest
from tests import test_energy_analysis_service as fixtures


class AnalysisGroupBaselineTest(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        h = fixtures.Harness()
        h.now = datetime(2026, 10, 18, 12, 34, tzinfo=timezone.utc)
        original = h.usage
        async def usage(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            sensors, start = args[1], args[2]
            current = start.date().isoformat() == '2026-10-10'
            device = sensors[0]['entity_id'] == 'sensor.device'
            value = (4 if current else 2) if device else (12 if current else 10)
            for rows in snapshot['source_buckets'].values():
                for row in rows: row['kwh'] = value
            return snapshot
        h.usage = usage
        return h

    async def test_full_room_delta_reconciles_household_without_allocating_fixed_fees(self):
        h = self.harness()
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            compared = result['comparisons']['previous']
            baseline = compared['baseline_analysis']
            self.assertEqual(Decimal(240), Decimal(baseline['totals']['kwh']))
            self.assertEqual(Decimal(48), Decimal(baseline['groups']['areas']['office']['kwh']))
            self.assertEqual('current_snapshot', baseline['groups']['membership_mode'])
            self.assertEqual(result['groups']['registry_revision'], baseline['groups']['registry_revision'])
            self.assertEqual(1, len(h.registry_ids))
            self.assertEqual(4, len(h.queries))
            groups = compared['groups']
            self.assertTrue(groups['reconciles_household_delta'])
            office = groups['areas']['office']
            self.assertEqual('ready', office['status'])
            self.assertEqual(Decimal(48), Decimal(office['delta_kwh']))
            self.assertEqual(100, office['percent'])
            self.assertEqual('48.00', office['delta_usage_cost'])
            self.assertIsNone(office['delta_fixed_cost'])
            self.assertEqual(Decimal(0), Decimal(groups['unmeasured']['delta_kwh']))
            self.assertEqual(Decimal(compared['delta_kwh']), Decimal(office['delta_kwh'])+
                Decimal(groups['unassigned']['delta_kwh'])+Decimal(groups['unmeasured']['delta_kwh']))
            self.assertEqual('1.00', baseline['totals']['fixed_cost'])
            self.assertEqual('day', baseline['period']['kind'])
            self.assertEqual(24, len(baseline['chart']['bins']))
            self.assertNotIn('buckets', baseline)
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))

    async def test_baseline_consumer_revocation_cannot_publish_current_or_baseline(self):
        h = self.harness()
        async def revoke(ids, start, end):
            if ids == ('sensor.device',) and start.date().isoformat() == '2026-10-09':
                h.user.is_active = False
        h.on_read = revoke
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            self.assertIsNone(h.subject._cache)

    async def test_partial_room_uses_exact_common_hours_without_extrapolation_or_household_reconciliation(self):
        h = self.harness(); original = h.usage
        async def usage(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            if args[1][0]['entity_id'] == 'sensor.device' and args[2].day == 9:
                snapshot['source_buckets']['sensor.device'][0]['kwh'] = None
            return snapshot
        h.usage = usage
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            groups = result['comparisons']['previous']['groups']
            self.assertFalse(groups['reconciles_household_delta'])
            office = groups['areas']['office']
            self.assertEqual('ready', office['status'])
            self.assertEqual('common_measurements', office['comparison_scope'])
            self.assertEqual(Decimal(92), Decimal(office['current_kwh']))
            self.assertEqual(Decimal(46), Decimal(office['baseline_kwh']))
            self.assertEqual(Decimal(46), Decimal(office['delta_kwh']))
            self.assertEqual('46.00', office['delta_usage_cost'])
            self.assertIsNone(office['delta_fixed_cost'])

    async def test_historical_comparison_cost_matches_both_actual_period_bills(self):
        h = self.harness()
        old = fixtures.version(rate=.4, version_id='old', monthly=31)
        new = fixtures.version(start='2026-10-10', rate=.5, version_id='new', monthly=31)
        old['profile']['zones'][0]['variableRate'] = .1
        new['profile']['zones'][0]['variableRate'] = .2
        h.book = fixtures.book([old, new])
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            compared = result['comparisons']['previous']; costs = compared['costs']
            self.assertEqual('201.60', costs['current']['usage_cost'])
            self.assertEqual('120.00', costs['baseline']['usage_cost'])
            self.assertEqual('81.60', costs['delta_cost'])
            self.assertEqual(result['totals']['usage_cost'], costs['current']['usage_cost'])
            self.assertEqual(compared['baseline_analysis']['totals']['usage_cost'], costs['baseline']['usage_cost'])
            self.assertEqual('1.00', costs['current']['fixed_cost'])
            self.assertEqual('43.20', compared['groups']['areas']['office']['delta_usage_cost'])

    async def test_partial_cost_uses_same_common_hours_and_keeps_period_fees_separate(self):
        h = self.harness(); original = h.usage
        old = fixtures.version(rate=.4, version_id='old', monthly=31)
        new = fixtures.version(start='2026-10-10', rate=.5, version_id='new', monthly=31)
        old['profile']['zones'][0]['variableRate'] = .1
        new['profile']['zones'][0]['variableRate'] = .2
        h.book = fixtures.book([old, new])
        async def usage(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            if args[1][0]['entity_id'] == 'sensor.grid' and args[2].day == 10:
                snapshot['source_buckets']['sensor.grid'][0]['kwh'] = None
            return snapshot
        h.usage = usage
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            compared = result['comparisons']['previous']; costs = compared['costs']
            self.assertEqual((276, 230), (compared['current_kwh'], compared['baseline_kwh']))
            self.assertEqual('193.20', costs['current']['usage_cost'])
            self.assertEqual('115.00', costs['baseline']['usage_cost'])
            self.assertEqual('78.20', costs['delta_cost'])
            self.assertEqual('1.00', costs['current']['fixed_cost'])
            self.assertIsNone(costs['current']['total_cost'])
            self.assertIsNone(costs['current']['effective_price'])
            self.assertIsNone(result['totals']['total_cost'])
            self.assertEqual(23*3600, costs['baseline']['priced_source_seconds']['usage_cost'])


    async def test_different_missing_room_hours_use_one_matching_mask_and_historical_prices(self):
        h = self.harness(); original = h.usage
        old = fixtures.version(rate=.4, version_id='old', monthly=31)
        new = fixtures.version(start='2026-10-10', rate=.5, version_id='new', monthly=31)
        old['profile']['zones'][0]['variableRate'] = .1
        new['profile']['zones'][0]['variableRate'] = .2
        h.book = fixtures.book([old, new])
        async def usage(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            if args[1][0]['entity_id'] == 'sensor.device':
                snapshot['source_buckets']['sensor.device'][0 if args[2].day == 10 else 1]['kwh'] = None
            return snapshot
        h.usage = usage
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            compared = result['comparisons']['previous']; room = compared['groups']['areas']['office']
            self.assertEqual('ready', room['status'])
            self.assertEqual((Decimal(88), Decimal(44), Decimal(44)),
                tuple(Decimal(room[k]) for k in ('current_kwh', 'baseline_kwh', 'delta_kwh')))
            self.assertEqual('39.60', room['delta_usage_cost'])
            self.assertEqual(4, len(h.queries))
            self.assertEqual(1, len(h.registry_ids))
            self.assertFalse(compared['groups']['reconciles_household_delta'])
            self.assertEqual('81.60', compared['costs']['delta_cost'])
            self.assertIsNone(result['groups']['areas']['office']['kwh'])
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))

    async def test_below_ninety_percent_room_does_not_borrow_complete_household_coverage(self):
        h = self.harness(); original = h.usage
        async def usage(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            if args[1][0]['entity_id'] == 'sensor.device' and args[2].day == 9:
                for row in snapshot['source_buckets']['sensor.device'][:3]: row['kwh'] = None
            return snapshot
        h.usage = usage
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            compared = result['comparisons']['previous']; room = compared['groups']['areas']['office']
            self.assertEqual('ready', compared['status'])
            self.assertEqual('unknown', room['status'])
            self.assertIsNone(room['delta_kwh'])
            self.assertIsNone(room['delta_usage_cost'])
            self.assertFalse(compared['groups']['reconciles_household_delta'])

    async def test_service_proves_energy_partition_only_for_actual_shared_missing_hours(self):
        h = self.harness(); original = h.usage
        async def usage(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            for rows in snapshot['source_buckets'].values(): rows[0]['kwh'] = None
            return snapshot
        h.usage = usage
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='previous')
            compared = result['comparisons']['previous']; groups = compared['groups']
            self.assertEqual('common_measurements', compared['comparison_scope'])
            self.assertTrue(groups['reconciles_household_delta'])
            self.assertEqual(Decimal(46), Decimal(groups['areas']['office']['delta_kwh']))
            self.assertEqual(Decimal(compared['delta_kwh']),
                Decimal(groups['areas']['office']['delta_kwh']) + Decimal(groups['unassigned']['delta_kwh'])
                + Decimal(groups['unmeasured']['delta_kwh']))
            self.assertEqual(4, len(h.queries)); self.assertEqual(1, len(h.registry_ids))
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))


class GroupComparisonRobustnessTest(unittest.TestCase):
    def groups(self, kwh, *, currency='PLN', basis='gross', domain='grid_import', revision=2):
        row = {'kwh': kwh, 'measurement_complete': True,
               'coverage': {'observed_hours': 1, 'expected_hours': 1},
               'energy_cost': '1.00', 'variable_cost': '0.00', 'usage_cost': '1.00'}
        return {'registry_revision': revision, 'membership_mode': 'current_snapshot',
                'energy_domain': domain, 'currency': currency, 'price_basis': basis,
                'partition_complete': True, 'devices': {'sensor.a': row},
                'areas': {'office': row}, 'floors': {'up': row},
                'unassigned': row, 'unmeasured': row}

    def compare(self, a, b):
        return fixtures.service.compare_period_groups(a, b,
            {'status': 'ready', 'comparison_scope': 'calendar_totals'})

    def test_unrepresentable_percent_never_poison_snapshot_json(self):
        result = self.compare(self.groups('1000000000000'), self.groups('5e-324'))
        self.assertEqual('ready', result['areas']['office']['status'])
        self.assertIsNone(result['areas']['office']['percent'])
        json.dumps(result, allow_nan=False)

    def test_currency_basis_and_domain_never_mix_costs(self):
        for kwargs in ({'currency': 'EUR'}, {'basis': 'net'}, {'domain': 'site_consumption'}):
            with self.subTest(kwargs=kwargs):
                result = self.compare(self.groups('4'), self.groups('2', **kwargs))
                self.assertIsNone(result['areas']['office']['delta_usage_cost'])
                self.assertIsNone(result['currency'])
        same_domain = self.compare(self.groups('4', domain='site_consumption'),
                                   self.groups('2', domain='site_consumption'))
        self.assertEqual('2', same_domain['areas']['office']['delta_kwh'])
        self.assertFalse(same_domain['reconciles_household_delta'])

    def test_changed_registry_context_does_not_claim_same_historical_rooms(self):
        result = self.compare(self.groups('4'), self.groups('2', revision=3))
        self.assertEqual('group_context_changed', result['areas']['office']['reason'])
        self.assertIsNone(result['areas']['office']['delta_kwh'])
        self.assertFalse(result['reconciles_household_delta'])
