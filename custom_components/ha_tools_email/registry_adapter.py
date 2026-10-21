"""Read selected HA registry paths and invalidate current membership on events."""
import asyncio
from datetime import datetime, timezone

from homeassistant.core import callback

from .const import DOMAIN
from .energy_registry import MAX_SOURCES, resolve_membership


def _registry_modules():
    from homeassistant.helpers import entity_registry, device_registry, area_registry
    try:
        from homeassistant.helpers import floor_registry
    except ImportError:
        floor_registry = None
    return entity_registry, device_registry, area_registry, floor_registry


class RegistryMembership:
    """Event-loop only; one detached snapshot for requested and authorized IDs.

    Revisions advance immediately, while notifications coalesce within an event
    loop turn. Consumers must fence awaited work with this revision. No Recorder
    query, whole-registry export or historical membership reconstruction occurs.
    """

    def __init__(self, hass, on_change, *, registry_modules=None):
        self.hass, self.on_change = hass, on_change
        self._modules = registry_modules if registry_modules is not None else _registry_modules()
        self._unsubscribers = []
        self._handle = None
        self._running = False
        self.revision = 0

    @callback
    def _advance(self):
        bucket = self.hass.data.setdefault(DOMAIN, {})
        previous = bucket.get('energy_registry_revision', 0)
        self.revision = (previous if type(previous) is int and previous >= 0 else 0) + 1
        bucket['energy_registry_revision'] = self.revision

    @callback
    def start(self):
        if self._running:
            return
        self._running = True
        self._advance()
        names = ('EVENT_ENTITY_REGISTRY_UPDATED', 'EVENT_DEVICE_REGISTRY_UPDATED',
                 'EVENT_AREA_REGISTRY_UPDATED', 'EVENT_FLOOR_REGISTRY_UPDATED')
        try:
            for module, name in zip(self._modules, names):
                event = getattr(module, name, None)
                if event is not None:
                    self._unsubscribers.append(self.hass.bus.async_listen(event, self._changed))
        except Exception:
            self.stop()
            raise

    @callback
    def _changed(self, event):
        if not self._running:
            return
        self._advance()
        if self._handle is None:
            self._handle = asyncio.get_running_loop().call_soon(self._notify)

    @callback
    def _notify(self):
        self._handle = None
        if self._running:
            self.on_change(self.revision)

    @callback
    def stop(self):
        self._running = False
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    @callback
    def capture(self, source_ids):
        if not self._running:
            raise RuntimeError('energy_snapshot_unloaded')
        if (not isinstance(source_ids, list) or len(source_ids) > MAX_SOURCES
                or any(not isinstance(sid, str) or not sid for sid in source_ids)
                or len(set(source_ids)) != len(source_ids)):
            raise ValueError('invalid_source_ids')
        er, dr, ar, fr = self._modules
        entities, devices, areas = er.async_get(self.hass), dr.async_get(self.hass), ar.async_get(self.hass)
        floors = fr.async_get(self.hass) if fr is not None else None
        floor_get = getattr(floors, 'async_get_floor', None)
        data = {'entities': {}, 'devices': {}, 'areas': {}}
        if callable(floor_get):
            data['floors'] = {}
        for sid in source_ids:
            entity = entities.async_get(sid)
            if entity is None:
                continue
            device_id, entity_area = getattr(entity, 'device_id', None), getattr(entity, 'area_id', None)
            data['entities'][sid] = {key: getattr(entity, key, None) for key in
                                     ('area_id', 'device_id', 'name', 'original_name', 'has_entity_name')}
            device = devices.async_get(device_id) if device_id else None
            device_area = getattr(device, 'area_id', None)
            if device is not None:
                data['devices'][device_id] = {key: getattr(device, key, None)
                    for key in ('area_id', 'name', 'name_by_user')}
            area_id = entity_area or device_area
            area = areas.async_get_area(area_id) if area_id else None
            if area is not None:
                floor_id = getattr(area, 'floor_id', None)
                data['areas'][area_id] = {'name': area.name, 'floor_id': floor_id}
                floor = floor_get(floor_id) if floor_id and callable(floor_get) else None
                if floor is not None:
                    data['floors'][floor_id] = {'name': floor.name}
        return resolve_membership(source_ids, data,
            captured_at=datetime.now(timezone.utc).isoformat(), revision=self.revision)
