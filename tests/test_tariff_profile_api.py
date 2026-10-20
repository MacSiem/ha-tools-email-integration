"""Additive wire endpoints execute real storage/auth/projection code."""
import asyncio
import copy
from datetime import datetime, timezone
import types
import unittest

from tests.test_tariff_book_websocket import BookWebsocketTest
from tests.test_tariff_history import book
from tests.test_tariff_profile import profile as legacy_profile
from tests.test_tariff_profiles_v2 import profile

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class TariffProfileApiTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        result = BookWebsocketTest.fixture(self)
        result[1].config = types.SimpleNamespace(currency='PLN', time_zone='Europe/Warsaw')
        return result

    async def test_empty_get_never_writes_and_set_roundtrip_preserves_fee_rows(self):
        ws, hass, storage, lane, conn, results, errors = self.fixture()
        await ws._ws_get_tariff_profile(hass, conn, {'id': 1})
        self.assertEqual('empty', results[-1][1]['status'])
        self.assertEqual([], storage._store.saved)
        p = profile()
        p['fixed'].append({'id': 'własna/opłata', 'label': 'Własna opłata', 'monthly': 2.25})
        await ws._ws_set_tariff_profile(hass, conn, {'id': 2, 'profile': p, 'expected_revision': 0})
        self.assertEqual([], errors)
        self.assertEqual(1, results[-1][1]['revision'])
        self.assertEqual(p['fixed'], results[-1][1]['profile']['fixed'])
        await ws._ws_get_tariff_profile(hass, conn, {'id': 3})
        self.assertEqual(p['fixed'], results[-1][1]['profile']['fixed'])
        self.assertEqual(1, len(results[-1][1]['tariff_profiles']))
        self.assertEqual(1, len(storage._store.saved))
        # Additive API never writes or clears the legacy schema-1 slot.
        self.assertIsNone((await storage.async_get_tariff())['profile'])

    async def test_new_dated_version_and_other_tariff_are_retained(self):
        _, _, s, _, _, _, _ = self.fixture()
        initial = await s.async_save_tariff_profile(profile(), expected_revision=0, now=NOW)
        await s.async_save_tariff_book(dict((await s.async_get_tariff_book())['book'],
            archive_metadata={'immutable_reports': ['synthetic']}), expected_revision=1, now=NOW)
        await s.async_save_tariff_profile(profile(name='Alternative'), expected_revision=2,
                                         tariff_id='alt', now=NOW)
        old = (await s.async_get_tariff_book())['book']
        new = await s.async_save_tariff_profile(profile(validFrom='2026-11-01', name='Nowa umowa'),
                                               expected_revision=3, now=NOW)
        current = (await s.async_get_tariff_book())['book']
        self.assertEqual(2, len(new['tariff_profiles']))
        self.assertEqual('2026-11-01', new['tariff_profiles'][0]['profile']['validTo'])
        self.assertEqual(old['tariffs'][1], current['tariffs'][1])
        self.assertEqual(old['archive_metadata'], current['archive_metadata'])
        self.assertEqual([], new['historical_changes'])
        selected = await s.async_get_tariff_profile(version_id=initial['version_id'])
        self.assertEqual(initial['profile']['name'], selected['profile']['name'])

    async def test_historical_ack_is_required_and_revision_is_shared_with_old_routes(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        await ws._ws_set_tariff_profile(hass, conn, {'id': 1, 'profile': profile(), 'expected_revision': 0})
        changed = copy.deepcopy(results[-1][1]['profile'])
        changed['zones'][0]['energyRate'] = .81
        await ws._ws_set_tariff_profile(hass, conn, {'id': 2, 'profile': changed, 'expected_revision': 1})
        self.assertEqual('historical_edit_ack_required', errors[-1][1])
        self.assertEqual(1, len(s._store.saved))
        await ws._ws_set_tariff_profile(hass, conn, {'id': 3, 'profile': changed, 'expected_revision': 1,
                                                  'historical_edit_ack': True})
        self.assertEqual(2, results[-1][1]['revision'])
        self.assertTrue(results[-1][1]['historical_changes'])
        await ws._ws_set_tariff_book(hass, conn, {'id': 4, 'book': book(), 'expected_revision': 1})
        self.assertEqual('tariff_revision_conflict', errors[-1][1])
        self.assertEqual(2, len(s._store.saved))

    async def test_legacy_read_requires_semantics_and_never_persists_migration(self):
        _, _, s, _, _, _, _ = self.fixture()
        p = legacy_profile()
        await s.async_save_tariff(p, expected_revision=0)
        state = await s.async_get_tariff_profile()
        self.assertEqual('migration_required', state['status'])
        self.assertEqual(['explicit_legacy_semantics_required'], state['migration_required'])
        preview = await s.async_get_tariff_profile(legacy_combined=False)
        self.assertEqual(2, preview['profile']['schema'])
        self.assertEqual('fixed', preview['profile']['fixed'][0]['id'])
        self.assertEqual(p['fixedMonthly'], preview['profile']['fixed'][0]['monthly'])
        self.assertTrue(preview['migration_preview'])
        self.assertEqual(1, len(s._store.saved))
        self.assertEqual(p, (await s.async_get_tariff())['profile'])
        self.assertIsNone((await s.async_get_tariff_book())['book'])

    async def test_schema1_explicit_import_keeps_source_and_old_profile_verbatim(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        p = legacy_profile()
        await s.async_save_tariff(p, expected_revision=0)
        await ws._ws_set_tariff_profile(hass, conn, {'id': 1, 'profile': p, 'expected_revision': 1})
        self.assertEqual('invalid_tariff_profile', errors[-1][1])
        self.assertEqual(1, len(s._store.saved))
        await ws._ws_set_tariff_profile(hass, conn, {'id': 2, 'profile': p, 'expected_revision': 1,
                                                  'legacy_combined': False})
        self.assertEqual(2, results[-1][1]['revision'])
        saved = (await s.async_get_tariff_book())['book']
        self.assertEqual(p, saved['source_schema1'])
        self.assertEqual(p, saved['tariffs'][0]['versions'][0]['source_schema1'])
        self.assertEqual(p, (await s.async_get_tariff())['profile'])

    async def test_schema1_new_tariff_import_does_not_overwrite_existing_user_history(self):
        _, _, s, _, _, _, _ = self.fixture()
        await s.async_save_tariff_profile(profile(), expected_revision=0, now=NOW)
        before = (await s.async_get_tariff_book())['book']
        await s.async_save_tariff_profile(legacy_profile(), expected_revision=1, now=NOW,
                                         tariff_id='imported', legacy_combined=False)
        after = (await s.async_get_tariff_book())['book']
        self.assertEqual(before['tariffs'][0], after['tariffs'][0])
        self.assertEqual('user', after['active_tariff_id'])
        self.assertEqual('legacy', after['tariffs'][1]['origin']['kind'])

    async def test_old_book_without_sidecar_reads_without_rewrite(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        await s.async_save_tariff_book(book(), expected_revision=0, now=NOW)
        saved = copy.deepcopy(s._store.saved)
        await ws._ws_get_tariff_profile(hass, conn, {'id': 1})
        self.assertEqual([], errors)
        self.assertEqual(2, results[-1][1]['profile']['schema'])
        self.assertEqual('components', results[-1][1]['profile']['rateSemantics'])
        self.assertEqual(saved, s._store.saved)

    async def test_read_policy_denies_all_additive_read_routes(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        await s.async_save_feature_access({'schema': 1, 'features': {
            'energy_tariff': {'read': {'mode': 'admin'}}}}, expected_revision=0)
        conn.user.is_admin = False
        for handler in (ws._ws_get_tariff_profile, ws._ws_list_tariff_templates):
            await handler(hass, conn, {'id': 1})
            self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], results)
        self.assertEqual(1, len(s._store.saved))

    async def test_execute_policy_allows_selected_user_and_blocks_household_default(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        conn.user.is_admin = False
        msg = {'id': 1, 'profile': profile(), 'expected_revision': 0}
        await ws._ws_set_tariff_profile(hass, conn, msg)
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], s._store.saved)
        await s.async_save_feature_access({'schema': 1, 'features': {
            'energy_tariff': {'execute': {'mode': 'selected_users', 'user_ids': ['synthetic']}}}},
            expected_revision=0)
        errors.clear()
        await ws._ws_set_tariff_profile(hass, conn, msg)
        self.assertEqual([], errors)
        self.assertEqual(1, results[-1][1]['revision'])

    async def test_authorization_revoked_during_projection_never_writes_or_returns_data(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        lane.hook = lambda: setattr(hass.auth.current, 'is_admin', False)
        await ws._ws_set_tariff_profile(hass, conn, {'id': 1, 'profile': profile(), 'expected_revision': 0})
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], s._store.saved)
        self.assertEqual([], results)
        lane.hook = lambda: setattr(hass.auth.current, 'is_active', False)
        await ws._ws_get_tariff_profile(hass, conn, {'id': 2})
        self.assertEqual('unauthorized', errors[-1][1])
        self.assertEqual([], results)

    async def test_lifecycle_replaced_during_validation_never_writes(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        lane.hook = lambda: hass.data['ha_tools_email'].update(energy_snapshot=object())
        await ws._ws_set_tariff_profile(hass, conn, {'id': 1, 'profile': profile(), 'expected_revision': 0})
        self.assertEqual([], results)
        self.assertEqual([], s._store.saved)
        self.assertEqual('tariff_profile_save_failed', errors[-1][1])

    async def test_failed_confirmed_write_does_not_publish_revision(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        s._store.swallowed_failure = True
        await ws._ws_set_tariff_profile(hass, conn, {'id': 1, 'profile': profile(), 'expected_revision': 0})
        self.assertEqual('tariff_profile_save_failed', errors[-1][1])
        self.assertEqual([], results)
        self.assertEqual([], s._store.saved)
        self.assertEqual(0, s.current_tariff_revision)
        self.assertIsNone((await s.async_get_tariff_book())['book'])

    async def test_two_concurrent_editors_cannot_lose_history(self):
        _, _, s, _, _, _, _ = self.fixture()
        results = await asyncio.gather(
            s.async_save_tariff_profile(profile(name='A'), expected_revision=0, now=NOW),
            s.async_save_tariff_profile(profile(name='B'), expected_revision=0, now=NOW),
            return_exceptions=True)
        self.assertEqual(1, sum(isinstance(item, dict) for item in results))
        self.assertEqual(1, len(s._store.saved))
        self.assertEqual(1, s.current_tariff_revision)

    async def test_templates_use_home_currency_and_explicit_holiday_dates_with_null_prices(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        await ws._ws_list_tariff_templates(hass, conn, {'id': 1, 'holiday_dates': ['2026-11-11']})
        self.assertEqual([], errors)
        result = results[-1][1]
        self.assertEqual('PLN', result['currency'])
        self.assertEqual('Europe/Warsaw', result['timezone'])
        self.assertEqual(['G11', 'G12', 'G12w'], [row['id'] for row in result['templates']])
        for row in result['templates']:
            self.assertTrue(all(z['energyRate'] is None for z in row['profile']['zones']))
        self.assertEqual(['2026-11-11'], result['templates'][2]['profile']['exceptions'][0]['dates'])
        self.assertEqual([], s._store.saved)
        await ws._ws_list_tariff_templates(hass, conn, {'id': 2, 'holiday_dates': ['PRIVATE invalid']})
        self.assertEqual('invalid_tariff_template_request', errors[-1][1])
        self.assertNotIn('PRIVATE', repr(errors))

    async def test_policy_revision_changed_after_read_drops_the_result(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        original = s.async_get_tariff_profile
        async def read(**kwargs):
            result = await original(**kwargs)
            await s.async_save_feature_access({'schema': 1, 'features': {
                'energy_tariff': {'read': {'mode': 'admin'}}}}, expected_revision=0)
            return result
        s.async_get_tariff_profile = read
        await ws._ws_get_tariff_profile(hass, conn, {'id': 1})
        self.assertEqual([], results)
        self.assertEqual('unauthorized', errors[-1][1])

    async def test_stale_read_revision_is_fenced_after_projection(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        original = s.async_get_tariff_profile
        async def read(**kwargs):
            result = await original(**kwargs)
            await s.async_save_tariff_profile(profile(), expected_revision=0, now=NOW)
            return result
        s.async_get_tariff_profile = read
        await ws._ws_get_tariff_profile(hass, conn, {'id': 1})
        self.assertEqual([], results)
        self.assertEqual('tariff_revision_conflict', errors[-1][1])
        self.assertEqual(1, len(s._store.saved))

    async def test_old_endpoints_keep_schema1_payload_and_shared_revision(self):
        ws, hass, s, lane, conn, results, errors = self.fixture()
        p = legacy_profile()
        await ws._ws_set_tariff(hass, conn, {'id': 1, 'profile': p, 'expected_revision': 0})
        await ws._ws_get_tariff(hass, conn, {'id': 2})
        self.assertEqual(p, results[-1][1]['profile'])
        self.assertEqual(1, results[-1][1]['revision'])
        self.assertEqual([], errors)


if __name__ == '__main__':
    unittest.main()
