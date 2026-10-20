"""Cheap-tariff binary sensor, unknown without explicit classification."""
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .energy_entity import EnergySnapshotEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EnergyCheapSensor(hass.data[DOMAIN]['energy_snapshot'], entry,
                                        'cheap', 'current', 'cheap')])
    native = hass.data[DOMAIN].get('email_native')
    if native is not None:
        async_add_entities([ReportsProblemSensor(native, entry)])


class EnergyCheapSensor(EnergySnapshotEntity, BinarySensorEntity):
    @property
    def is_on(self):
        return self.value


class ReportsProblemSensor(BinarySensorEntity):
    """Aggregate report attention state; no private history in entity attributes."""
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = 'reports_problem'

    def __init__(self, coordinator, entry):
        self.coordinator = coordinator
        self._attr_unique_id = f'{entry.entry_id}_reports_problem'
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f'{entry.entry_id}_energy')}, name='HA Tools Energy',
            manufacturer='HA Tools', model='Shared tariff')

    @property
    def available(self):
        return self.coordinator.running and self.coordinator.report_problem is not None

    @property
    def is_on(self):
        return self.coordinator.report_problem

    @property
    def extra_state_attributes(self):
        return {'last_outcome': self.coordinator.report_outcome,
                'outcome_unknown': self.coordinator.report_outcome_unknown}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(
            lambda fresh: self.async_write_ha_state()))
