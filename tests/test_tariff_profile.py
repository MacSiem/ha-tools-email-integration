"""Parity cases from shared/ha-tools-energy-tariff.js; no SMTP operations."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
spec = importlib.util.spec_from_file_location('email_tariff', ROOT / 'tariff.py')
tariff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tariff)


def base(**changes):
    return dict(schema=1, currency='EUR', timezone='UTC', clock={'mode': 'civil'},
                priceBasis='gross', validFrom='2026-01-01', validTo='2026-12-31',
                defaultZone='day', zones=[{'id': 'day', 'energyRate': 1, 'variableRate': 0},
                                          {'id': 'night', 'energyRate': .4, 'variableRate': 0}],
                rules=[], fixedMonthly=7, **changes)


def profile(**changes):
    p = base()
    p.update(changes)
    return p


def hour(at='2026-01-01T12:00Z', kwh=2):
    from datetime import datetime, timedelta
    end = datetime.fromisoformat(at.replace('Z', '+00:00')) + timedelta(hours=1)
    return {'from': at, 'to': end.isoformat(), 'kwh': kwh}


OVERNIGHT = {'zone': 'night', 'intervals': [{'from': '22:00', 'to': '06:00'}]}


class TariffParityTest(unittest.TestCase):
    def model(self, **changes):
        return tariff.EnergyTariff(profile(**changes))

    def test_flat_zero_net_and_unknown_fee(self):
        r = self.model().price_buckets([hour()])
        self.assertEqual((True, 2, 0, 2, 7, 'EUR', 'gross'),
                         tuple(r[k] for k in ('complete', 'energyCost', 'variableCost',
                                              'usageCost', 'fixedMonthly', 'currency', 'priceBasis')))
        z = [{'id': 'day', 'energyRate': 0, 'variableRate': 0}]
        self.assertEqual(0, self.model(zones=z).price_buckets([hour(kwh=0)])['usageCost'])
        r = self.model(priceBasis='net', zones=[{'id': 'day', 'energyRate': .5}]).price_buckets([hour()])
        self.assertEqual(1, r['energyCost'])
        self.assertIsNone(r['variableCost'])
        self.assertIsNone(r['usageCost'])
        self.assertEqual('net', r['priceBasis'])

    def test_invalid_values_are_not_zero(self):
        for rate in (None, True, '', '0.4', float('nan'), float('inf')):
            with self.subTest(rate=rate):
                self.assertFalse(self.model(zones=[{'id': 'day', 'energyRate': rate}]).valid)
        for changes in ({'timezone': 'No/SuchZone'}, {'currency': ''}, {'validFrom': '2026-02-30'},
                        {'validFrom': '2026-12-31', 'validTo': '2026-01-01'},
                        {'clock': {'mode': 'fixed', 'offsetMinutes': True}},
                        {'clock': {'mode': 'fixed', 'offsetMinutes': 5000}},
                        {'rules': [{'zone': 'missing', 'intervals': [{'from': '00:00', 'to': '24:00'}]}]}):
            self.assertFalse(self.model(**changes).valid)

    def test_overnight_weekday_start_season_exceptions_priority(self):
        m = self.model(rules=[dict(OVERNIGHT, weekdays=[5])])
        self.assertEqual('night', m.rate_at('2026-01-03T02:00Z')['zone'])
        self.assertEqual('day', m.rate_at('2026-01-02T02:00Z')['zone'])
        winter = dict(OVERNIGHT, season={'from': '10-01', 'to': '03-31'})
        m = self.model(rules=[winter])
        self.assertEqual('night', m.rate_at('2026-03-31T23:00Z')['zone'])
        self.assertEqual('day', m.rate_at('2026-04-01T23:00Z')['zone'])
        conflict = {'zone': 'day', 'intervals': [{'from': '23:00', 'to': '24:00'}]}
        self.assertEqual('ambiguous_rules', self.model(rules=[OVERNIGHT, conflict]).rate_at('2026-01-01T23:30Z')['reason'])
        self.assertEqual('day', self.model(rules=[OVERNIGHT, dict(conflict, priority=2)]).rate_at('2026-01-01T23:30Z')['zone'])
        exception = {'zone': 'night', 'dates': ['2026-07-09'], 'intervals': [{'from': '00:00', 'to': '24:00'}]}
        self.assertEqual('night', self.model(exceptions=[exception]).rate_at('2026-07-09T12:00Z')['zone'])
        self.assertEqual('missing_rate', self.model(defaultZone=None).rate_at('2026-07-09T12:00Z')['reason'])

    def test_dst_fixed_clock_and_local_validity(self):
        rule = {'zone': 'night', 'intervals': [{'from': '13:00', 'to': '15:00'}]}
        self.assertEqual('night', self.model(timezone='Europe/Warsaw', rules=[rule]).rate_at('2026-07-01T11:30Z')['zone'])
        self.assertEqual('day', self.model(timezone='Europe/Warsaw', rules=[rule], clock={'mode': 'fixed', 'offsetMinutes': 60}).rate_at('2026-07-01T11:30Z')['zone'])
        for dates in (['2026-10-25T00:00Z', '2026-10-25T01:00Z'], ['2026-03-29T00:00Z', '2026-03-29T01:00Z']):
            r = self.model(timezone='Europe/Warsaw', rules=[OVERNIGHT]).price_buckets([hour(dates[0], 2), hour(dates[1], 3)])
            self.assertTrue(r['complete'])
            self.assertEqual((5, 2), (r['kwh'], r['energyCost']))
        m = self.model(timezone='Pacific/Auckland')
        self.assertTrue(m.rate_at('2025-12-31T12:00Z')['available'])
        self.assertEqual('outside_validity', m.rate_at('2026-12-31T12:00Z')['reason'])

    def test_measurement_rejection_no_interpolation_and_coverage(self):
        m = self.model(rules=[{'zone': 'night', 'intervals': [{'from': '12:30', 'to': '13:00'}]}])
        self.assertEqual('mixed_rate_bucket', m.price_buckets([hour()])['issues'][0]['reason'])
        b = {'from': '2026-01-01T12:30Z', 'to': '2026-01-01T13:00Z', 'kwh': 2}
        self.assertEqual(.8, m.price_buckets([b])['energyCost'])
        for rows in ([], [hour(kwh=None)], [hour(kwh=True)], [hour(), hour()], [hour(), hour('2026-01-01T12:30Z')]):
            r = self.model().price_buckets(rows)
            self.assertFalse(r['complete'])
            self.assertIsNone(r['energyCost'])
        r = self.model().price_buckets([hour()], {'from': '2026-01-01T12:00Z', 'to': '2026-01-01T14:00Z'})
        self.assertIn('missing_measurement', [i['reason'] for i in r['issues']])
        self.assertEqual('invalid_timestamp', self.model().rate_at('2026-01-01T12:00')['reason'])

    def test_dynamic_gaps_overlap_negative_optin_subminute(self):
        interval = {'from': '2026-01-01T12:00Z', 'to': '2026-01-01T13:00Z', 'energyRate': -.2, 'variableRate': 0}
        self.assertFalse(self.model(dynamic={'intervals': [interval]}).valid)
        m = self.model(dynamic={'intervals': [interval]}, allowNegativeEnergy=True)
        self.assertEqual(-.4, m.price_buckets([hour()])['energyCost'])
        self.assertEqual('missing_rate', m.rate_at('2026-01-01T13:30Z')['reason'])
        self.assertFalse(self.model(dynamic={'intervals': [dict(interval, energyRate=.2), dict(interval, energyRate=.8)]}).valid)
        intervals = [dict(interval, energyRate=.2, to='2026-01-01T12:00:30Z'), dict(interval, energyRate=.8, **{'from': '2026-01-01T12:00:30Z'})]
        self.assertEqual('mixed_rate_bucket', self.model(dynamic={'intervals': intervals}).price_buckets([hour()])['issues'][0]['reason'])

    def test_inputs_unchanged_and_bounded_validation(self):
        p, rows = profile(custom_future={'enabled': True}), [hour()]
        before = copy.deepcopy((p, rows))
        tariff.EnergyTariff(p).price_buckets(rows)
        self.assertEqual(before, (p, rows))
        for p in (profile(name='x'*65536), profile(rules=[OVERNIGHT]*513), profile(exceptions=[dict(OVERNIGHT, dates=['2026-01-01'])]*513)):
            with self.assertRaises(tariff.TariffValidationError):
                tariff.validate_tariff_profile(p)


def load_storage():
    """Stub only the HA Store boundary; execute the real storage implementation."""
    package = types.ModuleType('tariff_test_package')
    package.__path__ = [str(ROOT)]
    core = types.ModuleType('homeassistant.core')
    core.HomeAssistant = object
    core.callback = lambda function: function
    ha_store = types.ModuleType('homeassistant.helpers.storage')
    class Store:
        def __class_getitem__(cls, item):
            return cls
        def __init__(self, *args):
            self.saved, self.loaded, self.fail = [], {}, False
            self.swallowed_failure = self.skipped = self.old_hook = False
            self.save_calls = self.write_calls = 0
            self.write_started = self.write_release = None
        async def async_load(self):
            return copy.deepcopy(self.loaded)
        async def async_save(self, value):
            self.save_calls += 1
            if self.fail:
                raise OSError('private detail must not be exposed')
            if self.skipped:
                return
            try:
                arguments = ('synthetic-path', value) if self.old_hook else (value,)
                await self._async_write_data(*arguments)
            except OSError:
                if not self.swallowed_failure:
                    raise
        async def _async_write_data(self, *arguments):
            self.write_calls += 1
            if self.write_started is not None:
                self.write_started.set()
                await self.write_release.wait()
            if self.swallowed_failure:
                raise OSError('synthetic lower write failure')
            self.saved.append(copy.deepcopy(arguments[-1]))
    ha_store.Store = Store
    with patch.dict(sys.modules, {package.__name__: package, core.__name__: core, ha_store.__name__: ha_store}):
        name = package.__name__ + '.storage'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'storage.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._tariff_test_module = sys.modules[package.__name__ + '.tariff']
    return module


class TariffStorageTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_save_conflict_failure_restart_unknown_keys(self):
        mod = load_storage()
        hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda *args: self.fail('tariff must not inspect SMTP')))
        s = mod.EmailStorage(hass)
        s._store.loaded = {'smtp_config': {'server': 'local'}, 'schedules': [], 'future': {'keep': True}}
        self.assertEqual({'profile': None, 'revision': 0, 'updated_at': None}, await s.async_get_tariff())
        self.assertEqual([], s._store.saved)
        saved = await s.async_save_tariff(profile(), expected_revision=0)
        self.assertEqual(1, saved['revision'])
        self.assertEqual({'keep': True}, s._store.saved[0]['future'])
        self.assertEqual({'server': 'local'}, s._store.saved[0]['smtp_config'])
        with self.assertRaises(mod.TariffRevisionConflict):
            await s.async_save_tariff(profile(), expected_revision=0)
        s._store.fail = True
        with self.assertRaises(OSError):
            await s.async_save_tariff(profile(zones=[{'id': 'day', 'energyRate': 0}]), expected_revision=1)
        self.assertEqual(saved, await s.async_get_tariff())
        again = mod.EmailStorage(hass)
        again._store.loaded = s._store.saved[-1]
        self.assertEqual(saved, await again.async_get_tariff())
        changed = await again.async_save_tariff(None, expected_revision=1)
        self.assertIsNone(changed['profile'])
        self.assertEqual(2, changed['revision'])

    async def test_validation_before_load_and_concurrent_revision(self):
        mod = load_storage()
        s = mod.EmailStorage(types.SimpleNamespace())
        with self.assertRaises(mod.TariffValidationError):
            await s.async_save_tariff(profile(zones=[]), expected_revision=0)
        self.assertIsNone(s._data)
        results = await asyncio.gather(s.async_save_tariff(profile(), expected_revision=0),
                                       s.async_save_tariff(profile(), expected_revision=0), return_exceptions=True)
        self.assertEqual(1, sum(isinstance(x, mod.TariffRevisionConflict) for x in results))
        self.assertEqual(1, len(s._store.saved))


def load_composer():
    package = types.ModuleType('tariff_composer_package')
    package.__path__ = [str(ROOT)]
    with patch.dict(sys.modules, {package.__name__: package, package.__name__ + '.tariff': tariff}):
        name = package.__name__ + '.composer'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'composer.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, package


class TariffComposerTest(unittest.IsolatedAsyncioTestCase):
    async def compose(self, p, *, gap=False, bad=False):
        from datetime import datetime, timedelta, timezone
        from tests.helper_composer_schema2 import ComposerFixture,schema2_book
        end=datetime(2026,1,2,tzinfo=timezone.utc);start=end-timedelta(days=1)
        points=[{'start':(start+timedelta(hours=i)).timestamp(),
            'change':True if bad and i==4 else 20 if i==3 else 1}
            for i in range(24) if not (gap and i==8)]
        fx=ComposerFixture(end,points=lambda a,b,ids:{'sensor.grid':points},book=schema2_book(p))
        self.last_fixture=fx
        with fx.patches():return await fx.compose()

    async def test_actual_hourly_cost_and_separate_fixed_fee(self):
        from decimal import Decimal
        payload=await self.compose(profile(rules=[OVERNIGHT]));s=payload['summary'];facts=self.last_fixture.snapshot
        self.assertEqual(Decimal('43'),Decimal(s['total_kwh']))
        self.assertEqual(Decimal('26.80'),Decimal(s['totals']['energy_cost']))
        self.assertEqual(Decimal('26.80'),Decimal(s['totals']['usage_cost']))
        self.assertEqual(7,facts['rates'][0]['fixed_monthly']['subscription'])
        self.assertEqual('0.23',s['totals']['fixed_cost'])
        self.assertEqual('27.03',s['totals']['total_cost'])
        self.assertEqual('EUR',s['currency'])
        self.assertEqual({'night','day'},{zone['zone_id'] for row in facts['chart']['bins']
            for zone in row['zone_breakdown']['zones']})
        self.assertIn('26.80',payload['body']);self.assertIn('EUR',payload['body'])
        self.assertNotIn('hourly_buckets',s)
        self.assertEqual(1,len(self.last_fixture.statistics_calls))

    async def test_missing_invalid_profile_and_incomplete_measurements(self):
        missing=await self.compose(None);s=missing['summary']
        self.assertEqual('not_configured',self.last_fixture.snapshot['pricing_status'])
        self.assertIsNone(s['totals']['energy_cost']);self.assertIsNone(s['total_cost'])
        with self.assertRaises(ValueError):await self.compose({'schema':0})
        self.assertEqual([],self.last_fixture.statistics_calls)
        for kwargs in ({'gap':True},{'bad':True}):
            with self.subTest(kwargs=kwargs):
                s=(await self.compose(profile(),**kwargs))['summary']
                self.assertFalse(s['totals']['pricing_complete'])
                self.assertIsNone(s['totals']['energy_cost'])
                self.assertIsNone(s['totals']['usage_cost'])
                self.assertEqual(23,self.last_fixture.adapter_reads[0]['result']['coverage']['measured_source_hours'])


def load_websocket():
    storage = load_storage()
    package = types.ModuleType('tariff_ws_package')
    package.__path__ = [str(ROOT)]
    core = types.ModuleType('homeassistant.core')
    core.HomeAssistant = object
    core.callback = lambda function: function
    ws = types.ModuleType('homeassistant.components.websocket_api')
    ws.ActiveConnection = object
    ws.websocket_command = lambda schema: lambda fn: fn
    ws.async_response = lambda fn: fn
    def require_admin(fn):
        fn.tariff_requires_admin = True
        return fn
    ws.require_admin = require_admin
    components = types.ModuleType('homeassistant.components')
    components.websocket_api = ws
    vol = types.ModuleType('voluptuous')
    vol.Required = vol.Optional = lambda key, **kw: key
    vol.Any = vol.All = vol.In = vol.Range = lambda *a, **kw: object()
    with patch.dict(sys.modules, {package.__name__: package, package.__name__ + '.storage': storage,
                                  package.__name__ + '.tariff': storage._tariff_test_module,
                                  core.__name__: core, components.__name__: components,
                                  ws.__name__: ws, vol.__name__: vol}):
        name = package.__name__ + '.websocket_api'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'websocket_api.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    if hasattr(module, 'async_require_energy_source_access'):
        original_access = module.async_require_energy_source_access
        async def source_access(*args, **kwargs):
            # Stub only native type/policy constants; execute the real provider
            # permission check against each fixture's actual auth/permissions.
            native_core = types.ModuleType('homeassistant.core')
            native_core.valid_entity_id = lambda sid: ':' not in sid and len(sid.split('.')) == 2
            native_const = types.ModuleType('homeassistant.auth.permissions.const')
            native_const.POLICY_READ = 'read'
            with patch.dict(sys.modules, {native_core.__name__: native_core,
                                          native_const.__name__: native_const}):
                return await original_access(*args, **kwargs)
        module.async_require_energy_source_access = source_access
    return module, storage


class TariffWebsocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_separate_read_role_and_safe_save_failures(self):
        ws, mod = load_websocket()
        self.assertFalse(getattr(ws._ws_get_tariff, 'tariff_requires_admin', False))
        self.assertFalse(getattr(ws._ws_set_tariff, 'tariff_requires_admin', False))
        class Storage:
            fail = None
            current_tariff_revision = 0
            current_feature_access_revision = 0
            async def async_get_feature_access(self):
                return {'policy': {'schema': 1, 'features': {}}, 'revision': 0, 'status': 'ready'}
            async def async_get_tariff(self):
                return {'profile': None, 'revision': self.current_tariff_revision, 'updated_at': None}
            async def async_save_tariff(self, p, *, expected_revision, before_save=None):
                if self.fail:
                    raise self.fail
                if before_save is not None and await before_save() is not True:
                    raise PermissionError('unauthorized')
                self.current_tariff_revision = expected_revision + 1
                return {'profile': p, 'revision': expected_revision + 1, 'updated_at': 'saved'}
        storage = Storage()
        user = types.SimpleNamespace(id='synthetic', is_active=True, is_admin=True)
        async def get_user(uid):
            return user
        hass = types.SimpleNamespace(data={'ha_tools_email': {'storage': storage}},
                                     auth=types.SimpleNamespace(async_get_user=get_user))
        results, errors = [], []
        connection = types.SimpleNamespace(user=user, send_result=lambda *a: results.append(a), send_error=lambda *a: errors.append(a))
        await ws._ws_get_tariff(hass, connection, {'id': 1})
        self.assertEqual({'profile': None, 'revision': 0, 'updated_at': None}, results[-1][1])
        msg = {'id': 2, 'profile': profile(), 'expected_revision': 0}
        await ws._ws_set_tariff(hass, connection, msg)
        self.assertEqual(1, results[-1][1]['revision'])
        for failure, code in ((mod.TariffRevisionConflict('private'), 'tariff_revision_conflict'),
                              (mod.TariffValidationError([]), 'invalid_tariff_profile'),
                              (OSError('private SMTP path'), 'tariff_save_failed')):
            storage.fail = failure
            await ws._ws_set_tariff(hass, connection, msg)
            self.assertEqual(code, errors[-1][1])
            self.assertNotIn('private', errors[-1][2])


class AnalysisUpdateSubscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_signals_sanitize_and_cleanup_once_after_replacement(self):
        ws, _ = load_websocket()
        listeners, removed, delivered, results = {}, [], [], []
        user = types.SimpleNamespace(id='household', is_active=True)
        async def get_user(user_id):
            self.assertEqual(user_id, user.id)
            return user
        def listen(event, callback):
            listeners[event] = callback
            def remove():
                self.assertNotIn(event, removed)
                removed.append(event)
                listeners.pop(event)
            return remove
        service = types.SimpleNamespace(_stopped=False, _update_subscriptions=set())
        bucket = {'energy_analysis': service}
        hass = types.SimpleNamespace(data={ws.DOMAIN: bucket},
            auth=types.SimpleNamespace(async_get_user=get_user),
            bus=types.SimpleNamespace(async_listen=listen))
        foreign = lambda: None
        connection = types.SimpleNamespace(user=user, subscriptions={99: foreign},
            send_event=lambda key, data: delivered.append((key, data)),
            send_result=lambda key: results.append(key),
            send_error=lambda *args: self.fail(str(args)))
        await ws._ws_subscribe_analysis_updates(hass, connection, {'id': 7})
        self.assertEqual(results, [7])
        self.assertEqual(set(listeners), {ws.DOMAIN+'_energy_analysis_updated', ws.DOMAIN+'_energy_updated', ws.DOMAIN+'_feature_access_updated',
                                         ws.DOMAIN+'_report_status_updated', ws.DOMAIN+'_schedules_changed'})
        callbacks = list(listeners.values())
        for changed in callbacks:
            changed(types.SimpleNamespace(data={'private': 'secret'}))
        self.assertEqual(delivered, [(7, {})] * 5)
        bucket['energy_analysis'] = object()
        callbacks[0](None)
        connection.subscriptions[7]()
        callbacks[1](None)
        self.assertEqual(len(removed), 5)
        self.assertFalse(service._update_subscriptions)
        self.assertIs(connection.subscriptions[99], foreign)
        self.assertEqual(delivered, [(7, {})] * 5)
