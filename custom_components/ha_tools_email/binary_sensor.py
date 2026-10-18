"""Cheap-tariff binary sensor, unknown without explicit classification."""
from homeassistant.components.binary_sensor import BinarySensorEntity

from .const import DOMAIN
from .energy_entity import EnergySnapshotEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EnergyCheapSensor(hass.data[DOMAIN]['energy_snapshot'], entry,
                                        'cheap', 'current', 'cheap')])


class EnergyCheapSensor(EnergySnapshotEntity, BinarySensorEntity):
    @property
    def is_on(self):
        return self.value
