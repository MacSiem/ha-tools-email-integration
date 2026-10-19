"""Owned native Email metadata lifecycle; no SMTP during refresh."""
import asyncio
from datetime import datetime, timedelta, timezone

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir, device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, EVENT_REPORT_STATUS_UPDATED
from .feature_access import feature_access_allowed
from .report_status import PURPOSE_FEATURES
from .scheduler import async_send_now


class EmailNativeCoordinator:
    def __init__(self, hass, entry, storage):
        self.hass, self.entry, self.storage = hass, entry, storage
        self.data, self.identities = {}, {}
        self.running = False
        self._unsubscribers, self._listeners, self._platforms = [], set(), []
        self._seen = None
        self._task = None
        self._requested = False

    async def async_start(self):
        self.running = True
        try:
            await self.async_refresh()
        except Exception:
            # Metadata IO/schema failure cannot break existing Energy entities.
            self.data = {}
        changed = callback(lambda event: self.async_request_refresh())
        self._unsubscribers = [self.hass.bus.async_listen(EVENT_REPORT_STATUS_UPDATED, changed),
            async_track_time_interval(self.hass, changed, timedelta(seconds=30))]

    def register_platform(self, add, factory):
        slot = [add, factory, set()]
        self._platforms.append(slot)
        self._add_new(slot)

    def _add_new(self, slot):
        add, factory, known = slot
        fresh = set(self.data) - known
        if fresh:
            add([entity for key in sorted(fresh) for entity in factory(key)])
            known.update(fresh)

    def async_add_listener(self, listener):
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def async_request_refresh(self):
        if not self.running:
            return
        self._requested = True
        if self._task is None or self._task.done():
            task = self.hass.async_create_task(self._refresh_loop())
            self._task = task
            def done(finished):
                if self._task is finished:
                    self._task = None
                if not finished.cancelled():
                    finished.exception()
            task.add_done_callback(done)

    async def _refresh_loop(self):
        while self.running and self._requested:
            self._requested = False
            try:
                await self.async_refresh()
            except Exception:
                if self.running:
                    self.data = {}
                    for listener in tuple(self._listeners):
                        listener({})

    async def async_refresh(self):
        bucket = self.hass.data.get(DOMAIN, {})
        zone, generation = self.hass.config.time_zone, bucket.get('schedule_generation')
        consumed = bucket.get('schedule_consumed_periods', {})
        if not isinstance(consumed, dict) or len(consumed) > 128:
            raise ValueError('native_report_context_unavailable')
        consumed = dict(consumed)
        result, token = await self.storage.async_get_report_status(None, now=datetime.now(timezone.utc),
            local_timezone=zone, consumed=consumed, native=True)
        if (not self.running or self.hass.data.get(DOMAIN) is not bucket
                or bucket.get('email_native') is not self or bucket.get('storage') is not self.storage
                or zone != self.hass.config.time_zone or generation != bucket.get('schedule_generation')
                or consumed != bucket.get('schedule_consumed_periods', {})
                or not self.storage.report_status_context_is_current(token)):
            return
        seen = set(result['history_ids']) if result['history_ids'] is not None else None
        fresh = {} if self._seen is None or seen is None else {
            key: [row for row in rows if row['id'] not in self._seen]
            for key, rows in result['attempts'].items()}
        # Track the complete bounded history, including currently unconfigured
        # schedules, so deletion/re-add never replays old attempts as new events.
        if seen is not None:
            self._seen = seen
        self.data, self.identities = result['schedules'], result['identities']
        # Update only this integration's exact schedule devices; HA preserves
        # name_by_user/area/disabled settings. No source Energy device matches.
        devices = dr.async_get(self.hass)
        own = dr.async_entries_for_config_entry(devices, self.entry.entry_id)
        for key, row in self.data.items():
            identifier = (DOMAIN, f'{self.entry.entry_id}_email_{key}')
            for device in own:
                name = f"HA Tools Email {row['cadence']}"
                if identifier in device.identifiers and device.name != name:
                    devices.async_update_device(device.id, name=name)
        for issue, active in result['repairs'].items():
            if active is True:
                ir.async_create_issue(self.hass, DOMAIN, issue, is_fixable=False,
                    severity=ir.IssueSeverity.WARNING, translation_key=issue)
            elif active is False:
                ir.async_delete_issue(self.hass, DOMAIN, issue)
        for slot in self._platforms:
            self._add_new(slot)
        for listener in tuple(self._listeners):
            listener(fresh)

    async def async_send(self, key, user_id, entity_id):
        if not isinstance(user_id, str) or not user_id:
            raise HomeAssistantError('An authenticated user is required')
        user = await self.hass.auth.async_get_user(user_id)
        if user is None or not user.is_active:
            raise HomeAssistantError('Email action unavailable')
        policy = await self.storage.async_get_feature_access()
        bucket = self.hass.data.get(DOMAIN, {})
        sid = self.identities.get(key)
        smtp_revision, schedule_revision = self.storage.current_smtp_revision, self.storage.current_schedule_revision
        generation = bucket.get('schedule_generation')
        selected = None
        async def guard():
            current = await self.hass.auth.async_get_user(user_id)
            row = self.data.get(key)
            if (not self.running or bucket is not self.hass.data.get(DOMAIN)
                    or bucket.get('email_native') is not self or bucket.get('storage') is not self.storage
                    or self.storage.current_feature_access_revision != policy['revision']
                    or self.storage.current_smtp_revision != smtp_revision
                    or self.storage.current_schedule_revision != schedule_revision
                    or generation != bucket.get('schedule_generation')
                    or row is None or not row['enabled'] or self.identities.get(key) != sid
                    or current is None or current.is_active is not True
                    or (row['kind'] == 'log_digest' and current.is_admin is not True)
                    or not current.permissions.check_entity(entity_id, POLICY_CONTROL)
                    or policy['status'] != 'ready'
                    or not feature_access_allowed(policy['policy'], PURPOSE_FEATURES[row['kind']], 'execute',
                        user_id=user_id, is_active=current.is_active, is_admin=current.is_admin)):
                raise HomeAssistantError('Email action unavailable')
        await guard()
        selected = await self.storage.async_get_schedule(sid)
        await guard()
        if selected is None or selected.get('enabled') is not True or selected.get('kind') != self.data[key]['kind']:
            raise HomeAssistantError('Email schedule unavailable')
        async def before_send():
            current = await self.storage.async_get_schedule(sid)
            await guard()
            if current != selected:
                raise HomeAssistantError('Email schedule changed')
        try:
            await async_send_now(self.hass, kind=selected['kind'], cadence=selected['cadence'],
                recipients=selected.get('recipients') or [], actor=user_id,
                before_send=before_send, attempt_schedule_id=sid)
        except Exception:
            raise HomeAssistantError('Email attempt unavailable; check its status') from None
        finally:
            self.async_request_refresh()

    def async_stop(self):
        self.running = False
        for remove in self._unsubscribers:
            remove()
        self._unsubscribers.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._listeners.clear()
        self._platforms.clear()
