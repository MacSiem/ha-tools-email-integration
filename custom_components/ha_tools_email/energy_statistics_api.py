"""Administrator-only backend contract for explicit global dataset sharing."""
import voluptuous as vol
from homeassistant.components import websocket_api

from .const import DOMAIN
from .energy_statistics_publisher import EnergyStatisticsPublisher


async def _actor(hass, connection):
    actor = getattr(getattr(connection, 'user', None), 'id', None)
    if type(actor) is not str:
        raise PermissionError('statistics_native_admin_required')
    user = await hass.auth.async_get_user(actor)
    if user is None or not user.is_active or not user.is_admin:
        raise PermissionError('statistics_native_admin_required')
    return actor


def _publisher(hass):
    bucket = hass.data.get(DOMAIN, {})
    service = bucket.get('energy_analysis')
    if service is None:
        raise RuntimeError('energy_analysis_unavailable')
    service._check_running()
    subject = bucket.get('energy_statistics')
    if subject is None or subject.service is not service:
        if subject is not None:
            subject.stop()
        subject = EnergyStatisticsPublisher(hass, service)
        bucket['energy_statistics'] = subject
    return subject


async def _execute(hass, connection, msg, operation):
    try:
        actor = await _actor(hass, connection)
        subject = _publisher(hass)
        if operation == 'status':
            result = await subject.async_status(actor)
        elif operation == 'prepare':
            result = await subject.async_prepare(actor)
        elif operation == 'enable':
            result = await subject.async_enable(actor, msg['proposal'], expected_revision=msg['expected_revision'],
                acknowledge_global=msg['acknowledge_global'],
                acknowledge_current_membership=msg['acknowledge_current_membership'])
        elif operation == 'disable':
            result = await subject.async_disable(actor, expected_revision=msg['expected_revision'])
        else:
            result = await subject.async_refresh(actor, backfill=msg.get('backfill', False),
                acknowledge_current_membership=msg.get('acknowledge_current_membership', False))
        if await _actor(hass, connection) != actor:
            raise PermissionError('statistics_actor_changed')
        subject._check()
        if hass.data.get(DOMAIN, {}).get('energy_statistics') is not subject:
            raise RuntimeError('energy_statistics_changed')
        connection.send_result(msg['id'], result)
    except PermissionError:
        connection.send_error(msg['id'], 'unauthorized', 'Native administrator access is required.')
    except (ValueError, RuntimeError, OSError):
        connection.send_error(msg['id'], 'energy_statistics_unavailable',
            'Sharing could not be confirmed. Review its current status before retrying.')


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/energy_statistics_status'})
@websocket_api.async_response
async def ws_statistics_status(hass, connection, msg):
    await _execute(hass, connection, msg, 'status')


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/energy_statistics_prepare'})
@websocket_api.async_response
async def ws_statistics_prepare(hass, connection, msg):
    await _execute(hass, connection, msg, 'prepare')


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/energy_statistics_enable',
    vol.Required('proposal'): dict, vol.Required('expected_revision'): vol.All(int, vol.Range(min=0)),
    vol.Required('acknowledge_global'): bool, vol.Required('acknowledge_current_membership'): bool})
@websocket_api.async_response
async def ws_statistics_enable(hass, connection, msg):
    await _execute(hass, connection, msg, 'enable')


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/energy_statistics_disable',
    vol.Required('expected_revision'): vol.All(int, vol.Range(min=0))})
@websocket_api.async_response
async def ws_statistics_disable(hass, connection, msg):
    await _execute(hass, connection, msg, 'disable')


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'): f'{DOMAIN}/energy_statistics_refresh',
    vol.Optional('backfill', default=False): bool,
    vol.Optional('acknowledge_current_membership', default=False): bool})
@websocket_api.async_response
async def ws_statistics_refresh(hass, connection, msg):
    await _execute(hass, connection, msg, 'refresh')


def async_register_statistics_commands(hass):
    for command in (ws_statistics_status, ws_statistics_prepare, ws_statistics_enable,
                    ws_statistics_disable, ws_statistics_refresh):
        websocket_api.async_register_command(hass, command)
