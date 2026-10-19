"""Real status Store method: caller cancellation cannot free raw worker slots."""
import asyncio
import gc
import threading
import types
import unittest
import weakref
from datetime import datetime, timezone
from unittest.mock import patch

from tests.test_tariff_profile import load_storage

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


class ReportStatusCancelLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_raw_jobs_remain_held_after_cancel_fifth_denied_then_settle(self):
        module = load_storage()
        storage = module.EmailStorage(types.SimpleNamespace())
        loop = asyncio.get_running_loop()
        release = threading.Event()
        started = [asyncio.Event() for _ in range(5)]
        settled = asyncio.Event()
        raw_calls = []
        observations = []
        caller = None
        raw_lock = threading.Lock()
        completions = []
        references = []
        unhandled = []
        old_handler = loop.get_exception_handler()
        original = module.project_report_status

        def blocking_projection(*args, **kwargs):
            with raw_lock:
                index = len(raw_calls)
                raw_calls.append(index)
            loop.call_soon_threadsafe(started[index].set)
            release.wait()
            # Both success and failure must release exactly once, even though
            # the four cancelled callers never await their raw task outcomes.
            result = original(*args, **kwargs)
            with raw_lock:
                observations.append((id(args[0]), result['schedule_state']))
            if index in (1, 3):
                raise RuntimeError('PRIVATE worker failure must be consumed')
            return result

        def watch(task):
            references.append(weakref.ref(task))
            def done(_):
                completions.append(True)
                if len(completions) == 4:
                    settled.set()
            task.add_done_callback(done)

        async def read():
            return await storage.async_get_report_status('energy_report', now=NOW,
                local_timezone='UTC')

        loop.set_exception_handler(lambda _, context: unhandled.append(context))
        try:
            with patch.object(module, 'project_report_status', new=blocking_projection):
                for index in range(4):
                    caller = asyncio.create_task(read())
                    await asyncio.wait_for(started[index].wait(), 10)
                    # A successful raw start means there is exactly one new task
                    # in addition to those retained by prior cancelled callers.
                    for worker in tuple(storage._report_status_tasks):
                        if all(ref() is not worker for ref in references):
                            watch(worker)
                    del worker
                    caller.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await caller
                    self.assertEqual(index + 1, storage._report_status_pending)
                    self.assertEqual(index + 1, len(storage._report_status_tasks))
                    self.assertFalse(storage._lock.locked())
                self.assertEqual(4, len(raw_calls))
                with self.assertRaisesRegex(RuntimeError, '^report_status_busy$'):
                    await read()
                self.assertEqual(4, len(raw_calls))
                self.assertTrue(all(ref() is not None and not ref().done() for ref in references))
                # Cancellation released the lock: a real confirmed COW write
                # can proceed while the four workers still hold the old state.
                old_data = storage._data
                await storage.async_upsert_schedule({'id': 'new', 'kind': 'energy_report',
                    'cadence': 'daily', 'time': '12:30', 'timezone': 'UTC', 'enabled': True})
                self.assertIsNot(old_data, storage._data)
                self.assertEqual([], old_data['schedules'])
                self.assertEqual(4, storage._report_status_pending)
                release.set()
                await asyncio.wait_for(settled.wait(), 10)
                await asyncio.sleep(0)  # Drain task/shield callbacks, no polling.
                self.assertEqual(0, storage._report_status_pending)
                self.assertFalse(storage._report_status_tasks)
                self.assertEqual([(id(old_data), 'unconfigured')] * 4, observations)
                gc.collect()
                await asyncio.sleep(0)
                self.assertEqual([], unhandled)
                self.assertTrue(all(ref() is None for ref in references))
                result, token = await read()
                self.assertEqual('enabled', result['schedule_state'])
                self.assertTrue(storage.report_status_context_is_current(token))
                self.assertEqual(5, len(raw_calls))
                self.assertEqual(0, storage._report_status_pending)
                self.assertFalse(storage._report_status_tasks)
        finally:
            release.set()  # Never leave a raw thread blocked on assertion failure.
            if caller is not None and not caller.done():
                caller.cancel()
                await asyncio.gather(caller, return_exceptions=True)
            if storage._report_status_tasks:
                await asyncio.gather(*tuple(storage._report_status_tasks), return_exceptions=True)
            loop.set_exception_handler(old_handler)

    async def test_cancel_waiting_for_store_lock_releases_immediately_without_worker(self):
        module = load_storage()
        storage = module.EmailStorage(types.SimpleNamespace())
        await storage._lock.acquire()
        try:
            caller = asyncio.create_task(storage.async_get_report_status('energy_report',
                now=NOW, local_timezone='UTC'))
            await asyncio.sleep(0)
            self.assertEqual(1, storage._report_status_pending)
            self.assertFalse(storage._report_status_tasks)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertEqual(0, storage._report_status_pending)
            self.assertFalse(storage._report_status_tasks)
        finally:
            storage._lock.release()
