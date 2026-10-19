"""Privacy-bounded schedule entity identity shared across native platforms."""
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN


class EmailScheduleEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, key, field):
        self.coordinator, self.key, self.field = coordinator, key, field
        self._attr_unique_id = f'{coordinator.entry.entry_id}_email_{key}_{field}'
        self._attr_translation_key = f'email_{field}'
        cadence = coordinator.data[key]['cadence']
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN,
            f'{coordinator.entry.entry_id}_email_{key}')}, name=f'HA Tools Email {cadence}',
            manufacturer='HA Tools', model='Scheduled report')

    @property
    def row(self):
        return self.coordinator.data.get(self.key)

    @property
    def available(self):
        return self.coordinator.running and self.row is not None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self._changed))

    def _changed(self, fresh):
        self.async_write_ha_state()
