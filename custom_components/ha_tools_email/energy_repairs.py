"""Native Energy Repairs from existing fresh authoritative producers only.

No IO, identities, price calculation or new principal. Unknown is no decision.
The coordinator owns lifecycle; issue IDs and all presentation are fixed.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import math

from .analysis_periods import resolve_analysis_periods
from .analysis_provider import capture_energy_configuration
from .const import DOMAIN

MISSING_IMPORT = 'energy_missing_grid_import'
OVERLAP = 'energy_consumption_overlap'
GRID_DEVICES = 'energy_grid_is_devices'
STALE_PREFIX = 'energy_stale_source_'
ISSUES = (MISSING_IMPORT, OVERLAP, GRID_DEVICES)
_OVERLAP_REASONS = frozenset(('consumption_exceeds_grid_import', 'children_exceed_parent'))


def _owned(coordinator):
    bucket = coordinator.hass.data.get(DOMAIN, {})
    return (coordinator.running and bucket.get('energy_snapshot') is coordinator
            and bucket.get('storage') is coordinator.storage)


def _publish(coordinator, issue_id, present):
    from homeassistant.helpers import issue_registry as ir
    registry = ir.async_get(coordinator.hass)
    old = registry.async_get_issue(DOMAIN, issue_id)
    coordinator._energy_repairs_active = True
    if present and old is None:
        ir.async_create_issue(coordinator.hass, DOMAIN, issue_id,
            is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
            translation_key=issue_id)
    elif not present and old is not None:
        ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)



def _import_absence_confirmed(preferences):
    """Unknown/malformed source representations cannot prove import absence."""
    for row in preferences.get('energy_sources', []):
        if not isinstance(row, dict) or row.get('type') not in ('grid', 'solar', 'battery', 'gas', 'water'):
            return False
        if row['type'] != 'grid':
            continue
        flat = row.get('stat_energy_from')
        if flat is not None and (not isinstance(flat, str) or not flat.strip()):
            return False
        flows = row.get('flow_from', [])
        if not isinstance(flows, list):
            return False
        if any(not isinstance(flow, dict)
               or not isinstance(flow.get('stat_energy_from'), str)
               or not flow['stat_energy_from'].strip() for flow in flows):
            return False
    return True


def publish_missing_import(coordinator):
    """Called only after the trusted real-time background refresh returns.

    Composer can return [] after a source error. Confirm its IDs synchronously
    against the already-loaded real Energy manager owned by source association.
    Setup's first tick precedes that association: defer until its next existing
    tick, rather than load another manager or interpret absence as a proof.
    """
    if not _owned(coordinator) or coordinator.provider is not None:
        return
    result, key = coordinator.data, coordinator._measurement_key
    if result is None or key is None or not coordinator.tariff_context_is_current(result):
        return
    if result.get('profile_source') != 'shared':
        return
    association = coordinator.hass.data[DOMAIN].get('energy_source_device')
    if (association is None or not association._running
            or association.hass is not coordinator.hass or association._manager is None):
        return
    # No await after the refresh's final source/metadata/tariff checks. Invalid
    # configuration raises; it must not become a missing-import notification.
    raw = association._manager.data
    configuration = capture_energy_configuration({} if raw is None else raw)
    ids = tuple(row['entity_id'] for row in configuration['grid_sensors'])
    if ids != tuple(row[0] for row in key[3]):
        return
    if not ids and not _import_absence_confirmed({} if raw is None else raw):
        return
    _publish(coordinator, MISSING_IMPORT, not ids)
    # Equal IDs are a configuration fact, independent of usage and coverage.
    devices = {row['stat_consumption'] for row in configuration['consumers']}
    _publish(coordinator, GRID_DEVICES, bool(ids) and set(ids) == devices)



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
        from homeassistant.helpers import issue_registry as ir
        registry = ir.async_get(coordinator.hass)
        grid = {row['entity_id'] for row in configuration['grid_sensors']}
        devices = {row['stat_consumption'] for row in configuration['consumers']}
        _publish(coordinator, GRID_DEVICES, bool(grid) and grid == devices)
        status = {}
        # Registry survives coordinator replacement; remove issues of removed sources.
        active_ids = {_stale_id(sid) for sid in ids}
        for domain, issue_id in list(registry.issues):
            if domain == DOMAIN and issue_id.startswith(STALE_PREFIX) and issue_id not in active_ids:
                ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
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
            issue_id = _stale_id(sid)
            old = registry.async_get_issue(DOMAIN, issue_id)
            if stale:
                if old is None:
                    name = state.name if state is not None else sid
                    link = f'/config/entities/entity/{sid}' if not external else '/config/energy'
                    ir.async_create_issue(coordinator.hass, DOMAIN, issue_id,
                        is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.WARNING,
                        translation_key='energy_stale_external' if external else 'energy_stale_source',
                        translation_placeholders={'name': name, 'since': since.date().isoformat(), 'link': link})
            elif old is not None and basis != 'unavailable_state':
                ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
        coordinator.stale_sources = status
        coordinator._energy_repairs_active = True
    except Exception:
        # Recorder failure, configuration churn and malformed data cannot prove recovery.
        return

def publish_analysis(service, result):
    """Called immediately after the real analysis CPU/final fresh guard.

    An explicit known counterexample proves overlap even if pricing/residual
    allocation is partial. Only a complete current grid partition proves clear.
    Cached, historical, comparison, report and injected-clock results do not
    pass through this producer hook.
    """
    coordinator = service.coordinator
    if (not service._energy_repairs_realtime or not _owned(coordinator)
            or coordinator.provider is not None
            or coordinator.hass.data[DOMAIN].get('energy_analysis') is not service):
        return
    context, period, groups = result['context'], result['period'], result['groups']
    if (result['schema'] != 2 or context.get('request_kind') is not None
            or context.get('access_purpose') != 'analysis'
            or context.get('period_kind') != 'day' or period.get('kind') != 'day'
            or context.get('period_anchor') is not None
            or context.get('comparison_kind') is not None
            or context.get('measurement_timezone') != coordinator.hass.config.time_zone
            or not service.context_is_current(result)):
        return
    current = resolve_analysis_periods('day', datetime.now(timezone.utc),
                                       coordinator.hass.config.time_zone)
    if current['status'] != 'ready' or any(period.get(key) != current['current'][key]
                                         for key in ('start', 'end', 'timezone')):
        return
    if (groups.get('energy_domain') != 'grid_import'
            or groups.get('domain_provenance') != 'ha_energy_grid_only_configuration'
            or groups.get('membership_mode') != 'current_snapshot'
            or groups.get('registry_revision') != context.get('registry_revision')):
        return
    coverage = result['coverage']
    expected = coverage.get('expected_source_seconds')
    if coverage.get('complete') is not True or type(expected) is not int or expected <= 0:
        return
    reasons = groups['issues']
    if any(reason in _OVERLAP_REASONS for reason in reasons):
        _publish(coordinator, OVERLAP, True)
    elif groups.get('partition_complete') is True and not reasons:
        _publish(coordinator, OVERLAP, False)


def clear_owned_issues(coordinator):
    """Unload cleanup is not a claim that either problem was resolved."""
    current = coordinator.hass.data.get(DOMAIN, {}).get('energy_snapshot')
    if current is not None and current is not coordinator:
        return  # A late old owner cannot erase a replacement owner's facts.
    from homeassistant.helpers import issue_registry as ir
    registry = ir.async_get(coordinator.hass)
    dynamic = [issue_id for domain, issue_id in registry.issues
               if domain == DOMAIN and issue_id.startswith(STALE_PREFIX)]
    for issue_id in (*ISSUES, *dynamic):
        if registry.async_get_issue(DOMAIN, issue_id) is not None:
            ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
    coordinator._energy_repairs_active = False
