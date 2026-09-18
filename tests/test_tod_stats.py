"""Tests for tod_stats.py (time of day actual price statistics) and the bias
chart that draws them.

``slot_for_now`` floors to the containing slot, issue #45: requiring exact
equality on the minute meant a state write at, say, 10:06:39 matched no slot
at all and the sensor rendered unknown until the next boundary tick 24
minutes later.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from support import NEM_TZ, load

_engine_mod = load("calibration_engine")
_tod_stats = load("tod_stats")
_bias_chart = load("bias_chart")
compute, render_chart, TodStats, SlotStats = (
    _tod_stats.compute, _tod_stats.render_chart, _tod_stats.TodStats, _tod_stats.SlotStats,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_obs(interval_time: str, actual_rrp: float | None,
              forecast_run_at: str = "2026-04-20T07:00:00+10:00") -> dict:
    return {
        "interval_time": interval_time,
        "actual_rrp": actual_rrp,
        "forecast_run_at": forecast_run_at,
        "pd7day_forecast": 0.1,
    }


def _full_day_stats() -> TodStats:
    """Statistics with all 48 slots populated, one observation each."""
    return compute([
        _make_obs(f"2026-04-20T{hour:02d}:{minute:02d}:00+10:00", 0.10)
        for hour in range(24) for minute in (0, 30)
    ])


def _bucket(key, a, b, n):
    e = _engine_mod
    return e.BucketModel(
        key,
        e.LinearCoeff(a=a, b=b, n=n, mae=0.01, rmse=0.02),
        e.QuantileCoeff(0.1, a=a * 0.95, b=b, n=n),
        e.QuantileCoeff(0.5, a=a, b=b, n=n),
        e.QuantileCoeff(0.9, a=a * 1.15, b=b, n=n),
    )


def _calibration(keys):
    """A CalibrationResult with a fitted bucket for each key."""
    coeffs = {
        "h00_06__peak": (0.40, 0.060, 50),
        "h00_06__solar": (0.95, 0.012, 70),
        "h12_24__shoulder": (1.50, -0.057, 18),
        "h24_48__peak": (0.05, 0.087, 76),
    }
    return _engine_mod.CalibrationResult(
        fitted_at="2026-04-21T18:00:00+10:00",
        total_observations=500,
        models={k: _bucket(k, *coeffs[k]) for k in keys},
    )


# ── compute() ─────────────────────────────────────────────────────────────────

def test_empty_observations_returns_empty_stats():
    stats = compute([])
    assert stats.slots == []
    assert stats.unique_intervals == 0


def test_none_actual_rrp_excluded():
    stats = compute([
        _make_obs("2026-04-20T08:00:00+10:00", None),
        _make_obs("2026-04-20T08:30:00+10:00", 0.10),
    ])
    assert stats.unique_intervals == 1
    assert len(stats.slots) == 1
    assert stats.slots[0].label == "08:30"


def test_deduplication_same_interval_different_runs():
    """Multiple forecast runs for the same interval_time count as one actual."""
    stats = compute([
        _make_obs("2026-04-20T09:00:00+10:00", 0.10, "2026-04-20T07:00:00+10:00"),
        _make_obs("2026-04-20T09:00:00+10:00", 0.10, "2026-04-20T08:00:00+10:00"),
        _make_obs("2026-04-20T09:00:00+10:00", 0.10, "2026-04-19T18:00:00+10:00"),
    ])
    assert stats.unique_intervals == 1
    assert len(stats.slots) == 1
    assert stats.slots[0].n == 1


def test_multiple_days_same_slot_aggregated():
    """Same time-of-day slot across multiple days accumulates correctly."""
    stats = compute([
        _make_obs("2026-04-18T10:00:00+10:00", 0.05),
        _make_obs("2026-04-19T10:00:00+10:00", 0.07),
        _make_obs("2026-04-20T10:00:00+10:00", 0.09),
    ])
    assert stats.unique_intervals == 3
    assert len(stats.slots) == 1
    slot = stats.slots[0]
    assert slot.label == "10:00"
    assert slot.n == 3
    assert abs(slot.mean - 0.07) < 1e-9
    assert slot.p10 < slot.median < slot.p90


def test_slot_stats_ordering():
    """Slots are ordered by (hour, minute)."""
    stats = compute([
        _make_obs("2026-04-20T12:30:00+10:00", 0.08),
        _make_obs("2026-04-20T08:00:00+10:00", 0.10),
        _make_obs("2026-04-20T23:00:00+10:00", 0.06),
        _make_obs("2026-04-20T00:30:00+10:00", 0.09),
    ])
    labels = [s.label for s in stats.slots]
    assert labels == sorted(labels)


def test_negative_prices_included():
    """Negative actual prices (solar window) must be included, not filtered."""
    stats = compute([
        _make_obs("2026-04-20T11:00:00+10:00", -0.03),
        _make_obs("2026-04-20T11:30:00+10:00", -0.05),
    ])
    assert stats.unique_intervals == 2
    for slot in stats.slots:
        assert slot.mean < 0


def test_as_attributes_structure():
    stats = compute([
        _make_obs("2026-04-20T08:00:00+10:00", 0.10),
        _make_obs("2026-04-20T08:30:00+10:00", 0.12),
    ])
    attrs = stats.as_attributes()
    assert "unique_intervals" in attrs
    assert "slots" in attrs
    assert isinstance(attrs["slots"], list)
    assert len(attrs["slots"]) == 2
    for slot_dict in attrs["slots"]:
        for key in ("hour", "minute", "label", "n", "mean_kwh", "median_kwh",
                    "p10_kwh", "p25_kwh", "p75_kwh", "p90_kwh"):
            assert key in slot_dict, f"Missing key: {key}"


# ── slot_for_now containment, issue #45 ──────────────────────────────────────

@pytest.mark.parametrize(
    "minute,expected_label",
    [
        (0, "14:00"),
        (1, "14:00"),
        (15, "14:00"),
        (29, "14:00"),
        (30, "14:30"),
        (31, "14:30"),
        (45, "14:30"),
        (59, "14:30"),
    ],
)
def test_slot_for_now_floors_to_the_containing_slot(minute, expected_label):
    """Any minute must resolve to the 30 minute slot containing it."""
    slot = _full_day_stats().slot_for_now(datetime(2026, 4, 21, 14, minute, tzinfo=NEM_TZ))
    assert slot is not None, f"minute {minute} resolved to no slot"
    assert slot.label == expected_label


def test_slot_for_now_resolves_every_minute_of_the_day():
    """A populated set of slots must never yield None for any wall clock minute,
    which is the property that makes the sensor independent of when it happens
    to be written.
    """
    stats = _full_day_stats()
    unresolved = [
        (h, m)
        for h in range(24)
        for m in range(60)
        if stats.slot_for_now(datetime(2026, 4, 21, h, m, tzinfo=NEM_TZ)) is None
    ]
    assert unresolved == [], f"minutes with no slot: {unresolved[:10]}"


def test_slot_for_now_still_returns_none_when_the_slot_has_no_data():
    """Flooring must not invent a slot. An hour with no observations still
    resolves to None rather than borrowing a neighbouring slot.
    """
    stats = compute([
        _make_obs("2026-04-20T14:00:00+10:00", 0.10),
        _make_obs("2026-04-20T14:30:00+10:00", 0.12),
    ])
    assert stats.slot_for_now(datetime(2026, 4, 21, 14, 0, tzinfo=NEM_TZ)).label == "14:00"
    assert stats.slot_for_now(datetime(2026, 4, 21, 14, 6, tzinfo=NEM_TZ)) is not None
    assert stats.slot_for_now(datetime(2026, 4, 21, 15, 0, tzinfo=NEM_TZ)) is None
    assert stats.slot_for_now(datetime(2026, 4, 21, 15, 6, tzinfo=NEM_TZ)) is None


# ── render_chart ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("region", [None, "NSW1"])
def test_render_chart_returns_png_bytes(region):
    """A PNG for the default region and for a non-QLD1 one."""
    stats = compute([
        _make_obs(f"2026-04-{18 + d:02d}T{h:02d}:{m:02d}:00+10:00", 0.05 + h * 0.005)
        for d in range(5)
        for h, m in [(8, 0), (8, 30), (12, 0), (12, 30), (18, 0), (18, 30)]
    ])
    png = render_chart(stats) if region is None else render_chart(stats, region=region)
    assert isinstance(png, bytes)
    assert len(png) > 1000
    assert png[:4] == b"\x89PNG"


def test_render_chart_empty_returns_empty():
    assert render_chart(TodStats()) == b""


# ── bias_chart ────────────────────────────────────────────────────────────────

def test_bias_chart_none_calibration_returns_empty():
    assert _bias_chart.render_chart(None) == b""


def test_bias_chart_renders_png_from_calibration():
    """A few fitted buckets and no tod_stats (the placeholder panel) render."""
    cal = _calibration(("h00_06__peak", "h00_06__solar", "h12_24__shoulder", "h24_48__peak"))
    png = _bias_chart.render_chart(cal, obs_count=500, region="QLD1")
    assert isinstance(png, bytes)
    assert len(png) > 5000
    assert png[:4] == b"\x89PNG"


def test_bias_chart_renders_with_tod_stats():
    """bias_chart.render_chart accepts tod_stats and draws them."""
    stats = TodStats(
        slots=[
            SlotStats(hour=h, minute=m, n=10, mean=0.05 + h * 0.003,
                      median=0.05, p10=0.03, p25=0.04, p75=0.06, p90=0.07,
                      mean_raw=0.06 + h * 0.003, mean_calibrated=0.055 + h * 0.003)
            for h in range(0, 24) for m in (0, 30)
        ],
        unique_intervals=48,
        date_from="18 Apr",
        date_to="20 Apr 2026",
    )
    png = _bias_chart.render_chart(_calibration(("h00_06__peak",)), obs_count=100,
                                   region="SA1", tod_stats=stats)
    assert isinstance(png, bytes)
    assert len(png) > 5000
    assert png[:4] == b"\x89PNG"
