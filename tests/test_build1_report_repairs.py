"""Report failures use confirmed native history; no live HA / network calls."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock

from tests import test_email_native_projection as projection_fixture
from tests.test_attempt_history import record
from tests.test_energy_snapshot import entity_modules

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
NOW = datetime(2026,10,12,12,tzinfo=timezone.utc)
DOMAIN = 'ha_tools_email'
ISSUE = 'reports_delivery_problem'


def project(attempts=(), schema=1):
    return projection_fixture.NativeEmailProjectionTests().project({'schedules':[],
        'attempt_history':{'schema':schema,'attempts':list(attempts)}})


def attempt(outcome, offset=0, kind='energy_report'):
    return record(outcome=outcome,kind=kind,
        id=f'00000000-0000-4000-8000-{offset+10:012d}',
        timestamp=(NOW+timedelta(seconds=offset)).isoformat(),
        recipient_count=8, schedule_ref='a'*64,
        recipients=['PRIVATE@example.invalid'], label='PRIVATE LABEL')


def coordinator(result):
    """Execute the real coordinator class, doubling only HA service boundaries."""
    issues, changes = {}, []
    registry = types.SimpleNamespace(async_get_issue=lambda domain,key:issues.get(key))
    def create(hass,domain,key,**kwargs):
        issues[key]=kwargs;changes.append(('create',key,kwargs))
    def delete(hass,domain,key):
        issues.pop(key,None);changes.append(('delete',key))
    ir=types.SimpleNamespace(async_get=lambda hass:registry,async_create_issue=create,
        async_delete_issue=delete,IssueSeverity=types.SimpleNamespace(WARNING='warning'))
    dr=types.SimpleNamespace(async_get=lambda hass:None,async_entries_for_config_entry=lambda *args:[])
    token=object()
    storage=types.SimpleNamespace(async_get_report_status=AsyncMock(return_value=(result,token)),
        report_status_context_is_current=lambda value:value is token)
    hass=types.SimpleNamespace(data={DOMAIN:{'storage':storage,'schedule_generation':1}},
        config=types.SimpleNamespace(time_zone='UTC'))
    namespace={'asyncio':asyncio,'datetime':datetime,'timedelta':timedelta,'timezone':timezone,
        'callback':lambda fn:fn,'DOMAIN':DOMAIN,'ir':ir,'dr':dr}
    tree=ast.parse((ROOT/'email_native.py').read_text())
    cls=next(node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='EmailNativeCoordinator')
    exec(compile(ast.Module(body=[cls],type_ignores=[]),str(ROOT/'email_native.py'),'exec'),namespace)
    native=namespace['EmailNativeCoordinator'](hass,types.SimpleNamespace(entry_id='stable'),storage)
    native.running=True;hass.data[DOMAIN]['email_native']=native
    return native,issues,changes


class ProjectionTests(unittest.TestCase):
    def test_failures_unknown_and_partial_raise_attention_without_claiming_delivery(self):
        failures=('preparation_failed','authentication_failed','recipients_refused','tls_failed','send_failed')
        unknown=('transport_outcome_unknown','cancelled_outcome_unknown','outcome_unknown','partial_acceptance')
        for outcome in failures+unknown:
            with self.subTest(outcome=outcome):
                fact=project([attempt(outcome)])['delivery_problem']
                self.assertTrue(fact['history_ready']);self.assertTrue(fact['problem'])
                self.assertEqual(outcome,fact['outcome'])
                self.assertEqual(outcome in unknown,fact['outcome_unknown'])
                self.assertNotIn('@',repr(fact));self.assertNotIn('PRIVATE',repr(fact))
                self.assertNotIn('a'*64,repr(fact));self.assertNotIn('recipient_count',fact)

    def test_later_smtp_or_notify_acceptance_is_the_only_recovery_fact(self):
        for outcome in ('accepted_by_smtp','accepted_by_notify'):
            with self.subTest(outcome=outcome):
                fact=project([attempt('send_failed',-2),attempt(outcome,-1)])['delivery_problem']
                self.assertFalse(fact['problem']);self.assertFalse(fact['outcome_unknown'])
                self.assertEqual(outcome,fact['outcome'])
        fact=project([attempt('accepted_by_smtp',-2),attempt('transport_outcome_unknown',-1)])['delivery_problem']
        self.assertTrue(fact['problem']);self.assertTrue(fact['outcome_unknown'])

    def test_same_time_channel_acceptance_never_hides_failure_or_unknown(self):
        for outcome in ('send_failed','transport_outcome_unknown'):
            failed=attempt(outcome)
            accepted=attempt('accepted_by_notify');accepted['id']='ffffffff-ffff-4fff-8fff-ffffffffffff'
            fact=project([failed,accepted])['delivery_problem']
            self.assertTrue(fact['problem']);self.assertEqual(outcome,fact['outcome'])

    def test_manual_test_send_cannot_clear_a_report_problem_and_missing_history_is_unknown(self):
        fact=project([attempt('send_failed',-2),attempt('accepted_by_smtp',-1,kind='test')])['delivery_problem']
        self.assertTrue(fact['problem'])
        self.assertIsNone(project()['delivery_problem']['problem'])
        unknown=project(schema=99)['delivery_problem']
        self.assertFalse(unknown['history_ready']);self.assertIsNone(unknown['problem'])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_creates_one_owned_issue_and_success_clears_it(self):
        native,issues,changes=coordinator(project([attempt('send_failed',-1)]))
        await native.async_refresh();await native.async_refresh()
        self.assertTrue(native.report_problem);self.assertIn(ISSUE,issues)
        self.assertEqual(1,len([change for change in changes if change[:2]==('create',ISSUE)]))
        self.assertEqual({'is_fixable':False,'is_persistent':False,'severity':'warning','translation_key':ISSUE},issues[ISSUE])
        self.assertNotIn('PRIVATE',repr(issues));self.assertNotIn('@',repr(issues))
        self.assertTrue(native.storage.async_get_report_status.await_args.kwargs['native'])
        self.assertIsNone(native.storage.async_get_report_status.await_args.args[0])
        result=project([attempt('accepted_by_notify')])
        token=native.storage.async_get_report_status.return_value[1]
        native.storage.async_get_report_status.return_value=(result,token)
        await native.async_refresh()
        self.assertFalse(native.report_problem);self.assertNotIn(ISSUE,issues)

    async def test_unknown_keeps_attention_and_history_loss_never_proves_recovery(self):
        native,issues,changes=coordinator(project([attempt('transport_outcome_unknown')]))
        await native.async_refresh()
        self.assertTrue(native.report_problem);self.assertTrue(native.report_outcome_unknown)
        token=native.storage.async_get_report_status.return_value[1]
        native.storage.async_get_report_status.return_value=(project(schema=99),token)
        await native.async_refresh()
        self.assertIsNone(native.report_problem);self.assertIn(ISSUE,issues)
        native.storage.async_get_report_status.return_value=(project(),token)
        await native.async_refresh()
        self.assertTrue(native.report_problem);self.assertIn(ISSUE,issues)
        self.assertFalse(any(change[:2]==('delete',ISSUE) for change in changes))

    async def test_older_success_cannot_clear_latched_problem_after_history_loss(self):
        native,issues,changes=coordinator(project([attempt('send_failed')]))
        await native.async_refresh()
        token=native.storage.async_get_report_status.return_value[1]
        native.storage.async_get_report_status.return_value=(project([attempt('accepted_by_smtp',-1)]),token)
        await native.async_refresh()
        self.assertTrue(native.report_problem);self.assertIn(ISSUE,issues)

    async def test_stale_refresh_and_old_owner_stop_cannot_change_replacement_issue(self):
        native,issues,changes=coordinator(project([attempt('send_failed')]))
        await native.async_refresh();count=len(changes)
        native.storage.report_status_context_is_current=lambda value:False
        token=native.storage.async_get_report_status.return_value[1]
        native.storage.async_get_report_status.return_value=(project([attempt('accepted_by_smtp')]),token)
        await native.async_refresh()
        self.assertTrue(native.report_problem);self.assertEqual(count,len(changes))
        native.hass.data[DOMAIN]['email_native']=object()
        native.async_stop()
        self.assertIn(ISSUE,issues)

    async def test_owned_unload_cleans_ephemeral_issue_and_stops_listeners(self):
        native,issues,changes=coordinator(project([attempt('send_failed')]))
        await native.async_refresh();removed=[]
        native._unsubscribers.append(lambda:removed.append(True))
        native.async_stop()
        self.assertFalse(native.running);self.assertIsNone(native.report_problem)
        self.assertNotIn(ISSUE,issues);self.assertEqual([True],removed)


class EntityAndDescriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_problem_sensor_has_stable_identity_no_private_attributes_and_listener_cleanup(self):
        _,module=entity_modules()
        listeners=[]
        native=types.SimpleNamespace(running=True,report_problem=True,report_outcome='transport_outcome_unknown',
            report_outcome_unknown=True,async_add_listener=lambda fn:(listeners.append(fn) or (lambda:listeners.remove(fn))))
        sensor=module.ReportsProblemSensor(native,types.SimpleNamespace(entry_id='stable'))
        self.assertEqual('stable_reports_problem',sensor._attr_unique_id)
        self.assertEqual('reports_problem',sensor._attr_translation_key)
        self.assertEqual('problem',sensor._attr_device_class)
        self.assertEqual({(DOMAIN,'stable_energy')},sensor._attr_device_info['identifiers'])
        self.assertEqual({'last_outcome':'transport_outcome_unknown','outcome_unknown':True},sensor.extra_state_attributes)
        self.assertTrue(sensor.available);self.assertTrue(sensor.is_on)
        await sensor.async_added_to_hass();self.assertEqual(1,len(listeners))
        sensor.remove();self.assertEqual([],listeners)
        native.report_problem=None;self.assertFalse(sensor.available)
        added=[]
        hass=types.SimpleNamespace(data={DOMAIN:{'energy_snapshot':object(),'email_native':native}})
        await module.async_setup_entry(hass,types.SimpleNamespace(entry_id='stable'),lambda entities:added.extend(entities))
        self.assertEqual(1,sum(isinstance(entity,module.ReportsProblemSensor) for entity in added))

    async def test_service_and_issue_descriptions_cover_actual_runtime_fields_without_delivered_claim(self):
        for lang in ('en','pl'):
            translated=json.loads((ROOT/'translations'/f'{lang}.json').read_text())
            action=translated['services']['send_energy_report']
            self.assertEqual({'cadence','date','targets'},set(action['fields']))
            self.assertIn('YYYY-MM-DD',action['fields']['date']['description'])
            self.assertIn('persistent_notification',action['fields']['targets']['description'])
            self.assertIn('email',action['fields']['targets']['description'])
            self.assertIn(ISSUE,translated['issues'])
            self.assertIn('reports_problem',translated['entity']['binary_sensor'])
        yaml_source=(ROOT/'services.yaml').read_text()
        self.assertEqual(1,yaml_source.count('\nsend_energy_report:'))
        self.assertIn('SupportsResponse', (ROOT/'__init__.py').read_text())
