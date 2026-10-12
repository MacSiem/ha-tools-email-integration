"""Readonly calendar preview is opt-in; existing dashboard windows stay intact."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from tests.test_schedule_timezone_v3 import load_websocket
except ImportError:
    from test_schedule_timezone_v3 import load_websocket

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


class EnergyCalendarPreviewTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        ws = load_websocket(self)
        self.calls, self.results, self.errors = [], [], []
        forbidden = Mock(side_effect=AssertionError('Readonly preview cannot access SMTP or write Store'))
        hass = SimpleNamespace(config=SimpleNamespace(time_zone='Asia/Tokyo'),
            data={'ha_tools_email': {'send_email': forbidden, 'load_config': forbidden}},
            commands=[])
        conn = SimpleNamespace(user=SimpleNamespace(is_admin=True),
            send_result=lambda *a: self.results.append(a), send_error=lambda *a: self.errors.append(a))
        self.financial = {'energyCost': 0, 'variableCost': 0, 'usageCost': 0,
            'fixedMonthly': 7, 'currency': 'PLN', 'priceBasis': 'gross',
            'total_kwh': 0, 'total_cost': 0, 'status': 'ready',
            'source_ids': ['sensor.synthetic_grid'], 'devices': []}
        async def build(source, **kwargs):
            self.assertIs(source, hass)
            self.calls.append(kwargs)
            summary = copy.deepcopy(self.financial)
            start = kwargs.get('window_start', NOW.replace(hour=0))
            end = kwargs.get('window_end', NOW)
            summary['period'] = {'start': start.astimezone(UTC).isoformat(),
                'end': end.astimezone(UTC).isoformat(), 'cadence': kwargs['cadence']}
            return {'summary': summary}
        builder = patch.object(ws, 'async_build_energy_report_payload', build)
        builder.start(); self.addCleanup(builder.stop)
        clock = patch.object(ws, '_utcnow', lambda: NOW)
        clock.start(); self.addCleanup(clock.stop)
        return ws, hass, conn, forbidden

    async def test_existing_preview_keeps_original_builder_call_and_summary(self):
        ws, hass, conn, forbidden = self.fixture()
        await ws._ws_preview_energy_report(hass, conn, {'id':1,'cadence':'daily'})
        self.assertEqual([{'cadence':'daily'}], self.calls)
        result=self.results[0][1]
        self.assertNotIn('effective_timezone', result)
        self.assertNotIn('semantics', result['period'])
        self.assertEqual('2026-10-12T12:00:00+00:00',result['period']['end'])
        self.assertEqual([],self.errors); self.assertEqual(0,forbidden.call_count)

    async def test_explicit_calendar_uses_previous_iso_week_in_selected_country_and_preserves_cost_fields(self):
        ws,hass,conn,forbidden=self.fixture()
        before=copy.deepcopy(self.financial)
        await ws._ws_preview_energy_report(hass,conn,{'id':2,'cadence':'weekly','calendar_period':True,'timezone':'America/New_York'})
        self.assertEqual([],self.errors)
        call=self.calls[0]
        self.assertEqual('2026-10-05T04:00:00+00:00',call['window_start'].astimezone(UTC).isoformat())
        self.assertEqual('2026-10-12T04:00:00+00:00',call['window_end'].astimezone(UTC).isoformat())
        self.assertEqual('America/New_York',call['report_timezone'])
        result=self.results[0][1]
        self.assertEqual('weekly',result['cadence'])
        self.assertEqual('America/New_York',result['effective_timezone'])
        self.assertEqual('previous_completed_calendar_period',result['period']['semantics'])
        for key,value in before.items():self.assertEqual(value,result[key],key)
        self.assertEqual(before,self.financial);self.assertEqual(0,forbidden.call_count)

    async def test_default_ha_calendar_month_is_explicit_and_not_polish_or_fixed_utc(self):
        ws,hass,conn,forbidden=self.fixture()
        await ws._ws_preview_energy_report(hass,conn,{'id':3,'cadence':'monthly','calendar_period':True})
        call=self.calls[0]
        self.assertEqual('2026-08-31T15:00:00+00:00',call['window_start'].astimezone(UTC).isoformat())
        self.assertEqual('2026-09-30T15:00:00+00:00',call['window_end'].astimezone(UTC).isoformat())
        self.assertEqual('Asia/Tokyo',self.results[0][1]['effective_timezone'])
        self.assertEqual(0,forbidden.call_count)

    async def test_dst_days_are_completed_23_and_25_hour_recorder_windows(self):
        ws,hass,conn,forbidden=self.fixture()
        for now,hours in [(datetime(2026,3,30,12,tzinfo=UTC),23),(datetime(2026,10,26,12,tzinfo=UTC),25)]:
            with self.subTest(hours=hours),patch.object(ws,'_utcnow',lambda:now):
                await ws._ws_preview_energy_report(hass,conn,{'id':4,'cadence':'daily','calendar_period':True,'timezone':'Europe/Warsaw'})
                call=self.calls[-1]
                self.assertEqual(hours,(call['window_end'].astimezone(UTC)-call['window_start'].astimezone(UTC)).total_seconds()/3600)
        self.assertEqual(0,forbidden.call_count)

    async def test_invalid_calendar_parameters_do_not_read_recorder_or_smtp(self):
        ws,hass,conn,forbidden=self.fixture()
        for extra in [{'timezone':'America/New_York'}, {'calendar_period':'true'}, {'calendar_period':1},
                      {'calendar_period':True,'timezone':'No/Such_Zone'}, {'calendar_period':True,'timezone':''}]:
            with self.subTest(extra=extra):
                await ws._ws_preview_energy_report(hass,conn,{'id':5,'cadence':'daily',**extra})
        self.assertEqual([],self.calls);self.assertEqual([],self.results)
        self.assertEqual(['invalid_payload']*5,[row[1] for row in self.errors])
        self.assertEqual(0,forbidden.call_count)

    async def test_household_denied_before_read_even_for_calendar_mode(self):
        ws,hass,conn,forbidden=self.fixture();conn.user.is_admin=False
        await ws._ws_preview_energy_report(hass,conn,{'id':6,'cadence':'weekly','calendar_period':True})
        self.assertEqual([],self.calls);self.assertEqual([],self.results)
        self.assertEqual('unauthorized',self.errors[0][1]);self.assertEqual(0,forbidden.call_count)


class EnergyCalendarPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_composer_forwards_energy_calendar_zone_only_to_energy_builder(self):
        ws = load_websocket(self)
        import sys
        composer = sys.modules[ws.__package__ + '.composer']
        seen=[]
        async def build(*args, **kw): seen.append(kw); return {'summary': {}}
        with patch.object(composer, 'async_build_energy_report_payload', build):
            await composer.async_build_report_payload(None, None, kind='energy_report', cadence='weekly',
                now=NOW, report_timezone='America/New_York')
        self.assertEqual('America/New_York', seen[0]['report_timezone'])

    async def test_schedule_passes_its_calendar_zone_and_manual_default_has_no_override(self):
        ws = load_websocket(self)
        import sys
        scheduler = sys.modules[ws.__package__ + '.scheduler']
        seen=[]
        async def send(*args, **kw): seen.append(kw); return {'status': 'fixture_only'}
        hass=SimpleNamespace(data={'ha_tools_email': {'storage': object()}},
            config=SimpleNamespace(time_zone='Asia/Tokyo'))
        row={'id':'synthetic', 'kind':'energy_report', 'cadence':'weekly', 'timezone':'America/New_York'}
        with patch.object(scheduler, 'async_send_report', send):
            await scheduler.async_send_schedule(hass, row, now=NOW)
            await scheduler.async_send_now(hass, kind='energy_report', cadence='weekly')
        self.assertEqual('America/New_York', seen[0]['report_timezone'])
        self.assertEqual('2026-10-05T04:00:00+00:00', seen[0]['report_window'][0].astimezone(UTC).isoformat())
        self.assertNotIn('report_timezone', seen[1])
