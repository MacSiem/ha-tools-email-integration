"""Native Energy Repairs from existing fresh authoritative producers only.

No IO, identities, price calculation or new principal. Unknown is no decision.
The coordinator owns lifecycle; issue IDs and all presentation are fixed.
"""
from datetime import datetime, timezone

from .analysis_periods import resolve_analysis_periods
from .analysis_provider import capture_energy_configuration
from .const import DOMAIN

MISSING_IMPORT = 'energy_missing_grid_import'
OVERLAP = 'energy_consumption_overlap'
ISSUES = (MISSING_IMPORT, OVERLAP)
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
    for issue_id in ISSUES:
        if registry.async_get_issue(DOMAIN, issue_id) is not None:
            ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
    coordinator._energy_repairs_active = False
