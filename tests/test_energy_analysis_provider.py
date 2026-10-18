"""Actual HA Energy consumer config is distinct from grid import sources."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_energy_weighted_comparison import readings

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('provider_fixture'); package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
# Reuse the already isolated composer's HA-independent import fixture.
from tests.test_composer import composer
sys.modules[package.__name__ + '.composer'] = composer
spec = importlib.util.spec_from_file_location(package.__name__ + '.analysis_provider', ROOT / 'analysis_provider.py')
provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)

START, END = '2026-01-01T00:00Z', '2026-01-01T01:00Z'


def prefs():
    return {'energy_sources': [{'type': 'grid', 'stat_energy_from': 'sensor.grid'}],
            'device_consumption': [{'stat_consumption': 'sensor.parent', 'name': 'House meter'},
                                   {'stat_consumption': 'sensor.child', 'included_in_stat': 'sensor.parent'}]}


class AnalysisProviderTest(unittest.TestCase):
    def test_grid_imports_are_not_consumers_and_both_ha_schema_generations_work(self):
        source = prefs(); source['energy_sources'].append({'type': 'grid', 'flow_from': [
            {'stat_energy_from': 'utility:external'}, {'stat_energy_from': 'sensor.grid'}]})
        source['device_consumption_water'] = [{'stat_consumption': 'sensor.water'}]
        before = copy.deepcopy(source)
        result = provider.capture_energy_configuration(source)
        self.assertEqual(['sensor.grid', 'utility:external'], [row['entity_id'] for row in result['grid_sensors']])
        self.assertEqual(['sensor.parent', 'sensor.child'], [row['stat_consumption'] for row in result['consumers']])
        self.assertEqual('grid_import', result['consumer_domain'])
        self.assertEqual('ha_energy_grid_only_configuration', result['domain_provenance'])
        self.assertEqual(before, source)

    def test_solar_battery_export_and_unknown_sources_do_not_claim_grid_partition(self):
        extras = [{'type': 'solar', 'stat_energy_from': 'sensor.solar'},
                  {'type': 'battery', 'stat_energy_from': 'sensor.discharge'},
                  {'type': 'grid', 'stat_energy_to': 'sensor.export'},
                  {'type': 'grid', 'flow_to': [{'stat_energy_to': 'utility:export'}]},
                  {'type': 'unknown_future_electricity_source'}]
        for extra in extras:
            source = prefs(); source['energy_sources'].append(extra)
            with self.subTest(extra=extra):
                self.assertEqual('site_consumption', provider.capture_energy_configuration(source)['consumer_domain'])
        source = prefs(); source['energy_sources'] += [{'type': 'water'}, {'type': 'gas'}]
        self.assertEqual('grid_import', provider.capture_energy_configuration(source)['consumer_domain'])

    def test_signature_fences_parent_name_and_domain_without_reflecting_opaque_settings(self):
        source = prefs(); before = provider.capture_energy_configuration(source)
        source['opaque_settings'] = {'password': '!secret synthetic'}
        self.assertEqual(before, provider.capture_energy_configuration(source))
        source['device_consumption'][0]['name'] = 'New name'
        renamed = provider.capture_energy_configuration(source)
        self.assertNotEqual(before['source_signature'], renamed['source_signature'])
        source['device_consumption'][1].pop('included_in_stat')
        self.assertNotEqual(renamed['source_signature'], provider.capture_energy_configuration(source)['source_signature'])
        before['consumers'][0]['name'] = 'caller mutation'
        self.assertEqual('New name', provider.capture_energy_configuration(source)['consumers'][0]['name'])

    def test_invalid_hierarchy_and_unbounded_configuration_are_rejected(self):
        for consumers in ([{'stat_consumption': 'sensor.a', 'included_in_stat': 'sensor.a'}],
                          [{'stat_consumption': 'sensor.a', 'included_in_stat': 'sensor.absent'}],
                          [{'stat_consumption': 'sensor.a'}] * 2,
                          [{'stat_consumption': f'sensor.n{n}'} for n in range(513)]):
            source = prefs(); source['device_consumption'] = consumers
            with self.subTest(consumers=len(consumers)), self.assertRaises(ValueError):
                provider.capture_energy_configuration(source)

    def test_authorization_before_recorder_and_after_metadata_can_abort_without_history(self):
        async def run():
            config = provider.capture_energy_configuration(prefs())
            denied = AsyncMock(side_effect=PermissionError('source_access_denied'))
            meta, usage = AsyncMock(return_value={}), AsyncMock()
            with patch.object(provider, 'async_get_energy_metadata', meta), patch.object(provider, 'async_get_energy_usage', usage):
                with self.assertRaises(PermissionError):
                    await provider.async_read_energy_chunk(object(), config, START, END, guard=denied)
                meta.assert_not_awaited(); usage.assert_not_awaited()
                # Revocation during metadata IO is checked before history IO.
                guard = AsyncMock(side_effect=[None, PermissionError('source_access_denied')])
                with self.assertRaises(PermissionError):
                    await provider.async_read_energy_chunk(object(), config, START, END, guard=guard)
                self.assertEqual(1, meta.await_count); usage.assert_not_awaited()
        asyncio.run(run())

    def test_separate_bounded_queries_use_same_metadata_and_recheck_after_each_read(self):
        async def run():
            config = provider.capture_energy_configuration(prefs())
            metadata = {'synthetic_metadata': True}
            grid = readings(START, END, sources=('sensor.grid',))
            devices = readings(START, END, sources=('sensor.parent', 'sensor.child'))
            observed = []
            async def usage(hass, sensors, start, end, cadence, *, metadata):
                observed.append(([row['entity_id'] for row in sensors], metadata))
                return grid if len(observed) == 1 else devices
            guard = AsyncMock()
            with patch.object(provider, 'async_get_energy_metadata', AsyncMock(return_value=metadata)), \
                 patch.object(provider, 'async_get_energy_usage', side_effect=usage):
                result = await provider.async_read_energy_chunk(object(), config, START, END, guard=guard)
                self.assertEqual(grid, result['grid'])
                self.assertEqual(devices, result['consumers'])
                self.assertEqual([(['sensor.grid'], metadata), (['sensor.parent', 'sensor.child'], metadata)], observed)
                # Revocation after either history query prevents publication;
                # after the first query it also prevents the consumer read.
                for boundary in (1, 2):
                    observed.clear()
                    async def revoked():
                        if len(observed) >= boundary:
                            raise PermissionError('source_access_denied')
                    with self.assertRaises(PermissionError):
                        await provider.async_read_energy_chunk(object(), config, START, END, guard=revoked)
                    self.assertEqual(boundary, len(observed))
                observed.clear()
                with self.assertRaisesRegex(ValueError, 'analysis_chunk_too_large'):
                    await provider.async_read_energy_chunk(object(), config, START, '2026-01-09T00:00Z', guard=guard)
                self.assertEqual([], observed)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
