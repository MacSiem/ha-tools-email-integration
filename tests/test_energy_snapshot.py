"""W20 public snapshot regressions, pure modules and controlled IO only."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from tests.test_tariff_profile import profile

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
PACKAGE = 'energy_snapshot_test_package'
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = pkg
spec = importlib.util.spec_from_file_location(PACKAGE + '.energy', ROOT / 'energy.py')
energy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(energy)


def at(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def classified():
    p = profile()
    p['zones'][0]['classification'] = 'expensive'
    p['zones'][1]['classification'] = 'cheap'
    p['rules'] = [{'zone': 'night', 'intervals': [{'from': '22:00', 'to': '06:00'}]}]
    return p


def usage(start='2026-10-18T00:00Z', hours=12):
    stamp = at(start).timestamp() * 1000
    return {'status': 'ready', 'hourly_buckets': [
        {'from': stamp + i * 3600000, 'to': stamp + (i + 1) * 3600000, 'kwh': 1}
        for i in range(hours)], 'coverage': {'complete': True, 'sources': 1,
        'measured_source_hours': hours, 'expected_source_hours': hours,
        'complete_sources': 1, 'unavailable_sources': 0}}


class SnapshotTests(unittest.TestCase):
    def snapshot(self, p=None, now='2026-10-18T12:34:56Z', tz='UTC', measured=None):
        return energy.build_snapshot({'profile': p, 'revision': 7},
            usage() if measured is None else measured, now=at(now), measurement_timezone=tz)

    def test_primary_price_and_cost_are_usage_and_actual_currency(self):
        p = classified()
        p['zones'][0]['variableRate'] = .2
        result = self.snapshot(p)
        self.assertEqual('EUR', result['currency'])
        self.assertEqual(1.2, result['current']['usage_rate'])
        self.assertEqual('expensive', result['current']['zone_class'])
        self.assertAlmostEqual(6 * .4 + 6 * 1.2, result['today']['usage_cost'])
        self.assertEqual(7, result['today']['fixed_monthly'])
        self.assertEqual(7, result['tariff_revision'])
        self.assertEqual('2026-10-18T22:00:00+00:00', result['next_cheap']['start'])

    def test_no_name_or_minimum_inference_unknown_fee_is_not_zero(self):
        p = profile()
        p['zones'][0]['variableRate'] = None
        result = self.snapshot(p)
        self.assertIsNone(result['current']['zone_class'])
        self.assertIsNone(result['current']['cheap'])
        self.assertIsNone(result['current']['usage_rate'])
        self.assertIsNone(result['today']['usage_cost'])
        self.assertEqual('unknown', result['next_cheap']['status'])

    def test_current_cheap_returns_next_segment_and_mixed_is_false(self):
        result = self.snapshot(classified(), now='2026-10-18T02:00Z')
        self.assertTrue(result['current']['cheap'])
        self.assertEqual('2026-10-18T22:00:00+00:00', result['next_cheap']['start'])
        p = classified()
        p['zones'][0]['classification'] = 'mixed'
        self.assertFalse(self.snapshot(p)['current']['cheap'])

    def test_missing_and_invalid_profiles_preserve_unknown(self):
        for p in (None, {'schema': 0}):
            result = self.snapshot(p)
            self.assertIsNone(result['current']['usage_rate'])
            self.assertIsNone(result['today']['usage_cost'])
            self.assertEqual('unknown', result['next_cheap']['status'])

    def test_partial_measurement_and_fractional_local_midnight_withhold_cost(self):
        measured = usage()
        measured['status'] = 'partial'
        self.assertIsNone(self.snapshot(classified(), measured=measured)['today']['usage_cost'])
        result = self.snapshot(classified(), tz='Asia/Kathmandu')
        self.assertFalse(result['today']['boundary_complete'])
        self.assertIsNone(result['today']['usage_cost'])

    def test_warsaw_dst_expected_absolute_hours(self):
        for day, start, hours in [('2026-03-29T22:00Z', '2026-03-28T23:00Z', 23),
                                  ('2026-10-25T23:00Z', '2026-10-24T22:00Z', 25)]:
            # Inspect the window one second before next HA-local midnight.
            now = datetime.fromtimestamp(at(day).timestamp() - 1, timezone.utc)
            a, b, complete = energy.measurement_window(now, 'Europe/Warsaw')
            self.assertTrue(complete)
            self.assertEqual(at(start), a)
            self.assertEqual(hours - 1, int((b - a).total_seconds() / 3600))

    def test_dynamic_seconds_boundary_and_gap_before_candidate(self):
        p = classified()
        p['dynamic'] = {'intervals': [
            {'from': '2026-10-18T12:00Z', 'to': '2026-10-18T12:35:01Z',
             'energyRate': 1, 'variableRate': 0, 'classification': 'expensive'},
            {'from': '2026-10-18T12:35:01Z', 'to': '2026-10-19T00:00Z',
             'energyRate': .3, 'variableRate': 0, 'classification': 'cheap'}]}
        self.assertEqual('2026-10-18T12:35:01+00:00', self.snapshot(p)['next_cheap']['start'])
        p['dynamic']['intervals'][1]['from'] = '2026-10-18T12:36Z'
        result = self.snapshot(p)
        self.assertEqual('unknown', result['next_cheap']['status'])
        self.assertIsNone(result['next_cheap']['start'])

    def test_no_future_cheap_is_bounded_and_bad_class_rejected(self):
        p = classified()
        p['rules'] = []
        result = self.snapshot(p)
        self.assertEqual('not_found_in_horizon', result['next_cheap']['status'])
        p['zones'][0]['classification'] = 'lowest'
        self.assertEqual('invalid_profile', self.snapshot(p)['current']['reason'])

    def test_no_private_fields_in_snapshot(self):
        measured = usage()
        measured.update(source_ids=['sensor.PRIVATE'], devices=[{'name': 'PRIVATE'}],
                        measurement_provenance={'PRIVATE': 'PRIVATE'})
        p = classified()
        p['private'] = 'PRIVATE'
        self.assertNotIn('PRIVATE', str(self.snapshot(p, measured=measured)))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_reads_share_measurement_override_never_changes_entities(self):
        calls = []
        state = {'profile': classified(), 'revision': 1}
        class Storage:
            async def async_get_tariff(self):
                return copy.deepcopy(state)
        async def provider(hass, start, end):
            calls.append((start, end))
            await asyncio.sleep(0)
            return usage()
        hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone='UTC'))
        c = energy.EnergySnapshotCoordinator(hass, Storage(), provider=provider)
        now = at('2026-10-18T12:34:56Z')
        with patch.object(energy, 'async_grid_sensors', return_value=[{'entity_id': 'sensor.grid', 'name': 'PRIVATE'}]):
            a, b = await asyncio.gather(c.async_refresh(now=now), c.async_refresh(now=now))
            self.assertEqual(a, b)
            self.assertEqual(1, len(calls))
            alternate = classified()
            alternate['currency'] = 'PLN'
            custom = await c.async_snapshot(profile=alternate, now=now)
            self.assertEqual('PLN', custom['currency'])
            self.assertEqual('EUR', c.data['currency'])
            self.assertEqual(1, len(calls))
            state['revision'] = 2
            state['profile'] = None
            await c.async_refresh(now=now)
            self.assertIsNone(c.data['current']['usage_rate'])
            self.assertEqual(1, len(calls))
            c.async_stop()
            with self.assertRaises(RuntimeError):
                await c.async_refresh(now=now)

    async def test_unload_discards_late_result(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class Storage:
            async def async_get_tariff(self):
                return {'profile': classified(), 'revision': 1}
        async def provider(hass, start, end):
            entered.set()
            await release.wait()
            return usage()
        c = energy.EnergySnapshotCoordinator(types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone='UTC')), Storage(), provider=provider)
        with patch.object(energy, 'async_grid_sensors', return_value=[]):
            task = asyncio.create_task(c.async_refresh(now=at('2026-10-18T12:34Z')))
            await entered.wait()
            c.async_stop()
            release.set()
            with self.assertRaises(RuntimeError):
                await task
            self.assertIsNone(c.data)

    async def test_source_change_during_io_discards_answer(self):
        reads = iter([[{'entity_id': 'sensor.a'}], [{'entity_id': 'sensor.b'}]])
        class Storage:
            async def async_get_tariff(self):
                return {'profile': classified(), 'revision': 1}
        async def provider(hass, start, end):
            return usage()
        c = energy.EnergySnapshotCoordinator(types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone='UTC')), Storage(), provider=provider)
        with patch.object(energy, 'async_grid_sensors', side_effect=lambda _: next(reads)):
            with self.assertRaisesRegex(RuntimeError, 'sources_changed'):
                await c.async_refresh(now=at('2026-10-18T12:34Z'))
        self.assertIsNone(c.data)

    async def test_revision_confirmed_after_measurement_wait_is_used(self):
        state = {'profile': classified(), 'revision': 1}
        class Storage:
            async def async_get_tariff(self):
                return copy.deepcopy(state)
        async def provider(hass, start, end):
            state['revision'] = 2
            state['profile']['currency'] = 'PLN'
            return usage()
        c = energy.EnergySnapshotCoordinator(types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone='UTC')), Storage(), provider=provider)
        with patch.object(energy, 'async_grid_sensors', return_value=[]):
            result = await c.async_refresh(now=at('2026-10-18T12:34Z'))
        self.assertEqual((2, 'PLN'), (result['tariff_revision'], result['currency']))

    async def test_listener_and_second_boundary_callbacks_are_removed_on_stop(self):
        cancelled, points = [], []
        events = types.ModuleType('homeassistant.helpers.event')
        def interval(hass, callback, duration):
            return lambda: cancelled.append('interval')
        def point(hass, callback, instant):
            points.append(instant)
            return lambda: cancelled.append('point')
        events.async_track_time_interval = interval
        events.async_track_point_in_utc_time = point
        class Storage:
            async def async_get_tariff(self):
                return {'profile': classified(), 'revision': 1}
        async def provider(hass, start, end):
            return usage()
        c = energy.EnergySnapshotCoordinator(types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone='UTC')), Storage(), provider=provider)
        with patch.dict(sys.modules, {events.__name__: events}), \
             patch.object(energy, 'async_grid_sensors', return_value=[]):
            await c.async_start()
            now = at('2026-10-18T12:34:56Z')
            c._model_boundaries = [int(at('2026-10-18T12:34:57Z').timestamp() * 1000)]
            c._schedule_boundary(now)
            self.assertEqual(at('2026-10-18T12:34:57Z'), points[-1])
            hits = []
            remove = c.async_add_listener(lambda: hits.append(1))
            await c.async_refresh(now=now)
            self.assertEqual([1], hits)
            remove()
            await c.async_refresh(now=now)
            self.assertEqual([1], hits)
            c.async_stop()
        self.assertEqual(1, cancelled.count('interval'))
        self.assertEqual(len(points), cancelled.count('point'))
        self.assertFalse(c._listeners)


class SnapshotAdditionalTests(unittest.TestCase):
    def test_complete_zero_and_negative_cost_no_monthly_proration(self):
        for kwh, rate, expected in [(0, 1, 0), (1, -.5, -6)]:
            p = classified()
            p.update(allowNegativeEnergy=True, rules=[])
            p['zones'][0].update(energyRate=rate, variableRate=0)
            measured = usage()
            for row in measured['hourly_buckets']:
                row['kwh'] = kwh
            result = energy.build_snapshot({'profile': p, 'revision': 1}, measured,
                now=at('2026-10-18T12:34Z'), measurement_timezone='UTC')
            self.assertEqual(expected, result['today']['usage_cost'])
            self.assertEqual(7, result['today']['fixed_monthly'])

    def test_current_price_overflow_is_unknown_and_ratebucket_mixing_rejected(self):
        p = classified()
        p['zones'][0].update(energyRate=1e308, variableRate=1e308)
        snapshot = energy.build_snapshot({'profile': p}, usage(), now=at('2026-10-18T12:34Z'), measurement_timezone='UTC')
        self.assertIsNone(snapshot['current']['usage_rate'])
        p = classified()
        p['rules'] = [{'zone': 'night', 'intervals': [{'from': '00:30', 'to': '01:00'}]}]
        snapshot = energy.build_snapshot({'profile': p}, usage(), now=at('2026-10-18T12:34Z'), measurement_timezone='UTC')
        self.assertIsNone(snapshot['today']['usage_cost'])
        self.assertIn({'reason': 'mixed_rate_bucket'}, snapshot['today']['issues'])


def entity_modules():
    """Exercise actual entity properties with only HA's base classes doubled."""
    class Entity:
        async def async_added_to_hass(self):
            pass
        def async_on_remove(self, callback):
            self.remove = callback
        def async_write_ha_state(self):
            pass
    class SensorEntity(Entity):
        pass
    class BinarySensorEntity(Entity):
        pass
    entity = types.ModuleType('homeassistant.helpers.entity')
    entity.Entity = Entity
    registry = types.ModuleType('homeassistant.helpers.device_registry')
    registry.DeviceInfo = dict
    sensor = types.ModuleType('homeassistant.components.sensor')
    sensor.SensorEntity = SensorEntity
    sensor.SensorDeviceClass = types.SimpleNamespace(ENUM='enum', TIMESTAMP='timestamp', MONETARY='monetary')
    sensor.SensorStateClass = types.SimpleNamespace(MEASUREMENT='measurement')
    binary = types.ModuleType('homeassistant.components.binary_sensor')
    binary.BinarySensorEntity = BinarySensorEntity
    modules = [entity, registry, sensor, binary]
    with patch.dict(sys.modules, {m.__name__: m for m in modules}):
        loaded = []
        for name in ('energy_entity', 'sensor', 'binary_sensor'):
            spec = importlib.util.spec_from_file_location(PACKAGE + '.' + name, ROOT / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
    return loaded[1:]


class EntityContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_entities_share_exact_snapshot_units_ids_and_unload_listeners(self):
        sensor, binary = entity_modules()
        c = energy.EnergySnapshotCoordinator(types.SimpleNamespace(config=types.SimpleNamespace(time_zone='UTC')), None)
        c.data = energy.build_snapshot({'profile': classified(), 'revision': 42}, usage(),
            now=at('2026-10-18T12:34Z'), measurement_timezone='UTC')
        hass = types.SimpleNamespace(data={'ha_tools_email': {'energy_snapshot': c}})
        entry = types.SimpleNamespace(entry_id='stable-entry')
        entities = []
        await sensor.async_setup_entry(hass, entry, entities.extend)
        await binary.async_setup_entry(hass, entry, entities.extend)
        self.assertEqual(5, len(entities))
        self.assertEqual(5, len({e._attr_unique_id for e in entities}))
        self.assertEqual(1, len({str(e._attr_device_info) for e in entities}))
        self.assertEqual(['expensive', 1, at('2026-10-18T22:00Z'), 8.4],
            [entities[0].native_value, entities[1].native_value, entities[2].native_value,
             round(entities[3].native_value, 4)])
        self.assertFalse(entities[4].is_on)
        self.assertEqual('EUR/kWh', entities[1].native_unit_of_measurement)
        self.assertEqual('EUR', entities[3].native_unit_of_measurement)
        for e in entities:
            self.assertTrue(e.available)
            self.assertEqual(42, e.extra_state_attributes['tariff_revision'])
            await e.async_added_to_hass()
        self.assertEqual(5, len(c._listeners))
        for e in entities:
            e.remove()
        self.assertFalse(c._listeners)
        repeat = []
        await sensor.async_setup_entry(hass, entry, repeat.extend)
        await binary.async_setup_entry(hass, entry, repeat.extend)
        self.assertEqual([e._attr_unique_id for e in entities], [e._attr_unique_id for e in repeat])
        c.data = None
        self.assertTrue(all(not e.available for e in entities))


class WebsocketSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_during_io_never_receives_result_and_override_is_forwarded(self):
        from tests.test_tariff_profile import load_websocket
        ws, _ = load_websocket()
        user = types.SimpleNamespace(id='user-id', is_active=True)
        class Auth:
            async def async_get_user(self, uid):
                return user
        calls, results, errors = [], [], []
        class Coordinator:
            async def async_snapshot(self, **kwargs):
                calls.append(kwargs)
                user.is_active = False
                return {'safe': True}
        hass = types.SimpleNamespace(auth=Auth(), data={'ha_tools_email': {'energy_snapshot': Coordinator()}})
        connection = types.SimpleNamespace(user=user, send_result=lambda *args: results.append(args),
                                           send_error=lambda *args: errors.append(args))
        await ws._ws_energy_snapshot(hass, connection, {'id': 1, 'profile': None})
        self.assertEqual([{'profile': None}], calls)
        self.assertFalse(results)
        self.assertEqual('unauthorized', errors[-1][1])
        await ws._ws_energy_snapshot(hass, connection, {'id': 2})
        self.assertEqual(1, len(calls))

    async def test_safe_read_and_bounded_failure(self):
        from tests.test_tariff_profile import load_websocket
        ws, _ = load_websocket()
        user = types.SimpleNamespace(id='user-id', is_active=True)
        class Auth:
            async def async_get_user(self, uid):
                return user
        class Coordinator:
            fail = False
            async def async_snapshot(self, **kwargs):
                if self.fail:
                    raise OSError('SMTP PRIVATE PATH')
                return {'safe': True}
        coordinator = Coordinator()
        results, errors = [], []
        hass = types.SimpleNamespace(auth=Auth(), data={'ha_tools_email': {'energy_snapshot': coordinator}})
        connection = types.SimpleNamespace(user=user, send_result=lambda *args: results.append(args),
                                           send_error=lambda *args: errors.append(args))
        await ws._ws_energy_snapshot(hass, connection, {'id': 1})
        self.assertEqual({'safe': True}, results[-1][1])
        coordinator.fail = True
        await ws._ws_energy_snapshot(hass, connection, {'id': 2})
        self.assertEqual('energy_unavailable', errors[-1][1])
        self.assertNotIn('PRIVATE', str(errors))
