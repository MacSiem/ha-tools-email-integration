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

    async def test_lifecycle_unknown_recovery_removal_and_grid_configuration(self):
        c,issues,data,config,modules=self.fixture()
        sid='sensor.a'; key=(D,repairs._stale_id(sid)); grid=(D,repairs.GRID_DEVICES)
        now=datetime.now(timezone.utc)
        data[sid]={sid:[{'start':(now-timedelta(hours=27)).timestamp(),'sum':0.0}]}
        with patch.dict(sys.modules,modules):
            await repairs.async_publish_stale_sources(c)
            self.assertIn(key,issues); self.assertIn(grid,issues)
            self.assertTrue(c.stale_sources[sid]['stale'])
            self.assertEqual('Kitchen',issues[key]['translation_placeholders']['name'])
            data[sid]={}
            await repairs.async_publish_stale_sources(c)
            self.assertIn(key,issues) # missing evidence is not recovery
            data[sid]={sid:[{'start':(now-timedelta(hours=2)).timestamp(),'sum':0.0}]}
            state=c.hass.states.get(sid); state.state='0'; state.last_reported=now
            await repairs.async_publish_stale_sources(c)
            self.assertNotIn(key,issues); self.assertFalse(c.stale_sources[sid]['stale'])
            data[sid]={sid:[{'start':(now-timedelta(hours=27)).timestamp(),'sum':0.0}]}
            state.state='unavailable'; state.last_changed=now-timedelta(hours=27)
            await repairs.async_publish_stale_sources(c)
            config['energy_sources']=[]; config['device_consumption']=[]
            await repairs.async_publish_stale_sources(c)
            self.assertNotIn(key,issues);self.assertNotIn(grid,issues)

    async def test_main_meter_does_not_claim_equal_and_failure_preserves_issue(self):
        c,issues,data,config,modules=self.fixture()
        config['energy_sources'][0]['flow_from'].append({'stat_energy_from':'sensor.main'})
        now=datetime.now(timezone.utc)
        data['sensor.a']={'sensor.a':[{'start':(now-timedelta(hours=27)).timestamp(),'sum':1}]}
        with patch.dict(sys.modules,modules):
            await repairs.async_publish_stale_sources(c)
            self.assertNotIn((D,repairs.GRID_DEVICES),issues)
            key=(D,repairs._stale_id('sensor.a'));self.assertIn(key,issues)
            modules['homeassistant.components.recorder.statistics'].get_last_statistics=lambda *a: (_ for _ in ()).throw(RuntimeError())
            await repairs.async_publish_stale_sources(c)
            self.assertIn(key,issues);self.assertIsNone(c.stale_sources)

    async def test_fresh_compiled_bucket_cannot_clear_unavailable_and_unchanged_zero_can_recover(self):
        c,issues,data,config,modules=self.fixture()
        sid='sensor.a'; key=(D,repairs._stale_id(sid))
        now=datetime.now(timezone.utc)
        state=c.hass.states.get(sid)
        # An hourly compiled bucket can contain the pre-unavailable state.
        data[sid]={sid:[{'start':(now-timedelta(hours=2)).timestamp(),'sum':0.0}]}
        with patch.dict(sys.modules,modules):
            await repairs.async_publish_stale_sources(c)
            self.assertIn(key,issues)
            self.assertEqual('unavailable_state',c.stale_sources[sid]['basis'])
            # Unknown restarts the conservative gap; it cannot prove recovery.
            state.state='unknown';state.last_changed=now
            await repairs.async_publish_stale_sources(c)
            self.assertIn(key,issues)
            # Same value reported again is an actual HA report even when unchanged.
            state.state='0';state.last_reported=now
            await repairs.async_publish_stale_sources(c)
            self.assertNotIn(key,issues)
            self.assertFalse(c.stale_sources[sid]['stale'])
            # A newly compiled bucket cannot prove an old unchanged report fresh.
            state.last_reported=now-timedelta(hours=26)
            await repairs.async_publish_stale_sources(c)
            self.assertIn(key,issues)

    async def test_external_gap_is_statistics_only_and_missing_entity_is_unknown(self):
        c,issues,data,config,modules=self.fixture()
        now=datetime.now(timezone.utc); external='vendor:meter'
        config['energy_sources'][0]['flow_from']=[{'stat_energy_from':external}]
        config['device_consumption']=[]
        data[external]={external:[{'start':(now-timedelta(hours=27)).timestamp(),'sum':0}]}
        with patch.dict(sys.modules,modules):
            await repairs.async_publish_stale_sources(c)
            issue=issues[(D,repairs._stale_id(external))]
            self.assertEqual('energy_stale_external',issue['translation_key'])
            self.assertEqual('external_statistics',c.stale_sources[external]['basis'])
            config['energy_sources'][0]['flow_from']=[{'stat_energy_from':'sensor.missing'}]
            data['sensor.missing']={'sensor.missing':[{'start':(now-timedelta(hours=27)).timestamp(),'sum':0}]}
            await repairs.async_publish_stale_sources(c)
            self.assertNotIn((D,repairs._stale_id('sensor.missing')),issues)

    def test_incomplete_or_invalid_rows_cannot_prove_reading(self):
        now=datetime.now(timezone.utc)
        for row in ({'start':now.timestamp(),'sum':0}, {'start':0,'sum':float('nan')}, {'start':0,'sum':None}):
            self.assertIsNone(repairs._last_reading({'a':[row]},'a',timedelta(minutes=5),now))
