"""webcore.datetimeutil -- HTTP date formatting and parsing.

HTTP carries timestamps in a handful of header fields (``Date``, ``Expires``,
``Last-Modified``, ``If-Modified-Since``, cookie ``Expires``) and RFC 7231 pins
the preferred wire format to the fixed-width IMF-fixdate form::

    Sun, 06 Nov 1994 08:49:37 GMT

This module renders and parses that form (plus the two obsolete formats a
well-behaved parser must still accept) using only :mod:`datetime` and
:mod:`time`. :mod:`webcore.staticfiles` and :mod:`webcore.caching` use it for
conditional requests and cache freshness.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

__all__ = [
    "http_date",
    "parse_http_date",
    "cookie_date",
    "parse_date_any",
    "is_fresh",
    "seconds_until",
]

#: Weekday abbreviations indexed by :meth:`datetime.weekday` (Mon=0).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
#: Month abbreviations indexed by ``month - 1``.
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_MONTH_INDEX = {name: index + 1 for index, name in enumerate(_MONTHS)}

TimeLike = Union[None, int, float, datetime]


def _to_datetime(value: TimeLike) -> datetime:
    """Coerce ``None`` / epoch seconds / :class:`datetime` into an aware UTC dt."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def http_date(value: TimeLike = None) -> str:
    """Format ``value`` as an RFC 7231 IMF-fixdate string in GMT.

    ``value`` may be ``None`` (meaning *now*), a Unix timestamp, or a
    :class:`datetime`. Naive datetimes are assumed to already be UTC.
    """
    dt = _to_datetime(value)
    return "{wd}, {d:02d} {mon} {y:04d} {h:02d}:{mi:02d}:{s:02d} GMT".format(
        wd=_WEEKDAYS[dt.weekday()],
        d=dt.day,
        mon=_MONTHS[dt.month - 1],
        y=dt.year,
        h=dt.hour,
        mi=dt.minute,
        s=dt.second,
    )


def cookie_date(value: TimeLike = None) -> str:
    """Format a ``Set-Cookie`` ``Expires`` date.

    Historically this used ``-`` between the day, month and year; modern parsers
    accept the space-separated IMF-fixdate too, which is what we emit for
    consistency with :func:`http_date`.
    """
    return http_date(value)


def parse_http_date(value: str) -> Optional[datetime]:
    """Parse the preferred IMF-fixdate form, returning an aware UTC datetime.

    Returns ``None`` for anything that is not exactly the fixed-width form; use
    :func:`parse_date_any` to also accept the two obsolete formats.
    """
    if not value:
        return None
    value = value.strip()
    parts = value.split()
    # "Sun, 06 Nov 1994 08:49:37 GMT" -> 6 tokens.
    if len(parts) != 6 or parts[5] != "GMT":
        return None
    try:
        day = int(parts[1])
        month = _MONTH_INDEX[parts[2]]
        year = int(parts[3])
        clock = parts[4].split(":")
        hour, minute, second = (int(clock[0]), int(clock[1]), int(clock[2]))
    except (KeyError, ValueError, IndexError):
        return None
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_asctime(value: str) -> Optional[datetime]:
    """Parse the ANSI C ``asctime`` form: ``Sun Nov  6 08:49:37 1994``."""
    parts = value.split()
    if len(parts) != 5:
        return None
    try:
        month = _MONTH_INDEX[parts[1]]
        day = int(parts[2])
        clock = parts[3].split(":")
        hour, minute, second = (int(clock[0]), int(clock[1]), int(clock[2]))
        year = int(parts[4])
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except (KeyError, ValueError, IndexError):
        return None


def _parse_rfc850(value: str) -> Optional[datetime]:
    """Parse the obsolete RFC 850 form: ``Sunday, 06-Nov-94 08:49:37 GMT``."""
    value = value.strip()
    parts = value.split()
    if len(parts) != 4 or parts[3] != "GMT":
        return None
    date_part = parts[1].split("-")
    if len(date_part) != 3:
        return None
    try:
        day = int(date_part[0])
        month = _MONTH_INDEX[date_part[1]]
        year = int(date_part[2])
        # Two-digit years: pivot on 70 like the RFC's era.
        year += 1900 if year >= 70 else 2000
        clock = parts[2].split(":")
        hour, minute, second = (int(clock[0]), int(clock[1]), int(clock[2]))
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except (KeyError, ValueError, IndexError):
        return None


def parse_date_any(value: str) -> Optional[datetime]:
    """Parse any of the three HTTP date formats, or return ``None``.

    Tries the preferred IMF-fixdate first, then the RFC 850 and ANSI C forms a
    conforming recipient is still required to accept.
    """
    if not value:
        return None
    for parser in (parse_http_date, _parse_rfc850, _parse_asctime):
        parsed = parser(value)
        if parsed is not None:
            return parsed
    return None


def seconds_until(value: TimeLike, now: TimeLike = None) -> float:
    """Return how many seconds from ``now`` until ``value`` (negative if past)."""
    target = _to_datetime(value)
    reference = _to_datetime(now)
    return (target - reference).total_seconds()


def is_fresh(last_modified: TimeLike, if_modified_since: str) -> bool:
    """Return whether a resource is unchanged for a conditional GET.

    ``True`` means the client's cached copy (dated ``if_modified_since``) is
    still current -- i.e. the resource has not been modified since -- and the
    caller may answer ``304 Not Modified``.
    """
    since = parse_date_any(if_modified_since)
    if since is None:
        return False
    modified = _to_datetime(last_modified).replace(microsecond=0)
    return modified <= since


def add_seconds(value: TimeLike, seconds: float) -> datetime:
    """Return ``value`` advanced by ``seconds`` as an aware UTC datetime."""
    return _to_datetime(value) + timedelta(seconds=seconds)


def now_timestamp() -> int:
    """The current time as whole Unix seconds (thin wrapper over :func:`time.time`)."""
    return int(time.time())
