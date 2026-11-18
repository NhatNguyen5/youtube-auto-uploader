"""US Pacific time without constructing ZoneInfo at import.

Windows has no IANA time zone database unless tzdata is installed. Importing
this module must stay safe on that platform; callers look the zone up lazily.
"""

import os
import time
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


def _load_zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _etc_localtime_name():
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    marker = f"{os.sep}zoneinfo{os.sep}"
    index = target.rfind(marker)
    if index == -1:
        return None
    name = target[index + len(marker) :]
    return name or None


def _sync_libc_zone():
    """Make the C library follow TZ. ZoneInfo reads the name itself."""
    if os.environ.get("TZ") and hasattr(time, "tzset"):
        time.tzset()


def _second_occurrence(local, stamp):
    """1 when this instant is the later of two times with the same wall clock."""
    if getattr(local, "tm_isdst", -1) != 0:
        return 0
    try:
        earlier = time.mktime(
            (
                local.tm_year,
                local.tm_mon,
                local.tm_mday,
                local.tm_hour,
                local.tm_min,
                local.tm_sec,
                -1,
                -1,
                1,
            )
        )
    except (OverflowError, OSError, ValueError):
        return 0
    if abs(earlier - stamp) > 1:
        return 1
    return 0


def _struct_offset(local, stamp):
    gmtoff = getattr(local, "tm_gmtoff", None)
    if gmtoff is not None:
        return timedelta(seconds=int(gmtoff))
    utc = datetime.fromtimestamp(stamp, UTC).replace(tzinfo=None)
    wall = datetime(
        local.tm_year,
        local.tm_mon,
        local.tm_mday,
        local.tm_hour,
        local.tm_min,
        local.tm_sec,
    )
    return wall - utc


class _LibcLocal(tzinfo):
    """Local offset for the wall time in question, from the C library.

    Used when the IANA name cannot be loaded. ``datetime.now().astimezone()``
    only knows today's offset, so a July date entered in November would be
    saved an hour off where daylight saving applies.
    """

    def utcoffset(self, dt):
        if dt is None:
            return None
        _sync_libc_zone()
        # fold 0 is the first occurrence (daylight time); fold 1 is the second.
        isdst = 0 if dt.fold else 1
        try:
            stamp = time.mktime(
                (
                    dt.year,
                    dt.month,
                    dt.day,
                    dt.hour,
                    dt.minute,
                    dt.second,
                    -1,
                    -1,
                    isdst,
                )
            )
        except (OverflowError, OSError, ValueError):
            return None
        return _struct_offset(time.localtime(stamp), stamp)

    def dst(self, dt):
        offset = self.utcoffset(dt)
        if offset is None:
            return None
        return timedelta(0)

    def tzname(self, dt):
        return "local"

    def fromutc(self, dt):
        if not isinstance(dt, datetime):
            raise TypeError("fromutc() requires a datetime argument")
        if dt.tzinfo is not self:
            raise ValueError("dt.tzinfo is not self")
        _sync_libc_zone()
        stamp = dt.replace(tzinfo=UTC).timestamp()
        local = time.localtime(stamp)
        return datetime(
            local.tm_year,
            local.tm_mon,
            local.tm_mday,
            local.tm_hour,
            local.tm_min,
            local.tm_sec,
            dt.microsecond,
            tzinfo=self,
            fold=_second_occurrence(local, stamp),
        )


_LIBC_LOCAL = _LibcLocal()


def local_zone():
    """The machine's local zone, looked up on each call.

    An IANA name from ``TZ`` or ``/etc/localtime`` is used when it can be
    loaded, so the offset is the one in effect on the publication date.
    Import does not construct a zone. When that name is missing, the C
    library supplies the offset for the date instead of today's fixed offset.
    """
    configured = os.environ.get("TZ")
    if configured:
        zone = _load_zone(configured)
        if zone is not None:
            return zone
        return _LIBC_LOCAL
    name = _etc_localtime_name()
    if name:
        zone = _load_zone(name)
        if zone is not None:
            return zone
    return _LIBC_LOCAL
