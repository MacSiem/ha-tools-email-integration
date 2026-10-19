"""Supported native Recorder imports/clear/readback for owned cost series."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial

DOMAIN = 'ha_tools_email'
UTC = timezone.utc


def metadata_for(record, mean_type, *, supports_unit_class=True):
    result = {'source': DOMAIN, 'statistic_id': record['statistic_id'],
              'name': record['name'], 'unit_of_measurement': record['identity']['currency'],
              'has_sum': True}
    if mean_type is None:
        result['has_mean'] = False
    else:
        result['mean_type'] = mean_type.NONE
        if supports_unit_class:
            result['unit_class'] = None
    return result


def metadata_unit(metadata):
    return metadata.get('statistics_unit_of_measurement', metadata.get('unit_of_measurement'))


class NativeStatisticsRecorder:
    def __init__(self, hass):
        from homeassistant.components.recorder import get_instance, models
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics, async_list_statistic_ids, statistics_during_period)
        self.hass, self.instance = hass, get_instance(hass)
        fields = models.StatisticMetaData.__annotations__
        self.mean_type = getattr(models, 'StatisticMeanType', None) if 'mean_type' in fields else None
        self.supports_unit_class = 'unit_class' in fields
        self._add, self._list, self._read = async_add_external_statistics, async_list_statistic_ids, statistics_during_period

    async def async_metadata(self, ids):
        if not ids:
            return {}
        return {row['statistic_id']: row for row in await self._list(self.hass, set(ids))}

    def enqueue(self, plan):
        """One event-loop turn: supported clear then deterministic imports.

        Caller has just freshly guarded author, source, dataset and context.
        Return means enqueued, never a persistence acknowledgement.
        """
        clear = [item['statistic_id'] for item in plan if item['clear']]
        if clear:
            self.instance.async_clear_statistics(clear)
        for item in plan:
            metadata = metadata_for(item['record'], self.mean_type, supports_unit_class=self.supports_unit_class)
            rows = item['rows']
            if not rows:
                self._add(self.hass, metadata, [])
            for offset in range(0, len(rows), 168):
                self._add(self.hass, metadata.copy(), [
                    {'start': datetime.fromtimestamp(stamp/1000, UTC), 'sum': float(Decimal(total)),
                     'last_reset': None} for stamp, total in rows[offset:offset+168]])

    async def async_verify(self, plan):
        # The public barrier's older fast path can race an already dequeued job.
        # A separate content readback is therefore mandatory; mismatch retains
        # the durable intent, it never produces a false acknowledgement.
        await self.instance.async_block_till_done()
        metadata = await self.async_metadata([item['statistic_id'] for item in plan])
        for item in plan:
            sid, record = item['statistic_id'], item['record']
            meta = metadata.get(sid)
            if meta is None or meta.get('source') != DOMAIN or metadata_unit(meta) != record['identity']['currency']:
                return False
            points = record['points']
            if not points:
                return False
            start = datetime.fromtimestamp(min(map(int, points))/1000, UTC)
            end = datetime.fromtimestamp(max(map(int, points))/1000, UTC) + timedelta(hours=1)
            data = await self.instance.async_add_executor_job(partial(self._read,
                self.hass, start, end, {sid}, 'hour', None, {'sum'}))
            actual = {int(round(row['start']*1000)): row.get('sum') for row in data.get(sid, [])}
            expected = dict(item['rows'])
            if set(actual) != set(expected):
                return False
            if any(actual[stamp] is None or float(actual[stamp]) != float(Decimal(value)) for stamp, value in expected.items()):
                return False
        return True
