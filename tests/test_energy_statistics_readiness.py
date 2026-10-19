"""Reject premature completion; exercise real cursor/publisher/runtime together.

Break caught: target unknown ACK advancing the cursor before source measurements.
Only source IO, physical Store and Recorder are substituted in these unit cases.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from importlib import import_module
import unittest

from tests import test_energy_statistics_publisher as fixtures
from tests import test_energy_statistics_runtime as runtime_fixture

publisher_module = fixtures.publisher
ledger = fixtures.ledger
UTC = timezone.utc
HOUR = datetime(2026, 10, 19, 12, tzinfo=UTC)


class ReadinessTest(unittest.IsolatedAsyncioTestCase):
    async def setup_case(self):
        runner = runtime_fixture.RuntimeTest()
        source, store, recorder, publisher, runtime = runner.fixture()
        runner.now = HOUR
        source.ready = False
        source.requested = []
        original = source.async_get_statistics_hours
        async def read(actor, *, hours=1, end=None):
            source.requested.append((actor, hours, end))
            facts = await original(actor, hours=hours, end=end)
            facts['measurement_ready'] = source.ready
            return facts
        source.async_get_statistics_hours = read
        await runner.enable(publisher)
        runtime.async_start()
        await runner.drain(runtime)
        return runner, source, store, recorder, publisher, runtime

    async def test_12h00_absent_then_12h00m10_ready_is_published_once_without_gesture(self):
        runner, src, store, rec, pub, rt = await self.setup_case()
        try:
            self.assertEqual(0, rec.enqueues)
            self.assertIsNone((await pub.async_status('admin'))['automation']['last_completed_hour'])
            self.assertIsNotNone(store.data['runtime'])
            first_end = src.requested[-1][2]
            self.assertEqual(HOUR, first_end)
            src.ready = True
            runner.now += timedelta(seconds=15)
            timer = next(t for t in runner.timers if t['active'])
            self.assertLessEqual(timer['at'], runner.now)
            timer['callback'](runner.now)
            await runner.drain(rt)
            self.assertEqual(HOUR, src.requested[-1][2])
            kitchen = next(sid for sid,row in pub._state['series'].items() if row['identity']['key']=='kitchen')
            self.assertEqual({int((HOUR-timedelta(hours=1)).timestamp()*1000): .3}, rec.rows[kitchen])
            count = rec.enqueues
            for _ in range(3): rt._tick(runner.now)
            await runner.drain(rt)
            self.assertEqual(count, rec.enqueues)
            self.assertEqual(HOUR.isoformat(), rt.public_status(pub._state)['last_completed_hour'])
        finally: rt.async_stop()

    async def test_waiting_hour_survives_crossing_13h_and_is_not_replaced_by_new_hour(self):
        runner, src, store, rec, pub, rt = await self.setup_case()
        try:
            runner.now = HOUR+timedelta(hours=1,seconds=15)
            src.ready = True
            rt._tick(runner.now); await runner.drain(rt)
            self.assertEqual(HOUR, src.requested[-1][2])
            self.assertEqual(HOUR.isoformat(), rt.public_status(pub._state)['last_completed_hour'])
            rt._tick(runner.now+timedelta(seconds=15)); await runner.drain(rt)
            self.assertEqual(HOUR+timedelta(hours=1), src.requested[-1][2])
            row = next(row for row in pub._state['series'].values() if row['identity']['key']=='kitchen')
            self.assertEqual([.3,.6], list(rec.rows[row['statistic_id']].values()))
            self.assertTrue(all(hours==1 for _,hours,_ in src.requested))
            self.assertTrue(all(actor=='admin' for actor,_,_ in src.requested))
        finally: rt.async_stop()

    async def test_cold_reload_restores_unready_cursor_and_exact_author_from_store(self):
        runner, src, store, rec, pub, rt = await self.setup_case()
        original = copy.deepcopy(store.data['runtime'])
        rt.async_stop()
        # A new lane dictionary represents an actual process restart, not memory reuse.
        src.hass.data['ha_tools_email'].pop('energy_statistics_runtime_lanes')
        replacement = publisher_module.EnergyStatisticsPublisher(src.hass, src, store=store, recorder=rec)
        fresh = runtime_fixture.runtime_module.EnergyStatisticsRuntime(src.hass, src.entry_id,
            publisher_factory=lambda: replacement, clock=lambda: runner.now,
            schedule=lambda callback, at: lambda: None)
        runner.now += timedelta(hours=1,seconds=20); src.ready=True
        fresh.async_start()
        try:
            await runner.drain(fresh)
            self.assertEqual(HOUR, src.requested[-1][2])
            self.assertEqual('admin', src.requested[-1][0])
            self.assertEqual(original['next_end']+3600000, store.data['runtime']['next_end'])
            self.assertEqual(1, rec.enqueues)
        finally: fresh.async_stop()

    async def test_price_or_zone_unknown_with_ready_measurements_does_not_retry_forever(self):
        runner, src, store, rec, pub, rt = await self.setup_case()
        try:
            src.ready=True
            for hour in src.facts['hours']:
                for cell in [*hour['groups']['areas'].values(),hour['groups']['unassigned'],hour['groups']['unmeasured'],hour['household'],hour['zone']]:
                    cell.update(usage_cost=None,complete=False,pricing_complete=False,reason='price_unavailable')
            runner.now += timedelta(seconds=15)
            rt._tick(runner.now); await runner.drain(rt)
            self.assertEqual(HOUR.isoformat(), rt.public_status(pub._state)['last_completed_hour'])
            reads=src.reads
            rt._tick(runner.now); await runner.drain(rt)
            self.assertEqual(reads,src.reads)
            self.assertTrue(all(v['cost'] is None for row in pub._state['series'].values() for v in row['points'].values()))
        finally: rt.async_stop()

    async def test_disable_and_revoke_stop_unready_hour_without_enqueue(self):
        for revoke in (True,False):
            runner, src, store, rec, pub, rt = await self.setup_case()
            try:
                if revoke: src.user.is_active=False
                else: await pub.async_disable('admin',expected_revision=1)
                runner.now += timedelta(seconds=15); src.ready=True
                rt._tick(runner.now); await runner.drain(rt)
                self.assertEqual(0,rec.enqueues)
                self.assertFalse(any(t['active'] for t in runner.timers))
            finally: rt.async_stop()

    async def test_expired_168_hour_backlog_is_durably_paused_without_source_read(self):
        runner, src, store, rec, pub, rt = await self.setup_case()
        try:
            runner.now += timedelta(hours=168)
            reads=src.reads
            rt._tick(runner.now); await runner.drain(rt)
            self.assertEqual(reads,src.reads)
            self.assertEqual(0,rec.enqueues)
            self.assertTrue(store.data['runtime']['paused'])
            self.assertEqual('paused',rt.public_status(pub._state)['state'])
            self.assertFalse(any(t['active'] for t in runner.timers))
            restored=publisher_module.EnergyStatisticsPublisher(src.hass,src,store=store,recorder=rec)
            result=await restored.async_refresh_runtime('admin',expected_share=await restored.async_runtime_context(),now=runner.now)
            self.assertEqual('paused',result['status']); self.assertEqual(reads,src.reads)
        finally: rt.async_stop()

    async def test_cursor_intent_write_failure_prevents_source_io(self):
        src,store,rec,pub=fixtures.PublisherTest().fixture()
        await fixtures.PublisherTest().enable(pub)
        reads=src.reads; store.fail=True
        with self.assertRaises(OSError):
            await pub.async_refresh_runtime('admin',expected_share=await pub.async_runtime_context(),now=HOUR)
        self.assertEqual(reads,src.reads); self.assertEqual(0,rec.enqueues)

    async def test_crash_after_target_enqueue_retries_same_hour_without_double_sum(self):
        runner,src,store,rec,pub,rt=await self.setup_case()
        try:
            src.ready=True; rec.fail_readback=True
            runner.now+=timedelta(seconds=15); rt._tick(runner.now); await runner.drain(rt)
            self.assertIsNotNone(store.data['pending'])
            self.assertEqual(int(HOUR.timestamp()*1000),store.data['runtime']['next_end'])
            rt.async_stop(); src.hass.data['ha_tools_email'].pop('energy_statistics_runtime_lanes')
            replacement=publisher_module.EnergyStatisticsPublisher(src.hass,src,store=store,recorder=rec)
            rec.fail_readback=False; runner.now+=timedelta(hours=1)
            result=await replacement.async_refresh_runtime('admin',expected_share=await replacement.async_runtime_context(),now=runner.now)
            self.assertEqual('completed',result['status']); self.assertIsNone(store.data['pending'])
            kitchen=next(sid for sid,row in replacement._state['series'].items() if row['identity']['key']=='kitchen')
            self.assertEqual([.3],list(rec.rows[kitchen].values()))
        finally: rt.async_stop()

    async def test_existing_analysis_backpressure_retries_durable_hour_without_reauthorization(self):
        runner,src,store,rec,pub,rt=await self.setup_case()
        original=src.async_get_statistics_hours
        async def busy(*args,**kwargs):raise RuntimeError('energy_analysis_busy')
        src.async_get_statistics_hours=busy
        try:
            runner.now+=timedelta(seconds=15);rt._tick(runner.now);await runner.drain(rt)
            self.assertIsNotNone(rt._consent)
            self.assertEqual(int(HOUR.timestamp()*1000),store.data['runtime']['next_end'])
            src.async_get_statistics_hours=original;src.ready=True
            runner.now+=timedelta(seconds=15);rt._tick(runner.now);await runner.drain(rt)
            self.assertEqual(1,rec.enqueues)
            self.assertEqual(HOUR.isoformat(),rt.public_status(pub._state)['last_completed_hour'])
        finally:rt.async_stop()

    def test_old_store_schema_is_migrated_without_invented_cursor_and_bad_cursor_rejected(self):
        old={'schema':1,'revision':0,'share':None,'series':{},'pending':None}
        migrated=ledger.validate_state(old,'synthetic')
        self.assertIsNone(migrated['runtime']); self.assertNotIn('runtime',old)
        bad=copy.deepcopy(migrated);bad['runtime']={'next_end':1}
        with self.assertRaises(ValueError):ledger.validate_state(bad,'synthetic')
