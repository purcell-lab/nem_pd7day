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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .const import FETCH_TIMES_NEM, INTERVAL_DURATION, NEM_TZ

# ISO 8601 format used throughout the integration
_ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"       # parses both +10:00 and naive
_ISO_OUT = "%Y-%m-%dT%H:%M:%S+10:00"   # always write with explicit offset

# Compared against in parse_iso to recognise a timestamp that is already at NEM
# time and so needs no conversion. Derived from NEM_TZ rather than restated, so
# the two cannot drift.
_NEM_UTCOFFSET = NEM_TZ.utcoffset(None)


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
    Always returns a timezone-aware datetime whose tzinfo is NEM_TZ.

    Handles:
      "2026-04-14T07:30:00+10:00"   (correctly stored)
      "2026-04-14T07:30:00"         (legacy naive — assumed NEM time)
      "2026-04-13T21:30:00Z"        (other offsets — converted to NEM time)

    Uses ``fromisoformat`` rather than ``strptime``. This is one of the hottest
    functions in the integration, reached roughly eight times per forecast
    interval per state write, and the two parsers measure 0.16 us against
    5.84 us on the canonical string. The previous implementation spent 0.316 s
    of an 0.802 s tariff attribute build inside ``strptime``. See #62.

    A side effect of the change is that any ISO-8601 form CPython accepts now
    parses, including fractional seconds, minute precision and a space date
    separator. The old code raised ValueError on all three when they carried a
    "+10:00" suffix.
    """
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Last resort for anything fromisoformat rejects.
        naive = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return naive.replace(tzinfo=NEM_TZ)

    offset = dt.utcoffset()
    if offset is None or offset == _NEM_UTCOFFSET:
        # Either a legacy naive value, assumed to be NEM time, or already at
        # +10:00. Attach NEM_TZ rather than keeping whatever fromisoformat
        # built: NEM_TZ carries name="AEST", and a bare timezone(timedelta(
        # hours=10)) compares equal to it but reports a different tzname(),
        # which would leak into anything formatting %Z. replace() is a field
        # swap with no conversion, so the instant is untouched.
        return dt.replace(tzinfo=NEM_TZ)
    # Some other offset. Convert, which is the only case that shifts fields.
    return dt.astimezone(NEM_TZ)


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


def _amber_express_cutoff(now: datetime) -> datetime:
    """
    Return the earliest datetime that PD7DAY should cover.

    Amber Express provides forecasts through a window that shrinks between
    3:30am and 12:30pm NEM time (it only reaches to 3:30am the next day).
    Outside that window, it covers a full rolling 24h.

    During the "short window" (3:30am–12:30pm NEM):
        cutoff = tomorrow 3:30am NEM  (pinned boundary)
    Outside (12:30pm–3:30am NEM):
        cutoff = now + 24h            (rolling horizon)

    NEM time is UTC+10, no DST. The day 2-7 sensors use this rule only when no
    Amber forecast entity is configured or it has no current forecast; see
    day27_start (#235).
    """
    from datetime import timedelta
    window_start = now.replace(hour=3, minute=30, second=0, microsecond=0)
    window_end = now.replace(hour=12, minute=30, second=0, microsecond=0)
    if window_start <= now < window_end:
        tomorrow_330 = window_start + timedelta(days=1)
        return tomorrow_330
    else:
        return now + timedelta(hours=24)


def amber_coverage_end(attributes: Mapping[str, Any]) -> datetime | None:
    """Where an Amber forecast stops: the end of its last interval, or None.

    Amber Electric publishes ``forecasts`` and Amber Express ``detailedForecast``,
    and each entry carries its ``end_time``. Amber Express's plain ``forecast``
    lists interval starts only, so without the detailed list its end is the
    last start plus one interval (#235).
    """
    for key in ("forecasts", "detailedForecast"):
        entries = attributes.get(key)
        if isinstance(entries, list) and entries:
            ends = [_parse_or_none(e.get("end_time")) for e in entries if isinstance(e, Mapping)]
            found = [e for e in ends if e is not None]
            if found:
                return max(found)
    entries = attributes.get("forecast")
    if isinstance(entries, list) and entries:
        starts = [_parse_or_none(e.get("time")) for e in entries if isinstance(e, Mapping)]
        found = [s for s in starts if s is not None]
        if found:
            return max(found) + INTERVAL_DURATION
    return None


def _parse_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Day27Start:
    """Where the day 2-7 series begins, and which rule placed it."""

    at: datetime
    source: str     # "amber" or "clock"

    def includes(self, interval_start: datetime) -> bool:
        # Amber's end is the first interval it does not cover, so it is listed.
        # The clock cutoff keeps its exclusive comparison, unchanged since v2.
        if self.source == "amber":
            return interval_start >= self.at
        return interval_start > self.at


def day27_start(amber_attributes: Mapping[str, Any] | None, now: datetime | None = None) -> Day27Start:
    """Start the day 2-7 series where the Amber forecast it is summed with ends.

    Falls back to the clock rule when there is no Amber forecast or its coverage
    ends before ``now`` (#235).
    """
    if now is None:
        now = now_nem()
    if amber_attributes is not None:
        end = amber_coverage_end(amber_attributes)
        if end is not None and end > now:
            return Day27Start(end, "amber")
    return Day27Start(_amber_express_cutoff(now), "clock")


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
