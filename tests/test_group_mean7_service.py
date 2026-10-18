"""Mean-seven group DTO uses eight guarded consumer windows and one registry capture."""
from decimal import Decimal
import unittest
from tests import test_mean7_service as fixtures


class MeanSevenGroupServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_groups_share_snapshot_hash_cache_and_all_eight_consumer_reads(self):
        h = fixtures.MeanSevenServiceTest().harness()
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='mean7')
            groups = result['comparisons']['mean7']['groups']
            row = groups['areas']['office']
            self.assertEqual((Decimal(96), Decimal(48), Decimal(48)),
                tuple(Decimal(row[k]) for k in ('current_kwh','baseline_kwh','delta_kwh')))
            self.assertEqual('43.20', row['delta_usage_cost'])
            self.assertTrue(groups['reconciles_household_delta'])
            self.assertTrue(groups['reconciles_household_cost_delta'])
            self.assertEqual(Decimal(result['comparisons']['mean7']['costs']['delta_cost']),
                Decimal(row['delta_usage_cost'])+Decimal(groups['unassigned']['delta_usage_cost'])+
                Decimal(groups['unmeasured']['delta_usage_cost']))
            self.assertNotIn('fixed_cost', row['costs']['current'])
            self.assertEqual([['sensor.device']], h.registry_ids)
            self.assertEqual(16, len(h.queries))
            self.assertEqual(8, sum(ids == ('sensor.device',) for ids,_,_ in h.queries))
            self.assertEqual(result['groups']['captured_at'], groups['membership_as_of'])
            self.assertEqual(result, await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='mean7'))
            self.assertEqual(16, len(h.queries))
            self.assertTrue(await h.subject.async_validate_result(result,'user'))
            result['comparisons']['mean7']['groups']['areas']['office']['delta_kwh'] = 'tampered'
            cached = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='mean7')
            self.assertEqual(Decimal(48), Decimal(cached['comparisons']['mean7']['groups']['areas']['office']['delta_kwh']))

    async def test_baseline_consumer_revocation_never_returns_or_caches_group_result(self):
        h = fixtures.MeanSevenServiceTest().harness()
        async def revoke(ids, start, end):
            if ids == ('sensor.device',) and start.day == 7:
                h.user.is_active = False
        h.on_read = revoke
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day','user',anchor='2026-10-10',comparison='mean7')
            self.assertIsNone(h.subject._cache)

    async def test_revocation_after_group_cpu_never_returns_or_caches_result(self):
        h = fixtures.MeanSevenServiceTest().harness(); reached=[]
        def revoke(job):
            if getattr(job.func,'__name__','') == 'compare_daily_group_mean7':
                reached.append(True); h.user.is_active=False
        h.on_cpu = revoke
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day','user',anchor='2026-10-10',comparison='mean7')
            self.assertEqual([True], reached)
            self.assertIsNone(h.subject._cache)
