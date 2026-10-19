"""Consent, durable intent and correction semantics on real producer facts."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
import unittest

from tests.test_energy_analysis_service import service
from tests import test_energy_statistics_hours as hours_fixture
from tests.test_tariff_history import book, version

publisher = import_module(service.__package__ + '.energy_statistics_publisher')
ledger = import_module(service.__package__ + '.energy_statistics_ledger')
adapter = import_module(service.__package__ + '.energy_statistics_recorder')


class MemoryStore:
    def __init__(self):
        self.data = None
        self.fail = False
    async def async_load(self):
        return copy.deepcopy(self.data)
    async def async_save(self, value):
        if getattr(self, 'write_started', None) is not None:
            self.write_started.set()
            await self.write_release.wait()
        if self.fail:
            raise OSError('synthetic disk failure')
        self.data = copy.deepcopy(value)


class Recorder:
    def __init__(self):
        self.rows, self.metadata = {}, {}
        self.enqueues = 0
        self.fail_readback = False
        self.on_metadata = None
    async def async_metadata(self, ids):
        if self.on_metadata:
            self.on_metadata()
        return {sid: copy.deepcopy(self.metadata[sid]) for sid in ids if sid in self.metadata}
    def enqueue(self, plan):
        self.enqueues += 1
        for item in plan:
            sid = item['statistic_id']
            if item['clear']:
                self.rows.pop(sid, None)
                self.metadata.pop(sid, None)
            self.metadata[sid] = adapter.metadata_for(item['record'], None)
            self.rows.setdefault(sid, {}).update({a: float(Decimal(b)) for a, b in item['rows']})
    async def async_verify(self, plan):
        if self.fail_readback:
            raise RuntimeError('synthetic uncertain readback')
        return all(self.rows.get(item['statistic_id'], {}) == {a: float(Decimal(b)) for a, b in item['rows']}
                   for item in plan)


class Source:
    def __init__(self, hours=1):
        self.running, self.valid, self.reads = True, True, 0
        self.fixture = hours_fixture.StatisticsHoursTest()
        self.facts = self.fixture.facts(self.fixture.builder(hours))
        self.facts['context'].update(normalization_signature='normal', feature_access_revision=0,
            source_signature='source', measurement_timezone='UTC', service_generation='live',
            request_kind='statistics', actor_signature='server')
        self.facts['publication_policy_signature'] = 'policy'
        self.facts['measurement_ready'] = True
        self.facts['area_labels'] = {'kitchen': 'Kitchen', 'office': 'Office'}
        self.entry_id = 'synthetic'
        self.user = SimpleNamespace(is_admin=True, is_active=True)
        async def user(_): return self.user
        async def cpu(job): return job()
        self.events = []
        self.hass = SimpleNamespace(data={}, auth=SimpleNamespace(async_get_user=user),
            bus=SimpleNamespace(async_fire=lambda event, data: self.events.append((event, data))))
        self.coordinator = SimpleNamespace(_async_run_cpu=cpu)
    def _check_running(self):
        if not self.running: raise RuntimeError('unloaded')
    async def async_get_statistics_hours(self, user_id, *, hours=1, end=None):
        self._check_running(); self.reads += 1
        facts = copy.deepcopy(self.facts)
        if end is not None:
            offset = int(end.timestamp()*1000)-facts['hours'][-1]['to']
            for hour in facts['hours']:
                hour['from'] += offset; hour['to'] += offset
        return facts
    async def async_validate_statistics_hours(self, facts, user_id):
        return self.valid and self.running


class PublisherTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self, hours=1):
        src, store, rec = Source(hours), MemoryStore(), Recorder()
        subject = publisher.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        return src, store, rec, subject
    async def enable(self, subject):
        prepared = await subject.async_prepare('admin')
        return await subject.async_enable('admin', prepared['proposal'], expected_revision=prepared['revision'],
            acknowledge_global=True, acknowledge_current_membership=True)
    async def test_default_disabled_never_reads_sources_or_recorder(self):
        src, store, rec, p = self.fixture()
        result = await p.async_refresh('admin')
        self.assertFalse(result['enabled']); self.assertEqual(0, src.reads); self.assertEqual(0, rec.enqueues)
    async def test_native_admin_and_both_explicit_acknowledgements_are_required(self):
        src, store, rec, p = self.fixture()
        src.user.is_admin = False
        with self.assertRaises(PermissionError): await p.async_prepare('admin')
        self.assertEqual(0, src.reads)
        src.user.is_admin = True
        prepared = await p.async_prepare('admin')
        for global_ack, membership_ack in ((False, True), (True, False), (1, True)):
            with self.assertRaises(ValueError):
                await p.async_enable('admin', prepared['proposal'], expected_revision=0,
                    acknowledge_global=global_ack, acknowledge_current_membership=membership_ack)
        self.assertIsNone(store.data)
    async def test_proposal_binds_exact_dataset_and_revision(self):
        src, store, rec, p = self.fixture()
        prepared = await p.async_prepare('admin'); proposal = prepared['proposal']
        for key in ('source_signature', 'normalization_signature', 'currency', 'policy_signature'):
            changed = copy.deepcopy(proposal); changed[key] = 'untrusted'
            with self.assertRaises(ValueError):
                await p.async_enable('admin', changed, expected_revision=0, acknowledge_global=True,
                    acknowledge_current_membership=True)
        await self.enable(p)
        with self.assertRaises(ValueError): await p.async_disable('admin', expected_revision=0)
        self.assertEqual('all_authenticated_ha_users', (await p.async_status('admin'))['reader_scope'])
    async def test_real_fact_amounts_and_stable_idempotent_restart(self):
        src, store, rec, p = self.fixture(); await self.enable(p)
        await p.async_refresh('admin'); enqueues = rec.enqueues
        status = await p.async_status('admin'); before = copy.deepcopy(rec.rows)
        await p.async_refresh('admin')
        self.assertEqual(enqueues, rec.enqueues)
        replacement = publisher.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        await replacement.async_refresh('admin')
        self.assertEqual(enqueues, rec.enqueues); self.assertEqual(before, rec.rows)
        self.assertTrue(all(':energy_' in x['statistic_id'] for x in status['catalog']))
        self.assertTrue(all('Kitchen' not in x['statistic_id'] for x in status['catalog']))
    async def test_late_revision_rewrites_every_later_prefix_without_double_add(self):
        src, store, rec, p = self.fixture(24); await self.enable(p)
        await p.async_refresh('admin', backfill=True, acknowledge_current_membership=True)
        state = store.data
        kitchen = next(sid for sid, row in state['series'].items() if row['identity']['key'] == 'kitchen')
        stamps = sorted(rec.rows[kitchen]); first = src.facts['hours'][0]
        first['groups']['areas']['kitchen']['usage_cost'] = '-1'
        await p.async_refresh('admin', backfill=True, acknowledge_current_membership=True)
        self.assertEqual(-1, rec.rows[kitchen][stamps[0]])
        self.assertAlmostEqual(-1 + 23*.3, rec.rows[kitchen][stamps[-1]])
        saved = copy.deepcopy(rec.rows); await p.async_refresh('admin', backfill=True, acknowledge_current_membership=True)
        self.assertEqual(saved, rec.rows)
    async def test_known_to_unknown_clears_only_owned_series_then_rebuilds_gaps(self):
        src, store, rec, p = self.fixture(24); await self.enable(p)
        await p.async_refresh('admin', backfill=True, acknowledge_current_membership=True)
        kitchen = next(sid for sid, row in store.data['series'].items() if row['identity']['key'] == 'kitchen')
        first_stamp = src.facts['hours'][0]['from']
        row = src.facts['hours'][0]['groups']['areas']['kitchen']; row.update(usage_cost=None, complete=False)
        rec.rows['foreign:keep'] = {1: 99}
        await p.async_refresh('admin', backfill=True, acknowledge_current_membership=True)
        self.assertNotIn(first_stamp, rec.rows[kitchen]); self.assertEqual({1: 99}, rec.rows['foreign:keep'])
        self.assertAlmostEqual(23*.3, rec.rows[kitchen][max(rec.rows[kitchen])])
    async def test_durable_intent_precedes_enqueue_and_uncertain_replay_is_idempotent(self):
        src, store, rec, p = self.fixture(); await self.enable(p)
        store.fail = True
        with self.assertRaises(OSError): await p.async_refresh('admin')
        self.assertEqual(0, rec.enqueues)
        store.fail = False; rec.fail_readback = True
        with self.assertRaises(RuntimeError): await p.async_refresh('admin')
        self.assertIsNotNone(store.data['pending']); before = copy.deepcopy(rec.rows)
        rec.fail_readback = False
        p = publisher.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        await p.async_refresh('admin')
        self.assertIsNone(store.data['pending']); self.assertEqual(before, rec.rows)
    async def test_source_unit_policy_currency_or_actor_changes_stop_enqueue(self):
        for change in ('source_signature', 'normalization_signature', 'policy', 'currency', 'inactive', 'different_author'):
            src, store, rec, p = self.fixture(); await self.enable(p)
            if change in ('source_signature', 'normalization_signature'): src.facts['context'][change] = 'changed'
            elif change == 'policy': src.facts['publication_policy_signature'] = 'changed'
            elif change == 'currency': src.facts['hours'][-1]['groups']['currency'] = 'PLN'
            elif change == 'inactive': src.user.is_active = False
            with self.assertRaises((PermissionError, RuntimeError)):
                await p.async_refresh('other' if change == 'different_author' else 'admin')
            self.assertEqual(0, rec.enqueues)
    async def test_revocation_after_metadata_await_and_unload_are_fenced(self):
        for unload in (False, True):
            src, store, rec, p = self.fixture(); await self.enable(p)
            rec.on_metadata = lambda: setattr(src, 'running' if unload else 'valid', False)
            with self.assertRaises((PermissionError, RuntimeError)): await p.async_refresh('admin')
            self.assertEqual(0, rec.enqueues)
    async def test_disable_never_deletes_global_history_or_replays_pending(self):
        src, store, rec, p = self.fixture(); await self.enable(p); await p.async_refresh('admin')
        before = copy.deepcopy(rec.rows); count = rec.enqueues
        await p.async_disable('admin', expected_revision=1)
        self.assertFalse((await p.async_refresh('admin'))['enabled'])
        self.assertEqual(before, rec.rows); self.assertEqual(count, rec.enqueues)
    async def test_foreign_metadata_cannot_be_overwritten_or_cleared(self):
        src, store, rec, p = self.fixture(); await self.enable(p)
        rec.fail_readback = True
        with self.assertRaises(RuntimeError): await p.async_refresh('admin')
        sid = next(iter(rec.metadata)); rec.metadata[sid]['source'] = 'foreign'
        rec.fail_readback = False; count = rec.enqueues
        with self.assertRaises(PermissionError): await p.async_refresh('admin')
        self.assertEqual(count, rec.enqueues)
    async def test_backfill_acknowledges_current_snapshot_and_labels_keep_ids(self):
        src, store, rec, p = self.fixture(); await self.enable(p)
        with self.assertRaises(ValueError): await p.async_refresh('admin', backfill=True)
        await p.async_refresh('admin'); ids = set(rec.rows)
        src.facts['area_labels']['kitchen'] = 'Renamed'
        await p.async_refresh('admin')
        self.assertEqual(ids, set(rec.rows))
        self.assertEqual('current_snapshot', (await p.async_status('admin'))['membership_mode'])
    async def test_stored_ledger_corruption_never_becomes_foreign_recorder_operation(self):
        src, store, rec, p = self.fixture(); await self.enable(p); await p.async_refresh('admin')
        state = copy.deepcopy(store.data); sid = next(iter(state['series']))
        state['series']['foreign:owned'] = state['series'].pop(sid); store.data = state
        replacement = publisher.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        count = rec.enqueues
        with self.assertRaises(ValueError): await replacement.async_refresh('admin')
        self.assertEqual(count, rec.enqueues)

    async def test_zero_negative_unknown_and_absent_tariff_keep_cost_semantics(self):
        src, store, rec, p = self.fixture()
        b = book([version(rate=-.5)])
        b['tariffs'][0]['versions'][0]['profile']['allowNegativeEnergy'] = True
        facts = src.fixture.facts(src.fixture.builder(1, tariff=b), values=(100, 40, 0))
        facts['context'].update(src.facts['context'])
        facts['publication_policy_signature'] = src.facts['publication_policy_signature']
        facts['area_labels'] = src.facts['area_labels']
        src.facts = facts
        await self.enable(p)
        await p.async_refresh('admin')
        kitchen = next(sid for sid, row in store.data['series'].items() if row['identity']['key'] == 'kitchen')
        unassigned = next(sid for sid, row in store.data['series'].items() if row['identity']['kind'] == 'unassigned')
        self.assertEqual(-30, next(iter(rec.rows[kitchen].values())))
        self.assertEqual(0, next(iter(rec.rows[unassigned].values())))
        # Same explicitly shared dataset, but its tariff version now unknown:
        # invalidate prior facts rather than retaining old monetary rows.
        hour = src.facts['hours'][0]
        hour['groups'].update(currency=None, price_basis=None, tariff_id=None)
        for row in [*hour['groups']['areas'].values(), hour['groups']['unassigned'], hour['groups']['unmeasured']]:
            row.update(usage_cost=None, complete=False)
        hour['zone'].update(usage_cost=None, complete=False, zone_id=None, version_id=None)
        await p.async_refresh('admin')
        self.assertEqual({}, rec.rows[kitchen])
        self.assertEqual({}, rec.rows[unassigned])

    async def test_capacity_and_readback_failure_never_claim_persistence(self):
        from unittest.mock import patch
        src, store, rec, p = self.fixture(); await self.enable(p)
        with patch.object(ledger, 'MAX_LEDGER_POINTS', 1):
            with self.assertRaises(ValueError): await p.async_refresh('admin')
        self.assertEqual(0, rec.enqueues)
        rec.fail_readback = True
        with self.assertRaises(RuntimeError): await p.async_refresh('admin')
        self.assertTrue((await p.async_status('admin'))['pending'])

    async def test_explicit_noop_refresh_repairs_deleted_native_rows_from_ledger(self):
        src, store, rec, p = self.fixture(); await self.enable(p); await p.async_refresh('admin')
        before = copy.deepcopy(rec.rows); sid = next(iter(rec.rows))
        rec.rows.pop(sid); rec.metadata.pop(sid)
        replacement = publisher.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        await replacement.async_refresh('admin')
        self.assertEqual(before, rec.rows)
        self.assertIsNone(store.data['pending'])

    async def test_explicit_new_source_currency_epoch_preserves_old_units_and_ids(self):
        src, store, rec, p = self.fixture(); await self.enable(p); await p.async_refresh('admin')
        old_ids = set(rec.rows)
        src.facts['context']['source_signature'] = 'new-source-set'
        for hour in src.facts['hours']:
            hour['groups']['currency'] = 'PLN'; hour['zone']['currency'] = 'PLN'
        await self.enable(p); await p.async_refresh('admin')
        status = await p.async_status('admin')
        current = {row['statistic_id'] for row in status['catalog'] if row['current_dataset']}
        self.assertTrue(current); self.assertFalse(old_ids & current)
        self.assertTrue(old_ids <= set(rec.rows))
        self.assertEqual({'EUR'}, {rec.metadata[sid]['unit_of_measurement'] for sid in old_ids})
        self.assertEqual({'PLN'}, {rec.metadata[sid]['unit_of_measurement'] for sid in current})

    async def test_disabled_pending_is_archived_and_fresh_consent_uses_new_epoch(self):
        src, store, rec, p = self.fixture(); await self.enable(p)
        rec.fail_readback = True
        with self.assertRaises(RuntimeError): await p.async_refresh('admin')
        old = copy.deepcopy(store.data); old_rows = copy.deepcopy(rec.rows)
        await p.async_disable('admin', expected_revision=1)
        count, reads = rec.enqueues, src.reads
        await p.async_refresh('admin')
        self.assertEqual(count, rec.enqueues); self.assertEqual(reads, src.reads)
        self.assertIsNone(store.data['pending'])
        archive = copy.deepcopy(store.data['archived_intents'])
        self.assertEqual(old['pending'], {key: archive[0][key] for key in ('epoch', 'series', 'clear')})
        prepared = await p.async_prepare('admin')
        result = await p.async_enable('admin', prepared['proposal'], expected_revision=2,
            acknowledge_global=True, acknowledge_current_membership=True)
        self.assertNotEqual(old['share']['epoch'], result['active_epoch'])
        rec.fail_readback = False
        await p.async_refresh('admin')
        self.assertIsNone(store.data['pending'])
        self.assertEqual(archive, store.data['archived_intents'])
        self.assertEqual(old_rows, {sid: rec.rows[sid] for sid in old_rows})

    async def test_cancelled_durable_write_keeps_shared_reload_lane_until_complete(self):
        src, store, rec, p = self.fixture(); await self.enable(p)
        store.write_started, store.write_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(p.async_refresh('admin'))
        await store.write_started.wait()
        task.cancel()
        replacement = publisher.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        self.assertIs(p._lock, replacement._lock)
        self.assertTrue(replacement._lock.locked())
        store.write_release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(0, rec.enqueues)
        self.assertIsNotNone(store.data['pending'])
        store.write_started = None
        await replacement.async_refresh('admin')
        self.assertIsNone(store.data['pending'])

    async def test_stop_during_durable_write_prevents_enqueue_and_reload_recovers(self):
        src, store, rec, p = self.fixture(); await self.enable(p)
        store.write_started, store.write_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(p.async_refresh('admin'))
        await store.write_started.wait(); p.stop()
        replacement = publisher.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        self.assertIs(p._lock, replacement._lock)
        store.write_release.set()
        with self.assertRaises(RuntimeError): await task
        self.assertEqual(0, rec.enqueues)
        store.write_started = None
        await replacement.async_refresh('admin')
        self.assertIsNone(store.data['pending'])

    async def test_different_native_admin_can_explicitly_reauthorize_same_dataset(self):
        src, store, rec, p = self.fixture(); await self.enable(p); await p.async_refresh('admin')
        before, epoch = copy.deepcopy(rec.rows), store.data['share']['epoch']
        prepared = await p.async_prepare('other')
        await p.async_enable('other', prepared['proposal'], expected_revision=1,
            acknowledge_global=True, acknowledge_current_membership=True)
        self.assertEqual(epoch, store.data['share']['epoch'])
        self.assertEqual('other', store.data['share']['author'])
        with self.assertRaises(PermissionError): await p.async_refresh('admin')
        await p.async_refresh('other')
        self.assertEqual(before, rec.rows)


class MetadataAdapterTest(unittest.TestCase):
    def test_old_and_new_core_metadata_currency_is_exact_without_fx_class(self):
        record = {'identity': {'currency': 'PLN'}, 'name': 'Cost', 'statistic_id': 'ha_tools_email:energy_area_a'}
        old = adapter.metadata_for(record, None)
        self.assertIs(False, old['has_mean']); self.assertNotIn('mean_type', old)
        modern = adapter.metadata_for(record, SimpleNamespace(NONE=0))
        self.assertEqual(0, modern['mean_type']); self.assertIsNone(modern['unit_class'])
        self.assertNotIn('has_mean', modern); self.assertEqual('PLN', modern['unit_of_measurement'])
        intermediate = adapter.metadata_for(record, SimpleNamespace(NONE=0), supports_unit_class=False)
        self.assertNotIn('unit_class', intermediate)
        self.assertEqual(0, intermediate['mean_type'])
