"""Read-only calendar of actual configured report occurrences."""
from datetime import datetime, timedelta, timezone
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.exceptions import HomeAssistantError
from .const import DOMAIN
from .email_entity import EmailScheduleEntity
from .native_report_projection import calendar_occurrences


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN]['email_native']
    coordinator.register_platform(async_add_entities,
        lambda key: [EmailReportCalendar(coordinator, key, 'schedule')])


class EmailReportCalendar(EmailScheduleEntity, CalendarEntity):
    @property
    def event(self):
        if not self.available or not self.row['enabled']:
            return None
        now = datetime.now(timezone.utc)
        # Include a currently active one-minute calendar occurrence; a strict
        # next-fire timestamp alone would hide it at the first timer refresh.
        starts = calendar_occurrences(self.row['calendar'], now - timedelta(minutes=1)
            + timedelta(microseconds=1), now + timedelta(days=33),
            local_timezone=self.hass.config.time_zone)
        if not starts:
            return None
        start = starts[0]
        return CalendarEvent(start=start, end=start + timedelta(minutes=1), summary='HA Tools Email report')

    async def async_get_events(self, hass, start_date, end_date):
        if not self.available:
            raise HomeAssistantError('Report schedule unavailable')
        if not self.row['enabled']:
            return []
        try:
            starts = calendar_occurrences(self.row['calendar'], start_date, end_date,
                                           local_timezone=hass.config.time_zone)
        except ValueError:
            raise HomeAssistantError('Report calendar range unavailable') from None
        return [CalendarEvent(start=start, end=start + timedelta(minutes=1), summary='HA Tools Email report')
                for start in starts]
