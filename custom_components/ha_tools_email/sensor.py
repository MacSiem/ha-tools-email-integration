"""Stable Energy sensors reading the accepted public coordinator snapshot."""
from datetime import datetime

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass

from .const import DOMAIN
from .energy_entity import EnergySnapshotEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]['energy_snapshot']
    async_add_entities([
        EnergySensor(coordinator, entry, 'zone', 'current', 'zone_id'),
        EnergySensor(coordinator, entry, 'price', 'current', 'usage_rate'),
        EnergySensor(coordinator, entry, 'next_cheap', 'next_cheap', 'start'),
        EnergySensor(coordinator, entry, 'today_cost', 'today', 'usage_cost'),
        EnergySensor(coordinator, entry, 'today', 'entity_facts', 'import_kwh'),
        EnergySensor(coordinator, entry, 'cheap_share_today', 'entity_facts', 'cheap_share_pct'),
    ])
    native = hass.data[DOMAIN].get("email_native")
    if native is not None:
        from .email_sensor import entities
        native.register_platform(async_add_entities, lambda key: entities(native, key))


class EnergySensor(EnergySnapshotEntity, SensorEntity):
    _unrecorded_attributes = frozenset({'generated_at', 'zone_options', 'measurement_through',
                                      'next_change', 'next_zone', 'reason'})

    @property
    def value(self):
        data = self.coordinator.data
        if not data:
            return None
        if self.key in ('today', 'cheap_share_today', 'today_cost') and 'entity_facts' in data:
            # Refuse stale/injected historical shared snapshots. Historical
            # report DTOs are never this entity's producer.
            try:
                if not self.coordinator.tariff_context_is_current(data):
                    return None
            except AttributeError:
                return None
        if self.key == 'today_cost' and 'entity_facts' in data:
            return data['entity_facts'].get('usage_cost')
        return data.get(self.section, {}).get(self.field)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if not data:
            return {}
        attributes = {'generated_at': data.get('generated_at'), 'tariff_revision': data.get('tariff_revision'),
            'price_basis': data.get('price_basis'), 'measurement_timezone': data.get('measurement_timezone'),
            'reason': data.get(self.section, {}).get('reason')}
        facts = data.get('entity_facts', {})
        if self.key == 'zone':
            attributes.update(label=facts.get('label'), rate=data['current'].get('usage_rate'),
                classification=data['current'].get('zone_class'),
                next_change=facts.get('next_change'), next_zone=facts.get('next_zone'),
                zone_options=self.options)
        if self.key in ('today', 'cheap_share_today', 'today_cost'):
            attributes.update(measurement_through=facts.get('measurement_through'),
                resolution=facts.get('resolution'), coverage_complete=facts.get('coverage_complete'),
                reason=facts.get('reason'))
        return attributes

    @property
    def native_value(self):
        value = self.value
        return datetime.fromisoformat(value) if self.key == 'next_cheap' and value else value

    @property
    def device_class(self):
        return {'zone': SensorDeviceClass.ENUM, 'next_cheap': SensorDeviceClass.TIMESTAMP,
                'today_cost': SensorDeviceClass.MONETARY, 'today': SensorDeviceClass.ENERGY}.get(self.key)

    @property
    def options(self):
        if self.key != 'zone':
            return None
        data = self.coordinator.data or {}
        options = data.get('entity_facts', {}).get('zone_options', [])
        value = data.get('current', {}).get('zone_id')
        return list(dict.fromkeys([*options, *([value] if value is not None else [])]))

    @property
    def state_class(self):
        if self.key in ('today_cost', 'today'):
            return SensorStateClass.TOTAL
        return SensorStateClass.MEASUREMENT if self.key in ('price', 'cheap_share_today') else None

    @property
    def last_reset(self):
        # Use the published measurement calendar, never the tariff timezone or
        # the wall clock at property-read time (including a stale snapshot).
        if self.key not in ('today_cost', 'today') or not self.coordinator.data:
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
        if self.key == 'today':
            return 'kWh'
        if self.key == 'cheap_share_today':
            return '%'
        data = self.coordinator.data
        currency = data['currency'] if data else None
        if currency is None:
            return None
        return f'{currency}/kWh' if self.key == 'price' else currency if self.key == 'today_cost' else None
