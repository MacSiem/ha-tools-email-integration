"""Privacy-bounded shared entity behavior for W20 Energy."""
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


class EnergySnapshotEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, entry, key, section, field):
        self.coordinator = coordinator
        self.key, self.section, self.field = key, section, field
        self._attr_unique_id = f'{entry.entry_id}_energy_{key}'
        self._attr_translation_key = f'energy_{key}'
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f'{entry.entry_id}_energy')}, name='HA Tools Energy',
            manufacturer='HA Tools', model='Shared tariff')

    @property
    def value(self):
        data = self.coordinator.data
        return data[self.section][self.field] if data else None

    @property
    def available(self):
        return self.value is not None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if data is None:
            return {}
        return {'generated_at': data['generated_at'], 'tariff_revision': data['tariff_revision'],
                'price_basis': data['price_basis'],
                'reason': data[self.section].get('reason'),
                'measurement_timezone': data['measurement_timezone']}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))
