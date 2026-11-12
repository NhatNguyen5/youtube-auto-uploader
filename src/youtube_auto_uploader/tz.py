"""US Pacific time without constructing ZoneInfo at import.

Windows has no IANA time zone database unless tzdata is installed. Importing
this module must stay safe on that platform; callers look the zone up lazily.
"""

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _nth_sunday(year, month, n):
    first = datetime(year, month, 1)
    return 1 + (6 - first.weekday()) % 7 + (n - 1) * 7


def _pacific_offset(moment):
    """US Pacific offset at this instant. PDT is UTC-7; PST is UTC-8."""
    utc = moment.astimezone(UTC)
    # 02:00 PST == 10:00 UTC; 02:00 PDT == 09:00 UTC.
    start = datetime(utc.year, 3, _nth_sunday(utc.year, 3, 2), 10, tzinfo=UTC)
    end = datetime(utc.year, 11, _nth_sunday(utc.year, 11, 1), 9, tzinfo=UTC)
    if start <= utc < end:
        return timedelta(hours=-7)
    return timedelta(hours=-8)


def _pacific_midnight_utc(year, month, day):
    """UTC instant of midnight at the start of this Pacific calendar date."""
    start_day = _nth_sunday(year, 3, 2)
    end_day = _nth_sunday(year, 11, 1)
    # Midnight on the spring-forward Sunday is still PST. Midnight on the
    # fall-back Sunday is still PDT.
    pdt = (3, start_day) < (month, day) <= (11, end_day)
    return datetime(year, month, day, 7 if pdt else 8, tzinfo=UTC)


def _next_pacific_midnight(moment):
    """Next US Pacific midnight after moment, without the IANA time zone database.

    PDT (UTC-7) runs from 02:00 local on the second Sunday of March until
    02:00 local on the first Sunday of November. Every other time is PST (UTC-8).
    """
    utc = moment.astimezone(UTC)
    local = utc.astimezone(timezone(_pacific_offset(utc)))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight <= local:
        midnight += timedelta(days=1)
    return _pacific_midnight_utc(midnight.year, midnight.month, midnight.day)


def _pacific_wall_offset(moment):
    """Offset of a Pacific wall time. The gap and the overlap follow PEP 495 fold."""
    wall = datetime(
        moment.year,
        moment.month,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
        moment.microsecond,
    )
    fold = moment.fold
    spring = datetime(wall.year, 3, _nth_sunday(wall.year, 3, 2), 2)
    fall = datetime(wall.year, 11, _nth_sunday(wall.year, 11, 1), 1)
    if wall < spring:
        return timedelta(hours=-8)
    if wall < spring + timedelta(hours=1):
        # 02:00-03:00 does not exist. fold 0 keeps the earlier offset (PST).
        return timedelta(hours=-7) if fold else timedelta(hours=-8)
    if wall < fall:
        return timedelta(hours=-7)
    if wall < fall + timedelta(hours=1):
        # 01:00-02:00 happens twice. fold 0 is the first occurrence (PDT).
        return timedelta(hours=-8) if fold else timedelta(hours=-7)
    return timedelta(hours=-8)


class _PacificFallback(tzinfo):
    """US Pacific DST rule used when America/Los_Angeles cannot be loaded."""

    def utcoffset(self, dt):
        if dt is None:
            return None
        return _pacific_wall_offset(dt)

    def dst(self, dt):
        offset = self.utcoffset(dt)
        if offset is None:
            return None
        return offset - timedelta(hours=-8)

    def tzname(self, dt):
        offset = self.utcoffset(dt)
        if offset is None:
            return "PST"
        return "PDT" if offset == timedelta(hours=-7) else "PST"

    def fromutc(self, dt):
        if not isinstance(dt, datetime):
            raise TypeError("fromutc() requires a datetime argument")
        if dt.tzinfo is not self:
            raise ValueError("dt.tzinfo is not self")
        utc = datetime(
            dt.year,
            dt.month,
            dt.day,
            dt.hour,
            dt.minute,
            dt.second,
            dt.microsecond,
            tzinfo=UTC,
        )
        local = utc + _pacific_offset(utc)
        fall_back = datetime(utc.year, 11, _nth_sunday(utc.year, 11, 1), 9, tzinfo=UTC)
        fold = 1 if fall_back <= utc < fall_back + timedelta(hours=1) else 0
        return local.replace(tzinfo=self, fold=fold)


_PACIFIC_FALLBACK = _PacificFallback()


def pacific_zone():
    """America/Los_Angeles, or the US Pacific DST rule when that zone is missing.

    Looked up on each call so import does not touch the time zone database.
    """
    try:
        return ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        return _PACIFIC_FALLBACK
