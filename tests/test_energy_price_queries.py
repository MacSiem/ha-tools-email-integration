"""Historical card pricing uses the existing tariff engine; never measurements."""
import asyncio
import copy
import types
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.test_energy_snapshot import energy, classified, usage, at, entity_modules
from tests.test_tariff_profile import load_websocket


class Storage:
    current_feature_access_revision = 0
    async def async_get_feature_access(self):
        return {'policy': {'schema': 1, 'features': {}}, 'revision': 0, 'status': 'ready'}
    def __init__(self, profile=None):
        self.state = {'profile': classified() if profile is None else profile, 'revision': 1}
    @property
    def current_tariff_revision(self):
        return self.state['revision']
    async def async_get_tariff(self):
        return copy.deepcopy(self.state)


def coordinator(storage=None, executor=asyncio.to_thread):
    return energy.EnergySnapshotCoordinator(types.SimpleNamespace(
        config=types.SimpleNamespace(time_zone='UTC'), async_add_executor_job=executor), storage or Storage())


def query(rows=None, **extra):
    return {'buckets': usage(hours=2)['hourly_buckets'] if rows is None else rows, **extra}


class PriceQueriesTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_preserves_order_model_rows_indexes_and_perquery_coverage(self):
        c = coordinator()
        rows = usage(hours=2)['hourly_buckets']
        queries = [query(rows), query([rows[1]], coverage={'from': rows[0]['from'], 'to': rows[1]['to']}),
                   query([{**rows[0], 'kwh': None}])]
        before = copy.deepcopy(queries)
        sentinel = c.data = {'entities': 'unchanged'}
        with patch.object(energy, 'async_grid_sensors', side_effect=AssertionError('no Recorder measurements')), \
             patch.object(energy, 'async_get_energy_metadata', side_effect=AssertionError('no Recorder metadata')), \
             patch.object(energy, 'async_get_energy_usage', side_effect=AssertionError('no Recorder history')):
            result = await c.async_price_queries(queries)
        self.assertEqual((1, 1, 'shared'), (result['schema'], result['tariff_revision'], result['profile_source']))
        expected = energy.EnergyTariff(classified())
        for q, actual in zip(queries, result['results']):
            self.assertEqual(expected.price_buckets(q['buckets'], q.get('coverage')), actual)
        self.assertEqual([True, False, False], [r['complete'] for r in result['results']])
        self.assertEqual([0, 1], [r['index'] for r in result['results'][0]['rows']])
        self.assertEqual(queries, before)
        self.assertIs(c.data, sentinel)

    async def test_1500_hour_buckets_in_disjoint_queries_are_supported(self):
        start = at('2026-01-01T00:00Z')
        rows = [{'from': (start + timedelta(hours=i)).isoformat(),
                 'to': (start + timedelta(hours=i + 1)).isoformat(), 'kwh': 1} for i in range(1500)]
        c = coordinator()
        result = await c.async_price_queries([query(rows[:744]), query(rows[744:])])
        self.assertEqual([744, 756], [len(r['rows']) for r in result['results']])
        self.assertTrue(all(r['complete'] for r in result['results']))

    async def test_ephemeral_null_unknown_fees_mixed_buckets_and_private_clock(self):
        c = coordinator()
        p = classified()
        p['currency'] = 'PLN'
        p['clock']['SMTP_PRIVATE'] = 'PRIVATE'
        p['PRIVATE'] = 'PRIVATE'
        p['zones'][1]['variableRate'] = None
        result = await c.async_price_queries([query()], profile=p)
        self.assertEqual((None, 'override', 'PLN'), (result['tariff_revision'], result['profile_source'], result['results'][0]['currency']))
        self.assertIsNone(result['results'][0]['usageCost'])
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual({'mode': 'civil'}, result['results'][0]['clock'])
        self.assertEqual('EUR', c.storage.state['profile']['currency'])
        self.assertIsNone(c.data)
        cleared = await c.async_price_queries([query()], profile=None)
        self.assertFalse(cleared['results'][0]['complete'])
        self.assertIsNone(cleared['results'][0]['usageCost'])
        p = classified()
        p['rules'] = [{'zone': 'night', 'intervals': [{'from': '00:30', 'to': '01:00'}]}]
        result = await c.async_price_queries([query()], profile=p)
        self.assertIn('mixed_rate_bucket', [v['reason'] for v in result['results'][0]['issues']])

    async def test_batch_size_json_and_weighted_complexity_are_bounded(self):
        c = coordinator()
        for queries in ([query()] * 65, [query([{}] * 2049)], [query([{'extra': 'x' * 524288}])],
                        [query([{'from': 0, 'to': 86400000, 'kwh': 1}] * 100)]):
            with self.subTest(case=len(str(queries))):
                with self.assertRaises(ValueError):
                    await c.async_price_queries(queries)
        p = classified()
        p['rules'] = [{'zone': 'day', 'intervals': [{'from': '00:00', 'to': '24:00'}]}] * 512
        with self.assertRaises(ValueError):
            await c.async_price_queries([query(usage(hours=100)['hourly_buckets'])], profile=p)

    async def test_shared_revision_change_and_unload_during_cpu_discard_result(self):
        for mutation in ('revision', 'stop'):
            entered, release = asyncio.Event(), asyncio.Event()
            async def executor(fn):
                entered.set()
                await release.wait()
                return fn()
            c = coordinator(executor=executor)
            task = asyncio.create_task(c.async_price_queries([query()]))
            await asyncio.wait_for(entered.wait(), timeout=1)
            if mutation == 'revision':
                c.storage.state['revision'] = 2
            else:
                c.async_stop()
            release.set()
            with self.assertRaises(RuntimeError):
                await task
            self.assertIsNone(c.data)

    async def test_price_queries_and_snapshot_cpu_share_one_lane_and_bounded_waiters(self):
        active, maximum = 0, 0
        entered, release = asyncio.Event(), asyncio.Event()
        async def executor(fn):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            try:
                return fn()
            finally:
                active -= 1
        c = coordinator(executor=executor)
        first = asyncio.create_task(c._async_build_snapshot(c.storage.state, usage(),
            now=at('2026-10-18T12:34Z'), measurement_timezone='UTC'))
        await asyncio.wait_for(entered.wait(), timeout=1)
        tasks = [asyncio.create_task(c.async_price_queries([query()])) for _ in range(4)]
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError):
            await c.async_price_queries([query()])
        release.set()
        await first
        await asyncio.gather(*tasks)
        self.assertEqual(1, maximum)

    async def test_cpu_lane_is_retained_across_coordinator_replacement(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def executor(fn):
            calls.append(1)
            entered.set()
            await release.wait()
            return fn()
        hass = types.SimpleNamespace(data={}, config=types.SimpleNamespace(time_zone='UTC'),
                                     async_add_executor_job=executor)
        first = energy.EnergySnapshotCoordinator(hass, Storage())
        a = asyncio.create_task(first._async_run_cpu(lambda: 1))
        await asyncio.wait_for(entered.wait(), timeout=1)
        first.async_stop()
        second = energy.EnergySnapshotCoordinator(hass, Storage())
        b = asyncio.create_task(second._async_run_cpu(lambda: 2))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(1, len(calls))
        finally:
            release.set()
            await asyncio.gather(a, b, return_exceptions=True)

    async def test_cancelled_request_keeps_cpu_lane_until_its_thread_really_finishes(self):
        import threading
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        calls = []
        def blocked():
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=2):
                raise RuntimeError('test_release_missing')
        c = coordinator()
        a = asyncio.create_task(c._async_run_cpu(blocked))
        await asyncio.wait_for(entered.wait(), timeout=1)
        a.cancel()
        b = asyncio.create_task(c._async_run_cpu(lambda: calls.append('second')))
        try:
            await asyncio.sleep(.02)
            self.assertFalse(calls)
        finally:
            release.set()
            await asyncio.gather(a, b, return_exceptions=True)

    async def test_dst_fold_buckets_preserve_absolute_frames_and_pricing(self):
        p = classified()
        p['timezone'] = 'Europe/Warsaw'
        rows = [{'from': '2026-10-25T02:00:00+02:00', 'to': '2026-10-25T02:00:00+01:00', 'kwh': 1},
                {'from': '2026-10-25T02:00:00+01:00', 'to': '2026-10-25T03:00:00+01:00', 'kwh': 1}]
        result = (await coordinator().async_price_queries([query(rows)], profile=p))['results'][0]
        self.assertTrue(result['complete'])
        self.assertEqual(.8, result['usageCost'])
        self.assertEqual(3600000, result['rows'][1]['from'] - result['rows'][0]['from'])
        self.assertEqual([0, 1], [r['index'] for r in result['rows']])



class FinalAuthGuardTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, handler, mutation, *, profile_missing=True):
        ws, _ = load_websocket()
        user = types.SimpleNamespace(id='user', is_active=True, is_admin=False, permissions=types.SimpleNamespace(access_all_entities=lambda _: True))
        c = coordinator()
        async def snapshot(**kwargs):
            await kwargs['guard'](['sensor.grid'])
            return {'safe': True}
        c.async_snapshot = snapshot
        calls = []
        results, errors = [], []
        hass = types.SimpleNamespace(data={'ha_tools_email': {'energy_snapshot': c, 'storage': c.storage}})
        class Auth:
            async def async_get_user(self, uid):
                calls.append(uid)
                if len(calls) == (3 if handler == '_ws_energy_snapshot' else 2):
                    await asyncio.sleep(0)
                    if mutation == 'stop':
                        c.async_stop()
                    elif mutation == 'replacement':
                        hass.data['ha_tools_email']['energy_snapshot'] = coordinator()
                    elif mutation == 'revision':
                        c.storage.state['revision'] += 1
                    elif mutation == 'revoke':
                        user.is_active = False
                return user
        hass.auth = Auth()
        conn = types.SimpleNamespace(user=user, send_result=lambda *v: results.append(v), send_error=lambda *v: errors.append(v))
        msg = {'id': 1, 'queries': [query()]}
        if not profile_missing:
            msg['profile'] = classified()
        await getattr(ws, handler)(hass, conn, msg)
        return results, errors

    async def test_snapshot_unload_or_replacement_in_last_auth_await_never_sends_result(self):
        for mutation in ('stop', 'replacement'):
            result, errors = await self.exercise('_ws_energy_snapshot', mutation)
            self.assertFalse(result)
            self.assertEqual('energy_unavailable', errors[-1][1])

    async def test_price_queries_recheck_auth_loaded_identity_and_shared_revision_after_last_await(self):
        for mutation in ('stop', 'replacement', 'revision', 'revoke'):
            result, errors = await self.exercise('_ws_energy_price_queries', mutation)
            self.assertFalse(result)
            self.assertIn(errors[-1][1], ('energy_unavailable', 'unauthorized'))
            self.assertNotIn('PRIVATE', str(errors))
        result, errors = await self.exercise('_ws_energy_price_queries', None, profile_missing=False)
        self.assertTrue(result)
        self.assertFalse(errors)

    async def test_price_handler_returns_fixed_errors_without_private_input_or_exception(self):
        ws, _ = load_websocket()
        user = types.SimpleNamespace(id='user', is_active=True, is_admin=False, permissions=types.SimpleNamespace(access_all_entities=lambda _: True))
        class Auth:
            async def async_get_user(self, uid):
                return user
        c = coordinator()
        errors, results = [], []
        hass = types.SimpleNamespace(auth=Auth(), data={'ha_tools_email': {'energy_snapshot': c, 'storage': c.storage}})
        conn = types.SimpleNamespace(user=user, send_result=lambda *v: results.append(v), send_error=lambda *v: errors.append(v))
        await ws._ws_energy_price_queries(hass, conn, {'id': 1, 'queries': [query([{'PRIVATE': 'x' * 524288}])]})
        self.assertEqual('invalid_energy_price_queries', errors[-1][1])
        # These pure fixtures load the same tariff.py under two synthetic package
        # names. Align exception identity as in the real single-package runtime.
        with patch.object(ws, 'TariffValidationError', energy.validate_tariff_profile.__globals__['TariffValidationError']):
            await ws._ws_energy_price_queries(hass, conn, {'id': 2, 'queries': [query()], 'profile': {'schema': 0, 'PRIVATE': True}})
        self.assertEqual('invalid_tariff_profile', errors[-1][1])
        async def fail(*args, **kwargs):
            raise OSError('SMTP_PRIVATE_PATH')
        c.async_price_queries = fail
        await ws._ws_energy_price_queries(hass, conn, {'id': 3, 'queries': [query()]})
        self.assertEqual('energy_unavailable', errors[-1][1])
        self.assertFalse(results)
        self.assertNotIn('PRIVATE', str(errors))

    async def test_daily_monetary_sensor_has_total_and_price_remains_measurement(self):
        sensor, _ = entity_modules()
        c = coordinator()
        entity = sensor.EnergySensor(c, types.SimpleNamespace(entry_id='id'), 'today_cost', 'today', 'usage_cost')
        self.assertEqual('monetary', entity.device_class)
        self.assertEqual('total', entity.state_class)
        price = sensor.EnergySensor(c, types.SimpleNamespace(entry_id='id'), 'price', 'current', 'usage_rate')
        self.assertEqual('measurement', price.state_class)
        self.assertIsNone(price.device_class)
