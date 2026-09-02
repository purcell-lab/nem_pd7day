"""
``parse_iso`` moved from ``strptime`` to ``fromisoformat``.

It is one of the hottest functions in the integration. Profiling
``NemPd7dayTariffSensor.extra_state_attributes`` over a 330 interval forecast
put ``strptime`` at 0.316 s of 0.802 s total, reached through 13,200
``parse_iso`` calls, while the calibration everyone assumed was the cost
accounted for 0.063 s. See #62.

The branch it spent that time in was labelled the fast path:

    if s.endswith("+10:00"):
        # Fast path — strip and parse as naive then reattach
        naive = datetime.strptime(s[:-6], "%Y-%m-%dT%H:%M:%S")

``datetime.fromisoformat`` has parsed offsets natively since 3.11 and is a C
level parser, measured here at 0.16 us against 5.84 us for the strptime form,
about 37 times faster.

These tests exist because a parser swap is exactly the kind of change that
looks safe and silently shifts an edge case. The old implementation is
reproduced below and used as an oracle: for every input where the old code
returned a value, the new code must return the same instant with the same
tzinfo. Two behaviours are deliberately *not* equivalent, and both are
improvements asserted separately at the bottom.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.nem_pd7day.nem_time import NEM_TZ, parse_iso, to_nem_iso


def _old_parse_iso(s: str) -> datetime:
    """The implementation replaced in this PR, verbatim, as a reference.

    Kept in the test rather than the module so the comparison is against what
    shipped in v3.3.1 and cannot quietly follow future edits to the real one.
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
def test_matches_the_previous_implementation_exactly(raw):
    """Same instant, same offset, same tzname, for everything that worked before."""
    old = _old_parse_iso(raw)
    new = parse_iso(raw)

    assert new == old, f"{raw!r}: instant moved, {new.isoformat()} != {old.isoformat()}"
    assert new.utcoffset() == old.utcoffset(), f"{raw!r}: offset changed"
    assert new.tzname() == old.tzname(), f"{raw!r}: tzname changed"
    # Naive fields too, so a value that merely compares equal via a different
    # offset does not pass.
    assert new.timetuple()[:6] == old.timetuple()[:6], f"{raw!r}: wall clock moved"


@pytest.mark.parametrize("raw", EQUIVALENT_INPUTS)
def test_always_returns_nem_tz_itself(raw):
    """The returned tzinfo must be NEM_TZ, not merely something equal to it.

    ``fromisoformat`` builds a bare ``timezone(timedelta(hours=10))`` for a
    ``+10:00`` suffix. That compares equal to NEM_TZ, because timezone equality
    only looks at the offset, but it reports ``tzname()`` as "UTC+10:00" where
    NEM_TZ reports "AEST". Returning it directly would leak the wrong name into
    anything formatting %Z. This is why parse_iso normalises with replace().
    """
    result = parse_iso(raw)
    assert result.tzinfo is NEM_TZ, (
        f"{raw!r}: got {result.tzinfo!r} with tzname {result.tzname()!r}, "
        "expected NEM_TZ itself"
    )
    assert result.tzname() == "AEST"


def test_a_bare_ten_hour_timezone_would_have_failed_that_check():
    """Guards the test above from being vacuous.

    If NEM_TZ ever loses its name this test fails and the normalisation it
    protects becomes unnecessary, which is worth knowing explicitly.
    """
    bare = timezone(timedelta(hours=10))
    assert bare == NEM_TZ, "timezone equality is offset only, so these compare equal"
    assert bare.tzname(None) != NEM_TZ.tzname(None), (
        "NEM_TZ is expected to carry name='AEST'"
    )


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
def test_invalid_input_still_raises_value_error(raw):
    """The failure mode is unchanged: ValueError, not a silently wrong value."""
    with pytest.raises(ValueError):
        parse_iso(raw)
    with pytest.raises(ValueError):
        _old_parse_iso(raw)


# ── Deliberate improvements, where the two implementations differ ────────────


@pytest.mark.parametrize(
    "raw,expected_wall",
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
def test_forms_that_used_to_raise_now_parse(raw, expected_wall):
    """These are not equivalence cases. The old code raised on all three."""
    with pytest.raises(ValueError):
        _old_parse_iso(raw)

    result = parse_iso(raw)
    assert result.timetuple()[:6] == expected_wall
    assert result.tzinfo is NEM_TZ


def test_the_replaced_branch_was_the_slow_one():
    """Records the measurement that motivated the change.

    Not a performance assertion, which would be flaky on shared runners. It
    asserts only that the two parsers agree on the canonical string, so the
    comparison quoted in the docstring is measuring like for like.
    """
    s = "2026-09-02T07:30:00+10:00"
    via_strptime = datetime.strptime(s[:-6], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=NEM_TZ
    )
    assert parse_iso(s) == via_strptime
