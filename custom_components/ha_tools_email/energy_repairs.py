"""Retired issue cleanup and independent stale-source evidence for reports."""
from datetime import datetime, timedelta, timezone
import hashlib
import math

from .analysis_provider import capture_energy_configuration
from .const import DOMAIN

MISSING_IMPORT = 'energy_missing_grid_import'
OVERLAP = 'energy_consumption_overlap'
GRID_DEVICES = 'energy_grid_is_devices'
STALE_PREFIX = 'energy_stale_source_'
ISSUES = (MISSING_IMPORT, OVERLAP, GRID_DEVICES)


def _owned(coordinator):
    bucket = coordinator.hass.data.get(DOMAIN, {})
    return (coordinator.running and bucket.get('energy_snapshot') is coordinator
            and bucket.get('storage') is coordinator.storage)


def _stale_id(source_id):
    return STALE_PREFIX + hashlib.sha256(source_id.encode()).hexdigest()


def _last_reading(rows, source_id, duration, now):
    """A missing or malformed Recorder row is unknown, never evidence of a gap."""
    points = rows.get(source_id, []) if isinstance(rows, dict) else []
    if not points:
        return None
    row = points[-1]
    value, stamp = row.get('sum'), row.get('start')
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        if isinstance(stamp, datetime):
            if stamp.utcoffset() is None:
                return None
            when = stamp.astimezone(timezone.utc)
        elif type(stamp) in (int, float) and math.isfinite(stamp):
            when = datetime.fromtimestamp(stamp, timezone.utc)
        else:
            return None
        end = when + duration
        return end if end <= now else None
    except (ValueError, OverflowError, OSError):
        return None


async def async_publish_stale_sources(coordinator):
    """Background only: inspect completed Recorder statistics, never report requests."""
    if not _owned(coordinator) or coordinator.provider is not None:
        return
    association = coordinator.hass.data[DOMAIN].get('energy_source_device')
    if association is None or not association._running or association._manager is None:
        return
    coordinator.stale_sources = None
    try:
        configuration = capture_energy_configuration(association._manager.data or {})
        ids = [row['entity_id'] for row in configuration['grid_sensors']]
        ids += [row['stat_consumption'] for row in configuration['consumers']]
        ids = list(dict.fromkeys(ids))
        signature = configuration['source_signature']
        from homeassistant.components.recorder.statistics import get_last_statistics, get_last_short_term_statistics
        from homeassistant.components.recorder import get_instance
        recorder = get_instance(coordinator.hass)
        def read():
            return {sid: (get_last_statistics(coordinator.hass, 1, sid, False, {'sum'}),
                          get_last_short_term_statistics(coordinator.hass, 1, sid, False, {'sum'})) for sid in ids}
        data = await recorder.async_add_executor_job(read)
        if (not _owned(coordinator) or association is not coordinator.hass.data[DOMAIN].get('energy_source_device')
                or not association._running or capture_energy_configuration(association._manager.data or {})['source_signature'] != signature):
            return
        now = datetime.now(timezone.utc)
        status = {}
        for sid in ids:
            long, short = data[sid]
            stamps = [_last_reading(long, sid, timedelta(hours=1), now),
                      _last_reading(short, sid, timedelta(minutes=5), now)]
            stamps = [stamp for stamp in stamps if stamp is not None]
            if not stamps:
                continue
            latest = max(stamps)
            state = coordinator.hass.states.get(sid)
            # Core compiles repeated statistics from the last state even when no
            # new device update happened. last_reported advances on every HA
            # state report, including an unchanged numeric value (such as zero).
            # An unavailable/unknown report is not a measurement: use the time
            # that state began and never clear an existing issue from it.
            external = ':' in sid
            if external:
                since, basis = latest, 'external_statistics'
            elif state is None:
                continue
            elif state.state in ('unavailable', 'unknown'):
                since, basis = state.last_changed, 'unavailable_state'
            else:
                try:
                    value = float(state.state)
                    if not math.isfinite(value):
                        continue
                    since, basis = state.last_reported, 'entity_report'
                except (ValueError, TypeError, AttributeError):
                    continue
            if not isinstance(since, datetime) or since.utcoffset() is None:
                continue
            since = since.astimezone(timezone.utc)
            if since > now:
                continue
            stale = now - since >= timedelta(hours=24)
            status[sid] = {'stale': stale, 'last_reading': since.isoformat() if basis != 'unavailable_state' else None,
                           'since': since.isoformat(), 'basis': basis}
        coordinator.stale_sources = status
    except Exception:
        # Recorder failure, configuration churn and malformed data cannot prove recovery.
        return

def clear_legacy_issues(hass):
    """Remove only this integration's retired energy issues, including ignored ones."""
    from homeassistant.helpers import issue_registry as ir
    registry = ir.async_get(hass)
    for domain, issue_id in list(registry.issues):
        if domain == DOMAIN and (issue_id in ISSUES or issue_id.startswith(STALE_PREFIX)):
            ir.async_delete_issue(hass, DOMAIN, issue_id)
