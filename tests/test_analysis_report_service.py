"""Report lifecycle on the real calculation/CPU service without SMTP."""
from copy import deepcopy
import sys
import types
import unittest
from unittest.mock import patch
from tests.test_energy_analysis_service import Harness, service


class AnalysisReportServiceTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        h=Harness();h.user.is_admin=True
        calls=[]
        def validate(language,timezone_name,hour_cycle):
            if language not in ('en','pl') or timezone_name not in ('UTC','Europe/Warsaw') or hour_cycle not in ('12','24'):
                raise ValueError('invalid_report_preferences')
        def render_energy_analysis(snapshot,**options):
            calls.append((deepcopy(snapshot),dict(options)))
            return {'snapshot_id':snapshot['snapshot_id'],'html':'<table>synthetic</table>','text':'synthetic','csv':'synthetic'}
        renderer=types.SimpleNamespace(validate_report_preferences=validate,render_energy_analysis=render_energy_analysis)
        return h,calls,renderer

    async def test_report_uses_same_calculation_without_smtp_and_carries_fresh_context(self):
        h,calls,renderer=self.fixture()
        with h.patches(),patch.dict(sys.modules,{service.__package__+'.energy_report':renderer}):
            result=await h.subject.async_get_report('day','user',language='pl',timezone_name='Europe/Warsaw',hour_cycle='24')
            self.assertEqual('report_read',result['context']['access_purpose'])
            self.assertEqual(calls[0][0]['snapshot_id'],result['snapshot_id'])
            self.assertEqual({'language':'pl','timezone_name':'Europe/Warsaw','hour_cycle':'24'},calls[0][1])
            self.assertEqual({'language':'pl','timezone':'Europe/Warsaw','hour_cycle':'24'},result['display'])
            self.assertTrue(await h.subject.async_validate_result(result,'user'))
            self.assertTrue(h.subject.context_is_current(result))
        self.assertEqual(1,len(calls));self.assertNotIn('load_config',h.hass.data[service.DOMAIN])

    async def test_household_can_read_only_explicitly_delegated_report(self):
        h,calls,renderer=self.fixture();h.user.is_admin=False
        h.policy['features']={'energy_reports':{'read':{'mode':'household'}},'energy_analysis':{'read':{'mode':'admin'}}}
        with h.patches(),patch.dict(sys.modules,{service.__package__+'.energy_report':renderer}):
            result=await h.subject.async_get_report('day','user',language='en',timezone_name='UTC',hour_cycle='12')
            self.assertEqual('report_read',result['context']['access_purpose'])
        self.assertEqual(1,len(calls))

    async def test_default_denial_happens_before_render_or_recorder(self):
        h,calls,renderer=self.fixture();h.user.is_admin=False
        with h.patches(),patch.dict(sys.modules,{service.__package__+'.energy_report':renderer}):
            with self.assertRaises(PermissionError):
                await h.subject.async_get_report('day','user',language='en',timezone_name='UTC',hour_cycle='24')
        self.assertEqual([],calls);self.assertEqual([],h.queries)

    async def test_invalid_preferences_do_not_read_private_sources(self):
        for options in ({'language':'xx','timezone_name':'UTC','hour_cycle':'24'},
                        {'language':'pl','timezone_name':'No/Such','hour_cycle':'24'},
                        {'language':'en','timezone_name':'UTC','hour_cycle':'auto'}):
            h,calls,renderer=self.fixture()
            with h.patches(),patch.dict(sys.modules,{service.__package__+'.energy_report':renderer}):
                with self.assertRaises(ValueError):await h.subject.async_get_report('day','user',**options)
            self.assertEqual([],calls);self.assertEqual([],h.queries)

    async def test_role_or_source_revocation_during_renderer_never_returns_report(self):
        for mutation in ('role','source'):
            h,calls,renderer=self.fixture()
            h.user.is_admin=False;h.policy['features']={'energy_reports':{'read':{'mode':'household'}}}
            def revoke(job):
                if getattr(job,'func',None) is renderer.render_energy_analysis:
                    if mutation=='role':h.policy['features']['energy_reports']['read']={'mode':'admin'}
                    else:
                        h.permissions.access_all_entities=lambda _:False
                        h.permissions.check_entity=lambda *_:False
            h.on_cpu=revoke
            with h.patches(),patch.dict(sys.modules,{service.__package__+'.energy_report':renderer}):
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_report('day','user',language='en',timezone_name='UTC',hour_cycle='24')
            self.assertEqual(1,len(calls));self.assertIsNone(h.subject._cache)

    async def test_context_change_during_renderer_never_returns_old_report(self):
        h,calls,renderer=self.fixture()
        def change(job):
            if getattr(job,'func',None) is renderer.render_energy_analysis:h.registry.revision+=1
        h.on_cpu=change
        with h.patches(),patch.dict(sys.modules,{service.__package__+'.energy_report':renderer}):
            with self.assertRaises(RuntimeError):
                await h.subject.async_get_report('day','user',language='en',timezone_name='UTC',hour_cycle='24')
        self.assertEqual(1,len(calls))
