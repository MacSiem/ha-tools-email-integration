"""Mean-seven costs use the active history and existing service access fences."""
from decimal import Decimal
import unittest
from tests.test_analysis_group_baseline import AnalysisGroupBaselineTest
from tests import test_energy_analysis_service as fixtures


class MeanSevenServiceTest(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        h = AnalysisGroupBaselineTest().harness()
        old = fixtures.version(rate=.4, version_id='old', monthly=31)
        new = fixtures.version(start='2026-10-10', rate=.5, version_id='new', monthly=31)
        old['profile']['zones'][0]['variableRate'] = .1
        new['profile']['zones'][0]['variableRate'] = .2
        h.book = fixtures.book([old, new])
        return h

    async def test_active_mean_costs_follow_same_snapshot_and_all_seven_periods(self):
        h = self.harness()
        with h.patches():
            result = await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='mean7')
            compared = result['comparisons']['mean7']; costs = compared['costs']
            self.assertEqual((288, 240), (compared['current_kwh'], compared['baseline_kwh']))
            self.assertEqual(('201.60', '120.00', '81.60'),
                (costs['current']['usage_cost'], costs['baseline']['usage_cost'], costs['delta_cost']))
            self.assertEqual('1.00', costs['baseline']['fixed_cost'])
            self.assertEqual(7, len(costs['baselines']))
            self.assertEqual(Decimal(result['totals']['usage_cost']), Decimal(costs['current']['usage_cost']))
            count = len(h.queries)
            self.assertEqual(result, await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='mean7'))
            self.assertEqual(count, len(h.queries))
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))

    async def test_role_revocation_after_mean_pricing_never_returns_or_caches_result(self):
        h = self.harness(); reached = []
        def revoke(job):
            if getattr(job.func, '__name__', '') == 'compare_daily_mean7':
                reached.append(True)
                h.user.is_active = False
        h.on_cpu = revoke
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day', 'user', anchor='2026-10-10', comparison='mean7')
            self.assertEqual([True], reached)
            self.assertIsNone(h.subject._cache)
