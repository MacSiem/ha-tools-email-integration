"""Functional regressions for bounded snapshot admission and final context."""
import asyncio
import copy
import json
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.test_energy_snapshot import energy, classified, usage, at
from tests.test_energy_price_queries import coordinator, query
from tests.test_tariff_profile import load_websocket


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_snapshot_and_refresh_bound_waiters_before_lock(self):
        for route in ('refresh', 'shared', 'override'):
            with self.subTest(route=route):
                c = coordinator()
                await c._lock.acquire()
                def request():
                    if route == 'refresh':
                        return c.async_refresh(now=at('2026-10-18T12:34Z'))
                    return c.async_snapshot(now=at('2026-10-18T12:34Z'),
                        **({'profile': classified()} if route == 'override' else {}))
                tasks = []
                try:
                    for _ in range(energy.MAX_PENDING_CPU_JOBS):
                        tasks.append(asyncio.create_task(request()))
                    await asyncio.sleep(0)
                    extra = asyncio.create_task(request())
                    tasks.append(extra)
                    await asyncio.sleep(0)
                    self.assertTrue(extra.done(), 'Overflow must reject before waiting for the lock')
                    with self.assertRaisesRegex(RuntimeError, 'energy_pricing_busy'):
                        extra.result()
                    # Cancelling an admitted waiter must immediately reopen capacity.
                    tasks[0].cancel()
                    await asyncio.gather(tasks[0], return_exceptions=True)
                    replacement = asyncio.create_task(request())
                    tasks.append(replacement)
                    await asyncio.sleep(0)
                    self.assertFalse(replacement.done(), 'A released slot must admit a new waiter')
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    c._lock.release()
                async def provider(*args):
                    return usage()
                c.provider = provider
                with patch.object(energy, 'async_grid_sensors', return_value=[]), \
                     patch.object(energy, 'async_get_energy_metadata', return_value={}):
                    result = await request()
                self.assertEqual('ready', result['status'])

    async def test_override_capacity_is_held_through_override_cpu_without_double_counting(self):
        c = coordinator()
        release = asyncio.Event()
        entered = []
        async def build(state, measurement, **kwargs):
            if kwargs.get('profile_source') == 'override':
                entered.append(1)
                await release.wait()
            return energy.build_snapshot(state, measurement, **kwargs)
        async def provider(*args):
            return usage()
        c.provider = provider
        tasks = []
        with patch.object(energy, 'async_grid_sensors', return_value=[]), \
             patch.object(energy, 'async_get_energy_metadata', return_value={}), \
             patch.object(c, '_async_build_snapshot', side_effect=build):
            try:
                tasks = [asyncio.create_task(c.async_snapshot(profile=classified(),
                         now=at('2026-10-18T12:34Z'))) for _ in range(energy.MAX_PENDING_CPU_JOBS)]
                for _ in range(50):
                    await asyncio.sleep(0)
                self.assertEqual(energy.MAX_PENDING_CPU_JOBS, len(entered))
                extra = asyncio.create_task(c.async_snapshot(profile=classified(),
                                            now=at('2026-10-18T12:34Z')))
                tasks.append(extra)
                await asyncio.sleep(0)
                self.assertTrue(extra.done(), 'Override CPU wait still consumes admission')
                with self.assertRaisesRegex(RuntimeError, 'energy_pricing_busy'):
                    extra.result()
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_invalid_override_does_not_leak_admission(self):
        c = coordinator()
        error = energy.validate_tariff_profile.__globals__['TariffValidationError']
        for _ in range(energy.MAX_PENDING_CPU_JOBS * 2):
            with self.assertRaises(error):
                await c.async_snapshot(profile={'schema': 0})

    async def test_periodic_ticks_coalesce_while_a_refresh_is_pending(self):
        c = coordinator()
        release = asyncio.Event()
        calls = []
        async def blocked():
            calls.append(1)
            await release.wait()
        tasks = []
        with patch.object(c, 'async_refresh', side_effect=blocked):
            try:
                tasks = [asyncio.create_task(c._async_tick(None)) for _ in range(20)]
                await asyncio.sleep(0)
                self.assertEqual(1, len(calls), 'Only one timer refresh may remain pending')
            finally:
                release.set()
                await asyncio.gather(*tasks)
            await c._async_tick(None)
            self.assertEqual(2, len(calls), 'Completion must release the tick guard')


class CompiledCalendarCostTests(unittest.TestCase):
    def test_date_membership_cost_does_not_scale_with_the_number_of_dates(self):
        class CountedDate(str):
            comparisons = 0
            __hash__ = str.__hash__
            def __deepcopy__(self, memo):
                return self
            def __eq__(self, other):
                CountedDate.comparisons += 1
                return str.__eq__(self, other)
        p = classified()
        dates = [CountedDate((at('2000-01-01T00:00Z') + timedelta(days=i)).date().isoformat())
                 for i in range(3660)]
        p['rules'] = [{'zone': 'night', 'dates': dates, 'weekdays': [0, 1, 2, 3, 4, 5, 6],
                       'intervals': [{'from': '00:00', 'to': '24:00'}]}]
        before = json.dumps(p)
        model = energy.EnergyTariff(p)
        self.assertTrue(model.valid)
        CountedDate.comparisons = 0
        result = model.price_buckets(usage(hours=2)['hourly_buckets'])
        comparisons = CountedDate.comparisons
        self.assertTrue(result['complete'])
        self.assertEqual(2, result['usageCost'])
        self.assertLess(comparisons, 1000, 'Pricing must not linearly scan 3660 dates per checkpoint')
        self.assertEqual(before, json.dumps(p))
        self.assertEqual(before, json.dumps(model.profile))

    def test_unhashable_invalid_calendar_fields_still_report_validation_error(self):
        error = energy.validate_tariff_profile.__globals__['TariffValidationError']
        for field in ('dates', 'weekdays'):
            for invalid in ([{}], [[]], {'x': 1}):
                with self.subTest(field=field, invalid=invalid):
                    p = classified()
                    p['rules'][0][field] = invalid
                    with self.assertRaises(error):
                        energy.validate_tariff_profile(p)


class FinalCalendarContextTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mutation, override, historical=False):
        ws, _ = load_websocket()
        c = coordinator()
        hass = c.hass
        hass.data = {'ha_tools_email': {'energy_snapshot': c, 'storage': c.storage}}
        instant = at('2026-10-18T12:34Z')
        clock = [instant]
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
        response = (dict(schema=1, profile_source='override' if override else 'shared',
                         tariff_revision=None if override else 1, results=[])
                    if historical else energy.build_snapshot(
                        {'profile': classified(), 'revision': None if override else 1}, usage(),
                        now=instant, measurement_timezone='UTC',
                        profile_source='override' if override else 'shared'))
        async def answer(*args, **kwargs):
            return copy.deepcopy(response)
        c.async_snapshot = c.async_price_queries = answer
        user = types.SimpleNamespace(id='household', is_active=True)
        calls = []
        class Auth:
            async def async_get_user(self, uid):
                calls.append(uid)
                if len(calls) == 2:
                    await asyncio.sleep(0)
                    if mutation == 'zone':
                        hass.config.time_zone = 'Europe/Warsaw'
                    elif mutation == 'hour':
                        clock[0] = at('2026-10-18T13:00Z')
                    elif mutation == 'day':
                        clock[0] = at('2026-10-19T00:00Z')
                return user
        hass.auth = Auth()
        results, errors = [], []
        connection = types.SimpleNamespace(user=user, send_result=lambda *v: results.append(v),
                                           send_error=lambda *v: errors.append(v))
        msg = {'id': 1, **({'profile': classified()} if override else {})}
        if historical:
            msg['queries'] = [query()]
        with patch.object(energy, 'datetime', Clock):
            await getattr(ws, '_ws_energy_price_queries' if historical else '_ws_energy_snapshot')(
                hass, connection, msg)
        self.assertEqual(2, len(calls))
        return results, errors

    async def test_final_authorization_await_rejects_zone_hour_and_day_changes(self):
        for override in (False, True):
            for mutation in ('zone', 'hour', 'day'):
                with self.subTest(override=override, mutation=mutation):
                    results, errors = await self.exercise(mutation, override)
                    self.assertFalse(results)
                    self.assertEqual('energy_unavailable', errors[-1][1])

    async def test_current_snapshot_and_historical_prices_keep_their_valid_context(self):
        for override in (False, True):
            results, errors = await self.exercise(None, override)
            self.assertTrue(results)
            self.assertFalse(errors)
            for mutation in ('zone', 'hour', 'day'):
                results, errors = await self.exercise(mutation, override, historical=True)
                self.assertTrue(results)
                self.assertFalse(errors)
