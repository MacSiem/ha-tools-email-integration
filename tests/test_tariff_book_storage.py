"""Versioned book uses the existing confirmed Store and shared revision."""
import asyncio
import copy
from datetime import datetime, timezone
import types
import unittest
from tests.test_tariff_profile import load_storage
from tests.test_tariff_history import book, version

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


class BookStorageTest(unittest.IsolatedAsyncioTestCase):
    def storage(self):
        module = load_storage()
        class Lane:
            async def _async_run_cpu(self, job):
                return job()
        hass = types.SimpleNamespace(data={'ha_tools_email': {'energy_snapshot': Lane()}}, state='running',
            config=types.SimpleNamespace(path=lambda *args: self.fail('No SMTP migration for book IO')))
        return module, module.EmailStorage(hass)

    async def test_preview_exposes_exact_past_range_without_saving(self):
        _, s = self.storage()
        initial = await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)
        changed = copy.deepcopy(initial['book'])
        changed['tariffs'][0]['versions'].append(version('2026-10-01', 2, 'changed'))
        preview = await s.async_preview_tariff_book(changed, expected_revision=1, now=NOW)
        self.assertEqual([{'tariff_id': 'active', 'from': '2026-10-01T00:00:00+00:00',
                           'to': '2026-10-18T12:00:00+00:00'}], preview['historical_changes'])
        self.assertEqual(1, preview['revision'])
        self.assertEqual(initial, await s.async_get_tariff_book())
        self.assertEqual(1, len(s._store.saved))

    async def test_entire_runtime_bucket_replaced_during_validation_never_writes(self):
        _, s = self.storage()
        lane = s.hass.data['ha_tools_email']['energy_snapshot']
        async def replaced(job):
            result = job()
            s.hass.data['ha_tools_email'] = {'energy_snapshot': object()}
            return result
        lane._async_run_cpu = replaced
        with self.assertRaisesRegex(RuntimeError, 'energy_snapshot_unloaded'):
            await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)
        self.assertEqual([], s._store.saved)

    async def test_additive_save_restart_shared_revision_and_foreign_state(self):
        module, s = self.storage()
        foreign = {'smtp_config': {'password': '!secret synthetic'}, 'schedules': [{'id': 'keep'}],
                   'future': {'nested': [1]}, 'attempt_history': {'opaque': True},
                   'tariff_profile': {'schema': 0}}
        s._store.loaded = copy.deepcopy(foreign)
        first = await s.async_get_tariff_book()
        self.assertIsNone(first['book'])
        self.assertEqual(0, first['revision'])
        self.assertEqual([], s._store.saved)
        saved = await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)
        self.assertEqual(1, saved['revision'])
        self.assertEqual('active', saved['book']['active_tariff_id'])
        self.assertEqual(1, s.current_tariff_revision)
        for key, value in foreign.items():
            self.assertEqual(value, s._store.saved[-1][key], key)
        again = type(s)(s.hass)
        again._store.loaded = copy.deepcopy(s._store.saved[-1])
        self.assertEqual(saved, await again.async_get_tariff_book())
        with self.assertRaises(module.TariffRevisionConflict):
            await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)

    async def test_historical_edit_requires_ack_but_future_version_does_not(self):
        module, s = self.storage()
        initial = await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)
        altered = copy.deepcopy(initial['book'])
        altered['tariffs'][0]['versions'][0]['profile']['zones'][0]['energyRate'] = 2
        with self.assertRaisesRegex(ValueError, 'historical_edit_ack_required'):
            await s.async_save_tariff_book(altered, expected_revision=1, now=NOW)
        self.assertEqual(initial, await s.async_get_tariff_book())
        accepted = await s.async_save_tariff_book(altered, expected_revision=1,
                                                 historical_edit_ack=True, now=NOW)
        future = copy.deepcopy(accepted['book'])
        future['tariffs'][0]['versions'].append(version('2026-11-01', 3, 'future'))
        saved = await s.async_save_tariff_book(future, expected_revision=2, now=NOW)
        self.assertEqual(3, saved['revision'])
        self.assertEqual('2026-11-01', saved['book']['tariffs'][0]['versions'][0]['valid_to'])
        # Acknowledgement never rewrites the already-saved historical snapshots.
        self.assertEqual(1, s._store.saved[0]['tariff_book']['tariffs'][0]['versions'][0]['profile']['zones'][0]['energyRate'])

    async def test_failed_lower_write_does_not_publish_book_or_revision(self):
        module, s = self.storage()
        old = await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)
        changed = copy.deepcopy(old['book']); changed['tariffs'][0]['name'] = 'Renamed'
        s._store.swallowed_failure = True
        with self.assertRaises(module.StorageWriteError):
            await s.async_save_tariff_book(changed, expected_revision=1, now=NOW)
        self.assertEqual(old, await s.async_get_tariff_book())
        self.assertEqual(1, s.current_tariff_revision)
        s._store.swallowed_failure = False
        async def revoked():
            return False
        with self.assertRaises(PermissionError):
            await s.async_save_tariff_book(changed, expected_revision=1, now=NOW, before_save=revoked)
        self.assertEqual(2, s._store.save_calls)

    async def test_admission_and_cancelled_waiter_recover_without_extra_writes(self):
        _, s = self.storage()
        await s._lock.acquire()
        tasks = []
        try:
            for _ in range(4):
                tasks.append(asyncio.create_task(s.async_save_tariff_book(book(), expected_revision=0, now=NOW)))
            await asyncio.sleep(0)
            overflow = asyncio.create_task(s.async_save_tariff_book(book(), expected_revision=0, now=NOW))
            tasks.append(overflow)
            await asyncio.sleep(0)
            self.assertTrue(overflow.done())
            with self.assertRaisesRegex(RuntimeError, 'energy_pricing_busy'):
                overflow.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            s._lock.release()
        result = await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)
        self.assertEqual(1, result['revision'])
        self.assertEqual(1, len(s._store.saved))
