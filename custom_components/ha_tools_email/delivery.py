"""Bounded HA notification targets and guarded native handoff.

No recipient inference, SMTP reads, automatic retry or delivery claim. The caller
owns source/actor authorization and revalidates context immediately before IO.
"""
import re
from .history import async_dispatch_channel_attempt

_NOTIFY = re.compile(r'notify\.[a-z0-9_]{1,128}')

def normalize_channels(channels, *, legacy=False):
    """Missing old channels preserve email; new reports work without SMTP."""
    if channels is None:
        return ['email'] if legacy else ['persistent_notification']
    if not isinstance(channels,list) or not 1 <= len(channels) <= 32:
        raise ValueError('invalid_delivery_channels')
    result=[]
    for value in channels:
        if isinstance(value,str) and value.startswith('notify:'):
            value='notify.'+value[7:]
        if not isinstance(value,str) or not (value in ('email','persistent_notification') or _NOTIFY.fullmatch(value)):
            raise ValueError('invalid_delivery_channel')
        if value not in result:
            result.append(value)
    return result


def list_delivery_targets(services, *, smtp_configured=False, default_mobile_target=None):
    """Use registry service names, not caller-provided labels or private config.

    A mobile default requires separately proven association with the report owner;
    selecting the first mobile target can disclose household data to a guest.
    """
    notify=services.get('notify',{}) if isinstance(services,dict) else {}
    persistent=services.get('persistent_notification',{}) if isinstance(services,dict) else {}
    ids=['notify.'+name for name in sorted(notify) if isinstance(name,str) and _NOTIFY.fullmatch('notify.'+name)]
    if 'create' in persistent:
        ids.append('persistent_notification')
    if smtp_configured:
        ids.append('email')
    default=(default_mobile_target if default_mobile_target in ids and default_mobile_target.startswith('notify.mobile_app_')
             else 'persistent_notification' if 'persistent_notification' in ids else None)
    return [{'id':target,'name':target,'kind':'smtp' if target=='email' else 'persistent_notification' if target=='persistent_notification' else 'notify',
             'default':target==default} for target in ids]


async def async_dispatch_notification(hass,storage,target,compose,validate,**attempt):
    """Cross the service boundary once after both caller-owned authorization fences."""
    normalized=normalize_channels([target])[0]
    if normalized=='email':
        raise ValueError('smtp_requires_existing_dispatcher')
    async def prepare():
        await validate()
        payload=await compose()
        if not isinstance(payload,dict) or not all(isinstance(payload.get(key),str) for key in ('title','message')):
            raise ValueError('invalid_notification_payload')
        clean={key:payload[key] for key in ('title','message')}
        if normalized!='persistent_notification' and isinstance(payload.get('data'),dict):
            clean['data']=payload['data']
        await validate()
        return clean
    async def handoff(payload):
        domain,service=('persistent_notification','create') if normalized=='persistent_notification' else normalized.split('.',1)
        await hass.services.async_call(domain,service,payload,blocking=True)
    return await async_dispatch_channel_attempt(storage,prepare,handoff,**attempt)
