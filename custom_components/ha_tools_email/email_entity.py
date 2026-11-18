"""Privacy-bounded schedule entity identity shared across native platforms."""
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.device_registry import DeviceInfo
from .const import DOMAIN


def schedule_name(row, language):
    pl = str(language or '').lower().startswith('pl')
    kind = ('Raport dziennika' if row['kind'] == 'log_digest' else 'Raport energii') if pl else (
        'Log report' if row['kind'] == 'log_digest' else 'Energy report')
    cadence = ({'daily': 'dzienny', 'weekly': 'tygodniowy', 'monthly': 'miesięczny'} if pl else
               {'daily': 'daily', 'weekly': 'weekly', 'monthly': 'monthly'})[row['cadence']]
    return f'{kind} · {cadence}'


class EmailScheduleEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, key, field):
        self.coordinator, self.key, self.field = coordinator, key, field
        self._attr_unique_id = f'{coordinator.entry.entry_id}_email_{key}_{field}'
        self._attr_translation_key = f'email_{field}'
        name = schedule_name(coordinator.data[key], coordinator.hass.config.language)
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN,
            f'{coordinator.entry.entry_id}_email_{key}')}, name=name,
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
