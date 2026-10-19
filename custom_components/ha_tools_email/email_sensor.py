"""Per-schedule timestamps/result; absent complete log producer stays unknown."""
from datetime import datetime
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from .email_entity import EmailScheduleEntity
from .native_report_projection import RESULT_OPTIONS


def entities(coordinator, key):
    return [EmailStatusSensor(coordinator, key, field) for field in
            ('last_attempt', 'next_report', 'result', 'errors_24h', 'warnings_24h')]


class EmailStatusSensor(EmailScheduleEntity, SensorEntity):
    @property
    def device_class(self):
        if self.field in ('last_attempt', 'next_report'):
            return SensorDeviceClass.TIMESTAMP
        return SensorDeviceClass.ENUM if self.field == 'result' else None

    @property
    def options(self):
        return RESULT_OPTIONS if self.field == 'result' else None

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT if self.field in ('errors_24h', 'warnings_24h') else None

    @property
    def native_value(self):
        row = self.row
        if row is None:
            return None
        last = row['last_attempt']
        if self.field == 'last_attempt':
            return datetime.fromisoformat(last['timestamp']) if last else None
        if self.field == 'next_report':
            return datetime.fromisoformat(row['next_report_at']) if row['next_report_at'] else None
        if self.field == 'result':
            return last['outcome'] if last else None
        return row[self.field]
