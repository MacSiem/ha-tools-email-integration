"""One consent-bound hourly caller of the existing statistics kernel."""
import asyncio
from datetime import datetime, timedelta, timezone

from homeassistant.core import callback

from .const import DOMAIN

from .energy_statistics_publisher import EVENT_STATISTICS_SHARE_CHANGED


class EnergyStatisticsRuntime:
    def __init__(self, hass, entry_id, *, publisher_factory=None, clock=None, schedule=None):
        self.hass, self.entry_id = hass, entry_id
        self._factory = publisher_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._schedule = schedule
        self.running = False
        self._timer = self._unsubscribe = None
        self._publisher = self._consent = None
        self._control = self._requested = False
        lanes = hass.data.setdefault(DOMAIN, {}).setdefault('energy_statistics_runtime_lanes', {})
        self._lane = lanes.setdefault(entry_id, {'task': None, 'next': None, 'completed': None})

    def async_start(self):
        self.running = True
        self._unsubscribe = self.hass.bus.async_listen(EVENT_STATISTICS_SHARE_CHANGED, self._changed)
        self.async_request(control=True)

    @callback
    def _changed(self, event):
        # Empty payload: consent/author is always read from confirmed kernel state.
        self.async_request(control=True)

    def async_request(self, *, control=False):
        if not self.running:
            return
        self._control |= control
        self._requested = True
        if self._lane['task'] is not None:
            self._lane['next'] = self
            return
        task = asyncio.create_task(self._run())
        self._lane['task'] = task
        task.add_done_callback(self._settled)

    def _settled(self, task):
        if not task.cancelled():
            task.exception()  # Consume any unexpected failure exactly once.
        if self._lane['task'] is task:
            self._lane['task'] = None
        successor = self._lane['next']
        self._lane['next'] = None
        if successor is not None and successor.running and successor._requested:
            successor.async_request()

    def _get_publisher(self):
        if self._factory is not None:
            return self._factory()
        from .energy_statistics_api import _publisher
        return _publisher(self.hass)

    def _disarm(self):
        if self._timer is not None:
            self._timer()
            self._timer = None

    def _arm(self):
        self._disarm()
        if not self.running or self._consent is None:
            return
        now = self._clock().astimezone(timezone.utc)
        boundary = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        schedule = self._schedule
        if schedule is None:
            from homeassistant.helpers.event import async_track_point_in_utc_time
            schedule = lambda callback, at: async_track_point_in_utc_time(self.hass, callback, at)
        self._timer = schedule(self._tick, boundary)

    @callback
    def _tick(self, now):
        self._disarm()
        self.async_request()

    async def _run(self):
        while self.running and self._requested:
            self._requested = False
            control, self._control = self._control, False
            try:
                publisher = self._get_publisher()
                self._publisher = publisher
                consent = await publisher.async_runtime_context()
                if not self.running:
                    return
                if control:
                    self._consent = consent
                elif consent != self._consent:
                    self._consent = None  # Never adopt another author from a tick.
                if consent is None or self._consent is None:
                    self._disarm()
                    continue
                hour = self._clock().astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                token = (consent, hour)
                if self._lane['completed'] != token:
                    # Kernel alone selects the last completed hour and repeats
                    # fresh author/dataset/source/normalization/policy guards.
                    await publisher.async_refresh(consent[0], backfill=False, expected_share=consent)
                    if self.running:
                        self._lane['completed'] = token
            except asyncio.CancelledError:
                # Owner never cancels this task: doing so could abandon raw I/O.
                self._consent = None
                raise
            except RuntimeError as error:
                # A pending native import can settle after its first readback.
                # Retry only at the next hour, using the same persisted author.
                if str(error) not in ('statistics_readback_unconfirmed', 'energy_statistics_busy'):
                    self._consent = None
                self._requested = False
            except Exception:
                # Revoke/source/dataset errors require explicit reauthorization.
                self._consent = None
                self._requested = False
            finally:
                self._arm()

    def async_stop(self):
        self.running = False
        self._requested = self._control = False
        self._disarm()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._lane['next'] is self:
            self._lane['next'] = None
        if self._publisher is not None:
            self._publisher.stop()  # Synchronous kernel fence before any late enqueue.
        # Keep the task and shared lane until its actual coroutine/raw I/O settles.
