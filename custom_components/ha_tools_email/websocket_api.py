"""WebSocket API for HA Tools Email."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, EVENT_SCHEDULES_CHANGED, EVENT_REPORT_STATUS_UPDATED, VALID_CADENCES, VALID_KINDS
from .composer import (async_build_energy_report_payload, async_build_report_payload,
                       async_get_system_log_entries, build_log_digest_payload)
from .scheduler import (async_reload_schedules, async_send_now, calculate_next_fire,
                        calendar_report_window, effective_schedule_timezone, fire_period_key,
                        normalize_schedule_calendar, async_resolve_manual_period)
from .storage import EmailStorage, TariffRevisionConflict, FeatureAccessRevisionConflict
from .feature_access import default_feature_access, effective_feature_capabilities, feature_access_allowed
from .report_status import MAX_STATUS_SCHEDULES, PURPOSE_FEATURES
from .tariff import TariffValidationError
from .tariff_profiles import list_tariff_templates
from .analysis_provider import async_require_energy_source_access
from .energy_statistics_api import async_register_statistics_commands


def _bucket(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data[DOMAIN]


def _storage(hass: HomeAssistant) -> EmailStorage:
    return _bucket(hass)["storage"]


async def _safe_smtp_config(hass: HomeAssistant) -> dict[str, Any]:
    cfg = await _bucket(hass)["load_config"]()
    raw_password = cfg.get("password", "")
    return {
        "server": cfg.get("server", ""),
        "port": cfg.get("port", 587),
        "username": cfg.get("username", ""),
        "sender": cfg.get("sender", ""),
        "encryption": cfg.get("encryption", "starttls"),
        "default_recipient": cfg.get("default_recipient", ""),
        "uses_secret": str(raw_password).startswith("!secret "),
        "smtp_configured": bool(
            cfg.get("server") and cfg.get("username") and raw_password
        ),
    }


async def _snapshot_authorized(hass, connection):
    """Recheck the authenticated active household user across asynchronous IO."""
    user = getattr(connection, 'user', None)
    if user is None or not getattr(user, 'is_active', False):
        return False
    current = await hass.auth.async_get_user(user.id)
    return current is not None and current.is_active


def _energy_context_current(hass, coordinator, result):
    """No await between this lifecycle/context guard and the result send."""
    if hass.data.get(DOMAIN, {}).get('energy_snapshot') is not coordinator:
        return False
    if not getattr(coordinator, 'running', not getattr(coordinator, '_stopped', False)):
        return False
    check = getattr(coordinator, 'tariff_context_is_current', None)
    return check(result) if check is not None else True


async def _book_authorized(hass, connection, *, admin=False):
    user = getattr(connection, 'user', None)
    if user is None or not getattr(user, 'is_active', False):
        return False
    current = await hass.auth.async_get_user(user.id)
    return (current is not None and current.is_active
            and (not admin or getattr(current, 'is_admin', False)))


def _book_context_current(hass, storage, coordinator, revision=None):
    bucket = hass.data.get(DOMAIN, {})
    return (bucket.get('storage') is storage and bucket.get('energy_snapshot') is coordinator
            and coordinator is not None and getattr(coordinator, 'running', False)
            and (revision is None or storage.current_tariff_revision == revision))


async def _feature_authorized(hass, connection, feature, operation, *, token=None):
    """Capture policy once; later checks use fresh identity plus revision fencing.

    Store write callbacks pass this internal token, so they never reenter the
    Store lock. Nothing in this token comes from a client or cached role.
    """
    try:
        if token is None:
            storage = hass.data.get(DOMAIN, {}).get('storage')
            state = await storage.async_get_feature_access()
            token = storage, state
        else:
            storage, state = token
        user = await _feature_user(hass, connection)
        if (user is None or state['status'] != 'ready'
                or not feature_access_allowed(state['policy'], feature, operation,
                    user_id=connection.user.id, is_active=user.is_active,
                    is_admin=getattr(user, 'is_admin', False))
                or not _feature_context_current(hass, storage, state['revision'])):
            return None
        return token
    except Exception:
        # Fail closed without exposing Store or authentication error details.
        # Cancellation inherits BaseException and still propagates normally.
        return None


def _feature_context_current(hass, storage, revision=None):
    return (storage is not None
            and hass.data.get(DOMAIN, {}).get('storage') is storage
            and getattr(hass, 'state', None) != 'stopping'
            and (revision is None or storage.current_feature_access_revision == revision))


async def _feature_user(hass, connection):
    user = getattr(connection, 'user', None)
    if user is None or not getattr(user, 'is_active', False):
        return None
    current = await hass.auth.async_get_user(user.id)
    return current if current is not None and current.is_active is True else None


@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/subscribe_analysis_updates'})
@websocket_api.async_response
async def _ws_subscribe_analysis_updates(hass, connection, msg):
    """Control-only invalidation, independent of feature read permission.

    Revoking feature access must still purge an already mounted view. Actual
    reads retain their own fresh feature and native source authorization.
    """
    service = hass.data.get(DOMAIN, {}).get('energy_analysis')
    if await _feature_user(hass, connection) is None:
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    if (service is None or service._stopped
            or hass.data.get(DOMAIN, {}).get('energy_analysis') is not service):
        connection.send_error(msg['id'], 'analysis_subscription_unavailable', 'Analysis updates unavailable.')
        return
    removers = []
    stopped = False

    @callback
    def unsubscribe():
        nonlocal stopped
        if stopped:
            return
        stopped = True
        for remove in removers:
            remove()
        removers.clear()
        service._update_subscriptions.discard(unsubscribe)
        # Core owns its subscription dictionary (including close iteration).
        # Keep this idempotent callback there until Core removes it.

    @callback
    def changed(_event):
        if stopped:
            return
        if (service._stopped or
                hass.data.get(DOMAIN, {}).get('energy_analysis') is not service):
            unsubscribe()
            return
        # Never forward event.data, identities, policies or entity values.
        connection.send_event(msg['id'], {})

    try:
        for event in (f'{DOMAIN}_energy_analysis_updated', f'{DOMAIN}_feature_access_updated',
                      EVENT_REPORT_STATUS_UPDATED, EVENT_SCHEDULES_CHANGED):
            removers.append(hass.bus.async_listen(event, changed))
        service._update_subscriptions.add(unsubscribe)
        connection.subscriptions[msg['id']] = unsubscribe
        connection.send_result(msg['id'])
    except BaseException:
        unsubscribe()
        raise


@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/get_feature_access'})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_get_feature_access(hass, connection, msg):
    """Allowlist settings are visible only to the current active administrator."""
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    storage = hass.data.get(DOMAIN, {}).get('storage')
    try:
        result = await storage.async_get_feature_access()
    except Exception:
        connection.send_error(msg['id'], 'feature_access_read_failed', 'Access settings could not be read.')
        return
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    if not _feature_context_current(hass, storage, result['revision']):
        connection.send_error(msg['id'], 'feature_access_changed', 'Access settings changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/feature_capabilities'})
@websocket_api.async_response
async def _ws_feature_capabilities(hass, connection, msg):
    """Only this authenticated user's tool gates; native resource rights still apply."""
    if await _feature_user(hass, connection) is None:
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    storage = hass.data.get(DOMAIN, {}).get('storage')
    try:
        state = await storage.async_get_feature_access()
        user = await _feature_user(hass, connection)
        if user is None:
            raise PermissionError('unauthorized')
        valid = state['status'] == 'ready'
        capabilities = effective_feature_capabilities(
            state['policy'] if valid else default_feature_access(), user_id=connection.user.id,
            is_active=valid, is_admin=getattr(user, 'is_admin', False))
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    except Exception:
        connection.send_error(msg['id'], 'feature_access_read_failed', 'Access settings could not be read.')
        return
    if not _feature_context_current(hass, storage, state['revision']):
        connection.send_error(msg['id'], 'feature_access_changed', 'Access settings changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], {'capabilities': capabilities, 'revision': state['revision'],
                                      'status': state['status'], 'energy_model_schema': 'ha_tools.energy_report/1'})


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/set_feature_access',
    vol.Required('policy'): dict,
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_set_feature_access(hass, connection, msg):
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    storage = hass.data.get(DOMAIN, {}).get('storage')

    async def before_save():
        if not await _book_authorized(hass, connection, admin=True):
            return False
        return _feature_context_current(hass, storage)

    try:
        result = await storage.async_save_feature_access(msg['policy'],
            expected_revision=msg['expected_revision'], before_save=before_save)
    except FeatureAccessRevisionConflict:
        connection.send_error(msg['id'], 'feature_access_revision_conflict', 'Access settings changed. Reload before saving.')
        return
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    except ValueError:
        connection.send_error(msg['id'], 'invalid_feature_access', 'Access settings or revision are invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'feature_access_save_failed', 'Access settings could not be saved.')
        return
    # This event is safe even if the saving user's role is revoked after commit.
    # It contains no identities, policy content, or household data.
    hass.bus.async_fire(f'{DOMAIN}_feature_access_updated', {'revision': result['revision']})
    if not await _book_authorized(hass, connection, admin=True):
        connection.send_error(msg['id'], 'unauthorized', 'Active administrator required.')
        return
    if not _feature_context_current(hass, storage, result['revision']):
        connection.send_error(msg['id'], 'feature_access_changed', 'Access settings changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/get_tariff_profile',
    vol.Optional('tariff_id'): str,
    vol.Optional('version_id'): str,
    vol.Optional('legacy_combined'): bool,
})
@websocket_api.async_response
async def _ws_get_tariff_profile(hass, connection, msg):
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'read')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    storage = access[0]
    coordinator = hass.data.get(DOMAIN, {}).get('energy_snapshot')
    try:
        result = await storage.async_get_tariff_profile(tariff_id=msg.get('tariff_id'),
            version_id=msg.get('version_id'), legacy_combined=msg.get('legacy_combined'))
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile or selected version is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_profile_read_failed', 'Tariff profile could not be read.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'read', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff settings changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/set_tariff_profile',
    vol.Required('profile'): dict,
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
    vol.Optional('tariff_id'): str,
    vol.Optional('version_id'): str,
    vol.Optional('legacy_combined'): bool,
    vol.Optional('historical_edit_ack', default=False): bool,
})
@websocket_api.async_response
async def _ws_set_tariff_profile(hass, connection, msg):
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'execute')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    storage = access[0]
    coordinator = hass.data.get(DOMAIN, {}).get('energy_snapshot')

    async def before_save():
        if not await _feature_authorized(hass, connection, 'energy_tariff', 'execute', token=access):
            return False
        return _book_context_current(hass, storage, coordinator)

    try:
        result = await storage.async_save_tariff_profile(msg['profile'],
            expected_revision=msg['expected_revision'], tariff_id=msg.get('tariff_id'),
            version_id=msg.get('version_id'), legacy_combined=msg.get('legacy_combined'),
            historical_edit_ack=msg.get('historical_edit_ack', False), before_save=before_save)
    except TariffRevisionConflict:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff settings changed. Reload before saving.')
        return
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile, migration or revision is invalid.')
        return
    except ValueError as error:
        if str(error) == 'historical_edit_ack_required':
            connection.send_error(msg['id'], 'historical_edit_ack_required',
                'This edit changes historical calculations. Review the affected dates and confirm explicitly.')
        else:
            connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile or revision is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_profile_save_failed', 'Tariff profile could not be saved.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'execute', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff settings changed. Reload before continuing.')
        return
    hass.async_create_task(coordinator._async_tick(None))
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/list_tariff_templates',
    vol.Optional('holiday_dates'): list,
})
@websocket_api.async_response
async def _ws_list_tariff_templates(hass, connection, msg):
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'read')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    storage = access[0]
    config = getattr(hass, 'config', None)
    currency, zone = getattr(config, 'currency', None), getattr(config, 'time_zone', None)
    try:
        if not isinstance(currency, str) or not isinstance(zone, str):
            raise ValueError('missing_home_configuration')
        # Only bounded template construction (at most 366 explicit dates).
        # This does not compile schedules or invoke the pricing engine.
        result = {'templates': list_tariff_templates(currency=currency, timezone=zone,
                                                     holiday_dates=msg.get('holiday_dates')),
                  'currency': currency, 'timezone': zone}
    except (TariffValidationError, ValueError):
        connection.send_error(msg['id'], 'invalid_tariff_template_request', 'Template settings or holiday dates are invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_templates_read_failed', 'Tariff templates could not be read.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'read', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    if (not _feature_context_current(hass, storage, access[1]['revision'])
            or (getattr(hass.config, 'currency', None), getattr(hass.config, 'time_zone', None)) != (currency, zone)):
        connection.send_error(msg['id'], 'tariff_context_changed', 'Tariff settings changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/get_tariff_book'})
@websocket_api.async_response
async def _ws_get_tariff_book(hass, connection, msg):
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'read')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    bucket = hass.data.get(DOMAIN, {})
    storage, coordinator = bucket.get('storage'), bucket.get('energy_snapshot')
    try:
        result = await storage.async_get_tariff_book()
    except Exception:
        connection.send_error(msg['id'], 'tariff_book_read_failed', 'Tariff history could not be read.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'read', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_book_changed', 'Tariff history changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/preview_tariff_book',
    vol.Required('book'): dict,
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
})
@websocket_api.async_response
async def _ws_preview_tariff_book(hass, connection, msg):
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'execute')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    bucket = hass.data.get(DOMAIN, {})
    storage, coordinator = bucket.get('storage'), bucket.get('energy_snapshot')
    try:
        result = await storage.async_preview_tariff_book(msg['book'], expected_revision=msg['expected_revision'])
    except TariffRevisionConflict:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff history changed. Reload before continuing.')
        return
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_book', 'Tariff history or revision is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_book_preview_failed', 'Tariff history could not be validated.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'execute', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_book_changed', 'Tariff history changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/set_tariff_book',
    vol.Required('book'): dict,
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
    vol.Optional('historical_edit_ack', default=False): bool,
})
@websocket_api.async_response
async def _ws_set_tariff_book(hass, connection, msg):
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'execute')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    bucket = hass.data.get(DOMAIN, {})
    storage, coordinator = bucket.get('storage'), bucket.get('energy_snapshot')

    async def before_save():
        if not await _feature_authorized(hass, connection, 'energy_tariff', 'execute', token=access):
            return False
        return _book_context_current(hass, storage, coordinator)

    try:
        result = await storage.async_save_tariff_book(msg['book'],
            expected_revision=msg['expected_revision'],
            historical_edit_ack=msg.get('historical_edit_ack', False), before_save=before_save)
    except TariffRevisionConflict:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff history changed. Reload before saving.')
        return
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_book', 'Tariff history or revision is invalid.')
        return
    except ValueError as error:
        if str(error) == 'historical_edit_ack_required':
            connection.send_error(msg['id'], 'historical_edit_ack_required',
                                  'This edit changes historical calculations. Review the affected dates and confirm explicitly.')
        else:
            connection.send_error(msg['id'], 'invalid_tariff_book', 'Tariff history or revision is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_book_save_failed', 'Tariff history could not be saved.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'execute', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    if not _book_context_current(hass, storage, coordinator, result['revision']):
        connection.send_error(msg['id'], 'tariff_book_changed', 'Tariff history changed. Reload before continuing.')
        return
    hass.async_create_task(coordinator._async_tick(None))
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_patterns',
    vol.Required('days'): vol.In((7, 30)),
})
@websocket_api.async_response
async def _ws_energy_patterns(hass, connection, msg):
    """Only the server chooses clock, timezone, sources and access purpose."""
    if (set(msg) - {'id', 'type', 'days'} or type(msg.get('days')) is not int
            or msg['days'] not in (7, 30)):
        connection.send_error(msg['id'], 'invalid_patterns_selection', 'Choose 7 or 30 local days.')
        return
    if not await _snapshot_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    service = hass.data.get(DOMAIN, {}).get('energy_analysis')
    if service is None:
        connection.send_error(msg['id'], 'energy_patterns_unavailable', 'Energy patterns are unavailable.')
        return
    user_id = connection.user.id
    try:
        result = await service.async_get_patterns(msg['days'], user_id)
        if not await _snapshot_authorized(hass, connection):
            raise PermissionError('energy_sources_unauthorized')
        if await service.async_validate_result(result, user_id) is not True:
            raise RuntimeError('energy_analysis_changed')
        if (hass.data.get(DOMAIN, {}).get('energy_analysis') is not service
                or result.get('context', {}).get('access_purpose') != 'analysis'
                or result.get('context', {}).get('request_kind') != 'patterns'
                or not service.context_is_current(result)):
            raise RuntimeError('energy_analysis_changed')
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Energy source access required.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_patterns_unavailable', 'Energy patterns are unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_analysis',
    vol.Optional('period', default='day'): vol.In(('day', 'week', 'month', 'year')),
    vol.Optional('anchor'): str,
    vol.Optional('comparison'): str,
})
@websocket_api.async_response
async def _ws_energy_analysis(hass, connection, msg):
    """Detailed source-aware analysis; identity comes only from the connection."""
    if not await _snapshot_authorized(hass, connection):
        connection.send_error(msg['id'], 'unauthorized', 'Active household user required.')
        return
    service = hass.data.get(DOMAIN, {}).get('energy_analysis')
    if service is None:
        connection.send_error(msg['id'], 'energy_analysis_unavailable', 'Energy analysis is unavailable.')
        return
    user_id = connection.user.id
    try:
        result = await service.async_get_analysis(msg.get('period', 'day'), user_id,
            **{key: msg[key] for key in ('anchor', 'comparison') if key in msg})
        if not await _snapshot_authorized(hass, connection):
            raise PermissionError('energy_sources_unauthorized')
        # This last async validation includes the concrete current source access
        # policy. Do not insert another await before the final context/send.
        if await service.async_validate_result(result, user_id) is not True:
            raise RuntimeError('energy_analysis_changed')
        if (hass.data.get(DOMAIN, {}).get('energy_analysis') is not service
                or not service.context_is_current(result)):
            raise RuntimeError('energy_analysis_changed')
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Energy source access required.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_analysis_unavailable', 'Energy analysis is unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_report',
    vol.Optional('period', default='day'): vol.In(('day', 'week', 'month', 'year')),
    vol.Optional('anchor'): str,
    vol.Optional('date'): str,
    vol.Optional('mode'): vol.In(('so_far','full')),
    vol.Optional('representation'): vol.In(('model','document')),
    vol.Optional('comparison'): str,
    vol.Optional('language'): vol.In(('pl', 'en')),
    vol.Optional('date_language'): vol.In(('pl', 'en')),
    vol.Optional('timezone'): str,
    vol.Optional('hour_cycle'): vol.In(('12', '24')),
})
@websocket_api.async_response
async def _ws_energy_report(hass, connection, msg):
    """Read-only report in HA; identity and feature purpose are server-owned."""
    if msg.get('representation') == 'model':
        if any(key in msg for key in ('anchor','comparison','language','date_language','timezone','hour_cycle')):
            connection.send_error(msg['id'],'invalid_model_request','Use date and mode for the model representation.')
            return
        if not await _feature_authorized(hass,connection,'energy_analysis','read'):
            connection.send_error(msg['id'],'unauthorized','Energy access required.')
            return
        service = hass.data.get(DOMAIN,{}).get('energy_analysis')
        try:
            result = await service.async_get_build1_model(msg.get('period','day'),connection.user.id,
                date=msg.get('date'),mode=msg.get('mode','so_far'),purpose='analysis')
            if (not await service.async_validate_result(result,connection.user.id)
                    or hass.data.get(DOMAIN,{}).get('energy_analysis') is not service
                    or not service.context_is_current(result)):
                raise RuntimeError('energy_analysis_changed')
        except PermissionError:
            connection.send_error(msg['id'],'unauthorized','Energy or source access required.')
            return
        except Exception:
            connection.send_error(msg['id'],'energy_report_unavailable','Energy report is unavailable.')
            return
        connection.send_result(msg['id'],result['report_model'])
        return
    if not all(key in msg for key in ('language','timezone','hour_cycle')) or any(key in msg for key in ('date','mode')):
        connection.send_error(msg['id'],'invalid_report_request','Document display preferences are required.')
        return

    if not await _feature_authorized(hass, connection, 'energy_reports', 'read'):
        connection.send_error(msg['id'], 'unauthorized', 'Report access required.')
        return
    service = hass.data.get(DOMAIN, {}).get('energy_analysis')
    if service is None:
        connection.send_error(msg['id'], 'energy_report_unavailable', 'Energy report is unavailable.')
        return
    user_id = connection.user.id
    try:
        result = await service.async_get_report(msg.get('period', 'day'), user_id,
            language=msg['language'], timezone_name=msg['timezone'], hour_cycle=msg['hour_cycle'],
            **{key: msg[key] for key in ('anchor', 'comparison', 'date_language') if key in msg})
        if await service.async_validate_result(result, user_id) is not True:
            raise RuntimeError('energy_analysis_changed')
        if (hass.data.get(DOMAIN, {}).get('energy_analysis') is not service
                or result.get('context', {}).get('access_purpose') != 'report_read'
                or not service.context_is_current(result)):
            raise RuntimeError('energy_analysis_changed')
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Report or source access required.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_report_unavailable', 'Energy report is unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_snapshot',
    vol.Optional('profile'): vol.Any(dict, None),
})
@websocket_api.async_response
async def _ws_energy_snapshot(hass, connection, msg):
    access = await _feature_authorized(hass, connection, 'energy_analysis', 'read')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Energy analysis access required.')
        return
    coordinator = hass.data.get(DOMAIN, {}).get('energy_snapshot')
    if coordinator is None:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy snapshot is unavailable.')
        return
    storage, state = access
    last_ids = None

    def current():
        return (_feature_context_current(hass, storage, state['revision'])
                and hass.data.get(DOMAIN, {}).get('energy_snapshot') is coordinator
                and getattr(coordinator, 'running', not getattr(coordinator, '_stopped', False)))

    async def guard(source_ids):
        nonlocal last_ids
        if not current():
            raise RuntimeError('energy_snapshot_changed')
        # The coordinator supplies actual configured IDs before private IO.
        # One fresh HA user is checked against BOTH feature and resource rights.
        await async_require_energy_source_access(hass, connection.user.id, source_ids,
                                                 feature_policy=state['policy'])
        if not current():
            raise RuntimeError('energy_snapshot_changed')
        last_ids = list(source_ids)

    try:
        result = await coordinator.async_snapshot(guard=guard,
            **({'profile': msg['profile']} if 'profile' in msg else {}))
        if last_ids is None:
            raise RuntimeError('energy_source_guard_not_called')
        await guard(last_ids)
        # No await between the final identity/context checks and sending.
        if not _energy_context_current(hass, coordinator, result):
            raise RuntimeError('energy_snapshot_changed')
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Energy source access required.')
        return
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy snapshot is unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/energy_price_queries',
    vol.Required('queries'): list,
    vol.Optional('profile'): vol.Any(dict, None),
})
@websocket_api.async_response
async def _ws_energy_price_queries(hass, connection, msg):
    """Read-only pricing of supplied card buckets, never household measurement."""
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'read')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    coordinator = hass.data.get(DOMAIN, {}).get('energy_snapshot')
    if coordinator is None:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy pricing is unavailable.')
        return
    try:
        result = await coordinator.async_price_queries(msg['queries'],
            **({'profile': msg['profile']} if 'profile' in msg else {}))
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile is invalid.')
        return
    except ValueError:
        connection.send_error(msg['id'], 'invalid_energy_price_queries', 'Price queries exceed supported structure or limits.')
        return
    except Exception:
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy pricing is unavailable.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'read', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    if not _energy_context_current(hass, coordinator, result):
        connection.send_error(msg['id'], 'energy_unavailable', 'Energy pricing is unavailable.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_tariff"})
@websocket_api.async_response
async def _ws_get_tariff(hass, connection, msg) -> None:
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'read')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    storage = access[0]
    try:
        result = await storage.async_get_tariff()
    except Exception:
        connection.send_error(msg['id'], 'tariff_read_failed', 'Tariff settings could not be read.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'read', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff read access required.')
        return
    if storage.current_tariff_revision != result['revision']:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff settings changed. Reload before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/set_tariff',
    vol.Required('profile'): vol.Any(dict, None),
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
})
@websocket_api.async_response
async def _ws_set_tariff(hass, connection, msg) -> None:
    access = await _feature_authorized(hass, connection, 'energy_tariff', 'execute')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    storage = access[0]

    async def before_save():
        return await _feature_authorized(hass, connection, 'energy_tariff', 'execute', token=access) is not None

    try:
        result = await storage.async_save_tariff(msg['profile'],
            expected_revision=msg['expected_revision'], before_save=before_save)
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    except TariffRevisionConflict:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff settings changed. Reload before saving.')
        return
    except TariffValidationError:
        connection.send_error(msg['id'], 'invalid_tariff_profile', 'Tariff profile or revision is invalid.')
        return
    except Exception:
        connection.send_error(msg['id'], 'tariff_save_failed', 'Tariff settings could not be saved.')
        return
    if not await _feature_authorized(hass, connection, 'energy_tariff', 'execute', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Tariff editing access required.')
        return
    if storage.current_tariff_revision != result['revision']:
        connection.send_error(msg['id'], 'tariff_revision_conflict', 'Tariff settings changed. Reload before continuing.')
        return
    coordinator = _bucket(hass).get('energy_snapshot')
    if coordinator is not None:
        hass.async_create_task(coordinator._async_tick(None))
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({
    vol.Required('type'): f'{DOMAIN}/get_report_status',
    vol.Required('purpose'): vol.In(sorted(PURPOSE_FEATURES)),
})
@websocket_api.async_response
async def _ws_get_report_status(hass, connection, msg):
    """Read only this purpose's compact facts, under independent feature policy."""
    purpose = msg.get('purpose')
    feature = PURPOSE_FEATURES.get(purpose) if isinstance(purpose, str) else None
    if feature is None:
        connection.send_error(msg['id'], 'invalid_report_status', 'Unsupported report purpose.')
        return
    access = await _feature_authorized(hass, connection, feature, 'read')
    if access is None:
        connection.send_error(msg['id'], 'unauthorized', 'Report status access denied.')
        return
    storage, policy = access
    bucket = hass.data.get(DOMAIN, {})
    coordinator = bucket.get('energy_snapshot')
    zone = getattr(getattr(hass, 'config', None), 'time_zone', 'UTC')
    generation = bucket.get('schedule_generation')
    consumed = bucket.get('schedule_consumed_periods', {})
    if (not _book_context_current(hass, storage, coordinator)
            or not isinstance(consumed, dict) or len(consumed) > MAX_STATUS_SCHEDULES):
        connection.send_error(msg['id'], 'report_status_unavailable', 'Report status unavailable.')
        return
    consumed = dict(consumed)
    try:
        result, token = await storage.async_get_report_status(purpose,
            now=_utcnow(), local_timezone=zone, consumed=consumed)
    except Exception:
        connection.send_error(msg['id'], 'report_status_unavailable', 'Report status unavailable.')
        return
    if not await _feature_authorized(hass, connection, feature, 'read', token=access):
        connection.send_error(msg['id'], 'unauthorized', 'Report status access denied.')
        return
    # No await between these context/revision/calendar guards and send_result.
    next_at = result['next_report_at']
    if (not _book_context_current(hass, storage, coordinator)
            or not storage.report_status_context_is_current(token)
            or bucket is not hass.data.get(DOMAIN)
            or generation != bucket.get('schedule_generation')
            or consumed != bucket.get('schedule_consumed_periods', {})
            or zone != getattr(getattr(hass, 'config', None), 'time_zone', 'UTC')
            or (next_at is not None and datetime.fromisoformat(next_at) <= _utcnow())):
        connection.send_error(msg['id'], 'report_status_unavailable', 'Report status changed. Refresh before continuing.')
        return
    connection.send_result(msg['id'], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_attempt_history"})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_get_attempt_history(hass, connection, msg) -> None:
    """Read bounded metadata only; history is never a content export."""
    try:
        result = await _storage(hass).async_get_attempt_history()
    except Exception:
        connection.send_error(msg["id"], "history_unavailable", "Email attempt history is unavailable.")
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get_config"})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_get_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return non-secret SMTP state and schedules."""
    config = await _safe_smtp_config(hass)
    config["schedules"] = [{key:value for key,value in row.items() if key != "author_user_id"}
                           for row in await _storage(hass).async_list_schedules()]
    connection.send_result(msg["id"], config)


def _utcnow() -> datetime:
    """Trusted server clock; client timestamps are not accepted."""
    return datetime.now(timezone.utc)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list_schedules"})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_list_schedules(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Legacy envelope plus computed metadata; never save or inspect SMTP."""
    storage = _storage(hass)
    rows = await storage.async_list_schedules()
    now = _utcnow()
    local_zone = getattr(getattr(hass, "config", None), "time_zone", None)
    result = []
    for row in rows:
        metadata = {"next_fire": None, "effective_timezone": None}
        try:
            zone = effective_schedule_timezone(row, now, local_zone)
            metadata["effective_timezone"] = getattr(zone, "key", str(zone))
            if row.get("enabled", True):
                next_fire = calculate_next_fire(row, now, local_timezone=local_zone)
                last = await storage.async_get_last_fired(row["id"])
                if fire_period_key(row, next_fire, local_timezone=local_zone) == last:
                    next_fire = calculate_next_fire(row, next_fire, local_timezone=local_zone)
                metadata["next_fire"] = next_fire.isoformat()
        except (ValueError, TypeError, KeyError):
            # No private parser errors or stored credentials in metadata.
            pass
        result.append({**{key:value for key,value in row.items() if key != "author_user_id"}, **metadata})
    connection.send_result(msg["id"], {"schedules": result})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_schedule",
        vol.Optional("action", default="upsert"): vol.In(["create", "update", "upsert", "delete"]),
        vol.Optional("schedule"): dict,
        vol.Optional("schedule_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_set_schedule(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Create, update, or delete a schedule."""
    storage = _storage(hass)
    action = msg.get("action", "upsert")
    try:
        if action == "delete":
            schedule_id = msg.get("schedule_id") or (msg.get("schedule") or {}).get("id")
            if not schedule_id:
                raise ValueError("schedule_id is required for delete")
            removed = await storage.async_delete_schedule(schedule_id)
            result = {"deleted": removed, "schedule_id": schedule_id}
        else:
            schedule = msg.get("schedule")
            if not isinstance(schedule, dict):
                raise ValueError("schedule object is required")
            if any(key in schedule for key in ('author_user_id','actor','actor_id','purpose','server_actor')):
                raise ValueError('reserved_schedule_actor_field')
            user = await hass.auth.async_get_user(connection.user.id)
            if user is None or not user.is_active or not user.is_admin:
                raise PermissionError('schedule_admin_required')
            saved = await storage.async_upsert_schedule(schedule,author_user_id=connection.user.id)
            saved = {key:value for key,value in saved.items() if key != 'author_user_id'}
            result = {"schedule": saved}
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_payload", str(err))
        return
    except Exception:
        connection.send_error(msg["id"], "storage_write_failed", "Schedule state could not be saved.")
        return

    await async_reload_schedules(hass)
    schedules = [{key:value for key,value in row.items() if key != "author_user_id"}
                 for row in await storage.async_list_schedules()]
    hass.bus.async_fire(EVENT_SCHEDULES_CHANGED, {"schedules": schedules})
    result["schedules"] = schedules
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/send_now",
        vol.Required("kind"): vol.In(sorted(VALID_KINDS)),
        vol.Required("cadence"): vol.In(sorted(VALID_CADENCES)),
        vol.Optional("recipients", default=[]): vol.Any([str], str),
        vol.Optional("report_preferences"): dict,
        vol.Optional("period_mode"): vol.In(["rolling", "calendar"]),
        vol.Optional("timezone"): str,
        vol.Optional("max_entries"): vol.All(int, vol.Range(min=1, max=1000)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_send_now(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Compose and send a report immediately."""
    recipients = msg.get("recipients") or []
    if isinstance(recipients, str):
        recipients = [item.strip() for item in recipients.split(",") if item.strip()]
    try:
        result = await async_send_now(
            hass,
            kind=msg["kind"],
            cadence=msg["cadence"],
            recipients=recipients,
            **({"actor":connection.user.id} if msg["kind"] == "energy_report" else {}),
            **({"report_preferences":msg["report_preferences"]} if msg["kind"] == "energy_report" and "report_preferences" in msg else {}),
            **({"period_mode": msg["period_mode"]} if "period_mode" in msg else {}),
            **({"report_timezone": msg["timezone"]} if "timezone" in msg else {}),
            **({"max_entries": msg["max_entries"]} if "max_entries" in msg else {}),
        )
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "send_failed", str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/preview_energy_report",
        vol.Optional("report_preferences"): dict,
        vol.Required("cadence"): vol.In(sorted(VALID_CADENCES)),
        vol.Optional("calendar_period"): bool,
        vol.Optional("format"): vol.In(["summary", "mail"]),
        vol.Optional("period_mode"): vol.In(["rolling", "calendar"]),
        vol.Optional("timezone"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_preview_energy_report(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Legacy admin projections from the single authorized report path."""
    if 'calendar_period' in msg and type(msg['calendar_period']) is not bool:
        connection.send_error(msg['id'],'invalid_payload','calendar_period must be boolean')
        return
    if msg.get('format') != 'mail' and 'period_mode' in msg:
        connection.send_error(msg['id'],'invalid_payload','period_mode requires format=mail')
        return
    if msg.get('format') != 'mail' and 'timezone' in msg and not msg.get('calendar_period',False):
        connection.send_error(msg['id'],'invalid_payload','Timezone requires calendar_period=true.')
        return
    await _async_preview_mail(hass,connection,msg,'energy_report')


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/preview_log_digest",
    vol.Optional("format"): vol.In(["summary", "mail"]),
    vol.Optional("period_mode"): vol.In(["rolling", "calendar"]),
    vol.Optional("max_entries"): vol.All(int, vol.Range(min=1, max=1000)),
    vol.Required("cadence"): vol.In(sorted(VALID_CADENCES)),
    vol.Optional("timezone"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_preview_log_digest(hass, connection, msg) -> None:
    """Readonly retained log aggregate preview for a completed HA calendar period."""
    if msg.get("format") == "mail":
        await _async_preview_mail(hass, connection, msg, "log_digest")
        return
    if "period_mode" in msg or "max_entries" in msg:
        connection.send_error(msg["id"], "invalid_payload", "period_mode/max_entries require format=mail")
        return
    now = _utcnow()
    cadence = msg["cadence"]
    local_zone = getattr(getattr(hass, "config", None), "time_zone", None)
    context = {"cadence": cadence}
    if "timezone" in msg:
        context["timezone"] = msg["timezone"]
    try:
        await asyncio.to_thread(normalize_schedule_calendar, context)
        zone = effective_schedule_timezone(context, now, local_zone)
        start, end = calendar_report_window(context, now, local_timezone=local_zone)
    except (ValueError, TypeError, KeyError):
        connection.send_error(msg["id"], "invalid_payload", "Invalid calendar timezone.")
        return
    entries = await async_get_system_log_entries(hass)
    # Reuse the existing pure composer, with a bounded timestamp filter. Missing
    # timestamps remain explicitly incomplete; counts are lifetime aggregates.
    from .composer import _entry_time
    entries = [row for row in entries if _entry_time(row) is None or _entry_time(row) < end]
    payload = build_log_digest_payload(entries, cadence=cadence, now=now, since=start - timedelta(microseconds=1))
    summary = payload["summary"]
    result = {key: summary[key] for key in
              ("total", "level_counts", "logger_counts", "recent_errors", "recent_warnings", "unique_problems", "counts_complete")}
    if not summary['unique_problems']:
        result.update(total=None, counts_complete=False, history_unavailable=True)
    # Internal datetime objects are not a WebSocket JSON contract.
    for key in ("recent_errors", "recent_warnings"):
        result[key] = [{"level": row.get("level"), "name": row.get("name"),
                        "message": row["_message"], "count": row.get("count"),
                        "timestamp": row["_timestamp_dt"].isoformat() if row["_timestamp_dt"] else None}
                       for row in result[key]]
    result.update(cadence=cadence, effective_timezone=getattr(zone, "key", str(zone)),
                  period={"from": start.isoformat(), "to": end.isoformat(),
                          "semantics": "previous_completed_calendar_period"},
                  source={"name": "system_log", "count_semantics": "retained_aggregates",
                          "complete": False, "retained_entries": len(entries),
                          "reason": "retained_last_observed_aggregates_not_full_event_history"})
    connection.send_result(msg["id"], result)


async def _async_preview_mail(hass, connection, msg, kind) -> None:
    """Shared server composer, no SMTP config, dispatch, Store writes or history."""
    now = _utcnow()
    # New mail contract defaults to rolling, matching explicit manual rolling send.
    # Existing summary contracts above preserve their historical default behavior.
    mode = msg.get("period_mode", "calendar" if msg.get("calendar_period") is True else "rolling")
    try:
        if "calendar_period" in msg and (type(msg["calendar_period"]) is not bool or
                msg["calendar_period"] != (mode == "calendar")):
            raise ValueError("calendar_period conflicts with period_mode")
        limit = msg.get("max_entries", 10)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("max_entries must be an integer between 1 and 1000")
        options = await async_resolve_manual_period(hass, kind=kind, cadence=msg["cadence"],
            now=now, period_mode=mode, report_timezone=msg.get("timezone"))
    except (ValueError, TypeError, KeyError) as err:
        connection.send_error(msg["id"], "invalid_payload", str(err))
        return
    start, end = options.pop("report_window")
    if kind == 'energy_report':
        try:
            # Pin the producing service before composition's first await. A new
            # generation must not validate and publish an older service's DTO.
            service = _bucket(hass).get('energy_analysis')
            if service is None:
                raise RuntimeError('energy_analysis_not_ready')
            user_id = connection.user.id
            payload = await async_build_report_payload(hass, _bucket(hass).get('storage'),
                kind=kind, cadence=msg['cadence'], now=now,
                window_start=start, window_end=end, **options,
                actor=user_id, purpose='report_read',
                report_preferences=msg.get('report_preferences'))
            if msg.get('format') != 'mail':
                result = payload['summary']
            else:
                result = {**{key:payload[key] for key in
                    ('snapshot_id','subject','body','html','csv','period')},
                    'cadence':msg['cadence'],'generated_at':now.isoformat(),
                    'effective_timezone':payload['period']['timezone'],
                    'source':payload['summary']['source']}
            if await service.async_validate_result(payload,user_id) is not True:
                raise RuntimeError('energy_analysis_context_changed')
            if (hass.data.get(DOMAIN, {}).get('energy_analysis') is not service
                    or payload.get('context', {}).get('access_purpose') != 'report_read'
                    or not service.context_is_current(payload)):
                raise RuntimeError('energy_analysis_context_changed')
        except PermissionError:
            connection.send_error(msg['id'], 'unauthorized', 'Report or source access required.')
            return
        except Exception:
            connection.send_error(msg['id'], 'energy_report_unavailable', 'Energy report is unavailable.')
            return
        # No await between the final service/context/purpose fence and release.
        connection.send_result(msg['id'],result)
        return
    payload = await async_build_report_payload(hass, _bucket(hass).get("storage"),
        kind=kind, cadence=msg["cadence"], now=now,
        window_start=start, window_end=end, **options,
        **({"actor":connection.user.id,"purpose":"report_read",
             "report_preferences":msg.get("report_preferences")} if kind == "energy_report" else {}),
        **({"max_entries": limit} if kind == "log_digest" else {}))
    summary = payload["summary"]
    source = summary.get("source") or {
        "name": "recorder", "kind": "grid_import", "status": summary.get("status", "no_data"),
        "complete": summary.get("status") == "ready", "source_ids": summary.get("source_ids", [])}
    connection.send_result(msg["id"], {
        "subject": payload["subject"], "body": payload["body"], "html": payload["html"],
        "cadence": msg["cadence"], "period": summary["period"], "source": source,
        "generated_at": now.isoformat(), **({"effective_timezone": options["report_timezone"]}
            if "report_timezone" in options else {}),
    })


@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/list_delivery_targets'})
@websocket_api.async_response
async def _ws_list_delivery_targets(hass,connection,msg):
    token = await _feature_authorized(hass,connection,'energy_reports','execute')
    if not token:
        connection.send_error(msg['id'],'unauthorized','Report access required.')
        return
    from .delivery import list_delivery_targets
    cfg = await _safe_smtp_config(hass)
    if not await _feature_authorized(hass,connection,'energy_reports','execute',token=token):
        connection.send_error(msg['id'],'unauthorized','Report access required.')
        return
    connection.send_result(msg['id'],list_delivery_targets(hass.services.async_services(),
        smtp_configured=cfg['smtp_configured']))


def async_register_commands(hass: HomeAssistant) -> None:
    """Register all websocket commands."""
    async_register_statistics_commands(hass)
    for handler in (
        _ws_get_feature_access,
        _ws_set_feature_access,
        _ws_feature_capabilities,
        _ws_subscribe_analysis_updates,
        _ws_get_tariff,
        _ws_get_tariff_book,
        _ws_get_tariff_profile,
        _ws_set_tariff_profile,
        _ws_list_tariff_templates,
        _ws_preview_tariff_book,
        _ws_set_tariff_book,
        _ws_energy_snapshot,
        _ws_energy_analysis,
        _ws_energy_patterns,
        _ws_energy_report,
        _ws_list_delivery_targets,
        _ws_energy_price_queries,
        _ws_set_tariff,
        _ws_get_config,
        _ws_get_attempt_history,
        _ws_get_report_status,
        _ws_list_schedules,
        _ws_set_schedule,
        _ws_send_now,
        _ws_preview_energy_report,
        _ws_preview_log_digest,
    ):
        websocket_api.async_register_command(hass, handler)
