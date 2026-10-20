"""Actual shared entity coordinator must consume the active historical tariff book."""
import copy
import types
import unittest
from unittest.mock import patch
from tests.test_energy_snapshot import energy, at, usage, classified
from tests.test_tariff_history import book, version


class BookSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def refresh(self, tariff_book, *, legacy=None, now='2026-10-18T12:34Z', zone='UTC', measured=None):
        state = {'book': tariff_book, 'legacy_profile': legacy, 'revision': 7}
        class Storage:
            current_tariff_revision = 7
            async def async_get_tariff_book(self):
                return copy.deepcopy(state)
            async def async_get_tariff(self):
                return {'profile': legacy, 'revision': 7}
        async def provider(hass, start, end):
            return copy.deepcopy(measured if measured is not None else usage())
        c = energy.EnergySnapshotCoordinator(types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone=zone)), Storage(), provider=provider)
        with patch.object(energy, 'async_grid_sensors', return_value=[]):
            result = await c.async_refresh(now=at(now))
        return c, result

    async def test_book_only_ignores_missing_invalid_or_conflicting_legacy_profile(self):
        p = classified(); p.pop('validFrom'); p.pop('validTo'); p['zones'][0]['variableRate'] = .2
        b = book([version(profile=p)])
        for legacy in (None, {'schema': 0}, {**classified(), 'currency': 'PLN'}):
            c, r = await self.refresh(b, legacy=legacy)
            self.assertEqual('EUR', r['currency'])
            self.assertEqual(1.2, r['current']['usage_rate'])
            self.assertEqual('expensive', r['current']['zone_class'])
            self.assertFalse(r['current']['cheap'])
            self.assertAlmostEqual(9.6, float(r['today']['usage_cost']))
            self.assertEqual('2026-10-18T22:00:00+00:00', r['next_cheap']['start'])
            self.assertEqual(7, r['tariff_revision'])
            self.assertEqual(r, c.data)
            self.assertNotIn('versions', str(r))

    async def test_day_cost_crosses_version_boundary_in_ha_calendar(self):
        a = version('2026-01-01', 1, 'a', valid_to='2026-10-19')
        b = version('2026-10-19', 2, 'b')
        measured = usage('2026-10-18T22:00Z', 12)
        _, r = await self.refresh(book([a, b]), now='2026-10-19T10:34Z', zone='Europe/Warsaw', measured=measured)
        self.assertEqual(2, r['current']['usage_rate'])
        self.assertEqual(22, float(r['today']['usage_cost']))
        self.assertEqual('2026-10-18T22:00:00+00:00', r['today']['from'])

    async def test_next_cheap_follows_future_version_instead_of_current_profile(self):
        a = version(valid_to='2026-10-19'); a['profile']['zones'][0]['classification']='expensive'
        b = version('2026-10-19', .3, 'future'); b['profile']['zones'][0]['classification']='cheap'
        _, r = await self.refresh(book([a, b]))
        self.assertEqual('2026-10-19T00:00:00+00:00', r['next_cheap']['start'])

    async def test_legacy_combined_rate_does_not_add_distribution_twice(self):
        v = version(rate_semantics='legacy_combined'); v['profile']['zones'][0]['variableRate']=None
        _, r = await self.refresh(book([v]))
        self.assertEqual(1, r['current']['usage_rate'])
        self.assertEqual(12, float(r['today']['usage_cost']))

    async def test_invalid_or_unselected_book_never_falls_back_to_legacy(self):
        no_active=book(); no_active.pop('active_tariff_id')
        for b in ({'schema':0}, no_active):
            _, r = await self.refresh(b, legacy=classified())
            self.assertIsNone(r['current']['usage_rate'])
            self.assertIsNone(r['today']['usage_cost'])

    async def test_legacy_fallback_and_explicit_override_remain_isolated(self):
        _, old = await self.refresh(None, legacy=classified())
        self.assertEqual(1, old['current']['usage_rate'])
        c, shared = await self.refresh(book())
        alternate=classified(); alternate['currency']='PLN'
        with patch.object(energy,'async_grid_sensors',return_value=[]):
            override=await c.async_snapshot(profile=alternate,now=at('2026-10-18T12:34Z'))
        self.assertEqual('PLN', override['currency'])
        self.assertEqual('EUR', c.data['currency'])
        self.assertEqual(shared,c.data)

    async def test_incomplete_measurement_keeps_current_price_but_withholds_today(self):
        measured=usage();measured['status']='partial';measured['coverage']['complete']=False
        _, r=await self.refresh(book(),measured=measured)
        self.assertEqual(1,r['current']['usage_rate'])
        self.assertIsNone(r['today']['usage_cost'])

    async def test_zero_negative_prices_and_fixed_fees_do_not_change_usage_cost(self):
        for price in (0, -2):
            v = version(rate=price, monthly=900)
            _, r = await self.refresh(book([v]))
            self.assertEqual(price, r['current']['usage_rate'])
            self.assertEqual(price * 12, float(r['today']['usage_cost']))
            self.assertEqual(900, r['today']['fixed_monthly'])

    async def test_midnight_never_invents_complete_zero_usage(self):
        _, r=await self.refresh(book(),now='2026-10-18T00:00Z',measured=usage(hours=0))
        self.assertEqual(1,r['current']['usage_rate'])
        self.assertIsNone(r['today']['usage_cost'])

    async def test_book_revision_change_during_cpu_cannot_publish(self):
        state={'book':book(),'legacy_profile':None,'revision':7}
        class Storage:
            current_tariff_revision=7
            async def async_get_tariff_book(self):return copy.deepcopy(state)
        async def provider(hass,start,end):return usage()
        async def executor(job):
            result=job()
            state['revision']=8
            return result
        c=energy.EnergySnapshotCoordinator(types.SimpleNamespace(
            config=types.SimpleNamespace(time_zone='UTC'),async_add_executor_job=executor),Storage(),provider=provider)
        with patch.object(energy,'async_grid_sensors',return_value=[]):
            with self.assertRaisesRegex(RuntimeError,'snapshot_changed'):
                await c.async_refresh(now=at('2026-10-18T12:34Z'))
        self.assertIsNone(c.data)
