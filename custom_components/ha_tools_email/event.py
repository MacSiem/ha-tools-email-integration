"""Confirmed attempt events; SMTP acceptance never means delivery."""
from homeassistant.components.event import EventEntity
from .const import DOMAIN
from .email_entity import EmailScheduleEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]['email_native']
    coordinator.register_platform(async_add_entities,
        lambda key: [EmailAttemptEvent(coordinator, key, 'attempt')])


class EmailAttemptEvent(EmailScheduleEntity, EventEntity):
    _attr_event_types = ['smtp_accepted', 'attempt_error', 'outcome_unknown', 'partial_acceptance']

    def _changed(self, fresh):
        for row in fresh.get(self.key, []):
            outcome = row['outcome']
            kind = ('smtp_accepted' if outcome == 'accepted_by_smtp' else
                    'partial_acceptance' if outcome == 'partial_acceptance' else
                    'outcome_unknown' if outcome in ('outcome_unknown', 'transport_outcome_unknown',
                                                     'cancelled_outcome_unknown') else 'attempt_error')
            self._trigger_event(kind, {'outcome': outcome})
        self.async_write_ha_state()
