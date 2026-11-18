"""Locally persisted observations of the same WARNING+ log stream as system_log.

A checkpoint records only coverage for which the observations were durably
written. A crash can lose the uncheckpointed tail, never turn it into zeroes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from homeassistant.components.system_log import LogEntry
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from .storage import _ConfirmedStore

RETENTION = timedelta(days=33)  # >= 31 calendar days including DST
MAX_PENDING = 4096
MAX_GROUPS_PER_HOUR = 128


def _utc(value):
    return datetime.fromtimestamp(value, timezone.utc)


def _hour(value):
    return int(value // 3600) * 3600


class _Capture(logging.Handler):
    def __init__(self, owner, source):
        super().__init__(logging.WARNING)
        self.owner, self.source = owner, source

    def emit(self, record):
        try:
            entry = LogEntry(record, self.source.paths_re, formatter=self.source.formatter,
                             figure_out_source=True).to_dict()
            self.owner.capture(entry)
        except Exception:
            # Never recurse into logging from a logging handler. Mark coverage
            # unknown if the HA source cannot be represented.
            self.owner.lost(record.created)


class LogHistory:
    """One integration-owned handler, checkpoint and bounded hourly ledger."""
    def __init__(self, hass):
        self.hass = hass
        self.store = _ConfirmedStore(hass, 1, 'ha_tools_email.log_history')
        self.data = {'schema': 1, 'hours': {}, 'coverage': []}
        self._lock = threading.Lock()
        self._io_lock = asyncio.Lock()
        self._pending = []
        self._lost = None
        self._since = None
        self._handler = None
        self._unsub = None
        self._task = None

    async def async_start(self):
        raw = await self.store.async_load()
        if isinstance(raw, dict) and raw.get('schema') == 1 and isinstance(raw.get('hours'), dict) and isinstance(raw.get('coverage'), list):
            self.data = raw
        source = self.hass.data.get('system_log')
        if source is None or not hasattr(source, 'paths_re'):
            return
        self._handler = _Capture(self, source)
        logging.root.addHandler(self._handler)
        self._since = datetime.now(timezone.utc).timestamp()
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(seconds=30))

    @callback
    def _tick(self, _now):
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self.async_checkpoint())

    def capture(self, entry):
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                self._lost = self._lost or float(entry['timestamp'])
                return
            # Bound the cross-thread queue; exception text contributes to identity
            # but is not copied into the persisted report history.
            exc = str(entry.get('exception') or '')
            self._pending.append({'timestamp': entry['timestamp'],
                'name': str(entry.get('name') or 'unknown')[:512],
                'level': str(entry.get('level') or 'UNKNOWN')[:32],
                'message': [str((entry.get('message') or [''])[0])[:1024]],
                'source': str(entry.get('source') or '')[:512],
                'exception': hashlib.sha256(exc.encode('utf-8', 'replace')).hexdigest()})

    def lost(self, stamp):
        with self._lock:
            self._lost = self._lost or float(stamp)

    async def async_checkpoint(self, now=None):
        async with self._io_lock:
            if self._since is None:
                return
            with self._lock:
                end = (now or datetime.now(timezone.utc)).timestamp()
                pending, self._pending = self._pending, []
                lost, self._lost = self._lost, None
            candidate = deepcopy(self.data)
            hours = candidate['hours']
            incomplete = lost is not None
            for row in pending:
                stamp = float(row['timestamp'])
                hour = str(_hour(stamp))
                identity = json.dumps([row.get('name'), row.get('level'), row.get('source'),
                                       row.get('exception')], ensure_ascii=False, default=str)
                key = hashlib.sha256(identity.encode()).hexdigest()
                group = hours.setdefault(hour, {})
                if key not in group:
                    if len(group) >= MAX_GROUPS_PER_HOUR:
                        incomplete = True
                        continue
                    group[key] = {'name': str(row.get('name') or 'unknown')[:512],
                        'level': str(row.get('level') or 'UNKNOWN')[:32],
                        'message': [str((row.get('message') or [''])[0])[:1024]],
                        'count': 0}
                group[key]['count'] += 1
            if not incomplete and end >= self._since:
                coverage = candidate['coverage']
                if coverage and coverage[-1][1] == self._since:
                    coverage[-1][1] = end
                else:
                    coverage.append([self._since, end])
            cutoff = end - RETENTION.total_seconds()
            candidate['hours'] = {key: val for key, val in hours.items() if int(key) + 3600 > cutoff}
            candidate['coverage'] = [[max(a, cutoff), b] for a, b in candidate['coverage'] if b > cutoff]
            # Snapshot + coverage are committed together. The in-memory tail
            # remains available for a retry if a lower Store write fails.
            try:
                await self.store.async_save_confirmed(candidate)
            except BaseException:
                with self._lock:
                    self._pending = pending + self._pending
                    self._lost = lost or self._lost
                raise
            self.data = candidate
            self._since = end

    async def async_query(self, start, end):
        await self.async_checkpoint()
        start, end = start.timestamp(), end.timestamp()
        rows = []
        for hour, groups in self.data['hours'].items():
            stamp = int(hour)
            if start <= stamp and stamp + 3600 <= end:
                for key, row in groups.items():
                    rows.append({**row, 'id': f'{hour}:{key}', 'timestamp': stamp + 3599})
        covered = sum(max(0, min(end, b) - max(start, a)) for a, b in self.data['coverage'])
        complete = (end > start and start % 3600 == 0 and end % 3600 == 0
                    and covered >= end - start - 0.001)
        return rows, {'complete': complete, 'covered_seconds': round(covered),
                      'requested_seconds': round(max(0, end - start)),
                      'resolution': 'hour', 'from': _utc(start).isoformat(), 'to': _utc(end).isoformat()}

    async def async_stop(self):
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._handler:
            logging.root.removeHandler(self._handler)
            self._handler = None
        if self._task:
            await self._task
        await self.async_checkpoint()
        self._since = None
