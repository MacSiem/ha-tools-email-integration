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


async def async_send_energy_channels(hass,storage,*,channels,cadence,actor=None,
        now=None,schedule_id=None,expected_schedule=None,report_window=None,
        report_timezone=None,period_semantics=None,report_preferences=None,
        before_send=None,attempt_schedule_id=None,recipients=None):
    """Compose once through the authorized producer and hand off each target once.

    Old callers keep the existing SMTP route. Explicit channels select this path;
    no SMTP configuration is read for a native-only report. A partial/unknown
    multi-target result is never retried here.
    """
    from datetime import datetime,timezone
    from .composer import async_build_report_payload
    from .history import async_dispatch_attempt
    selected=normalize_channels(channels)
    bucket=hass.data['ha_tools_email']
    service=bucket.get('energy_analysis')
    if service is None:
        raise RuntimeError('energy_analysis_not_ready')
    generation=bucket.get('schedule_generation',0)
    revision=storage.current_schedule_revision
    now=now or datetime.now(timezone.utc)
    if actor is None and expected_schedule is not None:
        actor=service._schedule_actor(expected_schedule,generation)
    service._actor_signature(actor)
    payload=None
    def fence():
        service._check_running()
        if (hass.data.get('ha_tools_email') is not bucket or bucket.get('storage') is not storage
            or bucket.get('energy_analysis') is not service or bucket.get('schedule_generation',0)!=generation
            or storage.current_schedule_revision!=revision):
            raise RuntimeError('email_delivery_context_changed')
        if payload is not None and not service.context_is_current(payload):
            raise RuntimeError('energy_analysis_context_changed')
    fence()
    payload=await async_build_report_payload(hass,storage,kind='energy_report',cadence=cadence,
        now=now,actor=actor,purpose='report_execute',report_preferences=report_preferences,
        **({'window_start':report_window[0],'window_end':report_window[1]} if report_window else {}),
        **({'report_timezone':report_timezone} if report_timezone is not None else {}),
        **({'period_semantics':period_semantics} if period_semantics is not None else {}))
    async def validate():
        fence()
        if expected_schedule is not None:
            if await storage.async_get_schedule(schedule_id)!=expected_schedule:
                raise ValueError('schedule_changed_before_dispatch')
        if before_send is not None:
            await before_send()
        if not await service.async_validate_result(payload,actor):
            raise RuntimeError('energy_analysis_context_changed')
        fence()
    await validate()
    results=[]
    for target in selected:
        metadata={'source':'schedule' if schedule_id is not None else 'manual',
            'kind':'energy_report','cadence':cadence,
            'period':('daily:'+now.date().isoformat() if cadence=='daily' else
                'weekly:'+f'{now.isocalendar().year}-W{now.isocalendar().week:02d}' if cadence=='weekly' else 'monthly:'+now.strftime('%Y-%m')),
            'schedule_id':attempt_schedule_id if attempt_schedule_id is not None else schedule_id,'now':now}
        if target=='email':
            load,send=bucket['load_config'],bucket['send_email']
            smtp_revision=storage.current_smtp_revision
            async def prepare_email():
                await validate()
                cfg=await load()
                await validate()
                if (storage.current_smtp_revision!=smtp_revision or bucket.get('load_config') is not load
                    or bucket.get('send_email') is not send):
                    raise RuntimeError('email_delivery_context_changed')
                to=', '.join(recipients or []) or cfg.get('default_recipient','')
                if not to:
                    raise ValueError('missing_email_recipient')
                return cfg,to,payload['subject'],payload['body'],payload['html']
            result=await async_dispatch_attempt(hass,storage,send,prepare_email,**metadata)
        else:
            async def compose():
                return payload.get('notify') or {'title':payload['subject'],'message':payload['body']}
            result=await async_dispatch_notification(hass,storage,target,compose,validate,**metadata)
        results.append({'channel':target,**result})
    return {**results[-1],'channels':results,'subject':payload['subject'],'kind':'energy_report','cadence':cadence}
