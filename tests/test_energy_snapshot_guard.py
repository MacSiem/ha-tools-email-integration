"""Per-request snapshot source access, with real coordinator and staged IO."""
import asyncio
import copy
import types
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tests.test_energy_snapshot import energy, at, classified, usage

NOW = at('2026-10-18T12:34Z')


class Denied(PermissionError):
    """The request lost source access."""


class SnapshotGuardTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, provider=False, blocked=None):
        owner = self
        owner.events = []
        owner.sources = ['sensor.grid', 'external:grid_import']
        owner.allowed = set(owner.sources)
        owner.entered, owner.release = asyncio.Event(), asyncio.Event()
        owner.blocked = blocked

        async def stage(name, ids=None):
            owner.events.append((name, None if ids is None else tuple(ids)))
            if name == owner.blocked:
                owner.entered.set()
                await owner.release.wait()

        async def sensors(hass):
            await stage('prefs')
            return [{'entity_id': sid} for sid in owner.sources]

        async def metadata(hass, ids):
            await stage('metadata', ids)
            return {sid: (1, {'has_sum': True, 'unit_class': 'energy',
                             'unit_of_measurement': 'kWh'}) for sid in ids}

        async def history(hass, sensors, start, end, period, *, metadata):
            await stage('history', [row['entity_id'] for row in sensors])
            return usage()

        async def supplied(hass, start, end):
            await stage('provider')
            return usage()

        async def executor(job):
            result = job()
            await stage('override_cpu' if result['profile_source'] == 'override' else 'cpu')
            return result

        class Storage:
            current_tariff_revision = 1

            async def async_get_tariff(self):
                await stage('settings')
                return {'profile': classified(), 'revision': 1}

        async def guard(ids):
            owner.assertIsInstance(ids, list)
            owner.assertTrue(all(isinstance(sid, str) for sid in ids))
            await stage('guard', ids)
            if not set(ids).issubset(owner.allowed):
                raise Denied('source_access_denied')

        hass = types.SimpleNamespace(data={}, config=types.SimpleNamespace(time_zone='UTC'),
                                     async_add_executor_job=executor)
        c = energy.EnergySnapshotCoordinator(hass, Storage(),
                                             provider=supplied if provider else None)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(energy, 'async_grid_sensors', sensors))
        stack.enter_context(patch.object(energy, 'async_get_energy_metadata', metadata))
        stack.enter_context(patch.object(energy, 'async_get_energy_usage', history))
        return c, guard

    def request(self, c, route, guard):
        if route == 'refresh':
            return c.async_refresh(now=NOW, guard=guard)
        if route == 'private_refresh':
            return c._async_refresh(now=NOW, guard=guard)
        kwargs = {'profile': classified()} if route in ('override', 'private_override') else {}
        method = c._async_snapshot if route in ('private_shared', 'private_override') else c.async_snapshot
        return method(now=NOW, guard=guard, **kwargs)

    def private_events(self):
        return [row for row in self.events if row[0] in ('metadata', 'history', 'provider')]

    async def test_fresh_denial_precedes_all_private_io_on_every_entry_point(self):
        for route in ('refresh', 'shared', 'override', 'private_refresh', 'private_shared', 'private_override'):
            with self.subTest(route=route):
                c, guard = self.fixture()
                self.allowed.clear()
                with self.assertRaises(Denied):
                    await self.request(c, route, guard)
                self.assertFalse(self.private_events())
                self.assertIn(('guard', tuple(self.sources)), self.events)
                self.assertIsNone(c.data)
                self.assertEqual(0, c._snapshot_pending)

    async def test_guard_receives_concrete_entity_and_external_statistics_ids(self):
        c, guard = self.fixture()
        result = await c.async_snapshot(now=NOW, guard=guard)
        self.assertEqual('ready', result['status'])
        ids = tuple(self.sources)
        checks = [row for row in self.events if row[0] == 'guard']
        self.assertTrue(checks)
        self.assertTrue(all(row == ('guard', ids) for row in checks))
        first_read = next(i for i, row in enumerate(self.events) if row[0] == 'metadata')
        self.assertIn(('guard', ids), self.events[:first_read])
        history = next(i for i, row in enumerate(self.events) if row[0] == 'history')
        self.assertIn(('guard', ids), self.events[first_read + 1:history])
        cpu = next(i for i, row in enumerate(self.events) if row[0] == 'cpu')
        self.assertIn(('guard', ids), self.events[cpu + 1:])
        self.assertNotIn('external:grid_import', str(result))

    async def test_new_configured_source_is_checked_before_its_metadata(self):
        for stage in ('metadata', 'history', 'cpu'):
            with self.subTest(stage=stage):
                c, guard = self.fixture(blocked=stage)
                task = asyncio.create_task(c.async_snapshot(now=NOW, guard=guard))
                try:
                    await asyncio.wait_for(self.entered.wait(), 2)
                    self.sources[:] = ['sensor.new_grid']
                finally:
                    self.release.set()
                with self.assertRaises(Denied):
                    await task
                self.assertIn(('guard', ('sensor.new_grid',)), self.events)
                self.assertFalse(any('sensor.new_grid' in (ids or ())
                                     for name, ids in self.private_events()))
                self.assertIsNone(c.data)

    async def test_revocation_at_each_await_stops_further_private_reads_and_output(self):
        for stage in ('metadata', 'history', 'settings', 'cpu', 'override_cpu'):
            with self.subTest(stage=stage):
                c, guard = self.fixture(blocked=stage)
                published = []
                c.async_add_listener(lambda: published.append(copy.deepcopy(c.data)))
                route = 'override' if stage == 'override_cpu' else 'shared'
                task = asyncio.create_task(self.request(c, route, guard))
                try:
                    await asyncio.wait_for(self.entered.wait(), 2)
                    reads_before = list(self.private_events())
                    data_before = copy.deepcopy(c.data)
                    publications_before = len(published)
                    self.allowed.clear()
                finally:
                    self.release.set()
                with self.assertRaises(Denied):
                    await task
                self.assertEqual(reads_before, self.private_events())
                self.assertEqual(data_before, c.data)
                self.assertEqual(publications_before, len(published))
                self.assertEqual(0, c._snapshot_pending)
                self.assertEqual(0, c._cpu_lane['pending'])

    async def test_provider_is_guarded_before_read_and_after_await(self):
        c, guard = self.fixture(provider=True)
        self.allowed.clear()
        with self.assertRaises(Denied):
            await c.async_refresh(now=NOW, guard=guard)
        self.assertFalse(self.private_events())
        c, guard = self.fixture(provider=True, blocked='provider')
        task = asyncio.create_task(c.async_refresh(now=NOW, guard=guard))
        try:
            await asyncio.wait_for(self.entered.wait(), 2)
            reads_before = list(self.private_events())
            self.allowed.clear()
        finally:
            self.release.set()
        with self.assertRaises(Denied):
            await task
        self.assertEqual(reads_before, self.private_events())
        self.assertIsNone(c.data)

    async def test_cached_measurements_and_overrides_cannot_bypass_request_guard(self):
        c, guard = self.fixture()
        baseline = await c.async_refresh(now=NOW, guard=guard)
        measurement = c._measurement
        self.assertTrue(any(name == 'history' for name, _ in self.events))
        for route in ('refresh', 'shared', 'override'):
            with self.subTest(route=route):
                self.events.clear()
                self.allowed.clear()
                with self.assertRaises(Denied):
                    await self.request(c, route, guard)
                self.assertFalse(self.private_events())
                self.assertEqual(baseline, c.data)
                self.assertIs(measurement, c._measurement)
        # An earlier denial is request-local and cannot poison the shared cache.
        self.allowed.update(self.sources)
        self.events.clear()
        recovered = await c.async_snapshot(now=NOW, guard=guard)
        self.assertEqual(baseline, recovered)
        self.assertFalse(any(name == 'history' for name, _ in self.events))
        self.assertTrue(any(name == 'guard' for name, _ in self.events))

    async def test_revoked_request_does_not_invalidate_existing_entity_snapshot(self):
        c, guard = self.fixture()
        baseline = await c.async_refresh(now=NOW, guard=guard)
        publications = []
        c.async_add_listener(lambda: publications.append(c.data))
        self.events.clear()
        self.blocked = 'metadata'
        task = asyncio.create_task(c.async_snapshot(now=NOW, guard=guard))
        try:
            await asyncio.wait_for(self.entered.wait(), 2)
            reads_before = list(self.private_events())
            self.allowed.clear()
        finally:
            self.release.set()
        with self.assertRaises(Denied):
            await task
        self.assertEqual(reads_before, self.private_events())
        self.assertEqual(baseline, c.data)
        self.assertFalse(publications)

    async def test_none_default_preserves_trusted_refresh_cache_and_override_behavior(self):
        c, _ = self.fixture()
        baseline = await c.async_refresh(now=NOW)
        alternate = classified()
        alternate['currency'] = 'PLN'
        result = await c.async_snapshot(profile=alternate, now=NOW)
        self.assertEqual('PLN', result['currency'])
        self.assertEqual(baseline, c.data)
        self.assertEqual(baseline, await c.async_snapshot(now=NOW))
        self.assertEqual(1, sum(name == 'history' for name, _ in self.events))
        self.assertFalse(any(name == 'guard' for name, _ in self.events))
