"""W28/D-T unit regressions; native registry/statistics proof is in tests_ha."""
import importlib.util
from types import SimpleNamespace
import unittest

from tests.test_energy_snapshot import ROOT, PACKAGE, at, entity_modules


def association_module():
    spec = importlib.util.spec_from_file_location(
        PACKAGE + '.energy_source_device', ROOT / 'energy_source_device.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DailyStatisticsTests(unittest.TestCase):
    def test_published_boundary_and_negative_zero_unknown(self):
        sensor, _ = entity_modules()
        coordinator = SimpleNamespace(data=None)
        entity = sensor.EnergySensor(coordinator, SimpleNamespace(entry_id='stable'),
                                     'today_cost', 'today', 'usage_cost')
        self.assertEqual('total', entity.state_class)
        self.assertIsNone(entity.last_reset)
        self.assertFalse(entity.available)
        # 23h / 25h Warsaw days. Properties must keep the published UTC boundary,
        # regardless of the time at which HA asks for the current attributes.
        for start in ('2026-03-28T23:00:00+00:00', '2026-03-29T22:00:00+00:00',
                      '2026-10-24T22:00:00+00:00', '2026-10-25T23:00:00+00:00'):
            for value in (0, -3.25, 8, None):
                coordinator.data = {'today': {'from': start, 'usage_cost': value},
                                    'currency': 'EUR'}
                self.assertEqual(at(start), entity.last_reset)
                self.assertEqual(value, entity.native_value)
                self.assertEqual(value is not None, entity.available)
                self.assertEqual('monetary', entity.device_class)
                self.assertEqual('EUR', entity.native_unit_of_measurement)
        for start in (None, '', 'invalid', '2026-10-18T00:00:00'):
            coordinator.data['today']['from'] = start
            self.assertIsNone(entity.last_reset)
        price = sensor.EnergySensor(coordinator, SimpleNamespace(entry_id='stable'),
                                    'price', 'current', 'usage_rate')
        self.assertEqual('measurement', price.state_class)
        self.assertIsNone(price.last_reset)
        self.assertIsNone(price.device_class)
        self.assertEqual('EUR/kWh', price.native_unit_of_measurement)


class SourceResolutionTests(unittest.TestCase):
    def test_exact_registry_identity_flat_legacy_and_fail_closed(self):
        resolve = association_module().resolve_source_device
        device = SimpleNamespace(id='source')
        records = {'sensor.import': SimpleNamespace(device_id='source', platform='meter'),
                   'sensor.other': SimpleNamespace(device_id='source', platform='meter'),
                   'sensor.detached': SimpleNamespace(device_id=None, platform='meter'),
                   'sensor.own': SimpleNamespace(device_id='source', platform='ha_tools_email')}
        entities = SimpleNamespace(async_get=records.get)
        devices = SimpleNamespace(async_get={'source': device}.get)
        def prefs(*ids, flat=False):
            return {'energy_sources': ([{'type': 'grid', 'stat_energy_from': sid}
                                       for sid in ids] if flat else
                    [{'type': 'grid', 'flow_from': [{'stat_energy_from': sid} for sid in ids]}])}
        for flat in (False, True):
            self.assertIs(device, resolve(prefs('sensor.import', flat=flat), entities, devices))
            for ids in ((), ('external:import',), ('sensor.detached',), ('sensor.own',),
                        ('sensor.import', 'sensor.other')):
                self.assertIsNone(resolve(prefs(*ids, flat=flat), entities, devices))
        self.assertIsNone(resolve(None, entities, devices))
        self.assertIsNone(resolve({'energy_sources': [{}] * 513}, entities, devices))
        self.assertIsNone(resolve(prefs('sensor.import'), entities,
                                  SimpleNamespace(async_get=lambda _: None)))
