"""Same-snapshot BUILD-1 presentation and exact legacy CSV regression contracts."""
from datetime import datetime, timezone
import importlib
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_energy_analysis_service import Harness, service

mail = importlib.import_module(service.__package__ + '.mail_energy')
legacy = importlib.import_module(service.__package__ + '.energy_report')
periods = importlib.import_module(service.__package__ + '.report_window')
UTC = timezone.utc
OPTIONS = {'language': 'pl', 'timezone_name': 'UTC', 'hour_cycle': '24'}


class Build1MailBridgeTest(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        h = Harness(); h.user.is_admin = True
        h.now = datetime(2026, 1, 11, 12, tzinfo=UTC)
        return h

    async def test_public_calendar_mail_and_exact_csv_share_one_snapshot(self):
        h = self.harness(); snapshots = []
        original = h.subject.async_get_analysis
        async def capture(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            snapshots.append(snapshot)
            return snapshot
        with h.patches(), patch.object(h.subject, 'async_get_analysis', side_effect=capture) as produce, patch.object(
                mail, 'render', wraps=mail.render) as render, patch.object(
                legacy, 'render_energy_analysis', wraps=legacy.render_energy_analysis) as export:
            result = await h.subject.async_get_report('day', 'user', anchor='2026-01-10', **OPTIONS)
            self.assertEqual(1, produce.await_count)
            self.assertTrue(produce.await_args.kwargs['_report_model'])
            self.assertEqual(1, render.call_count)
            self.assertEqual(1, export.call_count)
            self.assertIs(snapshots[0]['report_model'], render.call_args.args[0])
            export_view = export.call_args.args[0]
            self.assertNotIn('report_model', export_view)
            self.assertEqual(snapshots[0]['snapshot_id'], result['snapshot_id'])
            self.assertEqual(snapshots[0]['period'], result['period'])
            # Re-use the unpatched original to compare actual bytes below.
            actual_export = export._mock_wraps(export_view, **OPTIONS)
            self.assertEqual(actual_export['csv'], result['csv'])
            self.assertNotIn('report_model.', result['csv'])
            self.assertIn('<!-- block:12 -->', result['html'])
            self.assertIn('subject', result); self.assertIn('notify', result)
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))

    async def test_calendar_report_window_keeps_frozen_period_comparisons_and_mail_subject(self):
        h = self.harness()
        request = periods.resolve_report_window('daily', h.now, 'Europe/Warsaw', mode='calendar')
        with h.patches(), patch.object(mail, 'render', wraps=mail.render) as render, patch.object(
                legacy, 'render_energy_analysis', wraps=legacy.render_energy_analysis) as export, patch.object(
                h.subject, 'async_get_analysis', wraps=h.subject.async_get_analysis) as produce:
            result = await h.subject.async_prepare_report(request, 'user', purpose='report_execute', **OPTIONS)
            self.assertEqual(1, produce.await_count)
            self.assertTrue(produce.await_args.kwargs['_report_model'])
            self.assertEqual(request.period(h.now), result['period'])
            self.assertEqual('report_window', result['period']['kind'])
            self.assertEqual('report_execute', result['context']['access_purpose'])
            self.assertEqual('day', result['context']['period_kind'])
            self.assertEqual('Europe/Warsaw', render.call_args.args[0]['period']['timezone'])
            self.assertEqual('day', render.call_args.args[0]['period']['kind'])
            self.assertEqual('UTC', result['context']['measurement_timezone'])
            self.assertEqual('Europe/Warsaw', result['context']['comparison_timezone'])
            self.assertEqual('previous', result['context']['comparison_kind'])
            self.assertIn('previous', result['summary']['comparisons'])
            self.assertEqual(result['summary']['snapshot_id'], result['snapshot_id'])
            self.assertEqual(result['body'], result['text'])
            self.assertNotEqual('Energy report · daily', result['subject'])
            self.assertEqual(export._mock_wraps(export.call_args.args[0], **OPTIONS)['csv'], result['csv'])
            self.assertTrue(await h.subject.async_validate_result(result, 'user'))

    async def test_rolling_window_retains_legacy_output_without_model_or_mail_renderer(self):
        h = self.harness()
        request = periods.resolve_report_window('daily', h.now, 'UTC', mode='rolling')
        with h.patches(), patch.object(mail, 'render', side_effect=AssertionError('rolling mail model')), patch.object(
                legacy, 'render_energy_analysis', wraps=legacy.render_energy_analysis) as export:
            result = await h.subject.async_prepare_report(request, 'user', purpose='report_execute', **OPTIONS)
            self.assertNotIn('report_model', result['context'])
            expected = export._mock_wraps(export.call_args.args[0], **OPTIONS)
            for key in ('snapshot_id', 'html', 'text', 'csv'):
                self.assertEqual(expected[key], result[key])
            self.assertEqual('Energy report · daily', result['subject'])
            self.assertEqual('rolling_or_custom_window', result['summary']['comparison']['reason'])

    async def test_public_calendar_comparison_still_accepted_and_exported(self):
        h = self.harness()
        with h.patches(), patch.object(legacy, 'render_energy_analysis', wraps=legacy.render_energy_analysis) as export:
            result = await h.subject.async_get_report('day', 'user', anchor='2026-01-10', comparison='previous', **OPTIONS)
            self.assertIn('comparisons.previous.', result['csv'])
            self.assertEqual('previous', result['context']['comparison_kind'])
            self.assertEqual(export._mock_wraps(export.call_args.args[0], **OPTIONS)['csv'], result['csv'])

    async def test_calendar_capture_recapture_kinds_and_actual_color_day_no_io(self):
        h = self.harness()
        for cadence, kind in (('daily', 'day'), ('weekly', 'week'), ('monthly', 'month')):
            with self.subTest(cadence=cadence):
                request = periods.resolve_report_window(cadence, h.now, 'Europe/Warsaw', mode='calendar')
                config, period, context = h.subject._capture_report(request, 'report_read',
                    h.subject._actor_signature('user'), report_model=True)
                self.assertEqual(kind, context['period_kind'])
                self.assertEqual('2026-01-11', context['report_color_as_of'])
                self.assertEqual('full', context['report_mode'])
                self.assertEqual((config, period, context), h.subject._recapture(context))
        self.assertEqual([], h.queries)

    async def test_all_public_calendar_kinds_forward_model_flag_without_second_snapshot(self):
        h = self.harness()
        with h.patches():
            snapshot = await h.subject.async_get_analysis('day', 'user', anchor='2026-01-10',
                purpose='report_read', _report_model=True)
            for kind in ('day', 'week', 'month', 'year'):
                with self.subTest(kind=kind), patch.object(h.subject, 'async_get_analysis', AsyncMock(return_value=snapshot)) as produce:
                    await h.subject.async_get_report(kind, 'user', **OPTIONS)
                    produce.assert_awaited_once_with(kind, 'user', anchor=None, comparison=None,
                        purpose='report_read', _report_model=True)

    async def test_role_source_or_registry_change_during_model_render_blocks_release(self):
        for mutation in ('role', 'source', 'registry'):
            h = self.harness()
            h.user.is_admin = False
            h.policy['features'] = {'energy_reports': {'read': {'mode': 'household'}}}
            def mutate(job):
                if getattr(job, 'func', None) is mail.render:
                    if mutation == 'role':
                        h.policy['features']['energy_reports']['read'] = {'mode': 'admin'}
                    elif mutation == 'source':
                        h.permissions.access_all_entities = lambda _: False
                        h.permissions.check_entity = lambda *_: False
                    else:
                        h.registry.revision += 1
            h.on_cpu = mutate
            with self.subTest(mutation=mutation), h.patches(), self.assertRaises((PermissionError, RuntimeError)):
                await h.subject.async_get_report('day', 'user', anchor='2026-01-10', **OPTIONS)
            self.assertIsNone(h.subject._cache)

    async def test_invalid_preferences_and_rolling_model_optin_fail_before_private_io(self):
        h = self.harness()
        request = periods.resolve_report_window('daily', h.now, 'UTC', mode='rolling')
        with h.patches():
            with self.assertRaises(ValueError):
                await h.subject.async_get_report('day', 'user', language='invalid', timezone_name='UTC', hour_cycle='24')
            with self.assertRaises(ValueError):
                await h.subject.async_get_analysis('report_window', 'user', purpose='report_read',
                    _report_window=request, _report_model=True)
            self.assertEqual([], h.queries)
