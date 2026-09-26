"""Tests for nem_time: the NEM timezone helpers and parse_iso.

parse_iso history (issue #62). It moved from strptime to fromisoformat. It is
one of the hottest functions in the integration: profiling
NemPd7dayTariffSensor.extra_state_attributes over a 330 interval forecast put
strptime at 0.316 s of 0.802 s total, reached through 13,200 parse_iso calls,
while the calibration everyone assumed was the cost accounted for 0.063 s.
The branch it spent that time in was labelled the fast path:

    if s.endswith("+10:00"):
        # Fast path: strip and parse as naive then reattach
        naive = datetime.strptime(s[:-6], "%Y-%m-%dT%H:%M:%S")

datetime.fromisoformat has parsed offsets natively since 3.11 and is a C level
parser, measured at 0.16 us against 5.84 us for the strptime form, about 37
times faster. A parser swap is exactly the kind of change that looks safe and
silently shifts an edge case, so the old implementation is reproduced below
and used as an oracle: for every input where it returned a value, the new
code must return the same instant with the same tzinfo. Two behaviours are
deliberately not equivalent and are asserted separately at the bottom.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from support import load_chain

_const, nt = load_chain("const", "nem_time")

NEM_TZ = nt.NEM_TZ
parse_iso = nt.parse_iso
to_nem_iso = nt.to_nem_iso
UTC = timezone.utc


# ── Timezone helpers ─────────────────────────────────────────────────────────

def test_nem_tz_is_utc_plus_ten():
    assert NEM_TZ.utcoffset(None) == timedelta(hours=10)
    dt = datetime(2026, 4, 14, 7, 30, 0, tzinfo=NEM_TZ)
    # 07:30 AEST is 21:30 UTC the previous day.
    assert dt.astimezone(UTC) == datetime(2026, 4, 13, 21, 30, 0, tzinfo=UTC)


def test_parse_nem_csv_attaches_tz():
    """parse_nem_csv should return a tz-aware datetime in NEM time."""
    dt = nt.parse_nem_csv("2026/04/14 07:30:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=10)
    assert dt.hour == 7
    assert dt.minute == 30


@pytest.mark.parametrize(
    "dt",
    [
        datetime(2026, 4, 14, 7, 30, 0, tzinfo=NEM_TZ),
        # 21:30 UTC is 07:30 NEM the next day.
        datetime(2026, 4, 13, 21, 30, 0, tzinfo=UTC),
        # A naive value is assumed to already be NEM time and is not shifted.
        datetime(2026, 4, 14, 7, 30, 0),
    ],
    ids=["nem-aware", "utc-aware", "naive"],
)
def test_to_nem_iso_always_writes_the_plus_ten_form(dt):
    assert to_nem_iso(dt) == "2026-04-14T07:30:00+10:00"


@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 4, 14, 7, 0, 0, tzinfo=NEM_TZ), "2026-04-14T07:00:00+10:00"),
        (datetime(2026, 4, 14, 7, 29, 59, tzinfo=NEM_TZ), "2026-04-14T07:00:00+10:00"),
        (datetime(2026, 4, 14, 7, 44, 59, 999, tzinfo=NEM_TZ), "2026-04-14T07:30:00+10:00"),
    ],
)
def test_current_nem_interval_floors_to_the_half_hour(now, expected):
    with patch.object(nt, "now_nem", return_value=now):
        assert nt.current_nem_interval() == expected


def test_fetch_times_as_utc():
    """NEM fetch times 07:30, 13:00, 18:00 AEST are 21:30, 03:00, 08:00 UTC."""
    assert nt.fetch_times_as_utc() == ["21:30:00", "03:00:00", "08:00:00"]


def test_interval_start():
    """interval_start(nemtime_iso) is nemtime minus 30 minutes, +10:00 form."""
    nemtime_iso = "2026-04-14T08:00:00+10:00"
    start_iso = nt.interval_start(nemtime_iso)
    assert start_iso == "2026-04-14T07:30:00+10:00"
    assert parse_iso(nemtime_iso) - parse_iso(start_iso) == nt.INTERVAL_DURATION


# ── parse_iso against the strptime implementation it replaced (issue #62) ───

def _old_parse_iso(s: str) -> datetime:
    """The implementation replaced in v3.3.1, verbatim, as a reference.

    Kept in the test rather than the module so the comparison is against what
    shipped and cannot quietly follow future edits to the real one.
    """
    s = s.strip()
    if s.endswith("+10:00"):
        naive = datetime.strptime(s[:-6], "%Y-%m-%dT%H:%M:%S")
        return naive.replace(tzinfo=NEM_TZ)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.astimezone(NEM_TZ)
        return dt.replace(tzinfo=NEM_TZ)
    except ValueError:
        naive = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return naive.replace(tzinfo=NEM_TZ)


# Every shape the integration stores or has ever stored, plus the offsets a
# foreign producer could hand us.
EQUIVALENT_INPUTS = [
    # The canonical form, which is what to_nem_iso writes.
    "2026-04-14T07:30:00+10:00",
    "2026-09-01T18:00:00+10:00",
    "2026-01-01T00:00:00+10:00",
    "2026-12-31T23:59:59+10:00",
    # Leap day and a DST boundary in the southern states, neither of which the
    # NEM observes but which a converted value could carry.
    "2028-02-29T12:00:00+10:00",
    "2026-04-05T03:00:00+10:00",
    # Legacy naive values, assumed to already be NEM time.
    "2026-04-14T07:30:00",
    "2026-04-14T00:00:00",
    # Other offsets, which must be converted rather than relabelled.
    "2026-04-14T07:30:00+00:00",
    "2026-04-13T21:30:00Z",
    "2026-04-14T07:00:00+09:30",
    "2026-04-13T14:30:00-07:00",
    "2026-04-14T07:30:00+11:00",
    # Surrounding whitespace, which the function strips.
    "  2026-04-14T07:30:00+10:00  ",
    "\t2026-04-14T07:30:00\n",
    # Date only.
    "2026-04-14",
]


@pytest.mark.parametrize("raw", EQUIVALENT_INPUTS)
def test_parse_iso_matches_the_previous_implementation_exactly(raw):
    """Same instant, same offset, same tzname and the same wall clock as the
    old parser, and the returned tzinfo is NEM_TZ itself.

    fromisoformat builds a bare timezone(timedelta(hours=10)) for a +10:00
    suffix. That compares equal to NEM_TZ, because timezone equality only
    looks at the offset, but reports tzname() as "UTC+10:00" where NEM_TZ
    reports "AEST". Returning it directly would leak the wrong name into
    anything formatting %Z, which is why parse_iso normalises with replace().
    """
    old = _old_parse_iso(raw)
    new = parse_iso(raw)

    assert new == old, f"{raw!r}: instant moved, {new.isoformat()} != {old.isoformat()}"
    assert new.utcoffset() == old.utcoffset(), f"{raw!r}: offset changed"
    assert new.tzname() == old.tzname(), f"{raw!r}: tzname changed"
    # Naive fields too, so a value that merely compares equal via a different
    # offset does not pass.
    assert new.timetuple()[:6] == old.timetuple()[:6], f"{raw!r}: wall clock moved"
    assert new.tzinfo is NEM_TZ, (
        f"{raw!r}: got {new.tzinfo!r} with tzname {new.tzname()!r}, expected NEM_TZ itself"
    )
    assert new.tzname() == "AEST"


def test_a_bare_ten_hour_timezone_would_have_failed_that_check():
    """Guards the tzinfo identity assertion above from being vacuous.

    If NEM_TZ ever loses its name this test fails and the normalisation it
    protects becomes unnecessary, which is worth knowing explicitly.
    """
    bare = timezone(timedelta(hours=10))
    assert bare == NEM_TZ, "timezone equality is offset only, so these compare equal"
    assert bare.tzname(None) != NEM_TZ.tzname(None), "NEM_TZ is expected to carry name='AEST'"


def test_roundtrip_through_to_nem_iso_is_stable():
    """to_nem_iso then parse_iso, repeatedly, must not drift."""
    dt = datetime(2026, 9, 1, 18, 0, 0, tzinfo=NEM_TZ)
    s = to_nem_iso(dt)
    for _ in range(5):
        parsed = parse_iso(s)
        assert parsed == dt
        s2 = to_nem_iso(parsed)
        assert s2 == s, f"string drifted: {s!r} -> {s2!r}"
        s = s2


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not a timestamp",
        "2026-13-01T00:00:00+10:00",  # month 13
        "2026-04-31T00:00:00+10:00",  # April has 30 days
    ],
)
def test_parse_iso_invalid_input_still_raises_value_error(raw):
    """The failure mode is unchanged: ValueError, not a silently wrong value."""
    with pytest.raises(ValueError):
        parse_iso(raw)
    with pytest.raises(ValueError):
        _old_parse_iso(raw)


@pytest.mark.parametrize(
    "raw, expected_wall",
    [
        # Fractional seconds. The old fast path stripped the offset then handed
        # "...T07:30:00.500" to a format string with no %f, and the ValueError
        # escaped because the try/except only wrapped the other branch.
        ("2026-04-14T07:30:00.500+10:00", (2026, 4, 14, 7, 30, 0)),
        # Minute precision, valid ISO 8601.
        ("2026-04-14T07:30+10:00", (2026, 4, 14, 7, 30, 0)),
        # Space separator instead of T, which ISO 8601 permits and which
        # Home Assistant itself emits in some contexts.
        ("2026-04-14 07:30:00+10:00", (2026, 4, 14, 7, 30, 0)),
    ],
)
def test_parse_iso_forms_that_used_to_raise_now_parse(raw, expected_wall):
    """These are not equivalence cases. The old code raised on all three."""
    with pytest.raises(ValueError):
        _old_parse_iso(raw)

    result = parse_iso(raw)
    assert result.timetuple()[:6] == expected_wall
    assert result.tzinfo is NEM_TZ
