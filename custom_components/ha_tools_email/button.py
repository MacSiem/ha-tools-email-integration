"""Explicit user action; no automation/schedule actor impersonation."""
from homeassistant.components.button import ButtonEntity
from .const import DOMAIN
from .email_entity import EmailScheduleEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]['email_native']
    coordinator.register_platform(async_add_entities,
        lambda key: [EmailSendButton(coordinator, key, 'send')])


class EmailSendButton(EmailScheduleEntity, ButtonEntity):
    @property
    def available(self):
        return super().available and self.row['enabled']

    async def async_press(self):
        await self.coordinator.async_send(self.key, getattr(self._context, "user_id", None), self.entity_id)
