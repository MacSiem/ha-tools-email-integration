"""HA Energy source configuration and guarded, bounded Recorder chunk IO.

Grid imports and device consumption are different configured sets. This adapter
never infers consumers from grid rows or guesses a PV-to-grid cost allocation.
A service supplies a fresh authorization/lifecycle/context guard around each IO.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from .composer import (_energy_grid_import_ids, async_get_energy_metadata,
                       async_get_energy_usage)
from .energy_measurements import hour_grid, MAX_SOURCES
from .energy_registry import build_interval_breakdown
from .tariff_history import _bounded


def capture_energy_configuration(preferences):
    """Detach only operational fields from actual HA Energy preferences.

    Supports legacy flow_from/flow_to and current flat grid entries. HA declares
    consumer meters through device_consumption, including included_in_stat.
    A grid-only setup without export is an explicit configured-domain assumption,
    never an independent physical proof that every source exists in HA.
    """
    if not isinstance(preferences, dict):
        raise ValueError('invalid_energy_configuration')
    _bounded(preferences)
    sources, devices = preferences.get('energy_sources', []), preferences.get('device_consumption', [])
    if not isinstance(sources, list) or not isinstance(devices, list) or len(devices) > MAX_SOURCES:
        raise ValueError('invalid_energy_configuration')
    ids = _energy_grid_import_ids(preferences)
    if len(ids) > MAX_SOURCES:
        raise ValueError('invalid_energy_configuration')
    projection, mixed = [], False
    for source in sources:
        if not isinstance(source, dict):
            mixed = True
            projection.append({'type': 'invalid_source'})
            continue
        kind = source.get('type')
        if kind in ('water', 'gas'):
            continue
        clean = {'type': kind if isinstance(kind, str) else 'invalid_source'}
        for key in ('stat_energy_from', 'stat_energy_to'):
            value = source.get(key)
            clean[key] = value if isinstance(value, str) else None
        for key, field in (('flow_from', 'stat_energy_from'), ('flow_to', 'stat_energy_to')):
            flows = source.get(key, [])
            clean[key] = [flow[field] for flow in flows
                          if isinstance(flow, dict) and isinstance(flow.get(field), str)] if isinstance(flows, list) else []
        projection.append(clean)
        mixed = mixed or kind != 'grid' or bool(clean['stat_energy_to'] or clean['flow_to'])
    consumers = []
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError('invalid_energy_configuration')
        row = {key: deepcopy(device.get(key)) for key in ('stat_consumption', 'included_in_stat')}
        name = device.get('name')
        if isinstance(name, str) and name.strip():
            row['name'] = name
        consumers.append(row)
    probe = build_interval_breakdown(0, [{**row, 'domain': 'grid_import', 'kwh': 0} for row in consumers], {})
    if probe['configured_consumers'] != len(consumers):
        raise ValueError('invalid_energy_configuration')
    domain = 'site_consumption' if mixed or not ids else 'grid_import'
    result = {'grid_sensors': [{'entity_id': sid, 'name': sid} for sid in ids], 'consumers': consumers,
              'consumer_domain': domain, 'domain_provenance': ('ha_energy_grid_only_configuration'
                  if domain == 'grid_import' else 'ha_energy_mixed_or_unproven_configuration'),
              'electricity_configuration': projection}
    result['source_signature'] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':'),
                                                          allow_nan=False).encode('utf-8')).hexdigest()
    return result


async def async_capture_energy_configuration(hass):
    from homeassistant.components.energy.data import async_get_manager
    manager = await async_get_manager(hass)
    return capture_energy_configuration(manager.data or {})


def _instant(value):
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError('aware_analysis_boundary_required')
    return instant.astimezone(timezone.utc)


async def async_read_energy_chunk(hass, configuration, start, end, *, guard):
    """Read actual source hours after a caller-owned fresh context/access check.

    No authorization result is cached. A revoked role/context after any await
    aborts the read; the caller must recheck again before publishing its DTO.
    """
    start, end = _instant(start), _instant(end)
    grid = hour_grid(start, end)
    if len(grid) > 7 * 24:
        raise ValueError('analysis_chunk_too_large')
    configuration = deepcopy(configuration)
    sensors = configuration['grid_sensors']
    consumers = [{'entity_id': row['stat_consumption'], 'name': row.get('name') or row['stat_consumption']}
                 for row in configuration['consumers']]
    ids = list(dict.fromkeys(row['entity_id'] for row in [*sensors, *consumers]))
    await guard()
    metadata = await async_get_energy_metadata(hass, ids)
    await guard()
    household = await async_get_energy_usage(hass, sensors, start, end, 'hour', metadata=metadata)
    await guard()
    measured = await async_get_energy_usage(hass, consumers, start, end, 'hour', metadata=metadata) if consumers else None
    await guard()
    return {'grid': household, 'consumers': measured}
