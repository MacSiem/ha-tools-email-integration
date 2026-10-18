"""Unicode77: only display names normalize, through the real provider/service."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from html import escape
import json
import unittest

from tests import test_energy_analysis_service as service_fixture
from tests import helper_composer_schema2 as composer_fixture
from tests import test_tariff_profile as tariff_fixture

provider=service_fixture.provider


def preferences(name='Desk'):
    return {'energy_sources':[{'type':'grid','stat_energy_from':'sensor.grid'}],
        'device_consumption':[{'stat_consumption':'sensor.device','included_in_stat':None,'name':name}],
        'unrelated':{'keep':['RAW',7]}}


class ConfigurationUnicodeTest(unittest.TestCase):
    def test_scalar_emoji_pair_and_lone_surrogates_are_display_only(self):
        for raw,expected in [
            ('Żółć 😀 🧑🏽\u200d💻','Żółć 😀 🧑🏽\u200d💻'),
            ('pair \ud83d\ude00','pair 😀'),
            ('high \ud800','high \ufffd'),('low \udfff','low \ufffd'),
            ('mixed \ud800X\udfff \ud83d\ude00','mixed \ufffdX\ufffd 😀'),
            ('2026-10-05 <tag>& \ud800','2026-10-05 <tag>& \ufffd'),
        ]:
            with self.subTest(raw=ascii(raw)):
                original=preferences(raw);before=deepcopy(original)
                result=provider.capture_energy_configuration(original)
                self.assertEqual(expected,result['consumers'][0]['name'])
                self.assertEqual(before,original)
                self.assertEqual('sensor.device',result['consumers'][0]['stat_consumption'])
                self.assertIsNone(result['consumers'][0]['included_in_stat'])
                self.assertEqual([{'entity_id':'sensor.grid','name':'sensor.grid'}],result['grid_sensors'])
                json.dumps(result,ensure_ascii=False,allow_nan=False).encode('utf-8',errors='strict')
                self.assertFalse(any(0xD800<=ord(c)<=0xDFFF for c in result['consumers'][0]['name']))

    def test_signature_is_deterministic_operational_projection(self):
        for raw,clean in [('pair \ud83d\ude00','pair 😀'),('lone \ud800','lone \ufffd')]:
            left=provider.capture_energy_configuration(preferences(raw))
            right=provider.capture_energy_configuration(preferences(clean))
            self.assertEqual(right,left)
            preimage={key:value for key,value in left.items() if key!='source_signature'}
            expected=sha256(json.dumps(preimage,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
            self.assertEqual(expected,left['source_signature'])
        first=provider.capture_energy_configuration(preferences('Desk'))
        original=preferences('Desk');original['unrelated']['extra']='not operational'
        self.assertEqual(first,provider.capture_energy_configuration(original))
        changed=preferences('Desk');changed['device_consumption'][0]['name']='Other'
        self.assertNotEqual(first['source_signature'],provider.capture_energy_configuration(changed)['source_signature'])

    def test_structural_ids_and_parent_identity_are_never_repaired(self):
        cases=[]
        for bad in ('\ud800','\ud83d\ude00',False,7,''):
            value=preferences('display \ud800')
            value['device_consumption'][0]['stat_consumption']=bad
            cases.append(value)
        value=preferences('display \ud800');value['device_consumption'][0]['included_in_stat']='\udfff';cases.append(value)
        value=preferences('display \ud800');value['energy_sources'][0]['stat_energy_from']='sensor.\ud800';cases.append(value)
        value=preferences('display \ud800');value['unrelated']['private']='\udfff';cases.append(value)
        for value in cases:
            with self.subTest(value=ascii(value)):
                before=deepcopy(value)
                with self.assertRaises(ValueError):provider.capture_energy_configuration(value)
                self.assertEqual(before,value)
        valid=preferences('display \ud800')
        valid['device_consumption']=[
            {'stat_consumption':'sensor.parent','included_in_stat':None,'name':'parent \ud800'},
            {'stat_consumption':'sensor.child','included_in_stat':'sensor.parent','name':'child \udfff'}]
        result=provider.capture_energy_configuration(valid)
        self.assertEqual(['sensor.parent','sensor.child'],[r['stat_consumption'] for r in result['consumers']])
        self.assertEqual([None,'sensor.parent'],[r['included_in_stat'] for r in result['consumers']])

    def test_cycles_sizes_source_cap_and_non_json_remain_rejected(self):
        root=preferences('bad \ud800');root['cycle']=root
        with self.assertRaises(ValueError):provider.capture_energy_configuration(root)
        self.assertIs(root,root['cycle'])
        cases=[]
        value=preferences('bad \ud800');value['unrelated']['huge']='x'*4_000_001;cases.append(value)
        cases.append(preferences('\ud800'+'x'*4_000_001))
        value=preferences('bad \ud800');value['device_consumption']=[
            {'stat_consumption':'sensor.s'+str(i),'name':'bad \ud800'} for i in range(provider.MAX_SOURCES+1)];cases.append(value)
        value=preferences('bad \ud800');value['energy_sources']=[
            {'type':'grid','stat_energy_from':'sensor.g'+str(i)} for i in range(provider.MAX_SOURCES+1)];cases.append(value)
        value=preferences('bad \ud800');value['device_consumption']=[
            {'stat_consumption':'sensor.a','included_in_stat':'sensor.b','name':'a \ud800'},
            {'stat_consumption':'sensor.b','included_in_stat':'sensor.a','name':'b \udfff'}];cases.append(value)
        value=preferences('bad \ud800');value['unrelated']['object']=object();cases.append(value)
        for value in cases:
            with self.assertRaises(ValueError):provider.capture_energy_configuration(value)
        # Generic JSON validator remains strict even when a display-name caller
        # has a narrowly sanitized view. It must never accept RAW surrogates.
        with self.assertRaises(ValueError):provider._bounded(preferences('bad \ud800'))


class UnicodeServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_report_keeps_utf8_names_ids_and_literal_money_without_raw_mutation(self):
        now=datetime(2026,1,2,tzinfo=timezone.utc)
        book=composer_fixture.schema2_book(tariff_fixture.profile())
        for raw,expected in [
            ('2026-10-05 😀 \ud83d\ude00 <tag>&','2026-10-05 😀 😀 <tag>&'),
            ('2026-10-05 \ud800 \udfff 😀 <tag>&','2026-10-05 \ufffd \ufffd 😀 <tag>&')]:
            with self.subTest(raw=ascii(raw)):
                original=preferences(raw);before=deepcopy(original)
                fx=composer_fixture.ComposerFixture(now,preferences=original,
                    rows={'sensor.grid':{},'sensor.device':{}},book=book)
                manager_before=deepcopy(fx.manager.data)
                with fx.patches():payload=await fx.compose()
                self.assertEqual(before,original);self.assertEqual(manager_before,fx.manager.data)
                device=fx.snapshot['groups']['devices']['sensor.device']
                self.assertEqual(expected,device['name'])
                self.assertIn(expected,payload['body']);self.assertIn(escape(expected),payload['html'])
                self.assertNotIn('<tag>',payload['html'])
                self.assertEqual(Decimal('24'),Decimal(fx.snapshot['totals']['kwh']))
                self.assertEqual('24.00',fx.snapshot['totals']['usage_cost'])
                self.assertEqual('0.23',fx.snapshot['totals']['fixed_cost'])
                self.assertEqual('24.23',fx.snapshot['totals']['total_cost'])
                for field in ('body','html','csv'):payload[field].encode('utf-8',errors='strict')
                json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8',errors='strict')
                self.assertEqual([['sensor.device']],fx.h.registry_ids)
                self.assertNotIn('load_config',fx.h.hass.data[service_fixture.service.DOMAIN])
                self.assertEqual([{'sensor.grid'},{'sensor.device'}],[ids for a,b,ids in fx.statistics_calls])
                clean=composer_fixture.ComposerFixture(now,preferences=preferences(expected),
                    rows={'sensor.grid':{},'sensor.device':{}},book=book)
                with clean.patches():await clean.compose()
                self.assertEqual(clean.snapshot['totals'],fx.snapshot['totals'])
                self.assertEqual(clean.snapshot['context']['source_signature'],fx.snapshot['context']['source_signature'])

    async def test_display_name_normalization_cannot_bypass_native_all_source_access(self):
        raw=preferences('safe display \ud800')
        fx=composer_fixture.ComposerFixture(datetime(2026,1,2,tzinfo=timezone.utc),
            preferences=raw,rows={'sensor.grid':{},'sensor.device':{}})
        fx.h.user.is_admin=False
        fx.h.policy['features']={'energy_reports':{'read':{'mode':'household'}}}
        fx.h.permissions.access_all_entities=lambda _:False
        fx.h.permissions.check_entity=lambda sid,_:sid=='sensor.grid'
        before=deepcopy(fx.manager.data)
        with fx.patches(),self.assertRaises(PermissionError):await fx.compose()
        self.assertEqual(before,fx.manager.data)
        self.assertEqual([],fx.statistics_calls);self.assertEqual([],fx.metadata_reads)
        self.assertEqual([],fx.h.registry_ids);self.assertIsNone(fx.snapshot)
