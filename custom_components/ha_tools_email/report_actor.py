"""Private scheduler capability, never a synthetic HA user or serializable input."""
from copy import deepcopy
from uuid import uuid4


class _ScheduleActor:
    __slots__ = ('service','storage','generation','schedule','token','revision')

    def __init__(self, service, schedule, generation):
        self.service=service
        self.storage=service.storage
        self.generation=generation
        self.revision=service.storage.current_schedule_revision
        self.schedule=deepcopy(schedule)
        self.token=uuid4().hex

    def __reduce__(self):
        raise TypeError('private_schedule_actor')

    def __repr__(self):
        return '<private schedule actor>'

    def check(self):
        self.service._check_running()
        bucket=self.service.hass.data.get('ha_tools_email',{})
        if (self.storage is not self.service.storage or
                bucket.get('schedule_generation',0) != self.generation or
                self.storage.current_schedule_revision != self.revision or
                self.schedule.get('kind') != 'energy_report' or
                not self.schedule.get('id') or not self.schedule.get('enabled',True)):
            raise RuntimeError('schedule_actor_expired')
        author=self.schedule.get('author_user_id')
        if author is not None and (type(author) is not str or not author or len(author)>128):
            raise PermissionError('energy_schedule_author_required')
