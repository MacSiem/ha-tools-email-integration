"""Native YAML text defaults cross the real composer/report lane; NOT_RUN here."""
from datetime import datetime, timezone
import importlib
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch

from tests import test_mail67_delivery as mail67

composer = mail67.composer
legacy = importlib.import_module(mail67.service.__package__ + '.energy_report')


class YamlText(str):
    """HA NodeStrClass primitive contract; no nontext coercion."""


class ComposerNativeDefaultsTest(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        h, sent = mail67.ActualDeliveryTest().fixture()
        h.now = datetime(2026, 10, 20, 21, 41, tzinfo=timezone.utc)
        h.hass.config.time_zone = YamlText('Europe/Warsaw')
        # Deterministic reproduction of a cache seeded by native YAML text,
        # without clearing or changing the process-wide ZoneInfo cache.
        display = {'language': 'en', 'zone': ZoneInfo.no_cache(h.hass.config.time_zone)}
        self.assertIs(type(display['zone'].key), YamlText)
        return h, sent, display

    async def compose(self, h, purpose='report_read', semantics='previous_completed_calendar_period', **extra):
        return await composer.async_build_report_payload(
            h.hass, h.storage, kind='energy_report', cadence='daily', now=h.now,
            actor='real-admin', purpose=purpose,
            period_semantics=semantics, **extra)

    async def test_trusted_yaml_zone_default_reaches_actual_calendar_service_and_render_for_both_purposes(self):
        for purpose in ('report_read', 'report_execute'):
            with self.subTest(purpose=purpose):
                h, sent, display = self.fixture()
                with h.patches(), patch.object(composer, '_ha_presentation', return_value=display), patch.object(
                        h.subject, 'async_prepare_report', wraps=h.subject.async_prepare_report) as prepare, patch.object(
                        legacy, 'render_energy_analysis', wraps=legacy.render_energy_analysis) as export:
                    result = await self.compose(h, purpose)
                    self.assertEqual(1, prepare.await_count)
                    self.assertIs(type(prepare.await_args.kwargs['timezone_name']), str)
                    self.assertEqual('Europe/Warsaw', prepare.await_args.kwargs['timezone_name'])
                    self.assertEqual(purpose, result['context']['access_purpose'])
                    self.assertEqual('report_window', result['period']['kind'])
                    self.assertEqual('2026-10-18T22:00:00+00:00', result['period']['start'])
                    self.assertEqual('2026-10-19T22:00:00+00:00', result['period']['end'])
                    self.assertTrue(h.subject.context_is_current(result))
                    self.assertTrue(await h.subject.async_validate_result(result, 'real-admin'))
                    self.assertIn('<!-- block:12 -->', result['html'])
                    self.assertEqual(export._mock_wraps(export.call_args.args[0],
                        language='en', timezone_name='Europe/Warsaw', hour_cycle='24')['csv'], result['csv'])
                    self.assertTrue(h.queries, 'must traverse real service/provider/collector, with Recorder IO fixture only')
                self.assertEqual([], sent)
                h.storage.async_append_attempt.assert_not_awaited()

    async def test_legacy_rolling_composer_default_crosses_the_same_boundary_without_model_optin(self):
        h, sent, display = self.fixture()
        with h.patches(), patch.object(composer, '_ha_presentation', return_value=display):
            result = await self.compose(h, 'report_execute', semantics='rolling_completed_hours')
            self.assertEqual('rolling_completed_hours', result['period']['semantics'])
            self.assertEqual(24, result['period']['hours'])
            self.assertNotIn('report_model', result['context'])
            self.assertTrue(h.subject.context_is_current(result))
        self.assertEqual([], sent)
        h.storage.async_append_attempt.assert_not_awaited()

    async def test_explicit_preferences_keep_their_own_display_timezone_without_normalizing_the_request(self):
        h, sent, display = self.fixture()
        preferences = {'language': 'pl', 'timezone_name': 'UTC', 'hour_cycle': '12'}
        with h.patches(), patch.object(composer, '_ha_presentation', return_value=display), patch.object(
                h.subject, 'async_prepare_report', wraps=h.subject.async_prepare_report) as prepare:
            result = await self.compose(h, report_preferences=preferences)
            for key, value in preferences.items():
                self.assertEqual(value, prepare.await_args.kwargs[key])
            self.assertEqual('Europe/Warsaw', result['period']['timezone'])
            self.assertEqual('report_read', result['context']['access_purpose'])
            self.assertEqual({'language': 'pl', 'timezone_name': 'UTC', 'hour_cycle': '12'}, preferences)
        self.assertEqual([], sent)
        h.storage.async_append_attempt.assert_not_awaited()

    async def test_explicit_non_builtin_preferences_remain_strict_and_fail_before_measurement_io(self):
        class LooksLikeText:
            def __str__(self):
                return 'Europe/Warsaw'
        for zone in (YamlText('Europe/Warsaw'), LooksLikeText(), None, 1):
            with self.subTest(zone_type=type(zone).__name__):
                h, sent, display = self.fixture()
                with h.patches(), patch.object(composer, '_ha_presentation', return_value=display), self.assertRaisesRegex(
                        ValueError, 'report_timezone_required'):
                    await self.compose(h, report_preferences={
                        'language': 'en', 'timezone_name': zone, 'hour_cycle': '24'})
                self.assertEqual([], h.queries)
                self.assertEqual([], sent)
                h.storage.async_append_attempt.assert_not_awaited()
