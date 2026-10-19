"""Owned hourly runtime calls real publisher kernel; all I/O is synthetic."""
import asyncio
from datetime import datetime, timedelta, timezone
from importlib import import_module
from threading import Event
from types import SimpleNamespace
import unittest
import sys
from unittest.mock import AsyncMock, patch

from tests import test_energy_statistics_publisher as publisher_fixture
from tests.test_energy_analysis_service import service

with patch.dict(sys.modules, {'homeassistant.core': SimpleNamespace(callback=lambda function: function)}):
    runtime_module = import_module(service.__package__ + '.energy_statistics_runtime')


class Bus:
    def __init__(self):
        self.listeners = set()
    def async_listen(self, event, callback):
        self.listeners.add(callback)
        return lambda: self.listeners.discard(callback)
    def async_fire(self, event, data):
        if event == runtime_module.EVENT_STATISTICS_SHARE_CHANGED:
            assert data == {}
            for listener in tuple(self.listeners):
                listener(SimpleNamespace(data=data))


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        source, store, recorder, publisher = publisher_fixture.PublisherTest().fixture()
        source.hass.bus = Bus()
        recorder.async_metadata = AsyncMock(wraps=recorder.async_metadata)
        recorder.async_verify = AsyncMock(wraps=recorder.async_verify)
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        self.timers = []
        def schedule(callback, at):
            timer = {'callback': callback, 'at': at, 'active': True}
            self.timers.append(timer)
            return lambda: timer.update(active=False)
        runtime = runtime_module.EnergyStatisticsRuntime(source.hass, source.entry_id,
            publisher_factory=lambda: publisher, clock=lambda: self.now, schedule=schedule)
        return source, store, recorder, publisher, runtime

    async def drain(self, runtime):
        while runtime._lane['task'] is not None:
            task = runtime._lane['task']
            await asyncio.wait({task})
            if not task.cancelled():
                task.result()
            await asyncio.sleep(0)

    async def enable(self, publisher):
        return await publisher_fixture.PublisherTest().enable(publisher)

    async def test_default_zero_source_and_recorder_io_no_hour_timer(self):
        source, store, recorder, publisher, runtime = self.fixture()
        runtime.async_start()
        await self.drain(runtime)
        self.assertEqual(0, source.reads)
        self.assertEqual(0, recorder.enqueues)
        recorder.async_metadata.assert_not_awaited()
        recorder.async_verify.assert_not_awaited()
        self.assertFalse(self.timers)
        runtime.async_stop()
        self.assertFalse(source.hass.bus.listeners)

    async def test_optin_one_completed_hour_and_tick_burst_never_duplicates(self):
        source, store, recorder, publisher, runtime = self.fixture()
        runtime.async_start()
        await self.drain(runtime)
        await self.enable(publisher)
        await self.drain(runtime)
        self.assertEqual(1, recorder.enqueues)
        self.assertTrue(all(len(points) == 1 for points in recorder.rows.values()))
        before = source.reads
        for _ in range(50):
            runtime._tick(self.now)
        await self.drain(runtime)
        self.assertEqual(before, source.reads)
        self.assertEqual(1, recorder.enqueues)
        active = [timer for timer in self.timers if timer['active']]
        self.assertEqual(1, len(active))
        self.assertEqual(self.now + timedelta(hours=1), active[0]['at'])
        self.assertEqual(('admin', publisher._state['share']['epoch'], 1), runtime._consent)
        runtime.async_stop()

    async def test_author_revoked_pauses_before_source_io_or_enqueue(self):
        source, store, recorder, publisher, runtime = self.fixture()
        await self.enable(publisher)
        source.user.is_active = False
        before = source.reads
        runtime.async_start()
        await self.drain(runtime)
        self.assertEqual(before, source.reads)
        self.assertEqual(0, recorder.enqueues)
        self.assertIsNone(runtime._consent)
        self.assertFalse(any(timer['active'] for timer in self.timers))
        runtime.async_stop()

    async def test_disabled_and_changed_author_are_not_silently_reenabled(self):
        source, store, recorder, publisher, runtime = self.fixture()
        await self.enable(publisher)
        runtime.async_start()
        await self.drain(runtime)
        before = source.reads
        # A changed durable author without a confirmed control notification is
        # not adopted by a clock tick. No invented administrator is substituted.
        publisher._state['share']['author'] = 'different-author'
        publisher._state['revision'] += 1
        runtime._tick(self.now + timedelta(hours=1))
        await self.drain(runtime)
        self.assertIsNone(runtime._consent)
        self.assertEqual(before, source.reads)
        await publisher.async_disable('admin', expected_revision=2)
        await self.drain(runtime)
        self.assertFalse(any(timer['active'] for timer in self.timers))
        self.assertFalse(publisher._state['share']['enabled'])
        runtime.async_stop()

    async def test_source_change_fails_kernel_guard_and_pauses_no_enqueue(self):
        source, store, recorder, publisher, runtime = self.fixture()
        await self.enable(publisher)
        source.valid = False
        runtime.async_start()
        await self.drain(runtime)
        self.assertEqual(0, recorder.enqueues)
        self.assertIsNone(runtime._consent)
        self.assertFalse(any(timer['active'] for timer in self.timers))
        runtime.async_stop()

    async def test_kernel_consent_fence_rejects_stale_runtime_token_before_source_io(self):
        source, store, recorder, publisher, runtime = self.fixture()
        await self.enable(publisher)
        token = await publisher.async_runtime_context()
        await publisher.async_disable('admin', expected_revision=1)
        reads = source.reads
        with self.assertRaisesRegex(RuntimeError, 'statistics_consent_changed'):
            await publisher.async_refresh('admin', expected_share=token)
        self.assertEqual(reads, source.reads)
        self.assertEqual(0, recorder.enqueues)

    async def test_unconfirmed_import_retries_next_hour_without_new_consent(self):
        source, store, recorder, publisher, runtime = self.fixture()
        await self.enable(publisher)
        real_verify = recorder.async_verify
        recorder.async_verify = AsyncMock(return_value=False)
        runtime.async_start()
        await self.drain(runtime)
        self.assertIsNotNone(publisher._state['pending'])
        self.assertIsNotNone(runtime._consent)
        self.assertIsNone(runtime._lane['completed'])
        self.assertTrue(any(timer['active'] for timer in self.timers))
        recorder.async_verify = real_verify
        self.now += timedelta(hours=1)
        runtime._tick(self.now)
        await self.drain(runtime)
        self.assertIsNone(publisher._state['pending'])
        self.assertEqual(self.now, runtime._lane['completed'][1])
        self.assertEqual('admin', runtime._consent[0])
        runtime.async_stop()

    async def test_unload_retains_single_raw_job_and_reload_waits_for_settlement(self):
        source, store, recorder, publisher, runtime = self.fixture()
        await self.enable(publisher)
        entered, release = Event(), Event()
        original = source.async_get_statistics_hours
        async def blocked(actor, *, hours=1):
            self.assertEqual('admin', actor)
            self.assertEqual(1, hours)  # Never automatic seven-day backfill.
            def raw():
                entered.set()
                release.wait(5)
            await asyncio.to_thread(raw)
            return await original(actor, hours=hours)
        source.async_get_statistics_hours = blocked
        runtime.async_start()
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            held = runtime._lane['task']
            for _ in range(50):
                runtime._tick(self.now)
            self.assertIs(held, runtime._lane['task'])
            runtime.async_stop()
            self.assertFalse(held.done())
            replacement = runtime_module.EnergyStatisticsRuntime(source.hass, source.entry_id,
                publisher_factory=lambda: publisher, clock=lambda: self.now,
                schedule=lambda callback, at: lambda: None)
            replacement.async_start()
            self.assertIs(held, replacement._lane['task'])
            self.assertEqual(0, recorder.enqueues)
        finally:
            release.set()
            await self.drain(runtime)
            runtime.async_stop()
            if 'replacement' in locals():
                replacement.async_stop()
        self.assertEqual(0, recorder.enqueues)
        self.assertIsNone(runtime._lane['task'])

    async def test_public_status_distinguishes_active_paused_and_disabled_automation(self):
        source, store, recorder, publisher, runtime = self.fixture()
        source.hass.data['ha_tools_email']['energy_statistics_runtime'] = runtime
        self.assertEqual({'state': 'disabled', 'last_completed_hour': None},
                         (await publisher.async_status('admin'))['automation'])
        runtime.async_start()
        await self.drain(runtime)
        await self.enable(publisher)
        await self.drain(runtime)
        self.assertEqual({'state': 'active', 'last_completed_hour': self.now.isoformat()},
                         (await publisher.async_status('admin'))['automation'])
        source.user.is_active = False
        self.now += timedelta(hours=1)
        runtime._tick(self.now)
        await self.drain(runtime)
        self.assertEqual('paused', publisher._summary(publisher._state)['automation']['state'])
        source.user.is_active = True
        await self.enable(publisher)
        await self.drain(runtime)
        self.assertEqual('active', (await publisher.async_status('admin'))['automation']['state'])
        await publisher.async_disable('admin', expected_revision=2)
        await self.drain(runtime)
        self.assertEqual({'state': 'disabled', 'last_completed_hour': None},
                         (await publisher.async_status('admin'))['automation'])
        runtime.async_stop()
