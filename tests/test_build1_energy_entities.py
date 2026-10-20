"""Public Energy facts consume current measurements and historical engine proofs."""
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from datetime import timedelta

from tests.test_energy_snapshot import energy, at, classified, usage, entity_modules
from tests.test_tariff_history import book, version, profile

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
SPEC = importlib.util.spec_from_file_location(energy.__package__ + '.energy_entity_projection', ROOT/'energy_entity_projection.py')
projection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(projection)


def build(state=None, measured=None, now='2026-10-18T12:04Z', zone='UTC'):
    return projection.build_entity_snapshot(state or {'profile': classified(), 'revision': 4},
        measured if measured is not None else usage(), now=at(now), measurement_timezone=zone,
        _legacy_builder=energy.build_snapshot)[0]


class PublicEntityProjectionTests(unittest.TestCase):
    def test_reuses_values_preserves_raw_state_and_adds_only_small_allowlist(self):
        state = {'profile': classified(), 'revision': 4}
        measured = usage()
        before = copy.deepcopy((state, measured))
        base = energy.build_snapshot(state, measured, now=at('2026-10-18T12:04Z'), measurement_timezone='UTC')
        result = build(state, measured)
        self.assertEqual(before, (state, measured))
        self.assertEqual(base, {key: value for key, value in result.items() if key != 'entity_facts'})
        facts = result['entity_facts']
        self.assertEqual(12, facts['import_kwh'])
        self.assertEqual(6, facts['cheap_kwh'])
        self.assertEqual(50, facts['cheap_share_pct'])
        self.assertEqual(['day', 'night'], facts['zone_options'])
        self.assertEqual('2026-10-18T12:00:00+00:00', facts['measurement_through'])
        self.assertEqual('completed_5minute', facts['resolution'])
        self.assertEqual('2026-10-18T22:00:00+00:00', facts['next_change'])
        self.assertEqual('night', facts['next_zone'])
        self.assertNotIn('profile', facts)
        self.assertNotIn('hourly_buckets', facts)

    def test_share_uses_effective_history_of_each_measured_hour_not_current_profile(self):
        old = profile(timezone='Europe/Warsaw')
        old['zones'][0]['classification'] = 'cheap'
        new = copy.deepcopy(old)
        new['zones'][0]['classification'] = 'expensive'
        b = book([version(valid_to='2026-10-18', profile=old),
                  version('2026-10-18', version_id='new', profile=new)])
        result = build({'book': b, 'revision': 7}, usage('2026-10-17T00:00Z', 23), now='2026-10-17T23:04Z')
        facts = result['entity_facts']
        self.assertEqual('expensive', result['current']['zone_class'])
        self.assertEqual(23, facts['import_kwh'])
        self.assertEqual(22, facts['cheap_kwh'])
        self.assertAlmostEqual(100 * 22 / 23, facts['cheap_share_pct'])

    def test_no_tariff_still_preserves_measured_import_but_never_invents_cheapness(self):
        result = build({'profile': None, 'revision': 0})
        self.assertEqual(12, result['entity_facts']['import_kwh'])
        self.assertIsNone(result['entity_facts']['cheap_share_pct'])
        self.assertEqual('classification_unknown', result['entity_facts']['reason'])
        self.assertIsNone(result['current']['usage_rate'])

    def test_missing_classification_and_mixed_interval_stay_unknown(self):
        p = classified()
        p['zones'][0].pop('classification')
        self.assertIsNone(build({'profile': p, 'revision': 1})['entity_facts']['cheap_share_pct'])
        p = classified()
        p['rules'] = [{'zone': 'night', 'intervals': [{'from': '00:30', 'to': '01:00'}]}]
        result = build({'profile': p, 'revision': 1}, usage(hours=1), now='2026-10-18T01:04Z')
        self.assertEqual(1, result['entity_facts']['import_kwh'])
        self.assertIsNone(result['entity_facts']['cheap_share_pct'])

    def test_missing_duplicate_non_kwh_and_partial_sources_never_become_zero(self):
        for failure in ('missing', 'duplicate', 'negative', 'unit', 'partial'):
            measured = usage()
            if failure == 'missing': measured['hourly_buckets'].pop()
            elif failure == 'duplicate': measured['hourly_buckets'][1] = copy.deepcopy(measured['hourly_buckets'][0])
            elif failure == 'negative': measured['hourly_buckets'][0]['kwh'] = -1
            elif failure == 'unit': measured['hourly_buckets'][0]['unit'] = 'Wh'
            else: measured['status'] = 'partial'
            with self.subTest(failure=failure):
                facts = build(measured=measured)['entity_facts']
                self.assertIsNone(facts['import_kwh'])
                self.assertIsNone(facts['cheap_share_pct'])
                self.assertFalse(facts['coverage_complete'])

    def test_zero_import_is_real_zero_but_percentage_has_no_denominator(self):
        measured = usage()
        for hour in measured['hourly_buckets']: hour['kwh'] = 0
        facts = build(measured=measured)['entity_facts']
        self.assertEqual(0, facts['import_kwh'])
        self.assertEqual(0, facts['cheap_kwh'])
        self.assertIsNone(facts['cheap_share_pct'])
        self.assertEqual('zero_import', facts['reason'])

    def test_fall_day_measurement_uses_utc_intervals_and_local_midnight(self):
        measured = usage('2026-10-24T22:00Z', 24)
        facts = build(measured=measured, now='2026-10-25T23:04+01:00', zone='Europe/Warsaw')['entity_facts']
        self.assertEqual(24, facts['import_kwh'])
        self.assertEqual('2026-10-24T22:00:00+00:00', facts['measurement_from'])
        self.assertEqual('2026-10-25T22:00:00+00:00', facts['measurement_through'])


class NativeSensorTests(unittest.IsolatedAsyncioTestCase):
    async def sensors(self, *, fresh=True):
        sensor, _ = entity_modules()
        coordinator = types.SimpleNamespace(data=build(), tariff_context_is_current=lambda result: fresh)
        hass = types.SimpleNamespace(data={'ha_tools_email': {'energy_snapshot': coordinator}})
        entities = []
        await sensor.async_setup_entry(hass, types.SimpleNamespace(entry_id='stable'), entities.extend)
        return {item.key: item for item in entities}, coordinator

    async def test_stable_existing_ids_zone_id_enum_and_two_new_sensors_on_same_device(self):
        entities, _ = await self.sensors()
        self.assertEqual(6, len(entities))
        for key in ('zone', 'price', 'next_cheap', 'today_cost', 'today', 'cheap_share_today'):
            self.assertEqual('stable_energy_' + key, entities[key]._attr_unique_id)
        self.assertEqual('day', entities['zone'].native_value)
        self.assertEqual(['day', 'night'], entities['zone'].options)
        self.assertEqual('expensive', entities['zone'].extra_state_attributes['classification'])
        self.assertEqual('night', entities['zone'].extra_state_attributes['next_zone'])
        self.assertEqual('EUR/kWh', entities['price'].native_unit_of_measurement)
        self.assertEqual(12, entities['today'].native_value)
        self.assertEqual('kWh', entities['today'].native_unit_of_measurement)
        self.assertEqual('energy', entities['today'].device_class)
        self.assertEqual('total', entities['today'].state_class)
        self.assertEqual(at('2026-10-18T00:00Z'), entities['today'].last_reset)
        self.assertEqual(50, entities['cheap_share_today'].native_value)
        self.assertEqual('%', entities['cheap_share_today'].native_unit_of_measurement)
        self.assertEqual('measurement', entities['cheap_share_today'].state_class)
        self.assertEqual(1, len({str(item._attr_device_info) for item in entities.values()}))
        self.assertIn('zone_options', entities['zone']._unrecorded_attributes)
        self.assertIn('measurement_through', entities['today']._unrecorded_attributes)

    async def test_transient_unavailable_snapshot_keeps_enum_metadata_without_stale_value(self):
        entities, coordinator = await self.sensors()
        zone = entities['zone']
        self.assertEqual(['day', 'night'], zone.options)
        published = coordinator.data
        coordinator.data = None
        self.assertFalse(zone.available)
        self.assertIsNone(zone.native_value)
        self.assertEqual(['day', 'night'], zone.options)
        coordinator.data = published
        self.assertEqual(['day', 'night'], zone.options)
        # An accepted removal really clears the allowed values.
        coordinator.data = {'entity_facts': {'zone_options': []}, 'current': {'zone_id': None}}
        self.assertEqual([], zone.options)
        coordinator.data = None
        self.assertEqual([], zone.options)

    async def test_stale_or_historical_snapshot_does_not_supply_today(self):
        entities, coordinator = await self.sensors(fresh=False)
        self.assertIsNone(entities['today'].native_value)
        self.assertIsNone(entities['cheap_share_today'].native_value)
        self.assertFalse(entities['today'].available)
        # Reset is still the published calendar, never a newly fabricated date.
        self.assertEqual(at('2026-10-18T00:00Z'), entities['today'].last_reset)
        coordinator.data = None
        self.assertTrue(all(not item.available for item in entities.values()))
        self.assertEqual({}, entities['today'].extra_state_attributes)

    async def test_unwired_legacy_snapshot_stays_unavailable_instead_of_raising(self):
        entities, coordinator = await self.sensors()
        coordinator.data.pop('entity_facts')
        self.assertIsNone(entities['today'].native_value)
        self.assertIsNone(entities['cheap_share_today'].native_value)
        self.assertIsNone(entities['today'].extra_state_attributes['measurement_through'])
        self.assertEqual(['day'], entities['zone'].options)
