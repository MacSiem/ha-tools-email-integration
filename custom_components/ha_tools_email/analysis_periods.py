"""Pure calendar windows for analysis; energy and coverage belong to the caller.

All intervals are half open absolute UTC Recorder hours. Aware ``now`` is the
server clock, never a browser date. ``ready`` means resolved, not measured.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .composer import _calendar_comparison_window

UTC = timezone.utc
_CADENCE = {"day": "daily", "week": "weekly", "month": "monthly", "year": "yearly"}


def _wall(day, zone, hour=0, fold=0):
    # Same gap/fold policy as composer: preserve fold and roundtrip through UTC.
    return datetime(day.year, day.month, day.day, hour, tzinfo=zone,
                    fold=fold).astimezone(UTC)


def _next(day, kind):
    if kind == "day":
        return day + timedelta(days=1)
    if kind == "week":
        return day + timedelta(days=7)
    if kind == "month":
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return day.replace(year=day.year + 1)


def _period(start, end, limit, zone, *, partial, clamps=()):
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    aligned = not any(bound.timestamp() % 3600 for bound in (start, end, limit))
    hours = (end - start).total_seconds() / 3600
    reason = None
    if not aligned:
        reason = "calendar_boundary_not_recorder_hour"
    elif hours <= 0:
        reason = "no_completed_interval"
    elif hours > 366 * 24:
        # Bound each query; never substitute a rolling 365-day year.
        reason = "calendar_window_too_long"
    return {
        "status": "unknown" if reason else "ready", "reason": reason,
        "start": start.isoformat(), "end": end.isoformat(),
        "local_start": local_start.isoformat(), "local_end": local_end.isoformat(),
        "start_fold": local_start.fold, "end_fold": local_end.fold,
        "timezone": zone.key, "hours": hours, "partial": partial,
        "calendar_end": limit.isoformat(),
        "calendar_local_end": limit.astimezone(zone).isoformat(),
        "calendar_hours": (limit - start).total_seconds() / 3600,
        "recorder_hour_aligned": aligned, "calendar_clamped": bool(clamps),
        "clamps": list(clamps),
    }


def _comparison(periods):
    reason = next((p["reason"] for p in periods if p["status"] != "ready"), None)
    return {"status": "unknown" if reason else "ready", "reason": reason,
            "baseline_periods": periods}


def _extra_baseline(kind, start, end, target_day, zone):
    """Align the extra weekday/mean/year-ago baselines by local position."""
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    target_start = _wall(target_day, zone)
    target_limit = _wall(_next(target_day, kind), zone)
    limit = _wall(_next(local_start.date(), kind), zone)
    partial = end < limit
    clamps = []
    if not partial:
        target_end = target_limit
    else:
        if kind in ("day", "week"):
            cutoff_day = target_day + (local_end.date() - local_start.date())
        elif kind == "month":
            if local_end.day > monthrange(target_day.year, target_day.month)[1]:
                cutoff_day = None
                clamps.append("short_month_to_period_end")
            else:
                cutoff_day = target_day.replace(day=local_end.day)
        else:
            days = monthrange(target_day.year, local_end.month)[1]
            cutoff_day = target_day.replace(month=local_end.month,
                                            day=min(local_end.day, days))
            if local_end.day > days:
                clamps.append("leap_day_to_february_end")
        if cutoff_day is None:
            target_end = target_limit
        else:
            target_end = _wall(cutoff_day, zone, local_end.hour, local_end.fold)
            expected = datetime(cutoff_day.year, cutoff_day.month, cutoff_day.day,
                                local_end.hour)
            if target_end.astimezone(zone).replace(tzinfo=None) != expected:
                clamps.append("dst_gap_roundtrip")
            if target_end > target_limit:
                target_end = target_limit
                clamps.append("calendar_end")
    return _period(target_start, target_end, target_limit, zone,
                   partial=target_end < target_limit, clamps=clamps)


def resolve_analysis_periods(kind, now, timezone_name, *, anchor=None):
    """Resolve day/week/month/year and all W24 calendar comparison windows.

    Return JSON-safe dictionaries. An optional strict ISO date selects a past
    or current calendar period in the measurement zone; it never replaces the
    trusted server clock. Past periods end at their actual calendar boundary.
    Current end is the last completed absolute hour. At a new local calendar
    boundary no interval exists yet; do not fall
    back to yesterday or invent zero. ``mean7`` retains all seven windows even
    if a baseline is unresolvable. No measured-data completeness is implied.
    """
    result = {"status": "unknown", "reason": None, "kind": kind,
              "timezone": timezone_name, "current": None, "comparisons": {}}
    if not isinstance(kind, str) or kind not in _CADENCE:
        return {**result, "kind": None, "reason": "invalid_period_kind"}
    if not isinstance(timezone_name, str):
        return {**result, "timezone": None, "reason": "report_timezone_unavailable"}
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        return {**result, "reason": "report_timezone_unavailable"}
    if not isinstance(now, datetime):
        return {**result, "reason": "aware_server_clock_required"}
    try:
        if now.utcoffset() is None:
            return {**result, "reason": "aware_server_clock_required"}
        end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        today = now.astimezone(zone).date()
        day = today
        if anchor is not None:
            try:
                if type(anchor) is not str or len(anchor) != 10:
                    raise ValueError('invalid_anchor')
                selected = date.fromisoformat(anchor)
                if selected.isoformat() != anchor:
                    raise ValueError('invalid_anchor')
            except (ValueError, TypeError):
                return {**result, "reason": "invalid_analysis_anchor"}
            if selected > today:
                return {**result, "reason": "future_analysis_anchor"}
            day = selected
        if kind == "week":
            day -= timedelta(days=day.weekday())
        elif kind == "month":
            day = day.replace(day=1)
        elif kind == "year":
            day = day.replace(month=1, day=1)
            if day.year == 1:
                raise ValueError('no_previous_year')
        start, limit = _wall(day, zone), _wall(_next(day, kind), zone)
        # A selected past calendar period is complete. The server remains the
        # only authority for unfinished current periods and future selections.
        end = min(end, limit)
        if end < start:
            # E.g. a UTC hour preceding local half-hour midnight.
            return {**result, "reason": "calendar_boundary_not_recorder_hour"}
        current = _period(start, end, limit, zone, partial=end < limit)
        result["current"] = current
        if current["status"] != "ready":
            return {**result, "reason": current["reason"]}

        # The previous period policy has a single existing owner. Preserve its
        # full/partial rules, short-month clamp, leap-day and DST fold handling.
        previous = _calendar_comparison_window(_CADENCE[kind], start, end,
                                               timezone_name, True)
        if "previous_start" not in previous:
            return {**result, "reason": previous["reason"]}
        previous_start = datetime.fromisoformat(previous["previous_start"])
        previous_end = datetime.fromisoformat(previous["previous_end"])
        previous_day = previous_start.astimezone(zone).date()
        previous_limit = _wall(_next(previous_day, kind), zone)
        prior = _period(previous_start, previous_end, previous_limit, zone,
                        partial=previous_end < previous_limit,
                        clamps=("shared_calendar_alignment",)
                        if previous["calendar_clamped"] else ())
        if previous["status"] != "pending":
            prior.update(status="unknown", reason=previous["reason"])
        comparisons = {"previous": _comparison([prior])}
        if kind == "day":
            comparisons["same_weekday"] = _comparison([
                _extra_baseline(kind, start, end, day - timedelta(days=7), zone)])
            comparisons["mean7"] = _comparison([
                _extra_baseline(kind, start, end, day - timedelta(days=offset), zone)
                for offset in range(1, 8)])
        elif kind == "week":
            iso_year, iso_week, _ = day.isocalendar()
            try:
                prior_week = date.fromisocalendar(iso_year - 1, iso_week, 1)
            except ValueError:
                comparisons["year_ago"] = {"status": "unknown", "reason": "iso_week_unavailable",
                                            "baseline_periods": []}
            else:
                comparisons["year_ago"] = _comparison([
                    _extra_baseline(kind, start, end, prior_week, zone)])
        elif kind in ("month", "year"):
            comparisons["year_ago"] = _comparison([
                _extra_baseline(kind, start, end, day.replace(year=day.year - 1), zone)])
        result.update(status="ready", comparisons=comparisons)
        return result
    except (ValueError, OverflowError, OSError):
        # E.g. year 1 has no preceding year, year 9999 no calendar end.
        # Do not publish a partially assembled set with missing baselines.
        return {**result, "status": "unknown", "reason": "calendar_out_of_range",
                "comparisons": {}}
