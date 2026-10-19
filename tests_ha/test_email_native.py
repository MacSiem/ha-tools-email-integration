"""Native Email platforms/registry/issues/calendar and user-context button."""
from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import patch

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, device_registry as dr, issue_registry as ir

from custom_components.ha_tools_email.const import DOMAIN
from tests_ha.test_ha_runtime import _setup

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def forbid_external_smtp_and_automatic_dispatch():
    # Every native test is isolated from real SMTP, including a coincidental
    # wall-clock schedule minute during setup. Manual button uses its own stub.
    with patch('custom_components.ha_tools_email._send_email',
               side_effect=AssertionError('External SMTP forbidden')), \
         patch('custom_components.ha_tools_email.scheduler.async_send_schedule',
               side_effect=AssertionError('Automatic dispatch forbidden')):
        yield


def schedule(**changes):
    return {'id': 'stable-schedule', 'kind': 'log_digest', 'cadence': 'daily',
            'time': '02:30', 'timezone': 'Europe/Warsaw', 'enabled': True,
            'recipients': ['PRIVATE@example.invalid'], 'label': 'PRIVATE_LABEL', **changes}


def records(hass, entry):
    return {row.unique_id.rsplit('_', 1)[-1]: row for row in
        er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if '_email_' in row.unique_id}


def record(index, outcome='accepted_by_smtp'):
    return {'id': f'00000000-0000-4000-8000-{index:012d}',
        'timestamp': datetime.now(timezone.utc).isoformat(), 'source': 'schedule',
        'kind': 'log_digest', 'cadence': 'daily', 'period': None,
        'schedule_ref': hashlib.sha256(b'stable-schedule').hexdigest(),
        'recipient_count': 0, 'outcome': outcome}


async def test_native_no_schedules_preserves_exact_five_energy_entities(hass, hass_storage):
    entry, storage = await _setup(hass)
    assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 5
    assert not records(hass, entry)
    assert hass.data[DOMAIN]['email_native'].data == {}


async def test_native_metadata_registry_personalization_reload_events_and_repairs(hass, hass_storage, freezer):
    freezer.move_to('2026-10-25T00:00:00+00:00')
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    coordinator = hass.data[DOMAIN]['email_native']
    registry = er.async_get(hass)
    entries = [row for row in er.async_entries_for_config_entry(registry, entry.entry_id) if '_email_' in row.unique_id]
    assert len(entries) == 8
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    def entity(domain, field):
        return registry.async_get(registry.async_get_entity_id(domain, DOMAIN,
            f'{entry.entry_id}_email_{key}_{field}'))
    result = entity('sensor', 'result')
    counters = [entity('sensor', field) for field in ('errors_24h', 'warnings_24h')]
    for counter in counters:
        state = hass.states.get(counter.entity_id)
        assert state.state == 'unknown'
        assert state.attributes['state_class'] == 'measurement'
    assert hass.states.get(result.entity_id).state == 'unknown'
    assert set(hass.states.get(result.entity_id).attributes['options']) >= {'accepted_by_smtp', 'authentication_failed'}
    event = entity('event', 'attempt')
    assert hass.states.get(event.entity_id).state == 'unknown'  # no startup replay
    await storage.async_append_attempt(record(1, 'authentication_failed'))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(result.entity_id).state == 'authentication_failed'
    assert hass.states.get(event.entity_id).attributes['event_type'] == 'attempt_error'
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, 'email_native_auth') is not None
    for index in (2, 3):
        freezer.tick(timedelta(seconds=1))
        await storage.async_append_attempt(record(index, 'send_failed'))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, 'email_native_failures') is not None
    freezer.tick(timedelta(seconds=1))
    await storage.async_append_attempt(record(4))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, 'email_native_auth') is None
    assert issues.async_get_issue(DOMAIN, 'email_native_failures') is None
    assert hass.states.get(event.entity_id).attributes['event_type'] == 'smtp_accepted'
    assert hass.states.get(event.entity_id).attributes['outcome'] == 'accepted_by_smtp'
    before_event = hass.states.get(event.entity_id).state
    await storage.async_delete_schedule('stable-schedule')
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(event.entity_id).state == 'unavailable'
    await storage.async_upsert_schedule(schedule())
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(event.entity_id).state == before_event  # re-add is not a new attempt
    for row in entries:
        state = hass.states.get(row.entity_id)
        assert 'PRIVATE' not in str(state.attributes) and '@' not in str(state.attributes)
    renamed = registry.async_update_entity(result.entity_id, name='User result', new_entity_id='sensor.user_email_result')
    registry.async_update_entity_options(counters[0].entity_id, 'sensor', {'display_precision': 0})
    registry.async_update_entity(counters[0].entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    device = dr.async_get(hass).async_get(result.device_id)
    dr.async_get(hass).async_update_device(device.id, name_by_user='My report device')
    await hass.async_block_till_done()
    identities = {row.unique_id: (row.entity_id, row.id, row.device_id, row.name, row.disabled_by, row.options)
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not coordinator.running and not coordinator._unsubscribers and not coordinator._listeners
    assert identities == {row.unique_id: (row.entity_id, row.id, row.device_id, row.name, row.disabled_by, row.options)
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert hass.states.get(event.entity_id).state == before_event  # restored actual event, no replay
    assert dr.async_get(hass).async_get(device.id).name_by_user == 'My report device'
    replacement = hass.data[DOMAIN]['email_native']
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not replacement.running and not replacement._unsubscribers


@pytest.mark.parametrize('start,end,expected', [
    ('2026-03-28T23:00:00+00:00', '2026-03-30T00:00:00+00:00', '2026-03-29T01:30:00+00:00'),
    ('2026-10-24T22:00:00+00:00', '2026-10-26T00:00:00+00:00', '2026-10-25T00:30:00+00:00'),
])
async def test_native_calendar_uses_real_schedule_dst_without_delivery_claim(hass, hass_storage, freezer, start, end, expected):
    freezer.move_to(start)
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    registry = er.async_get(hass)
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    eid = registry.async_get_entity_id('calendar', DOMAIN, f'{entry.entry_id}_email_{key}_schedule')
    # Standard native calendar service executes the real CalendarEntity.
    response = await hass.services.async_call('calendar', 'get_events', {'entity_id': eid,
        'start_date_time': start, 'end_date_time': end}, blocking=True, return_response=True)
    events = response[eid]['events']
    assert events and datetime.fromisoformat(events[0]['start']).astimezone(timezone.utc).isoformat() == expected
    assert all(event['summary'] == 'HA Tools Email report' and not event.get('description') for event in events)
    await storage.async_upsert_schedule(schedule(enabled=False))
    await hass.data[DOMAIN]['email_native'].async_refresh()
    response = await hass.services.async_call('calendar', 'get_events', {'entity_id': eid,
        'start_date_time': start, 'end_date_time': end}, blocking=True, return_response=True)
    assert response[eid]['events'] == []


@pytest.mark.parametrize('revoke', [False, True])
async def test_native_button_user_context_real_guard_and_stub_transport_only(hass, hass_storage, hass_admin_user, revoke):
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    runtime = hass.data[DOMAIN]['email_native']
    bucket = hass.data[DOMAIN]
    sent = []
    revoked = False
    async def config():
        return {'default_recipient': 'PRIVATE@example.invalid'}
    async def compose(*args, **kwargs):
        nonlocal revoked
        if revoke:
            revoked = True
        return {'subject': 'PRIVATE', 'body': 'PRIVATE', 'html': '<p>PRIVATE</p>', 'dedup_keys': []}
    # A fresh nonadmin user is needed to exercise policy revocation; admins
    # retain administrator access by design, so deactivate this user instead.
    original = hass.auth.async_get_user
    calls = 0
    async def user(uid):
        nonlocal calls
        result = await original(uid)
        calls += 1
        if revoked:
            return None
        return result
    bucket['load_config'] = config
    bucket['send_email'] = lambda *args: sent.append('stub accepted')
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    eid = er.async_get(hass).async_get_entity_id('button', DOMAIN, f'{entry.entry_id}_email_{key}_send')
    with patch('custom_components.ha_tools_email.composer.async_build_report_payload', new=compose),          patch.object(hass.auth, 'async_get_user', new=user):
        if revoke:
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call('button', 'press', {'entity_id': eid},
                    blocking=True, context=Context(user_id=hass_admin_user.id))
        else:
            await hass.services.async_call('button', 'press', {'entity_id': eid},
                blocking=True, context=Context(user_id=hass_admin_user.id))
    assert sent == ([] if revoke else ['stub accepted'])
    history = await storage.async_get_attempt_history()
    assert history['attempts'][-1]['schedule_ref'] == key
    assert history['attempts'][-1]['source'] == 'manual'
    if not revoke:
        assert history['attempts'][-1]['outcome'] == 'accepted_by_smtp'
    with pytest.raises(HomeAssistantError):
        await runtime.async_send(key, None, eid)  # no automation/ScheduleActor fallback


async def test_native_button_feature_execute_and_native_control_both_required(hass, hass_storage, hass_admin_user):
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    runtime = hass.data[DOMAIN]['email_native']
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    eid = er.async_get(hass).async_get_entity_id('button', DOMAIN, f'{entry.entry_id}_email_{key}_send')
    user = await hass.auth.async_create_user('Native test', group_ids=['system-users'])
    # HA promotes the first user to owner, regardless of group_ids.
    assert not user.is_owner and not user.is_admin
    assert user.permissions.check_entity(eid, 'control')
    with pytest.raises(HomeAssistantError):
        await runtime.async_send(key, user.id, eid)  # Native control alone does not grant execute.
    await storage.async_save_feature_access({'schema': 1, 'features': {
        'log_reports': {'execute': {'mode': 'household'}},
        'energy_reports': {'execute': {'mode': 'household'}}}}, expected_revision=0)
    await storage.async_upsert_schedule(schedule(kind='energy_report'))
    await runtime.async_refresh()
    no_control = await hass.auth.async_create_user('No control', group_ids=[])
    assert not no_control.is_owner and not no_control.permissions.check_entity(eid, 'control')
    with pytest.raises(HomeAssistantError):
        await runtime.async_send(key, no_control.id, eid)  # Feature grant cannot exceed native ACL.
    assert (await storage.async_get_attempt_history())['attempts'] == []


async def test_native_failed_unload_keeps_owned_timer_listeners_and_entities(hass, hass_storage):
    from custom_components.ha_tools_email import async_unload_entry
    entry, storage = await _setup(hass, stored={'schedules': [schedule()]}, hass_storage=hass_storage)
    native = hass.data[DOMAIN]['email_native']
    before = tuple(native._unsubscribers)
    with patch.object(hass.config_entries, 'async_unload_platforms', return_value=False):
        assert not await async_unload_entry(hass, entry)
    assert native.running and tuple(native._unsubscribers) == before
    assert hass.data[DOMAIN]['email_native'] is native
    assert records(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not native.running and not native._unsubscribers


async def test_native_energy_button_passes_actual_user_not_schedule_actor_before_private_io(hass, hass_storage, hass_admin_user):
    entry, storage = await _setup(hass, stored={'schedules': [schedule(kind='energy_report')]}, hass_storage=hass_storage)
    runtime = hass.data[DOMAIN]['email_native']
    service = hass.data[DOMAIN]['energy_analysis']
    key = hashlib.sha256(b'stable-schedule').hexdigest()
    eid = er.async_get(hass).async_get_entity_id('button', DOMAIN, f'{entry.entry_id}_email_{key}_send')
    actors = []
    original = service._actor_signature
    def capture(actor):
        actors.append(actor)
        return original(actor)
    private = []
    async def forbidden():
        private.append('SMTP read')
        raise AssertionError('Unconfigured Energy must not read SMTP')
    hass.data[DOMAIN]['load_config'] = forbidden
    sent = []
    hass.data[DOMAIN]['send_email'] = lambda *args: sent.append('forbidden')
    with patch.object(service, '_actor_signature', new=capture):
        with pytest.raises(HomeAssistantError):
            await runtime.async_send(key, hass_admin_user.id, eid)
    assert actors and all(type(actor) is str and actor == hass_admin_user.id for actor in actors)
    assert private == []
    assert sent == []
