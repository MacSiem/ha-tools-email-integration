"""Frozen internal report requests; no browser clock or source selection."""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
_HOURS = {'daily': 24, 'weekly': 168, 'monthly': 720}


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('aware_report_boundary_required')
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ReportWindow:
    semantics: str
    cadence: str
    timezone: str
    start: str
    end: str

    def request(self):
        return asdict(self)

    def period(self, now):
        now = _aware(now)
        if self.cadence not in _HOURS or self.semantics not in (
                'rolling_completed_hours', 'previous_completed_calendar_period'):
            raise ValueError('invalid_report_window')
        zone = ZoneInfo(self.timezone)
        start, end = (_aware(datetime.fromisoformat(x)) for x in (self.start, self.end))
        hours = (end-start).total_seconds()/3600
        if not 0 < hours <= 768 or end > now.replace(minute=0, second=0, microsecond=0):
            raise ValueError('invalid_report_window')
        if any(x.timestamp() % 3600 for x in (start, end)):
            # No Recorder read or proportional estimate for unobservable edges.
            raise ValueError('calendar_boundary_not_recorder_hour')
        if self.semantics == 'rolling_completed_hours' and hours != _HOURS[self.cadence]:
            raise ValueError('invalid_report_window')
        if self.semantics == 'previous_completed_calendar_period':
            from .scheduler import calendar_report_window
            if (start,end) != calendar_report_window({'cadence':self.cadence,'timezone':zone.key},end):
                raise ValueError('invalid_report_calendar_boundaries')
        a,b = start.astimezone(zone),end.astimezone(zone)
        return {'status':'ready','reason':None,'kind':'report_window',
                'semantics':self.semantics,'cadence':self.cadence,
                'grain':'hour' if self.cadence == 'daily' else 'day',
                'start':start.isoformat(),'end':end.isoformat(),
                'local_start':a.isoformat(),'local_end':b.isoformat(),
                'start_fold':a.fold,'end_fold':b.fold,'timezone':self.timezone,
                'hours':hours,'partial':False,'calendar_end':end.isoformat(),
                'calendar_local_end':b.isoformat(),'calendar_hours':hours,
                'recorder_hour_aligned':True,'calendar_clamped':False,'clamps':[]}


def resolve_report_window(cadence, now, timezone_name, *, mode='rolling', bounds=None):
    """Resolve once at the trusted caller; subsequent checks validate frozen bounds."""
    now = _aware(now)
    if cadence not in _HOURS or mode not in ('rolling','calendar'):
        raise ValueError('invalid_report_window')
    zone = ZoneInfo(timezone_name)
    if bounds is None:
        if mode == 'rolling':
            end=now.replace(minute=0,second=0,microsecond=0)
            start=end-timedelta(hours=_HOURS[cadence])
        else:
            from .scheduler import calendar_report_window
            start,end=calendar_report_window({'cadence':cadence,'timezone':zone.key},now)
    else:
        if type(bounds) is not tuple or len(bounds) != 2:
            raise ValueError('invalid_report_window')
        start,end=(_aware(x) for x in bounds)
    request=ReportWindow('rolling_completed_hours' if mode == 'rolling' else
        'previous_completed_calendar_period',cadence,zone.key,start.isoformat(),end.isoformat())
    request.period(now)
    return request
