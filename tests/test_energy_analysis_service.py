"""Current-period service integration with actual provider/model and CPU lane.

No HA runtime or Recorder: only manager, auth, registry and Recorder IO are fake.
Owner runs these after applying both new-file patches.
"""
import asyncio
from contextlib import ExitStack
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_composer import composer
from tests.test_energy_weighted_comparison import readings
from tests.test_tariff_history import book, version

ROOT = Path(__file__).resolve().parents[1] / 'custom_components/ha_tools_email'
package = types.ModuleType('analysis_service_fixture')
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
sys.modules[package.__name__ + '.composer'] = composer
spec = importlib.util.spec_from_file_location(package.__name__ + '.analysis_service', ROOT / 'analysis_service.py')
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
provider = sys.modules[package.__name__ + '.analysis_provider']
energy = sys.modules[package.__name__ + '.energy']
registry_model = sys.modules[package.__name__ + '.energy_registry']
UTC = timezone.utc


class Harness:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 2, 34, tzinfo=UTC)
        self.manager = types.SimpleNamespace(data={
            'energy_sources': [{'type': 'grid', 'stat_energy_from': 'sensor.grid'}],
            'device_consumption': [{'stat_consumption': 'sensor.device', 'name': 'Desk'}]})
        self.permissions = types.SimpleNamespace(access_all_entities=lambda _: True,
                                                 check_entity=lambda sid, _: sid == 'sensor.grid')
        self.user = types.SimpleNamespace(is_active=True, is_admin=False, permissions=self.permissions)
        self.on_auth = None
        async def auth(user_id):
            if self.on_auth:
                self.on_auth()
            return self.user
        self.entry = object()
        self.hass = types.SimpleNamespace(data={}, config=types.SimpleNamespace(time_zone='UTC'),
            auth=types.SimpleNamespace(async_get_user=auth),
            config_entries=types.SimpleNamespace(async_get_entry=lambda _: self.entry))
        self.book = book([version(monthly=31)])
        self.policy = {'schema': 1, 'features': {}}
        self.storage = types.SimpleNamespace(current_tariff_revision=4, current_feature_access_revision=0)
        async def get_policy():
            return {'policy': copy.deepcopy(self.policy),
                    'revision': self.storage.current_feature_access_revision,
                    'status': 'ready' if self.policy.get('schema') == 1 else 'invalid_configuration'}
        self.storage.async_get_feature_access = get_policy
        async def get_book():
            return {'book': copy.deepcopy(self.book), 'revision': self.storage.current_tariff_revision}
        self.storage.async_get_tariff_book = get_book
        self.registry_ids = []
        def capture(ids):
            self.registry_ids.append(list(ids))
            return registry_model.resolve_membership(ids, {
                'entities': {'sensor.device': {'area_id': 'office'}},
                'areas': {'office': {'name': 'Office', 'floor_id': 'upstairs'}},
                'floors': {'upstairs': {'name': 'Upstairs'}}},
                revision=self.registry.revision, captured_at=self.now.isoformat())
        self.registry = types.SimpleNamespace(revision=2, capture=capture)
        self.coordinator = energy.EnergySnapshotCoordinator(self.hass, self.storage)
        self.subject = service.EnergyAnalysisService(self.hass, 'synthetic', self.coordinator,
            self.registry, configuration_manager=self.manager, clock=lambda: self.now)
        self.hass.data[service.DOMAIN].update(energy_analysis=self.subject,
                                             energy_snapshot=self.coordinator, storage=self.storage)
        self.metadata = {sid: {'has_sum': True, 'unit_of_measurement': 'kWh', 'unit_class': 'energy'}
                         for sid in ('sensor.grid', 'sensor.device', 'utility:external')}
        self.queries, self.on_read, self.on_cpu = [], None, None
        original_cpu = self.coordinator._async_run_cpu
        async def cpu(job):
            value = await original_cpu(job)
            if self.on_cpu:
                self.on_cpu(job)
            return value
        self.coordinator._async_run_cpu = cpu
        async def usage(hass, sensors, start, end, cadence, *, metadata):
            ids = tuple(row['entity_id'] for row in sensors)
            self.queries.append((ids, start, end))
            if self.on_read:
                await self.on_read(ids, start, end)
            return readings(start.isoformat(), end.isoformat(),
                            sources=ids, value=10 if ids[0] != 'sensor.device' else 2)
        self.usage = usage

    def patches(self):
        stack = ExitStack()
        core = types.ModuleType('homeassistant.core')
        core.valid_entity_id = lambda sid: ':' not in sid and len(sid.split('.')) == 2
        const = types.ModuleType('homeassistant.auth.permissions.const')
        const.POLICY_READ = 'read'
        stack.enter_context(patch.dict(sys.modules, {core.__name__: core, const.__name__: const}))
        async def metadata(hass, ids):
            return copy.deepcopy({sid: self.metadata.get(sid) for sid in ids})
        stack.enter_context(patch.object(service, 'async_get_energy_metadata', metadata))
        stack.enter_context(patch.object(provider, 'async_get_energy_metadata', metadata))
        stack.enter_context(patch.object(provider, 'async_get_energy_usage', self.usage))
        return stack


class EnergyAnalysisServiceTest(unittest.TestCase):
    def test_unavailable_configuration_and_invalid_book_never_query_history(self):
        async def run():
            for mode in ('no_grid', 'invalid_kind', 'invalid_book'):
                h = Harness()
                if mode == 'no_grid':
                    h.manager.data['energy_sources'] = []
                if mode == 'invalid_book':
                    h.book = {'schema': 0}
                with self.subTest(mode=mode), h.patches():
                    with self.assertRaises(ValueError):
                        await h.subject.async_get_analysis('historical' if mode == 'invalid_kind' else 'day', 'user')
                    self.assertEqual([], h.queries)
                    self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_exact_current_groups_costs_hash_and_detached_cache(self):
        async def run():
            h = Harness()
            with h.patches():
                result = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual('2026-01-01T00:00:00+00:00', result['period']['start'])
                self.assertEqual('2026-01-01T02:00:00+00:00', result['period']['end'])
                self.assertEqual(Decimal(20), Decimal(result['totals']['kwh']))
                self.assertEqual('20.08', result['totals']['total_cost'])
                groups = result['groups']
                self.assertEqual(Decimal(4), Decimal(groups['devices']['sensor.device']['kwh']))
                self.assertEqual('4.00', groups['areas']['office']['usage_cost'])
                self.assertEqual('4.00', groups['floors']['upstairs']['usage_cost'])
                self.assertEqual('16.00', groups['unmeasured']['usage_cost'])
                self.assertNotIn('fixed_cost', groups['devices']['sensor.device'])
                self.assertEqual('ha_energy_grid_only_configuration', groups['domain_provenance'])
                unhashed = copy.deepcopy(result); unhashed.pop('snapshot_id')
                self.assertEqual(service._digest(unhashed), result['snapshot_id'])
                self.assertEqual([['sensor.device']], h.registry_ids)
                self.assertTrue(await h.subject.async_validate_result(result, 'user'))
                before = copy.deepcopy(result)
                result['groups']['devices']['sensor.device']['kwh'] = 'tampered'
                result['buckets'].clear()
                cached = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual(before, cached)
                self.assertEqual(2, len(h.queries))
                h.manager.data['device_consumption'][0]['name'] = 'Renamed'
                fresh = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual('Renamed', fresh['groups']['devices']['sensor.device']['name'])
                self.assertNotEqual(before['snapshot_id'], fresh['snapshot_id'])
        asyncio.run(run())

    def test_feature_denied_or_invalid_policy_prevents_all_private_io(self):
        async def run():
            for policy in (
                {'schema': 1, 'features': {'energy_analysis': {'read': {'mode': 'admin'}}}},
                {'schema': 0},
            ):
                h = Harness(); h.policy = policy
                with self.subTest(policy=policy), h.patches(), patch.object(
                        service, 'async_get_energy_metadata', AsyncMock()) as metadata:
                    with self.assertRaises(PermissionError):
                        await h.subject.async_get_analysis('day', 'user')
                    metadata.assert_not_awaited()
                    self.assertEqual([], h.queries)
                    self.assertEqual([], h.registry_ids)
        asyncio.run(run())

    def test_selected_user_cache_does_not_grant_other_user_or_native_denied_source(self):
        async def run():
            h = Harness()
            h.policy = {'schema': 1, 'features': {'energy_analysis': {
                'read': {'mode': 'selected_users', 'user_ids': ['allowed']}}}}
            with h.patches():
                result = await h.subject.async_get_analysis('day', 'allowed')
                self.assertEqual(0, result['context']['feature_access_revision'])
                count = len(h.queries)
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_analysis('day', 'denied')
                self.assertEqual(count, len(h.queries))
                h.permissions.access_all_entities = lambda _: False
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_analysis('day', 'allowed')
                self.assertEqual(count, len(h.queries))
        asyncio.run(run())

    def test_policy_revocation_during_grid_io_blocks_consumer_and_final_output(self):
        async def run():
            h = Harness()
            async def revoke(*args):
                h.policy['features']['energy_analysis'] = {'read': {'mode': 'admin'}}
            h.on_read = revoke
            with h.patches():
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_analysis('day', 'user')
                self.assertEqual([('sensor.grid',)], [ids for ids, _, _ in h.queries])
                self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_confirmed_policy_revision_invalidates_cached_and_inflight_dto(self):
        async def run():
            h = Harness()
            with h.patches():
                result = await h.subject.async_get_analysis('day', 'user')
                h.storage.current_feature_access_revision = 1
                self.assertFalse(h.subject.context_is_current(result))
                self.assertFalse(await h.subject.async_validate_result(result, 'user'))
                fresh = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual(1, fresh['context']['feature_access_revision'])
                self.assertNotEqual(result['snapshot_id'], fresh['snapshot_id'])
        asyncio.run(run())

    def test_all_current_kinds_and_year_53_absolute_bounded_chunks(self):
        async def run():
            h = Harness(); h.book = None
            h.now = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
            with h.patches():
                for kind, start in [('day', '2026-12-31'), ('week', '2026-12-28'),
                                    ('month', '2026-12-01'), ('year', '2026-01-01')]:
                    h.queries.clear()
                    result = await h.subject.async_get_analysis(kind, 'user')
                    self.assertTrue(result['period']['start'].startswith(start))
                    hours = int(result['period']['hours'])
                    self.assertEqual(Decimal(hours * 10), Decimal(result['totals']['kwh']))
                    self.assertIsNone(result['totals']['total_cost'])
                    self.assertEqual(Decimal(hours * 2), Decimal(result['groups']['areas']['office']['kwh']))
                    self.assertNotIn('source_buckets', result)
                    if kind == 'year':
                        windows = [(a, b) for ids, a, b in h.queries if ids == ('sensor.grid',)]
                        self.assertEqual(53, len(windows))
                        self.assertEqual(8759, hours)
                        self.assertEqual(8759, len(result['buckets']))
                        self.assertTrue(all(0 < (b-a).total_seconds() <= 7*86400 for a, b in windows))
                        self.assertTrue(all(a[1] == b[0] for a, b in zip(windows, windows[1:])))
                        self.assertEqual(53 * 2, len(h.queries))
        asyncio.run(run())

    def test_revision_source_calendar_lifecycle_mutations_during_io_and_cpu_fail_closed(self):
        async def run():
            changes = {
                'tariff': lambda h: setattr(h.storage, 'current_tariff_revision', 5),
                'registry': lambda h: setattr(h.registry, 'revision', 3),
                'feature_policy': lambda h: setattr(h.storage, 'current_feature_access_revision', 1),
                'source': lambda h: h.manager.data['device_consumption'][0].update(name='Other'),
                'timezone': lambda h: setattr(h.hass.config, 'time_zone', 'Europe/Warsaw'),
                'calendar': lambda h: setattr(h, 'now', h.now + timedelta(hours=1)),
                'stop': lambda h: h.subject.async_stop(),
                'replacement': lambda h: h.hass.data[service.DOMAIN].update(energy_analysis=object()),
                'storage': lambda h: h.hass.data[service.DOMAIN].update(storage=object()),
                'entry': lambda h: setattr(h, 'entry', object()),
                'coordinator_storage': lambda h: setattr(h.coordinator, 'storage', object()),
                'unit': lambda h: h.metadata['sensor.grid'].update(unit_of_measurement='Wh')}
            for stage in ('io', 'add_chunk', '_finish'):
                for name, change in changes.items():
                    h = Harness()
                    async def read(*args):
                        change(h)
                    def cpu(job):
                        if getattr(job.func, '__name__', '') == stage:
                            change(h)
                    h.on_read = read if stage == 'io' else None
                    h.on_cpu = cpu if stage != 'io' else None
                    with self.subTest(stage=stage, mutation=name), h.patches():
                        with self.assertRaises((RuntimeError, ValueError)):
                            await h.subject.async_get_analysis('day', 'user')
                        self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_limited_entity_denied_before_private_io_and_role_revoked_after_await(self):
        async def run():
            h = Harness(); h.permissions.access_all_entities = lambda _: False
            with h.patches(), patch.object(service, 'async_get_energy_metadata', AsyncMock()) as meta:
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_analysis('day', 'limited')
                meta.assert_not_awaited()
                self.assertEqual([], h.queries)
                self.assertEqual([], h.registry_ids)
            h = Harness()
            async def revoke(*args):
                h.user.is_active = False
            h.on_read = revoke
            with h.patches():
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_analysis('day', 'user')
                self.assertEqual([('sensor.grid',)], [ids for ids, _, _ in h.queries])
                self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_external_unit_and_permission_cache_hits_are_revalidated(self):
        async def run():
            h = Harness(); h.book = None
            h.manager.data['energy_sources'][0]['stat_energy_from'] = 'utility:external'
            with h.patches():
                result = await h.subject.async_get_analysis('day', 'user')
                h.metadata['utility:external']['unit_of_measurement'] = 'Wh'
                self.assertFalse(await h.subject.async_validate_result(result, 'user'))
                self.assertIsNone(h.subject._cache)
                h.permissions.access_all_entities = lambda _: False
                with self.assertRaises(PermissionError):
                    await h.subject.async_get_analysis('day', 'user')
        asyncio.run(run())

    def test_ws_validation_and_final_auth_detect_context_change(self):
        async def run():
            h = Harness()
            with h.patches():
                result = await h.subject.async_get_analysis('day', 'user')
                h.on_auth = lambda: setattr(h.registry, 'revision', 3)
                self.assertFalse(await h.subject.async_validate_result(result, 'user'))
                self.assertFalse(h.subject.context_is_current(result))
            h = Harness()
            def on_cpu(job):
                if getattr(job.func, '__name__', '') == '_finish':
                    h.on_auth = lambda: setattr(h.storage, 'current_tariff_revision', 5)
            h.on_cpu = on_cpu
            with h.patches():
                with self.assertRaises(RuntimeError):
                    await h.subject.async_get_analysis('day', 'user')
                self.assertIsNone(h.subject._cache)
        asyncio.run(run())

    def test_ttl_refreshes_delayed_recorder_data_and_cache_remains_one_result(self):
        async def run():
            h = Harness()
            with h.patches(), patch.object(service, 'monotonic', return_value=0):
                first = await h.subject.async_get_analysis('day', 'user')
            with h.patches(), patch.object(service, 'monotonic', return_value=301):
                again = await h.subject.async_get_analysis('day', 'user')
                self.assertEqual(first, again)
                self.assertEqual(4, len(h.queries))
                self.assertEqual(again, h.subject._cache[2])
        asyncio.run(run())

    def test_bounded_admission_includes_waiters_and_cancel_releases_admission(self):
        async def run():
            h = Harness(); entered, release = asyncio.Event(), asyncio.Event()
            async def hold(*args):
                entered.set(); await release.wait()
            h.on_read = hold
            with h.patches():
                first = asyncio.create_task(h.subject.async_get_analysis('day', 'user'))
                await entered.wait()
                waiting = [asyncio.create_task(h.subject.async_get_analysis('day', 'user')) for _ in range(3)]
                await asyncio.sleep(0)
                with self.assertRaisesRegex(RuntimeError, 'energy_analysis_busy'):
                    await h.subject.async_get_analysis('day', 'user')
                for task in waiting:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                release.set()
                result = await first
                self.assertEqual(Decimal(20), Decimal(result['totals']['kwh']))
                self.assertEqual(0, h.subject._pending)
        asyncio.run(run())

    def test_cancelled_cpu_holds_actual_shared_lane_until_job_finishes(self):
        async def run():
            h = Harness(); entered, release = asyncio.Event(), asyncio.Event()
            async def executor(job):
                entered.set(); await release.wait(); return job()
            h.hass.async_add_executor_job = executor
            with h.patches():
                task = asyncio.create_task(h.subject.async_get_analysis('day', 'user'))
                await entered.wait()
                task.cancel(); await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertTrue(h.coordinator._cpu_lock.locked())
                self.assertEqual(1, h.subject._pending)
                other = energy.EnergySnapshotCoordinator(h.hass, h.storage)
                h.hass.data['ha_tools_email']['energy_snapshot'] = other
                ran = []
                queued = asyncio.create_task(other._async_run_cpu(lambda: ran.append(True)))
                await asyncio.sleep(0)
                self.assertEqual([], ran)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await queued
                self.assertEqual([True], ran)
                self.assertFalse(h.coordinator._cpu_lock.locked())
                self.assertIsNone(h.subject._cache)
                self.assertEqual(0, h.subject._pending)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
