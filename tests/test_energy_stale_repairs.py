"""Real Repairs lifecycle with Recorder and HA registry at their boundaries."""
import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from tests.test_energy_snapshot import PACKAGE

repairs = importlib.import_module(PACKAGE + '.energy_repairs')
D = repairs.DOMAIN

class RepairsTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        issues = {}
        registry = types.SimpleNamespace(issues=issues, async_get_issue=lambda domain, key: issues.get((domain,key)))
        ir = types.ModuleType('homeassistant.helpers.issue_registry')
        ir.IssueSeverity = types.SimpleNamespace(WARNING='warning')
        ir.async_get = lambda hass: registry
        ir.async_create_issue = lambda hass,domain,key,**kw: issues.__setitem__((domain,key),kw)
        ir.async_delete_issue = lambda hass,domain,key: issues.pop((domain,key),None)
        config = {'energy_sources':[{'type':'grid','flow_from':[{'stat_energy_from':'sensor.a'}]}],
                  'device_consumption':[{'stat_consumption':'sensor.a'}]}
        manager=types.SimpleNamespace(data=config)
        association=types.SimpleNamespace(_running=True,_manager=manager)
        storage=object()
        now = datetime.now(timezone.utc)
        state = types.SimpleNamespace(name='Kitchen', state='unavailable',
                                      last_changed=now-timedelta(hours=27), last_reported=now)
        hass=types.SimpleNamespace(data={D:{'storage':storage,'energy_source_device':association}},
             states=types.SimpleNamespace(get=lambda sid: state if sid == 'sensor.a' else None))
        coordinator=types.SimpleNamespace(hass=hass,storage=storage,running=True,provider=None)
        hass.data[D]['energy_snapshot']=coordinator
        data={}
        stat=types.ModuleType('homeassistant.components.recorder.statistics')
        stat.get_last_statistics=lambda hass,n,sid,convert,kinds:data.get(sid, {})
        stat.get_last_short_term_statistics=lambda *args:{}
        recorder=types.ModuleType('homeassistant.components.recorder')
        async def run(fn): return fn()
        recorder.get_instance=lambda hass:types.SimpleNamespace(async_add_executor_job=run)
        modules={'homeassistant':types.ModuleType('homeassistant'), 'homeassistant.helpers':types.ModuleType('homeassistant.helpers'),
                 'homeassistant.helpers.issue_registry':ir, 'homeassistant.components':types.ModuleType('homeassistant.components'),
                 'homeassistant.components.recorder':recorder, 'homeassistant.components.recorder.statistics':stat}
        return coordinator, issues, data, config, modules

    async def test_stale_status_is_independent_of_issue_registry(self):
        c, issues, data, config, modules = self.fixture()
        now = datetime.now(timezone.utc)
        data['sensor.a'] = {'sensor.a': [{'start': (now-timedelta(hours=27)).timestamp(), 'sum': 0.0}]}
        with patch.dict(sys.modules, modules):
            await repairs.async_publish_stale_sources(c)
            self.assertTrue(c.stale_sources['sensor.a']['stale'])
            self.assertEqual({}, issues)
            state = c.hass.states.get('sensor.a')
            state.state = '0'
            state.last_reported = now
            await repairs.async_publish_stale_sources(c)
            self.assertFalse(c.stale_sources['sensor.a']['stale'])
            data['sensor.a'] = {}
            await repairs.async_publish_stale_sources(c)
            self.assertEqual({}, c.stale_sources)
            self.assertEqual({}, issues)

    async def test_cleanup_scoped_and_idempotent(self):
        c, issues, _, _, modules = self.fixture()
        keys = [*repairs.ISSUES, repairs._stale_id('sensor.a')]
        for key in keys:
            issues[(D, key)] = {'ignored': True}
        issues[(D, 'other_feature')] = {'ignored': True}
        issues[('another_domain', repairs.MISSING_IMPORT)] = {}
        with patch.dict(sys.modules, modules):
            repairs.clear_legacy_issues(c.hass)
            repairs.clear_legacy_issues(c.hass)
        self.assertEqual({(D, 'other_feature'), ('another_domain', repairs.MISSING_IMPORT)}, set(issues))
