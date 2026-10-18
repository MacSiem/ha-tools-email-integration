"""Patterns shares real service/provider/CPU lifecycle with calendar analysis."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import unittest
from tests.test_energy_analysis_service import Harness, service
from unittest.mock import AsyncMock, patch


class PatternsServiceTest(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        h=Harness(); h.book=None; h.now=datetime(2026,5,6,tzinfo=timezone.utc)
        return h

    async def test_grid_only_chunks_shared_cache_detachment_and_hash(self):
        h=self.harness()
        with h.patches():
            first=await h.subject.async_get_patterns(7,'user')
            self.assertEqual([('sensor.grid',)], [ids for ids,_,_ in h.queries])
            self.assertEqual([],h.registry_ids)
            self.assertEqual(Decimal(1680),Decimal(first['totals']['kwh']))
            original=copy.deepcopy(first)
            first['patterns']['hours'][0]['kwh']='999'
            again=await h.subject.async_get_patterns(7,'user')
            self.assertEqual(original,again); self.assertEqual(1,len(h.queries))
            payload=copy.deepcopy(again); sid=payload.pop('snapshot_id')
            self.assertEqual(sid,hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest())
            thirty=await h.subject.async_get_patterns(30,'user')
            self.assertEqual(6,len(h.queries)); self.assertEqual(Decimal(7200),Decimal(thirty['totals']['kwh']))
            self.assertTrue(all(0<(end-start).total_seconds()<=168*3600 for _,start,end in h.queries))
            self.assertEqual(thirty,h.subject._cache[2])
            self.assertTrue(await h.subject.async_validate_result(thirty,'user'))

    async def test_invalid_selection_feature_and_native_denial_precede_private_io(self):
        for mode in ('days','feature','native'):
            h=self.harness()
            if mode=='feature': h.policy={'schema':1,'features':{'energy_analysis':{'read':{'mode':'admin'}}}}
            if mode=='native':
                h.permissions.access_all_entities=lambda _:False
            with self.subTest(mode=mode),h.patches():
                if mode=='feature':
                    with self.assertRaises(PermissionError):
                        await h.subject.async_get_patterns(7,'user')
                else:
                    with self.assertRaises(ValueError if mode=='days' else PermissionError):
                        await h.subject.async_get_patterns(7.0 if mode=='days' else 7,'user')
                self.assertEqual([],h.queries);self.assertEqual([],h.registry_ids)

    async def test_hour_turn_and_revisions_expire_context_and_cache(self):
        h=self.harness()
        with h.patches():
            first=await h.subject.async_get_patterns(7,'user')
            h.now+=timedelta(hours=1)
            self.assertFalse(h.subject.context_is_current(first))
            second=await h.subject.async_get_patterns(7,'user')
            self.assertEqual(2,len(h.queries));self.assertNotEqual(first['snapshot_id'],second['snapshot_id'])
            h.storage.current_tariff_revision+=1
            self.assertFalse(await h.subject.async_validate_result(second,'user'))

    async def test_role_revision_unit_and_lifecycle_changes_after_io_fail_closed(self):
        for mode in ('role','revision','unit','lifecycle','hour'):
            h=self.harness()
            async def mutate(*args):
                if mode=='role':h.user.is_active=False
                elif mode=='revision':h.registry.revision+=1
                elif mode=='unit':h.metadata['sensor.grid']['unit_of_measurement']='Wh'
                elif mode=='hour':h.now+=timedelta(hours=1)
                else:h.subject.async_stop()
            h.on_read=mutate
            with self.subTest(mode=mode),h.patches():
                with self.assertRaises((PermissionError,RuntimeError)):
                    await h.subject.async_get_patterns(7,'user')
                self.assertIsNone(h.subject._cache)

    async def test_admission_includes_calendar_waiters_and_cancel_releases_slot(self):
        h=self.harness();entered,release=asyncio.Event(),asyncio.Event()
        async def hold(*args):entered.set();await release.wait()
        h.on_read=hold
        with h.patches():
            first=asyncio.create_task(h.subject.async_get_patterns(7,'user'))
            await entered.wait()
            waiting=[asyncio.create_task(h.subject.async_get_patterns(30,'user')),
                     asyncio.create_task(h.subject.async_get_analysis('day','user')),
                     asyncio.create_task(h.subject.async_get_patterns(7,'user'))]
            await asyncio.sleep(0)
            with self.assertRaisesRegex(RuntimeError,'energy_analysis_busy'):
                await h.subject.async_get_patterns(7,'user')
            for task in waiting:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
            release.set();await first
            self.assertEqual(0,h.subject._pending)

    async def test_cancelled_patterns_cpu_retains_actual_lane_until_completion(self):
        h=self.harness();entered,release=asyncio.Event(),asyncio.Event()
        async def executor(job):entered.set();await release.wait();return job()
        h.hass.async_add_executor_job=executor
        with h.patches():
            task=asyncio.create_task(h.subject.async_get_patterns(7,'user'))
            await entered.wait();task.cancel();await asyncio.sleep(0)
            self.assertFalse(task.done());self.assertTrue(h.coordinator._cpu_lock.locked())
            self.assertEqual(1,h.subject._pending)
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertFalse(h.coordinator._cpu_lock.locked());self.assertEqual(0,h.subject._pending)
            self.assertIsNone(h.subject._cache)

    async def test_cache_hit_reauthorizes_selected_user_and_normalized_units(self):
        h=self.harness()
        h.policy={'schema':1,'features':{'energy_analysis':{'read':{'mode':'selected_users','user_ids':['allowed']}}}}
        with h.patches():
            result=await h.subject.async_get_patterns(7,'allowed')
            count=len(h.queries)
            with self.assertRaises(PermissionError):await h.subject.async_get_patterns(7,'denied')
            self.assertEqual(count,len(h.queries))
            h.metadata['sensor.grid']['unit_of_measurement']='Wh'
            self.assertFalse(await h.subject.async_validate_result(result,'allowed'))
            self.assertIsNone(h.subject._cache)

    async def test_mutation_after_final_cpu_is_caught_before_cache(self):
        h=self.harness()
        def change(job):
            if getattr(getattr(job,'func',None),'__name__','')=='_finish_patterns':
                h.storage.current_feature_access_revision+=1
        h.on_cpu=change
        with h.patches():
            with self.assertRaises(RuntimeError):await h.subject.async_get_patterns(7,'user')
            self.assertIsNone(h.subject._cache)

    async def test_feature_denial_has_no_metadata_io(self):
        h=self.harness()
        h.policy={'schema':1,'features':{'energy_analysis':{'read':{'mode':'admin'}}}}
        with h.patches(),patch.object(service,'async_get_energy_metadata',AsyncMock()) as metadata:
            with self.assertRaises(PermissionError):await h.subject.async_get_patterns(7,'user')
            metadata.assert_not_awaited()
            self.assertEqual([],h.queries);self.assertEqual([],h.registry_ids)

    async def test_queued_request_recaptures_revision_after_previous_failure(self):
        h=self.harness();entered,release=asyncio.Event(),asyncio.Event()
        async def hold(*args):entered.set();await release.wait()
        h.on_read=hold
        with h.patches():
            first=asyncio.create_task(h.subject.async_get_patterns(7,'user'))
            await entered.wait()
            queued=asyncio.create_task(h.subject.async_get_patterns(30,'user'))
            await asyncio.sleep(0)
            h.registry.revision=3;release.set()
            with self.assertRaises(RuntimeError):await first
            result=await queued
            self.assertEqual(3,result['context']['registry_revision'])
            self.assertEqual(30,result['context']['patterns_days'])
            self.assertEqual(0,h.subject._pending)
