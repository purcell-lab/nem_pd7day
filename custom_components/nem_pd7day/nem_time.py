"""
NEM time helpers — single source of truth for timezone handling.

The National Electricity Market operates on Australian Eastern Standard Time
(AEST), which is UTC+10:00 with NO daylight saving time, year-round.

We use a fixed-offset timezone (datetime.timezone with a 10-hour offset)
rather than "Australia/Brisbane" to avoid any dependency on the host
system's tzdata and to make the +10:00 offset explicit and immutable.

ALL timestamps stored, logged, or exposed by this integration are
timezone-aware strings in ISO-8601 format with the +10:00 suffix,
e.g. "2026-04-14T07:30:00+10:00".

This means:
  - The integration works correctly regardless of HA's system timezone
    (UTC in Docker, local time on bare-metal, etc.)
  - Timestamps can be correctly compared and subtracted without ambiguity
  - Downstream consumers (templates, Jinja2, EMHASS) receive unambiguous
    timestamps they can convert to their own timezone if needed
"""
from __future__ import annotations

from datetime import datetime

from .const import FETCH_TIMES_NEM, INTERVAL_DURATION, NEM_TZ

# ISO 8601 format used throughout the integration
_ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"       # parses both +10:00 and naive
_ISO_OUT = "%Y-%m-%dT%H:%M:%S+10:00"   # always write with explicit offset


def now_nem() -> datetime:
    """Return the current time as a timezone-aware datetime in NEM time."""
    return datetime.now(tz=NEM_TZ)


def parse_nem_csv(s: str) -> datetime:
    """
    Parse a datetime string from the AEMO PD7DAY CSV format
    ("YYYY/MM/DD HH:MM:SS") and attach the NEM timezone.

    The CSV contains no timezone marker — AEMO documents all times as
    NEM time (AEST, UTC+10:00).
    """
    naive = datetime.strptime(s.strip(), "%Y/%m/%d %H:%M:%S")
    return naive.replace(tzinfo=NEM_TZ)


def to_nem_iso(dt: datetime) -> str:
    """
    Format a datetime as an ISO-8601 string with explicit +10:00 offset.

    If dt is naive it is assumed to already be in NEM time and the
    +10:00 suffix is attached without conversion.
    If dt is aware it is converted to NEM time first.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NEM_TZ)
    else:
        dt = dt.astimezone(NEM_TZ)
    return dt.strftime(_ISO_OUT)


def parse_iso(s: str) -> datetime:
    """
    Parse an ISO-8601 string previously written by to_nem_iso().
    Always returns a timezone-aware datetime in NEM time.

    Handles both:
      "2026-04-14T07:30:00+10:00"   (correctly stored)
      "2026-04-14T07:30:00"         (legacy naive — assumed NEM time)
    """
    s = s.strip()
    if s.endswith("+10:00"):
        # Fast path — strip and parse as naive then reattach
        naive = datetime.strptime(s[:-6], "%Y-%m-%dT%H:%M:%S")
        return naive.replace(tzinfo=NEM_TZ)
    try:
        # Generic aware parse (handles other offsets defensively)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.astimezone(NEM_TZ)
        # Truly naive legacy value — assume NEM time
        return dt.replace(tzinfo=NEM_TZ)
    except ValueError:
        # Last resort
        naive = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return naive.replace(tzinfo=NEM_TZ)


def interval_start(nemtime_iso: str) -> str:
    """
    Given an AEMO interval-end timestamp (nemtime), return the interval-start
    timestamp as an ISO-8601 +10:00 string.

    AEMO convention: the published timestamp marks the END of the 30-minute
    dispatch interval.  The interval START is nemtime minus 30 minutes.

    Example:
        nemtime = "2026-04-14T07:30:00+10:00"   (interval ends at 07:30)
        time    = "2026-04-14T07:00:00+10:00"   (interval starts at 07:00)
    """
    return to_nem_iso(parse_iso(nemtime_iso) - INTERVAL_DURATION)


def current_nem_interval() -> str:
    """
    Return the ISO-8601 NEM-time string for the start of the current
    30-minute dispatch interval, e.g. "2026-04-14T07:30:00+10:00".
    Used to match Amber actual prices to the correct PD7DAY forecast interval.
    """
    now = now_nem()
    interval_start = now.replace(
        minute=(now.minute // 30) * 30,
        second=0,
        microsecond=0,
    )
    return to_nem_iso(interval_start)


def fetch_times_as_utc() -> list[str]:
    """
    Return the three daily PD7DAY fetch times converted to UTC HH:MM:SS
    strings suitable for async_track_time (which always fires in UTC).

    NEM fetch times: 07:30, 13:00, 18:00 AEST (UTC+10)
    UTC equivalents: 21:30 (prev day), 03:00, 08:00
    """
    # AEMO publish times in NEM hours/minutes
    utc_strings = []
    for h, m in FETCH_TIMES_NEM:
        # Subtract 10 hours to get UTC, wrapping at midnight
        total_minutes = h * 60 + m - 600   # -600 = -10 hours
        total_minutes %= 1440              # wrap to [0, 1440)
        uh, um = divmod(total_minutes, 60)
        utc_strings.append(f"{uh:02d}:{um:02d}:00")
    return utc_strings
