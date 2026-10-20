"""Bounded, guarded Recorder IO for monetary change statistics, without energy conversion."""
from datetime import datetime


async def async_read_native_cost_chunk(hass, ids, start, end, *, guard):
    if (not isinstance(ids, list) or len(ids) > 512 or len(set(ids)) != len(ids)
            or any(not isinstance(sid, str) or not sid for sid in ids)
            or not isinstance(start, datetime) or not isinstance(end, datetime)
            or start.utcoffset() is None or end.utcoffset() is None
            or start.timestamp() % 3600 or end.timestamp() % 3600
            or not 0 < (end-start).total_seconds() <= 168*3600):
        raise ValueError('invalid_native_cost_chunk')
    await guard()
    if not ids:
        return {}
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.components.recorder import get_instance
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, set(ids), 'hour', {}, {'change'})
    await guard()
    return result
