"""Durable synthetic log stream across restart and native 24-hour coverage."""
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.setup import async_setup_component
from homeassistant.helpers import entity_registry as er

from custom_components.ha_tools_email.const import DOMAIN
from custom_components.ha_tools_email.composer import async_build_log_digest_payload, _presentation
from custom_components.ha_tools_email.log_history import LogHistory
from tests_ha.test_email_native import schedule
from tests_ha.test_ha_runtime import _setup
import hashlib

pytestmark = pytest.mark.asyncio
UTC = timezone.utc


async def test_restart_in_active_hour_report_and_24h_sensors(hass, hass_storage, freezer):
    freezer.move_to('2026-10-01T00:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    bucket = hass.data[DOMAIN]
    history = bucket['log_history']
    freezer.move_to('2026-10-01T00:10:00Z')
    logging.getLogger('fixture.synthetic').warning('PRIVATE_MESSAGE_DO_NOT_PERSIST synthetic')
    freezer.move_to('2026-10-01T00:20:00Z')
    await history.async_stop()
    persisted = await history.store.async_load()
    assert 'PRIVATE_MESSAGE_DO_NOT_PERSIST' not in repr(persisted)
    assert 'message' not in repr(persisted)
    assert 'exception' not in repr(persisted)
    freezer.move_to('2026-10-01T00:25:00Z')
    history = LogHistory(hass)
    await history.async_start()
    bucket['log_history'] = history
    freezer.move_to('2026-10-01T00:40:00Z')
    logging.getLogger('fixture.synthetic').error('after restart synthetic')
    freezer.move_to('2026-10-02T00:00:00Z')
    await history.async_checkpoint()
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 2, tzinfo=UTC)
    rows, coverage = await history.async_query(start, end)
    synthetic = [row for row in rows if row['name'] == 'fixture.synthetic']
    assert sum(row['count'] for row in synthetic) == 2
    assert not coverage['complete']
    for cadence, window_start in [('daily', start), ('weekly', start - timedelta(days=6))]:
        payload = await async_build_log_digest_payload(hass, storage, cadence=cadence, now=end,
            window_start=window_start, window_end=end, manual_window=True,
            presentation=_presentation('pl', 'UTC'))
        assert payload['summary']['source']['name'] == 'system_log'
        assert payload['summary']['logger_counts']['fixture.synthetic'] == 2
        assert payload['summary']['level_counts']['WARNING'] >= 1
        assert payload['summary']['level_counts']['ERROR'] >= 1
        for part in ('body', 'html'):
            assert 'Niepełne pokrycie dziennika' in payload[part]
        from custom_components.ha_tools_email import websocket_api as ws
        results = []
        conn = SimpleNamespace(send_result=lambda _id, value: results.append(value),
                               send_error=lambda *_args: pytest.fail('preview rejected'))
        with patch.object(ws, '_utcnow', return_value=end):
            await inspect.unwrap(ws._ws_preview_log_digest)(hass, conn, {'id': 1, 'cadence': cadence, 'timezone': 'UTC'})
        if cadence == 'daily':
            result = results[0]
            assert result['period']['semantics'] == 'previous_completed_calendar_period'
            assert result['source']['name'] == 'system_log'
            assert result['source']['coverage']['complete'] is False
            assert result['logger_counts']['fixture.synthetic'] == payload['summary']['logger_counts']['fixture.synthetic']
            assert 'PRIVATE_MESSAGE_DO_NOT_PERSIST' not in repr(result)
    native = bucket['email_native']
    await native.async_refresh()
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id('sensor', DOMAIN, f'{entry.entry_id}_email_{key}_errors_24h')
    assert hass.states.get(entity_id).state == 'unknown'
    freezer.move_to('2026-10-02T00:26:00Z')
    await history.async_checkpoint()
    await native.async_refresh()
    await hass.async_block_till_done()
    assert int(hass.states.get(entity_id).state) >= 1
    assert hass.states.get(entity_id).attributes['coverage']['complete'] is True
    freezer.move_to('2026-10-02T01:25:00Z')
    logging.getLogger('fixture.synthetic').error('new full day')
    freezer.move_to('2026-10-03T01:00:00Z')
    await history.async_checkpoint()
    await native.async_refresh()
    await hass.async_block_till_done()
    assert int(hass.states.get(entity_id).state) >= 1
    assert hass.states.get(entity_id).attributes['coverage']['complete'] is True
    await history.async_stop()
    bucket.pop('log_history')


async def test_retention_prunes_older_than_33_days(hass, freezer):
    freezer.move_to('2026-10-01T00:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    history = LogHistory(hass)
    await history.async_start()
    logging.getLogger('fixture.retention').warning('old synthetic')
    freezer.move_to('2026-10-01T01:00:00Z')
    await history.async_checkpoint()
    freezer.move_to('2026-10-03T00:00:00Z')
    logging.getLogger('fixture.retention').warning('retained synthetic')
    await history.async_checkpoint()
    freezer.move_to('2026-11-04T02:00:00Z')
    await history.async_checkpoint()
    rows, _ = await history.async_query(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 4, 2, tzinfo=UTC))
    assert sum(row['count'] for row in rows if row['name'] == 'fixture.retention') == 1
    await history.async_stop()


async def test_partial_empty_observation_is_not_reported_as_wholly_unavailable(hass, hass_storage, freezer):
    freezer.move_to('2026-10-01T12:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    entry, storage = await _setup(hass, hass_storage=hass_storage)
    history = hass.data[DOMAIN]['log_history']
    freezer.move_to('2026-10-01T13:00:00Z')
    await history.async_checkpoint()
    # Isolate the no-observed-logs case from test infrastructure warnings.
    history.data['hours'] = {}
    report = await async_build_log_digest_payload(hass, storage, cadence='daily',
        now=datetime(2026, 10, 1, 13, tzinfo=UTC),
        window_start=datetime(2026, 10, 1, tzinfo=UTC),
        window_end=datetime(2026, 10, 1, 13, tzinfo=UTC), manual_window=True,
        presentation=_presentation('pl', 'UTC'))
    assert report['summary']['total'] == 0
    assert report['summary']['counts_complete'] is False
    assert 'Niepełne pokrycie dziennika' in report['body']


async def test_failed_checkpoint_retries_without_duplicate_or_lost_counts(hass, freezer):
    freezer.move_to('2026-10-01T00:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    history = LogHistory(hass)
    await history.async_start()
    freezer.move_to('2026-10-01T00:10:00Z')
    logging.getLogger('fixture.retry').warning('never persist this content')
    freezer.move_to('2026-10-01T01:00:00Z')
    with patch.object(history.store, 'async_save_confirmed', side_effect=OSError('synthetic')):
        with pytest.raises(OSError):
            await history.async_checkpoint()
        old_rows, old_coverage = await history.async_query(datetime(2026, 10, 1, tzinfo=UTC),
                                                           datetime(2026, 10, 1, 1, tzinfo=UTC))
        assert not old_coverage['complete']
        assert not any(row['name'] == 'fixture.retry' for row in old_rows)
    await history.async_checkpoint()
    rows, coverage = await history.async_query(datetime(2026, 10, 1, tzinfo=UTC),
                                               datetime(2026, 10, 1, 1, tzinfo=UTC))
    assert sum(row['count'] for row in rows if row['name'] == 'fixture.retry') == 1
    assert coverage['complete'] is True
    assert 'never persist this content' not in repr(await history.store.async_load())
    await history.async_stop()

@pytest.mark.parametrize("unload_during_stop", [False, True])
async def test_core_shutdown_flushes_tail_before_tick_and_replays_after_restart(hass, hass_storage, freezer, unload_during_stop):
    """Exercise Core's real STOP -> FINAL_WRITE -> CLOSE, not entry unload."""
    freezer.move_to('2026-10-01T00:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    _, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    history = hass.data[DOMAIN]['log_history']
    freezer.move_to('2026-10-01T00:00:20Z')  # prior to the 30-second checkpoint
    await hass.services.async_call('system_log', 'write', {
        'level': 'warning', 'logger': 'qa_sprint_before_restart',
        'message': 'PRIVATE_SHUTDOWN_TAIL_CONTENT'}, blocking=True)
    assert any(row['name'] == 'qa_sprint_before_restart' for row in history._pending)
    if unload_during_stop:
        async def unload_in_stop(_event):
            await history.async_stop()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, unload_in_stop)
    await hass.async_stop(force=True)
    persisted = hass_storage['ha_tools_email.log_history']['data']
    assert 'qa_sprint_before_restart' in repr(persisted)
    assert 'PRIVATE_SHUTDOWN_TAIL_CONTENT' not in repr(persisted)
    assert 'message' not in repr(persisted) and 'exception' not in repr(persisted)
    # A fresh reader sees precisely what Core committed, with no active source.
    restored = LogHistory(hass)
    await restored.async_start()
    assert any(row['name'] == 'qa_sprint_before_restart'
               for group in restored.data['hours'].values() for row in group.values())
    fake_hass = SimpleNamespace(data={DOMAIN: {'log_history': restored},
                                      'system_log': SimpleNamespace(records=[])},
                                config=SimpleNamespace(time_zone='UTC', language='pl'))
    now = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)
    for cadence, days in [('daily', 1), ('weekly', 7)]:
        payload = await async_build_log_digest_payload(fake_hass, storage, cadence=cadence,
            now=now, window_start=now - timedelta(days=days), window_end=now,
            manual_window=True, presentation=_presentation('pl', 'UTC'))
        assert payload['summary']['logger_counts']['qa_sprint_before_restart'] == 1
        assert payload['summary']['source']['coverage']['complete'] is False
        assert payload['body'].count('Niepełne pokrycie dziennika') == 1


async def test_hard_interruption_leaves_unwritten_tail_as_gap(hass, freezer):
    freezer.move_to('2026-10-01T00:00:00Z')
    assert await async_setup_component(hass, 'system_log', {})
    history = LogHistory(hass)
    await history.async_start()
    freezer.move_to('2026-10-01T00:05:00Z')
    await history.async_checkpoint()
    freezer.move_to('2026-10-01T00:05:30Z')
    logging.getLogger('fixture.lost_tail').warning('unwritten synthetic')
    assert any(row['name'] == 'fixture.lost_tail' for row in history._pending)
    # Simulate process death: remove hooks without invoking final checkpoint.
    logging.root.removeHandler(history._handler)
    history._unsub()
    history._unsub_shutdown()
    history._since = None
    freezer.move_to('2026-10-01T00:07:00Z')
    restored = LogHistory(hass)
    await restored.async_start()
    logging.getLogger('fixture.after_restart').warning('after synthetic')
    freezer.move_to('2026-10-01T00:10:00Z')
    await restored.async_checkpoint()
    rows, coverage = await restored.async_query(datetime(2026, 10, 1, tzinfo=UTC),
                                                 datetime(2026, 10, 1, 0, 10, tzinfo=UTC))
    assert not any(row['name'] == 'fixture.lost_tail' for row in rows)
    assert any(row['name'] == 'fixture.after_restart' for row in rows)
    assert coverage['complete'] is False
    await restored.async_stop()
