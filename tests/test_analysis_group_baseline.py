"""Baseline rooms use the same actual readings, hierarchy and captured registry."""
from datetime import datetime, timezone
from decimal import Decimal
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

    async def test_partial_room_is_unknown_without_extrapolation_or_household_reconciliation(self):
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
            self.assertIsNone(groups['areas']['office']['delta_kwh'])
            self.assertEqual('unknown', groups['areas']['office']['status'])
            # Honest initial baseline support; matched partial-room comparison
            # still needs hourly group evidence and must not be called complete.
            self.assertEqual('group_common_measurements_required', groups['areas']['office']['reason'])
