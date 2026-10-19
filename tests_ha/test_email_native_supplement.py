"""Review regressions through real platforms with local transport only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import patch

import pytest
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er

from custom_components.ha_tools_email.const import DOMAIN
from tests_ha.test_ha_runtime import _setup
from tests_ha.test_email_native import schedule, record

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def forbid_external_smtp_and_automatic_dispatch():
    with patch('custom_components.ha_tools_email._send_email',
               side_effect=AssertionError('External SMTP forbidden')), \
         patch('custom_components.ha_tools_email.scheduler.async_send_schedule',
               side_effect=AssertionError('Automatic dispatch forbidden')):
        yield


def entity_id(hass, entry, domain, field):
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f'{entry.entry_id}_email_{key}_{field}')


@pytest.mark.parametrize('same_timestamp', [False, True])
async def test_two_new_confirmed_attempts_emit_oldest_first_in_one_refresh(
        hass, hass_storage, freezer, same_timestamp):
    freezer.move_to('2026-10-19T12:00:00+00:00')
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    runtime = hass.data[DOMAIN]['email_native']
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    newest = record(20, 'accepted_by_smtp')
    older = record(10, 'authentication_failed')
    older['timestamp'] = (newest['timestamp'] if same_timestamp else
        (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    observed = []
    unsubscribe = runtime.async_add_listener(lambda fresh: observed.extend(fresh.get(key, [])))
    try:
        # A newer-started attempt completes first. Retain that order in
        # history, but emit chronologically (ID breaks timestamp ties).
        with patch.object(runtime, 'async_request_refresh'):
            await storage.async_append_attempt(newest)
            await storage.async_append_attempt(older)
        history = await storage.async_get_attempt_history()
        assert [row['id'] for row in history['attempts']] == [older['id'], newest['id']]
        await runtime.async_refresh()
        await hass.async_block_till_done()
        assert [row['id'] for row in observed] == [older['id'], newest['id']]
        event = hass.states.get(entity_id(hass, entry, 'event', 'attempt'))
        assert event.attributes['event_type'] == 'smtp_accepted'
        assert event.attributes['outcome'] == 'accepted_by_smtp'
        assert runtime.data[key]['last_attempt']['timestamp'] == newest['timestamp']
        assert hass.states.get(entity_id(hass, entry, 'sensor', 'result')).state == 'accepted_by_smtp'
        assert await storage.async_get_attempt_history() == history
        observed.clear()
        await runtime.async_refresh()
        assert observed == []
    finally:
        unsubscribe()


async def test_disabled_calendar_allows_manual_button_without_consuming_period(
        hass, hass_storage, hass_admin_user, freezer):
    freezer.move_to('2026-10-19T12:00:00+00:00')
    entry, storage = await _setup(hass, stored={'schedules': [schedule(enabled=False)]}, hass_storage=hass_storage)
    runtime = hass.data[DOMAIN]['email_native']
    bucket = hass.data[DOMAIN]
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    before = deepcopy(await storage.async_get_schedule('stable-schedule'))
    consumed = deepcopy(bucket.get('schedule_consumed_periods', {}))
    assert await storage.async_get_last_fired('stable-schedule') is None
    assert runtime.data[key]['enabled'] is False
    assert runtime.data[key]['next_report_at'] is None
    button = entity_id(hass, entry, 'button', 'send')
    assert hass.states.get(button).state != 'unavailable'
    sent = []
    async def config():
        return {'default_recipient': 'PRIVATE@example.invalid'}
    async def compose(*args, **kwargs):
        return {'subject': 'PRIVATE', 'body': 'PRIVATE', 'html': '<p>PRIVATE</p>', 'dedup_keys': []}
    bucket['load_config'] = config
    bucket['send_email'] = lambda *args: sent.append('stub accepted')
    with patch('custom_components.ha_tools_email.composer.async_build_report_payload', new=compose):
        await hass.services.async_call('button', 'press', {'entity_id': button},
            blocking=True, context=Context(user_id=hass_admin_user.id))
    await runtime.async_refresh()
    await hass.async_block_till_done()
    assert sent == ['stub accepted']
    history = await storage.async_get_attempt_history()
    assert len(history['attempts']) == 1
    attempt = history['attempts'][0]
    assert (attempt['source'], attempt['schedule_ref'], attempt['outcome'], attempt['period']) == (
        'manual', key, 'accepted_by_smtp', 'daily:2026-10-19')
    assert await storage.async_get_schedule('stable-schedule') == before
    assert before['enabled'] is False
    assert await storage.async_get_last_fired('stable-schedule') is None
    assert bucket.get('schedule_consumed_periods', {}) == consumed
    assert runtime.data[key]['enabled'] is False
    assert runtime.data[key]['next_report_at'] is None
    assert hass.states.get(entity_id(hass, entry, 'sensor', 'next_report')).state == 'unknown'
    calendar = entity_id(hass, entry, 'calendar', 'schedule')
    response = await hass.services.async_call('calendar', 'get_events', {'entity_id': calendar,
        'start_date_time': '2026-10-19T00:00:00+00:00',
        'end_date_time': '2026-10-21T00:00:00+00:00'}, blocking=True, return_response=True)
    assert response[calendar]['events'] == []
