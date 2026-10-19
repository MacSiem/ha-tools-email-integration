"""Four sensors reading one persisted shared Energy tariff snapshot."""
from datetime import datetime

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass

from .const import DOMAIN
from .energy_entity import EnergySnapshotEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    async_add_entities([
        EnergySensor(coordinator, entry, 'zone', 'current', 'zone_class'),
        EnergySensor(coordinator, entry, 'price', 'current', 'usage_rate'),
        EnergySensor(coordinator, entry, 'next_cheap', 'next_cheap', 'start'),
        EnergySensor(coordinator, entry, 'today_cost', 'today', 'usage_cost'),
    ])


class EnergySensor(EnergySnapshotEntity, SensorEntity):
    @property
    def native_value(self):
        value = self.value
        return datetime.fromisoformat(value) if self.key == 'next_cheap' and value else value

    @property
    def device_class(self):
        return {'zone': SensorDeviceClass.ENUM, 'next_cheap': SensorDeviceClass.TIMESTAMP,
                'today_cost': SensorDeviceClass.MONETARY}.get(self.key)

    @property
    def options(self):
        return ['cheap', 'expensive', 'mixed'] if self.key == 'zone' else None

    @property
    def state_class(self):
        if self.key == 'today_cost':
            return SensorStateClass.TOTAL
        return SensorStateClass.MEASUREMENT if self.key == 'price' else None

    @property
    def last_reset(self):
        # Use the published measurement calendar, never the tariff timezone or
        # the wall clock at property-read time (including a stale snapshot).
        if self.key != 'today_cost' or not self.coordinator.data:
            return None
        value = self.coordinator.data['today'].get('from')
        if not isinstance(value, str):
            return None
        try:
            reset = datetime.fromisoformat(value)
        except ValueError:
            return None
        return reset if reset.tzinfo is not None and reset.utcoffset() is not None else None

    @property
    def native_unit_of_measurement(self):
        data = self.coordinator.data
        currency = data['currency'] if data else None
        if currency is None:
            return None
        return f'{currency}/kWh' if self.key == 'price' else currency if self.key == 'today_cost' else None
