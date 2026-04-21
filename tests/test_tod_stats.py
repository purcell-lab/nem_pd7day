"""Tests for tod_stats — time-of-day actual price statistics."""
from __future__ import annotations

import pytest
from custom_components.nem_pd7day.tod_stats import compute, render_chart, TodStats, SlotStats


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_obs(interval_time: str, actual_rrp: float | None, forecast_run_at: str = "2026-04-20T07:00:00+10:00") -> dict:
    return {
        "interval_time": interval_time,
        "actual_rrp": actual_rrp,
        "forecast_run_at": forecast_run_at,
        "pd7day_forecast": 0.1,
    }


# ── compute() tests ─────────────────────────────────────────────────────────

def test_empty_observations_returns_empty_stats():
    stats = compute([])
    assert stats.slots == []
    assert stats.unique_intervals == 0


def test_none_actual_rrp_excluded():
    obs = [
        _make_obs("2026-04-20T08:00:00+10:00", None),
        _make_obs("2026-04-20T08:30:00+10:00", 0.10),
    ]
    stats = compute(obs)
    assert stats.unique_intervals == 1
    assert len(stats.slots) == 1
    assert stats.slots[0].label == "08:30"


def test_deduplication_same_interval_different_runs():
    """Multiple forecast runs for the same interval_time count as one actual."""
    obs = [
        _make_obs("2026-04-20T09:00:00+10:00", 0.10, "2026-04-20T07:00:00+10:00"),
        _make_obs("2026-04-20T09:00:00+10:00", 0.10, "2026-04-20T08:00:00+10:00"),
        _make_obs("2026-04-20T09:00:00+10:00", 0.10, "2026-04-19T18:00:00+10:00"),
    ]
    stats = compute(obs)
    assert stats.unique_intervals == 1
    assert len(stats.slots) == 1
    assert stats.slots[0].n == 1


def test_multiple_days_same_slot_aggregated():
    """Same time-of-day slot across multiple days accumulates correctly."""
    obs = [
        _make_obs("2026-04-18T10:00:00+10:00", 0.05),
        _make_obs("2026-04-19T10:00:00+10:00", 0.07),
        _make_obs("2026-04-20T10:00:00+10:00", 0.09),
    ]
    stats = compute(obs)
    assert stats.unique_intervals == 3
    assert len(stats.slots) == 1
    slot = stats.slots[0]
    assert slot.label == "10:00"
    assert slot.n == 3
    assert abs(slot.mean - 0.07) < 1e-9
    assert slot.p10 < slot.median < slot.p90


def test_slot_stats_ordering():
    """Slots are ordered by (hour, minute)."""
    obs = [
        _make_obs("2026-04-20T12:30:00+10:00", 0.08),
        _make_obs("2026-04-20T08:00:00+10:00", 0.10),
        _make_obs("2026-04-20T23:00:00+10:00", 0.06),
        _make_obs("2026-04-20T00:30:00+10:00", 0.09),
    ]
    stats = compute(obs)
    labels = [s.label for s in stats.slots]
    assert labels == sorted(labels)


def test_negative_prices_included():
    """Negative actual prices (solar window) must be included, not filtered."""
    obs = [
        _make_obs("2026-04-20T11:00:00+10:00", -0.03),
        _make_obs("2026-04-20T11:30:00+10:00", -0.05),
    ]
    stats = compute(obs)
    assert stats.unique_intervals == 2
    for slot in stats.slots:
        assert slot.mean < 0


def test_slot_for_now_returns_correct_slot():
    from datetime import datetime, timezone, timedelta
    NEM_TZ = timezone(timedelta(hours=10))
    obs = [
        _make_obs("2026-04-20T14:00:00+10:00", 0.10),
        _make_obs("2026-04-20T14:30:00+10:00", 0.12),
    ]
    stats = compute(obs)
    dt_match    = datetime(2026, 4, 21, 14, 0, tzinfo=NEM_TZ)
    dt_no_match = datetime(2026, 4, 21, 15, 0, tzinfo=NEM_TZ)
    assert stats.slot_for_now(dt_match) is not None
    assert stats.slot_for_now(dt_match).label == "14:00"
    assert stats.slot_for_now(dt_no_match) is None


def test_as_attributes_structure():
    obs = [
        _make_obs("2026-04-20T08:00:00+10:00", 0.10),
        _make_obs("2026-04-20T08:30:00+10:00", 0.12),
    ]
    stats = compute(obs)
    attrs = stats.as_attributes()
    assert "unique_intervals" in attrs
    assert "slots" in attrs
    assert isinstance(attrs["slots"], list)
    assert len(attrs["slots"]) == 2
    for slot_dict in attrs["slots"]:
        for key in ("hour", "minute", "label", "n", "mean_kwh", "median_kwh",
                    "p10_kwh", "p25_kwh", "p75_kwh", "p90_kwh"):
            assert key in slot_dict, f"Missing key: {key}"


def test_render_chart_returns_png_bytes():
    obs = [
        _make_obs(f"2026-04-{18+d:02d}T{h:02d}:{m:02d}:00+10:00", 0.05 + h * 0.005)
        for d in range(5)
        for h, m in [(8, 0), (8, 30), (12, 0), (12, 30), (18, 0), (18, 30)]
    ]
    stats = compute(obs)
    png = render_chart(stats)
    assert isinstance(png, bytes)
    assert len(png) > 1000
    # PNG magic bytes
    assert png[:4] == b'\x89PNG'


def test_render_chart_empty_returns_empty():
    stats = TodStats()
    result = render_chart(stats)
    assert result == b""
