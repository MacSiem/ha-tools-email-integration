"""Actual WS handler and real service, only native HA/Recorder IO fixtures."""
from datetime import datetime,timezone
import unittest
from tests import test_tariff_book_websocket as book_ws
from tests.test_energy_analysis_service import Harness


class PatternsWebsocketTest(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        ws,_,_,_,connection,results,errors=book_ws.BookWebsocketTest().fixture()
        h=Harness();h.book=None;h.now=datetime(2026,5,6,tzinfo=timezone.utc)
        connection.user.is_admin=False
        return ws,h,connection,results,errors

    async def test_authenticated_household_reads_real_producer_without_consumer_io(self):
        ws,h,connection,results,errors=self.harness()
        with h.patches():
            await ws._ws_energy_patterns(h.hass,connection,{'id':1,'type':'ha_tools_email/energy_patterns','days':7})
        self.assertEqual([],errors);self.assertEqual('patterns',results[-1][1]['kind'])
        self.assertEqual('analysis',results[-1][1]['context']['access_purpose'])
        self.assertEqual([('sensor.grid',)],[ids for ids,_,_ in h.queries])

    async def test_client_clock_purpose_sources_and_wrong_days_are_rejected_before_io(self):
        for field,value in [('now','2099-01-01'),('timezone','UTC'),('purpose','report_read'),
                            ('source_ids',['sensor.private']),('days',7.0)]:
            ws,h,connection,results,errors=self.harness()
            message={'id':1,'days':7,field:value}
            with self.subTest(field=field),h.patches():await ws._ws_energy_patterns(h.hass,connection,message)
            self.assertEqual([],results);self.assertEqual([],h.queries)
            self.assertEqual('invalid_patterns_selection',errors[-1][1])

    async def test_role_revoked_during_real_provider_never_sends_patterns(self):
        ws,h,connection,results,errors=self.harness()
        async def revoke(*args):h.user.is_active=False
        h.on_read=revoke
        with h.patches():await ws._ws_energy_patterns(h.hass,connection,{'id':1,'days':7})
        self.assertEqual([],results);self.assertEqual('unauthorized',errors[-1][1])

    async def test_replaced_service_during_native_final_auth_never_sends(self):
        ws,h,connection,results,errors=self.harness()
        def replace_after_result():
            if h.subject._cache is not None:
                h.hass.data['ha_tools_email']['energy_analysis']=object()
        h.on_auth=replace_after_result
        with h.patches():await ws._ws_energy_patterns(h.hass,connection,{'id':1,'days':7})
        self.assertEqual([],results);self.assertEqual('energy_patterns_unavailable',errors[-1][1])

    async def test_inactive_connection_never_starts_private_reads(self):
        ws,h,connection,results,errors=self.harness()
        connection.user.is_active=False
        with h.patches():await ws._ws_energy_patterns(h.hass,connection,{'id':1,'days':7})
        self.assertEqual([],results);self.assertEqual([],h.queries)
        self.assertEqual('unauthorized',errors[-1][1])
