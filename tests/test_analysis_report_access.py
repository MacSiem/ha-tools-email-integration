"""Report purpose uses its own policy and all fresh source-access fences."""
import unittest
from tests.test_energy_analysis_service import Harness, service


class AnalysisReportAccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_report_default_is_admin_even_when_analysis_is_household(self):
        h=Harness()
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day','user',purpose='report_read')
        self.assertEqual([],h.queries)
        self.assertEqual([],h.registry_ids)
        self.assertIsNone(h.subject._cache)

    async def test_report_read_delegation_is_independent_from_analysis_and_execute(self):
        h=Harness();h.policy['features']={
            'energy_analysis':{'read':{'mode':'admin'}},
            'energy_reports':{'read':{'mode':'selected_users','user_ids':['user']}}}
        with h.patches():
            result=await h.subject.async_get_analysis('day','user',purpose='report_read')
            self.assertEqual('report_read',result['context']['access_purpose'])
            self.assertTrue(await h.subject.async_validate_result(result,'user'))
            self.assertTrue(h.subject.context_is_current(result))
            for purpose in ('analysis','report_execute'):
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_analysis('day','user',purpose=purpose)

    async def test_native_sources_remain_upper_bound_for_delegated_report(self):
        h=Harness();h.policy['features']={'energy_reports':{'read':{'mode':'household'}}}
        h.permissions.access_all_entities=lambda _:False
        h.permissions.check_entity=lambda sid,_:sid=='sensor.grid'
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day','user',purpose='report_read')
        self.assertEqual([],h.queries)
        self.assertEqual([],h.registry_ids)

    async def test_report_revocation_after_cache_never_uses_analysis_policy(self):
        h=Harness();h.policy['features']={'energy_reports':{'read':{'mode':'household'}}}
        with h.patches():
            result=await h.subject.async_get_analysis('day','user',purpose='report_read')
            h.policy['features']['energy_reports']['read']={'mode':'admin'}
            with self.assertRaises(PermissionError):
                await h.subject.async_validate_result(result,'user')
            self.assertIsNone(h.subject._cache)

    async def test_report_revocation_during_recorder_stops_following_consumer_read(self):
        h=Harness();h.policy['features']={'energy_reports':{'read':{'mode':'household'}}}
        async def revoke(*_):
            h.policy['features']['energy_reports']['read']={'mode':'admin'}
        h.on_read=revoke
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day','user',purpose='report_read')
        self.assertEqual(1,len(h.queries));self.assertIsNone(h.subject._cache)

    async def test_report_revocation_after_cpu_never_caches_or_returns(self):
        h=Harness();h.policy['features']={'energy_reports':{'read':{'mode':'household'}}}
        def revoke(_):h.policy['features']['energy_reports']['read']={'mode':'admin'}
        h.on_cpu=revoke
        with h.patches():
            with self.assertRaises(PermissionError):
                await h.subject.async_get_analysis('day','user',purpose='report_read')
        self.assertIsNone(h.subject._cache)

    async def test_analysis_and_reports_have_distinct_context_hashes_and_cache_entries(self):
        h=Harness();h.user.is_admin=True
        with h.patches():
            analysis=await h.subject.async_get_analysis('day','user')
            report=await h.subject.async_get_analysis('day','user',purpose='report_read')
            self.assertEqual(analysis['totals'],report['totals'])
            self.assertNotEqual(analysis['snapshot_id'],report['snapshot_id'])
            self.assertEqual('analysis',analysis['context']['access_purpose'])
            self.assertEqual('report_read',report['context']['access_purpose'])
            cached=await h.subject.async_get_analysis('day','user',purpose='report_read')
            self.assertEqual(report,cached)
            self.assertTrue(await h.subject.async_validate_result(report,'user'))

    async def test_invalid_purpose_never_reaches_history_even_for_admin(self):
        h=Harness();h.user.is_admin=True
        with h.patches():
            for purpose in ('smtp','report_admin',None,{},[]):
                with self.assertRaises(ValueError):
                    await h.subject.async_get_analysis('day','user',purpose=purpose)
        self.assertEqual([],h.queries);self.assertEqual([],h.registry_ids)
