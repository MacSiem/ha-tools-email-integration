"""Internal producer uses original source/actor/CPU fences with native admin."""
from copy import deepcopy
from tests.test_energy_analysis_service import Harness
import unittest


class StatisticsServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_completed_hour_and_168_hour_producers_keep_public_cache_separate(self):
        h = Harness(); h.user.is_admin = True
        with h.patches():
            for hours in (1, 168):
                result = await h.subject.async_get_statistics_hours('admin', hours=hours)
                self.assertEqual(hours, len(result['hours']))
                self.assertEqual('statistics', result['context']['access_purpose'])
                self.assertEqual('current_snapshot', result['membership_mode'])
                self.assertEqual(['sensor.grid'], result['grid_source_ids'])
                self.assertTrue(await h.subject.async_validate_statistics_hours(result, 'admin'))
                self.assertIsNone(h.subject._cache)
                self.assertTrue(all((end-start).total_seconds() <= 168*3600 for _, start, end in h.queries))
    async def test_nonadmin_and_report_actor_cannot_start_source_io(self):
        h = Harness()
        with h.patches():
            with self.assertRaises(PermissionError): await h.subject.async_get_statistics_hours('household')
            with self.assertRaises(ValueError): await h.subject.async_get_statistics_hours(object())
        self.assertEqual([], h.queries); self.assertEqual([], h.registry_ids)
    async def test_revocation_during_source_io_aborts_and_cannot_be_revalidated(self):
        h = Harness(); h.user.is_admin = True
        async def revoke(*_): h.user.is_admin = False
        h.on_read = revoke
        with h.patches():
            with self.assertRaises(PermissionError): await h.subject.async_get_statistics_hours('admin')
        self.assertIsNone(h.subject._cache)
    async def test_context_role_source_and_unload_changes_after_return_are_rejected(self):
        for change in ('user', 'source', 'units', 'tariff', 'policy', 'registry', 'stop', 'actor'):
            h = Harness(); h.user.is_admin = True
            with h.patches():
                facts = await h.subject.async_get_statistics_hours('admin')
                if change == 'user': h.user.is_admin = False
                elif change == 'source': h.manager.data['device_consumption'] = []
                elif change == 'units': h.metadata['sensor.grid']['unit_of_measurement'] = 'Wh'
                elif change == 'tariff': h.storage.current_tariff_revision += 1
                elif change == 'policy': h.storage.current_feature_access_revision += 1
                elif change == 'registry': h.registry.revision += 1
                elif change == 'stop': h.subject.async_stop()
                with self.assertRaises((PermissionError, RuntimeError)):
                    await h.subject.async_validate_statistics_hours(facts, 'other' if change == 'actor' else 'admin')
    async def test_request_purpose_limits_and_default_analysis_do_not_enable_projection(self):
        h = Harness(); h.user.is_admin = True
        with h.patches():
            for hours in (0, 2, 169, True):
                with self.assertRaises(ValueError): await h.subject.async_get_statistics_hours('admin', hours=hours)
            with self.assertRaises(ValueError):
                await h.subject.async_get_analysis('day', 'admin', purpose='statistics')
            self.assertEqual([], h.queries)
            ordinary = await h.subject.async_get_analysis('day', 'admin')
            self.assertNotIn('statistics_hours', ordinary)
            self.assertEqual('analysis', ordinary['context']['access_purpose'])

    async def test_service_stop_removes_and_fences_only_its_own_publisher(self):
        from types import SimpleNamespace
        h = Harness(); stopped = []
        subject = SimpleNamespace(service=h.subject, stop=lambda: stopped.append(True))
        h.hass.data['ha_tools_email']['energy_statistics'] = subject
        h.subject.async_stop()
        self.assertEqual([True], stopped)
        self.assertNotIn('energy_statistics', h.hass.data['ha_tools_email'])

    async def test_naive_clock_is_rejected_before_any_recorder_read(self):
        from datetime import datetime
        h = Harness(); h.user.is_admin = True
        h.subject._clock = lambda: datetime(2026, 1, 1, 2, 34)
        with h.patches():
            with self.assertRaisesRegex(ValueError, 'aware_statistics_clock_required'):
                await h.subject.async_get_statistics_hours('admin')
        self.assertEqual([], h.queries)
