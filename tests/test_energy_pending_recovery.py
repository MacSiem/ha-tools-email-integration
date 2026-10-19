"""Explicit Disable archives unknown intent; fresh consent never retries it."""
import copy
import unittest
from unittest.mock import patch

from tests import test_energy_statistics_publisher as fixtures


class PendingRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def pending_fixture(self):
        source, store, recorder, publisher = fixtures.PublisherTest().fixture()
        source.facts['hours'][0]['groups']['unassigned']['complete'] = False
        source.facts['hours'][0]['groups']['unassigned']['usage_cost'] = None
        await fixtures.PublisherTest().enable(publisher)
        original = recorder.async_verify
        async def revoke_after_enqueue(plan):
            source.user.is_active = False
            return await original(plan)
        recorder.async_verify = revoke_after_enqueue
        with self.assertRaises(PermissionError):
            await publisher.async_refresh('admin')
        recorder.async_verify = original
        self.assertEqual(1, recorder.enqueues)
        self.assertIsNotNone(store.data['pending'])
        source.user.is_active = True
        return source, store, recorder, publisher

    async def test_disable_archives_exact_intent_policy_and_unknown_without_recorder_or_source_io(self):
        source, store, recorder, publisher = await self.pending_fixture()
        before = copy.deepcopy(store.data)
        rows = copy.deepcopy(recorder.rows)
        reads = source.reads
        status = await publisher.async_disable('another-current-admin', expected_revision=1)
        self.assertFalse(status['enabled'])
        self.assertFalse(status['pending'])
        self.assertEqual(1, status['unconfirmed_history'])
        self.assertIsNone(store.data['runtime'])
        archive = store.data['archived_intents'][0]
        self.assertEqual(before['pending'], {key: archive[key] for key in ('epoch', 'series', 'clear')})
        self.assertEqual(before['share']['binding'], archive['binding'])
        self.assertEqual(before['share']['author'], archive['author'])
        self.assertEqual(before['series'], store.data['series'])
        self.assertTrue(any(point['cost'] is None for row in store.data['series'].values()
                            for point in row['points'].values()))
        self.assertEqual(rows, recorder.rows)
        self.assertEqual(reads, source.reads)
        self.assertEqual(1, recorder.enqueues)
        again = await publisher.async_disable('admin', expected_revision=2)
        self.assertEqual(1, again['unconfirmed_history'])
        self.assertEqual([archive], store.data['archived_intents'])

    async def test_new_policy_pending_blocks_until_disable_then_fresh_enable_new_epoch_and_no_old_replay(self):
        source, store, recorder, publisher = await self.pending_fixture()
        old = copy.deepcopy(store.data)
        old_rows = copy.deepcopy(recorder.rows)
        source.facts['context']['feature_access_revision'] += 1
        source.facts['publication_policy_signature'] = 'new-policy'
        prepared = await publisher.async_prepare('admin')
        with self.assertRaisesRegex(RuntimeError, 'statistics_dataset_changed'):
            await publisher.async_refresh('admin')
        with self.assertRaisesRegex(RuntimeError, 'statistics_pending_dataset_requires_recovery'):
            await publisher.async_enable('admin', prepared['proposal'], expected_revision=1,
                acknowledge_global=True, acknowledge_current_membership=True)
        self.assertEqual(old, store.data)
        self.assertEqual(1, recorder.enqueues)
        await publisher.async_disable('admin', expected_revision=1)
        archive = copy.deepcopy(store.data['archived_intents'])
        proposal = await publisher.async_prepare('another-current-admin')
        status = await publisher.async_enable('another-current-admin', proposal['proposal'], expected_revision=2,
            acknowledge_global=True, acknowledge_current_membership=True)
        self.assertNotEqual(old['share']['epoch'], status['active_epoch'])
        self.assertEqual('another-current-admin', status['author_user_id'])
        self.assertEqual(archive, store.data['archived_intents'])
        self.assertFalse(status['pending'])
        self.assertEqual(1, recorder.enqueues)  # Enable is not a replay of old intent.
        await publisher.async_refresh('another-current-admin')
        self.assertEqual(archive, store.data['archived_intents'])
        self.assertEqual(old['series'], {sid: store.data['series'][sid] for sid in old['series']})
        self.assertEqual(old_rows, {sid: recorder.rows[sid] for sid in old_rows})
        current = {row['statistic_id'] for row in (await publisher.async_status('another-current-admin'))['catalog']
                   if row['current_dataset']}
        self.assertTrue(current)
        self.assertFalse(current & set(old_rows))
        self.assertEqual(old['share']['binding'], archive[0]['binding'])

    async def test_identical_binding_after_disable_also_has_new_epoch_ids_and_archive_survives_restart(self):
        source, store, recorder, publisher = await self.pending_fixture()
        old_epoch = store.data['share']['epoch']
        old_ids = set(recorder.rows)
        await publisher.async_disable('admin', expected_revision=1)
        archive = copy.deepcopy(store.data['archived_intents'])
        replacement = fixtures.publisher.EnergyStatisticsPublisher(source.hass, source, store=store, recorder=recorder)
        prepared = await replacement.async_prepare('admin')
        with self.assertRaises(ValueError):
            await replacement.async_enable('admin', prepared['proposal'], expected_revision=2,
                acknowledge_global=False, acknowledge_current_membership=True)
        status = await replacement.async_enable('admin', prepared['proposal'], expected_revision=2,
            acknowledge_global=True, acknowledge_current_membership=True)
        self.assertNotEqual(old_epoch, status['active_epoch'])
        await replacement.async_refresh('admin')
        current = {row['statistic_id'] for row in (await replacement.async_status('admin'))['catalog']
                   if row['current_dataset']}
        self.assertFalse(old_ids & current)
        self.assertEqual(archive, store.data['archived_intents'])
        self.assertEqual(1, (await replacement.async_status('admin'))['unconfirmed_history'])

    async def test_another_admin_cannot_take_over_pending_even_with_identical_binding(self):
        source, store, recorder, publisher = await self.pending_fixture()
        before = copy.deepcopy(store.data)
        prepared = await publisher.async_prepare('another-current-admin')
        with self.assertRaisesRegex(RuntimeError, 'statistics_pending_dataset_requires_recovery'):
            await publisher.async_enable('another-current-admin', prepared['proposal'], expected_revision=1,
                acknowledge_global=True, acknowledge_current_membership=True)
        self.assertEqual(before, store.data)
        self.assertEqual(1, recorder.enqueues)

    async def test_archive_save_failure_is_atomic_and_does_not_claim_disable_or_drop_pending(self):
        source, store, recorder, publisher = await self.pending_fixture()
        before = copy.deepcopy(store.data)
        events = len(source.events)
        store.fail = True
        with self.assertRaises(OSError):
            await publisher.async_disable('admin', expected_revision=1)
        self.assertEqual(before, store.data)
        self.assertEqual(events, len(source.events))
        self.assertEqual(1, recorder.enqueues)
        store.fail = False
        status = await publisher.async_status('admin')
        self.assertTrue(status['enabled'])
        self.assertTrue(status['pending'])
        self.assertEqual(0, status['unconfirmed_history'])

    async def test_disable_cas_conflict_preserves_intent_and_known_queued_data(self):
        source, store, recorder, publisher = await self.pending_fixture()
        before = copy.deepcopy(store.data)
        with self.assertRaisesRegex(ValueError, 'statistics_revision_conflict'):
            await publisher.async_disable('admin', expected_revision=0)
        self.assertEqual(before, store.data)
        self.assertEqual(1, recorder.enqueues)

    async def test_read_only_legacy_runtime_and_null_archive_migrations_preserve_pending(self):
        source, store, recorder, publisher = await self.pending_fixture()
        full = copy.deepcopy(store.data)
        for legacy in ({key: value for key, value in full.items() if key not in ('runtime', 'archived_intents')},
                       {key: value for key, value in full.items() if key != 'archived_intents'},
                       {**full, 'archived_intents': None}):
            before = copy.deepcopy(legacy)
            result = fixtures.ledger.validate_state(legacy, source.entry_id)
            self.assertEqual(before, legacy)
            self.assertEqual([], result['archived_intents'])
            self.assertEqual(full['pending'], result['pending'])
            self.assertEqual(full['series'], result['series'])

        # Legacy Disable had preserved pending while setting enabled=False.
        # Loading must not archive it; a second explicit Disable withdraws it.
        legacy = {key: copy.deepcopy(value) for key, value in full.items() if key != 'archived_intents'}
        legacy['share']['enabled'] = False
        legacy['revision'] += 1
        store.data = copy.deepcopy(legacy)
        replacement = fixtures.publisher.EnergyStatisticsPublisher(source.hass, source, store=store, recorder=recorder)
        status = await replacement.async_status('admin')
        self.assertFalse(status['enabled']); self.assertTrue(status['pending'])
        self.assertEqual(legacy, store.data)
        status = await replacement.async_disable('admin', expected_revision=legacy['revision'])
        self.assertFalse(status['pending']); self.assertEqual(1, status['unconfirmed_history'])

    async def test_archive_own_ids_epoch_uniqueness_and_existing_point_bounds_are_enforced(self):
        source, store, recorder, publisher = await self.pending_fixture()
        await publisher.async_disable('admin', expected_revision=1)
        good = copy.deepcopy(store.data)
        for defect in ('foreign_id', 'wrong_epoch', 'duplicate_epoch', 'too_many'):
            value = copy.deepcopy(good)
            if defect == 'foreign_id': value['archived_intents'][0]['series'] = ['foreign:cost']
            if defect == 'wrong_epoch': value['archived_intents'][0]['epoch'] = 'other'
            if defect == 'duplicate_epoch': value['archived_intents'] *= 2
            if defect == 'too_many': value['archived_intents'] *= fixtures.ledger.MAX_SERIES+1
            with self.subTest(defect=defect), self.assertRaises(ValueError):
                fixtures.ledger.validate_state(value, source.entry_id)
        with patch.object(fixtures.ledger, 'MAX_LEDGER_POINTS', 0):
            with self.assertRaises(ValueError):
                fixtures.ledger.validate_state(good, source.entry_id)
