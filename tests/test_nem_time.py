"""Unit tests for nem_time.py — timezone helpers."""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_nt = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
from custom_components.nem_pd7day.nem_time import (
    NEM_TZ, parse_nem_csv, to_nem_iso, parse_iso,
    current_nem_interval, fetch_times_as_utc, interval_start,
    INTERVAL_DURATION, _amber_express_cutoff,
)

UTC = timezone.utc


def test_nem_tz_offset():
    """NEM_TZ must be exactly UTC+10."""
    dt = datetime(2026, 4, 14, 7, 30, 0, tzinfo=NEM_TZ)
    utc_dt = dt.astimezone(UTC)
    assert utc_dt.hour == 21 or (utc_dt.day == dt.day - 1 and utc_dt.hour == 21)
    # 07:30 AEST = 21:30 UTC previous day
    assert utc_dt.strftime("%H:%M") == "21:30"
    print("  PASS: NEM_TZ is UTC+10")


def test_parse_nem_csv_attaches_tz():
    """parse_nem_csv should return tz-aware datetime in NEM time."""
    dt = parse_nem_csv("2026/04/14 07:30:00")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=10)
    assert dt.hour == 7
    assert dt.minute == 30
    print("  PASS: parse_nem_csv attaches UTC+10")


def test_to_nem_iso_format():
    """to_nem_iso should always produce +10:00 suffix."""
    dt = datetime(2026, 4, 14, 7, 30, 0, tzinfo=NEM_TZ)
    s = to_nem_iso(dt)
    assert s == "2026-04-14T07:30:00+10:00", f"Got: {s}"
    print(f"  PASS: to_nem_iso format: {s}")


def test_to_nem_iso_converts_utc():
    """to_nem_iso should convert a UTC datetime to NEM time."""
    # 21:30 UTC = 07:30 NEM next day
    utc_dt = datetime(2026, 4, 13, 21, 30, 0, tzinfo=UTC)
    s = to_nem_iso(utc_dt)
    assert s == "2026-04-14T07:30:00+10:00", f"Got: {s}"
    print(f"  PASS: to_nem_iso converts UTC→NEM: {s}")


def test_to_nem_iso_naive_assumed_nem():
    """to_nem_iso on a naive datetime should assume NEM time, not shift it."""
    naive = datetime(2026, 4, 14, 7, 30, 0)
    s = to_nem_iso(naive)
    assert s == "2026-04-14T07:30:00+10:00", f"Got: {s}"
    print(f"  PASS: to_nem_iso naive→NEM (no shift): {s}")


def test_parse_iso_roundtrip():
    """parse_iso(to_nem_iso(dt)) should recover the original datetime."""
    dt = datetime(2026, 4, 14, 17, 0, 0, tzinfo=NEM_TZ)
    s = to_nem_iso(dt)
    recovered = parse_iso(s)
    assert recovered == dt
    assert recovered.tzinfo is not None
    print(f"  PASS: parse_iso roundtrip: {s}")


def test_parse_iso_legacy_naive():
    """parse_iso on a legacy naive string assumes NEM time."""
    recovered = parse_iso("2026-04-14T07:30:00")
    assert recovered.tzinfo is not None
    assert recovered.utcoffset() == timedelta(hours=10)
    assert recovered.hour == 7
    print("  PASS: parse_iso legacy naive assumed NEM")


def test_horizon_calculation_tz_aware():
    """
    Horizon in hours between two NEM-aware timestamps must be correct
    regardless of the computation's host timezone.
    """
    run_at = parse_iso("2026-04-13T07:30:00+10:00")
    interval = parse_iso("2026-04-14T19:00:00+10:00")
    horizon_h = (interval - run_at).total_seconds() / 3600
    assert abs(horizon_h - 35.5) < 0.001, f"Got {horizon_h}"
    print(f"  PASS: horizon calculation: {horizon_h}h")


def test_current_nem_interval_format():
    """current_nem_interval should return a +10:00 ISO string."""
    s = current_nem_interval()
    assert s.endswith("+10:00"), f"Got: {s}"
    # Minutes should be 0 or 30
    dt = parse_iso(s)
    assert dt.minute in (0, 30), f"Unexpected minute: {dt.minute}"
    assert dt.second == 0
    print(f"  PASS: current_nem_interval: {s}")


def test_fetch_times_as_utc():
    """
    NEM fetch times 07:30, 13:00, 18:00 AEST should convert to
    21:30, 03:00, 08:00 UTC.
    """
    utc = fetch_times_as_utc()
    assert utc == ["21:30:00", "03:00:00", "08:00:00"], f"Got: {utc}"
    print(f"  PASS: fetch_times_as_utc: {utc}")


def test_interval_start():
    """
    interval_start(nemtime_iso) should return nemtime minus 30 minutes
    as an ISO-8601 +10:00 string.
    """
    nemtime_iso = "2026-04-14T08:00:00+10:00"
    start_iso = interval_start(nemtime_iso)
    assert start_iso == "2026-04-14T07:30:00+10:00", f"Got: {start_iso}"
    # Verify the difference is exactly INTERVAL_DURATION (30 min)
    end_dt = parse_iso(nemtime_iso)
    start_dt = parse_iso(start_iso)
    assert end_dt - start_dt == INTERVAL_DURATION
    print(f"  PASS: interval_start: {nemtime_iso} -> {start_iso}")


def test_amber_express_cutoff_importable_from_nem_time():
    """_amber_express_cutoff must be importable from nem_time (shared helper)."""
    assert callable(_amber_express_cutoff), "_amber_express_cutoff must be callable"
    # Quick smoke test: returns a datetime
    now = datetime(2026, 5, 19, 10, 0, 0, tzinfo=NEM_TZ)
    result = _amber_express_cutoff(now=now)
    assert isinstance(result, datetime), f"Expected datetime, got {type(result)}"
    assert result.tzinfo is not None, "Result must be timezone-aware"
    print("  PASS: _amber_express_cutoff importable from nem_time")


TESTS = [
    test_nem_tz_offset,
    test_parse_nem_csv_attaches_tz,
    test_to_nem_iso_format,
    test_to_nem_iso_converts_utc,
    test_to_nem_iso_naive_assumed_nem,
    test_parse_iso_roundtrip,
    test_parse_iso_legacy_naive,
    test_horizon_calculation_tz_aware,
    test_current_nem_interval_format,
    test_fetch_times_as_utc,
    test_interval_start,
    test_amber_express_cutoff_importable_from_nem_time,
]


def run_all():
    passed = 0
    failed = 0
    print(f"\nRunning {len(TESTS)} nem_time tests\n{'='*50}")
    for test in TESTS:
        name = test.__name__
        try:
            test()
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {name}\n        {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {name}\n        {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {len(TESTS)} tests")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
