"""Fixture71: real schema2 service/renderer and actual Recorder adapter.

Only HA manager/auth/Recorder scheduling boundaries are synthetic. No TestCase is
imported into this module; no legacy report builder, prices or aggregate rows are faked.
"""
from contextlib import ExitStack
from copy import deepcopy
import csv
from datetime import date, datetime, timedelta, timezone
import importlib
from io import StringIO
import types
from unittest.mock import patch

from tests import test_energy_analysis_service as service_fixture

service=service_fixture.service
provider=service_fixture.provider
composer=importlib.import_module(service.__package__+'.composer')
renderer=importlib.import_module(service.__package__+'.energy_report')
inputs=importlib.import_module(service.__package__+'.energy_inputs')
history=importlib.import_module(service.__package__+'.tariff_history')
UTC=timezone.utc


def schema2_book(profile):
    """Explicit test setup of known history/components, not runtime migration."""
    if profile is None:
        return None
    return {'schema':2,'active_tariff_id':'fixture','tariffs':[{
        'id':'fixture','name':'Fixture tariff','origin':{'kind':'user','reference':None},
        'versions':[{'id':'fixture-v1','valid_from':profile.get('validFrom') or '2026-01-01',
            'valid_to':(date.fromisoformat(profile['validTo'])+timedelta(days=1)).isoformat() if profile.get('validTo') else None,
            'profile':deepcopy(profile),'rate_semantics':'components',
            'fixed_monthly':{'subscription':profile.get('fixedMonthly'),'capacity':0,'distribution':0,'other':0},
            'legacy_combined_monthly':None}]}]}


def csv_facts(payload):
    return {row['field']:row['value'] for row in csv.DictReader(StringIO(payload['csv']))}


class ComposerFixture:
    def __init__(self,now,*,preferences=None,rows=None,points=None,book=None,
                 measurement_zone='UTC',language='en',display_zone=None):
        self.h=service_fixture.Harness()
        self.h.now=now
        self.h.book=deepcopy(book)
        self.h.user.id='fixture71-admin';self.h.user.is_admin=True
        self.auth_reads=[]
        async def user(uid):
            self.auth_reads.append(uid)
            return self.h.user if uid==self.h.user.id else None
        self.h.hass.auth.async_get_user=user
        self.manager=types.SimpleNamespace(data=deepcopy(preferences if preferences is not None else {
            'energy_sources':[{'type':'grid','stat_energy_from':'sensor.grid'}],
            'device_consumption':[]}))
        self.h.subject._manager=None
        self.h.hass.config.time_zone=measurement_zone
        self.h.hass.config.language=language
        self.h.hass.states={}
        self.rows=deepcopy(rows if rows is not None else {'sensor.grid':{'unit':'kWh'}})
        self.points=points or self._unit_points
        self.language=language;self.display_zone=display_zone or measurement_zone
        self.manager_reads=[];self.metadata_reads=[];self.statistics_calls=[]
        self.adapter_reads=[];self.rendered_snapshots=[];self.statistic_periods=[]

    def _unit_points(self,start,end,ids):
        return {sid:[{'start':(start+timedelta(hours=i)).timestamp(),'change':row.get('change',1)}
            for i in range(int((end-start).total_seconds()/3600))]
            for sid,row in self.rows.items()}

    @property
    def snapshot(self):
        return self.rendered_snapshots[-1] if self.rendered_snapshots else None

    @property
    def prefs(self):
        return {'language':self.language,'timezone_name':self.display_zone,'hour_cycle':'24'}

    def patches(self):
        stack=ExitStack()
        core=types.ModuleType('homeassistant.core')
        core.valid_entity_id=lambda sid:':' not in sid and len(sid.split('.'))==2
        const=types.ModuleType('homeassistant.auth.permissions.const');const.POLICY_READ='read'
        stats=types.ModuleType('homeassistant.components.recorder.statistics')
        def metadata(hass,*,statistic_ids):
            self.metadata_reads.append(set(statistic_ids))
            return {sid:(1,{'has_sum':row.get('has_sum',True),'name':row.get('name'),
                'unit_of_measurement':row.get('unit','kWh'),'unit_class':row.get('unit_class','energy')})
                for sid,row in self.rows.items() if sid in statistic_ids}
        def statistics(hass,start,end,ids,*args):
            self.statistics_calls.append((start,end,set(ids)))
            self.statistic_periods.append(args[0] if args else 'hour')
            if args and args[0] == '5minute':
                # Explicit short records only. Do not spread fixture daily/hour
                # aggregates into fabricated five-minute measurements.
                return {sid: deepcopy(row.get('short_points', [])) for sid,row in self.rows.items() if sid in ids}
            return deepcopy(self.points(start,end,ids))
        stats.get_metadata=metadata;stats.statistics_during_period=statistics
        class Recorder:
            async def async_add_executor_job(self,fn,*args,**kwargs):return fn(*args,**kwargs)
        util=types.ModuleType('homeassistant.components.recorder.util');util.get_instance=lambda hass:Recorder()
        recorder=types.ModuleType('homeassistant.components.recorder');recorder.get_instance=util.get_instance
        energy=types.ModuleType('homeassistant.components.energy.data')
        async def manager(hass):
            self.manager_reads.append(True)
            return self.manager
        energy.async_get_manager=manager
        actual_usage=composer.async_get_energy_usage
        async def usage(hass,sensors,start,end,cadence,*,metadata=None):
            value=await actual_usage(hass,sensors,start,end,cadence,metadata=metadata)
            self.adapter_reads.append({'sensors':deepcopy(sensors),'start':start,'end':end,
                'result':deepcopy(value)})
            return value
        actual_render=renderer.render_energy_analysis
        def render(snapshot,**kwargs):
            before=deepcopy(snapshot)
            result=actual_render(snapshot,**kwargs)
            if snapshot!=before:raise AssertionError('renderer mutated producer snapshot')
            self.rendered_snapshots.append(before)
            return result
        stack.enter_context(patch.dict('sys.modules',{m.__name__:m for m in (core,const,stats,util,recorder,energy)}))
        stack.enter_context(patch.object(composer,'__package__',service.__package__))
        stack.enter_context(patch.object(service,'async_get_energy_metadata',composer.async_get_energy_metadata))
        stack.enter_context(patch.object(provider,'async_get_energy_metadata',composer.async_get_energy_metadata))
        stack.enter_context(patch.object(inputs,'async_get_energy_metadata',composer.async_get_energy_metadata))
        stack.enter_context(patch.object(provider,'async_get_energy_usage',usage))
        stack.enter_context(patch.object(renderer,'render_energy_analysis',render))
        return stack

    async def compose(self,cadence='daily',*,start=None,end=None,report_zone=None,rolling=False):
        options={}
        if start is not None:options.update(window_start=start,window_end=end)
        return await composer.async_build_energy_report_payload(self.h.hass,cadence=cadence,
            now=self.h.now,actor=self.h.user.id,report_preferences=self.prefs,
            report_timezone=report_zone or self.h.hass.config.time_zone,
            period_semantics='rolling_completed_hours' if rolling or start is None else 'previous_completed_calendar_period',
            **options)

    async def analysis(self,kind,*,anchor=None,comparison=None):
        # Actual supported partial calendar API, not a fake partial ReportWindow.
        value=await self.h.subject.async_get_report(kind,self.h.user.id,anchor=anchor,
            comparison=comparison,**self.prefs)
        return {**value,'body':value['text']}

    def assert_queries_cover(self,testcase,start,end):
        # Multiple authorized consumers (legacy paired comparison and R9 model)
        # may reuse a window. Verify coverage, not an obsolete exact call count.
        selected=sorted(set((a,b) for a,b,ids in self.statistics_calls if start<=a<b<=end))
        testcase.assertTrue(selected)
        testcase.assertEqual(start,selected[0][0]);testcase.assertEqual(end,max(stop for _,stop in selected))
        for i,(a,b) in enumerate(selected):
            testcase.assertLessEqual((b-a).total_seconds(),168*3600)
            if i:testcase.assertLessEqual(a, max(stop for _,stop in selected[:i]))
