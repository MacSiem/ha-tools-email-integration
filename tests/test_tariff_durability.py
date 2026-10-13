"""Tariff publication requires completed lower writes; doubles prove no disk IO."""
import asyncio
import copy
import types
import unittest

try:
    from tests.test_tariff_profile import load_storage, profile
except ImportError:
    from test_tariff_profile import load_storage, profile


class TariffDurabilityTests(unittest.IsolatedAsyncioTestCase):
    def storage(self):
        mod = load_storage()
        hass = types.SimpleNamespace(state='running', config=types.SimpleNamespace(
            path=lambda *args: self.fail('tariff reads/writes must not migrate SMTP')))
        return mod, mod.EmailStorage(hass)

    async def test_swallowed_write_failure_preserves_old_snapshot_and_explicit_retry(self):
        # Break caught: publishing candidate on normal return after swallowed IO.
        mod, s = self.storage()
        foreign = {
            'smtp_config': {'server': 'synthetic', 'password': '!secret synthetic_smtp'},
            'schedules': [{'id': 'synthetic', 'future': ['untouched']}],
            'security': {'password_via_options': True, 'future': 9},
            'digest_state': {'synthetic': 1}, 'scheduler_state': {'synthetic': 2},
            'attempt_history': {'schema': 99, 'opaque': ['untouched']},
            'future': {'nested': [1, {'keep': True}]},
        }
        s._store.loaded = copy.deepcopy(foreign)
        old = await s.async_save_tariff(profile(), expected_revision=0)
        durable = copy.deepcopy(s._store.saved[-1])
        s._store.swallowed_failure = True
        with self.assertRaises(mod.StorageWriteError) as error:
            await s.async_save_tariff(None, expected_revision=1)
        self.assertNotIn('synthetic lower', str(error.exception))
        self.assertEqual(old, await s.async_get_tariff())
        self.assertEqual(durable, s._store.saved[-1])
        self.assertEqual(2, s._store.save_calls)  # no automatic retry
        s._store.swallowed_failure = False
        new = await s.async_save_tariff(None, expected_revision=1)
        self.assertEqual(2, new['revision'])
        self.assertIsNone(new['profile'])
        self.assertIsInstance(new['updated_at'], str)
        for key, value in foreign.items():
            self.assertEqual(value, s._store.saved[-1][key], key)
        self.assertEqual(foreign, s._store.loaded)
        self.assertEqual(3, s._store.save_calls)

    async def test_skipped_write_cannot_reuse_prior_confirmation(self):
        # Break caught: a successful previous call leaves a sticky receipt.
        mod, s = self.storage()
        old = await s.async_save_tariff(profile(), expected_revision=0)
        s._store.skipped = True
        with self.assertRaises(mod.StorageWriteError):
            await s.async_save_tariff(None, expected_revision=1)
        self.assertEqual(old, await s.async_get_tariff())
        self.assertEqual(1, len(s._store.saved))
        self.assertEqual(2, s._store.save_calls)

    async def test_stopping_fails_before_store_can_defer_candidate(self):
        # Break caught: entering HA's deferred-save path during shutdown.
        mod, s = self.storage()
        old = await s.async_get_tariff()
        s.hass.state = 'stopping'
        with self.assertRaises(mod.StorageWriteError):
            await s.async_save_tariff(profile(), expected_revision=0)
        self.assertEqual(old, await s.async_get_tariff())
        self.assertEqual(0, s._store.save_calls)
        self.assertEqual([], s._store.saved)

    async def test_success_waits_for_completed_old_and_current_lower_hook(self):
        # Break caught: marking confirmation before the awaited hook returns.
        for old_hook in (False, True):
            with self.subTest(old_hook=old_hook):
                _, s = self.storage()
                before = await s.async_get_tariff()
                s._store.old_hook = old_hook
                s._store.write_started = asyncio.Event()
                s._store.write_release = asyncio.Event()
                task = asyncio.create_task(s.async_save_tariff(profile(), expected_revision=0))
                try:
                    await asyncio.wait_for(s._store.write_started.wait(), timeout=1)
                    self.assertFalse(task.done())
                    self.assertEqual(0, s._data['tariff_revision'])
                    self.assertEqual(before['profile'], s._data['tariff_profile'])
                    self.assertEqual(before['updated_at'], s._data['tariff_updated_at'])
                finally:
                    s._store.write_release.set()
                    result = await task
                self.assertEqual(1, result['revision'])
                self.assertEqual(profile(), result['profile'])
                restarted = type(s)(s.hass)
                restarted._store.loaded = copy.deepcopy(s._store.saved[-1])
                self.assertEqual(result, await restarted.async_get_tariff())

    async def test_cancelled_lower_write_does_not_publish_or_poison_explicit_retry(self):
        # Break caught: publication/confirmation surviving cancelled IO.
        _, s = self.storage()
        old = await s.async_get_tariff()
        s._store.write_started = asyncio.Event()
        s._store.write_release = asyncio.Event()
        task = asyncio.create_task(s.async_save_tariff(profile(), expected_revision=0))
        try:
            await asyncio.wait_for(s._store.write_started.wait(), timeout=1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(old, await s.async_get_tariff())
        self.assertEqual([], s._store.saved)
        s._store.write_started = s._store.write_release = None
        self.assertEqual(1, (await s.async_save_tariff(profile(), expected_revision=0))['revision'])

    async def test_concurrent_editors_only_one_durable_winner_retains_foreign_keys(self):
        # Break caught: revision check/save running outside the existing lock.
        mod, s = self.storage()
        s._store.loaded = {'future': {'keep': True}, 'smtp_config': {'password': '!secret synthetic'}}
        results = await asyncio.gather(
            s.async_save_tariff(profile(), expected_revision=0),
            s.async_save_tariff(None, expected_revision=0), return_exceptions=True)
        self.assertEqual(1, sum(isinstance(r, mod.TariffRevisionConflict) for r in results))
        winner = next(r for r in results if isinstance(r, dict))
        self.assertEqual(1, winner['revision'])
        self.assertEqual(winner, await s.async_get_tariff())
        self.assertEqual(1, len(s._store.saved))
        self.assertEqual({'keep': True}, s._store.saved[0]['future'])
        self.assertEqual({'password': '!secret synthetic'}, s._store.saved[0]['smtp_config'])

    async def test_ordinary_store_save_keeps_existing_swallowed_error_behavior(self):
        # Break caught: imposing tariff confirmation on history/SMTP/schedules.
        _, s = self.storage()
        s._store.swallowed_failure = True
        await s._store.async_save({'synthetic': 'ordinary'})
        self.assertEqual([], s._store.saved)
        s._store.swallowed_failure = False
        s._store.skipped = True
        s.hass.state = 'stopping'
        await s._store.async_save({'synthetic': 'ordinary'})
        self.assertEqual(2, s._store.save_calls)
