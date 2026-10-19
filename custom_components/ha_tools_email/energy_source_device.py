"""Bounded native association of the existing five entities to Energy import."""
from datetime import timedelta

from .composer import _energy_grid_import_ids
from .const import DOMAIN

ENERGY_ENTITIES = (('sensor', 'zone'), ('sensor', 'price'), ('sensor', 'next_cheap'),
                   ('sensor', 'today_cost'), ('binary_sensor', 'cheap'))


def resolve_source_device(preferences, entities, devices):
    """Require one import statistic, its registry entry and a live source device.

    Multiple imports are deliberately ambiguous even when they share a device.
    No names, states, recorder metadata or statistic-id heuristics identify it.
    """
    if not isinstance(preferences, dict):
        return None
    sources = preferences.get('energy_sources', [])
    if not isinstance(sources, list) or len(sources) > 512:
        return None
    # Bound nested legacy lists before using the shared flat-before-flow rule.
    if any(isinstance(row, dict) and isinstance(row.get('flow_from'), list)
           and len(row['flow_from']) > 512 for row in sources):
        return None
    ids = _energy_grid_import_ids(preferences)
    if len(ids) != 1:
        return None
    source = entities.async_get(ids[0])
    if source is None or not source.device_id:
        return None
    device = devices.async_get(source.device_id)
    if device is not None and hasattr(device, 'config_entry_id'):
        # A pre-migration composite is ambiguous and cannot be an entity target.
        device = devices.async_get(source.device_id, include_composite_devices=False)
    # Do not associate to this integration's own fallback or derived entities.
    if source.platform == DOMAIN or device is None:
        return None
    return device


class EnergySourceDeviceAssociation:
    """Registry-only reconciliation; no source-device metadata is rewritten.

    Preferences are already loaded by HA's manager. The 30s timer reads memory;
    its listener API has no unsubscribe on both supported Core lines, so we do
    not register a persistent EnergyManager callback that would leak on reload.
    Registry events reconcile immediately, preference edits within 30 seconds.
    """

    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self._unsubscribers = []
        self._manager = None
        self._running = False

    async def async_start(self):
        from homeassistant.components.energy.data import async_get_manager
        from homeassistant.core import callback
        from homeassistant.helpers import entity_registry as er, device_registry as dr
        from homeassistant.helpers.event import async_track_time_interval

        self._manager = await async_get_manager(self.hass)
        self._running = True
        # All callbacks are synchronous: no stale await can rebind after unload.
        changed = callback(lambda event: self.async_reconcile())
        self._unsubscribers.extend([
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, changed),
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, changed),
            async_track_time_interval(self.hass, changed, timedelta(seconds=30)),
        ])
        self.async_reconcile()

    def async_reconcile(self):
        if not self._running:
            return
        from homeassistant.helpers import entity_registry as er, device_registry as dr

        entities, devices = er.async_get(self.hass), dr.async_get(self.hass)
        fallback = devices.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, f'{self.entry.entry_id}_energy')},
            name='HA Tools Energy', manufacturer='HA Tools', model='Shared tariff')
        target = resolve_source_device(self._manager.data, entities, devices) or fallback
        # Core 2024.7 uses shared device links and removes unlinked entities on
        # updates. Core 2026.10 has a single config_entry_id; foreign entities may
        # reference it without changing ownership. Its legacy add-link is a no-op.
        if (not hasattr(target, 'config_entry_id')
                and self.entry.entry_id not in target.config_entries):
            devices.async_update_device(target.id, add_config_entry_id=self.entry.entry_id)
        previous = set()
        for domain, key in ENERGY_ENTITIES:
            eid = entities.async_get_entity_id(domain, DOMAIN,
                f'{self.entry.entry_id}_energy_{key}')
            record = entities.async_get(eid) if eid else None
            if record is None or record.config_entry_id != self.entry.entry_id:
                continue
            if record.device_id != target.id:
                if record.device_id:
                    previous.add(record.device_id)
                entities.async_update_entity(eid, device_id=target.id)
        # Move our five entries first. Never remove another integration's link.
        for device_id in previous - {fallback.id, target.id}:
            device = devices.async_get(device_id)
            if (device is not None and not hasattr(device, 'config_entry_id')
                    and self.entry.entry_id in device.config_entries):
                devices.async_update_device(device_id, remove_config_entry_id=self.entry.entry_id)

    def async_stop(self):
        self._running = False
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        self._manager = None
