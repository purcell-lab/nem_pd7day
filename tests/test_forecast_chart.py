"""Tests for forecast_chart — 7-day forecast chart rendering."""
from __future__ import annotations

import pytest
from custom_components.nem_pd7day.forecast_chart import render_forecast_chart


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_interval(
    nemtime: str = "2026-05-01T08:00:00+10:00",
    raw_value: float = 0.08,
    calibrated: float = 0.075,
    p10: float = 0.06,
    p90: float = 0.09,
    calibrated_source: str = "ols",
    horizon_hours: float = 1.0,
) -> dict:
    return {
        "nemtime": nemtime,
        "raw_value": raw_value,
        "calibrated": calibrated,
        "p10": p10,
        "p90": p90,
        "calibrated_source": calibrated_source,
        "horizon_hours": horizon_hours,
    }


def _make_forecast(n: int = 10, base_hour: int = 7) -> list[dict]:
    """Create a list of n forecast intervals starting from base_hour."""
    intervals = []
    for i in range(n):
        hour = (base_hour + i) % 24
        day = 1 + (base_hour + i) // 24
        intervals.append(_make_interval(
            nemtime=f"2026-05-{day:02d}T{hour:02d}:30:00+10:00",
            raw_value=0.05 + i * 0.01,
            calibrated=0.048 + i * 0.009,
            p10=0.04 + i * 0.008,
            p90=0.06 + i * 0.012,
        ))
    return intervals


# ── Basic rendering tests ─────────────────────────────────────────────────────

def test_render_returns_png_bytes():
    """render_forecast_chart returns non-empty bytes that start with PNG header."""
    forecast = _make_forecast(10)
    result = render_forecast_chart(forecast, "QLD1")
    assert isinstance(result, bytes)
    assert len(result) > 0
    # PNG magic bytes
    assert result[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_empty_list_returns_empty():
    """Empty forecast list returns empty bytes."""
    result = render_forecast_chart([], "QLD1")
    assert result == b""


def test_render_single_interval():
    """Single interval should still render without error."""
    forecast = [_make_interval()]
    result = render_forecast_chart(forecast, "NSW1")
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_render_all_zero_forecast():
    """All-zero values should render without crash."""
    forecast = [
        _make_interval(
            nemtime=f"2026-05-01T{h:02d}:30:00+10:00",
            raw_value=0.0,
            calibrated=0.0,
            p10=0.0,
            p90=0.0,
        )
        for h in range(7, 17)
    ]
    result = render_forecast_chart(forecast, "VIC1")
    assert isinstance(result, bytes)
    assert len(result) > 0


# ── passthrough_high handling ─────────────────────────────────────────────────

def test_render_passthrough_high_intervals():
    """passthrough_high intervals should render without crash, with clipping."""
    forecast = _make_forecast(10)
    # Add a passthrough_high interval with extreme value
    forecast.append(_make_interval(
        nemtime="2026-05-02T17:30:00+10:00",
        raw_value=8.99,
        calibrated=8.99,
        p10=7.50,
        p90=10.00,
        calibrated_source="passthrough_high",
        horizon_hours=24.0,
    ))
    result = render_forecast_chart(forecast, "SA1")
    assert isinstance(result, bytes)
    assert len(result) > 0
    assert result[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_all_passthrough_high():
    """All passthrough_high intervals should still render."""
    forecast = [
        _make_interval(
            nemtime=f"2026-05-01T{h:02d}:30:00+10:00",
            raw_value=5.0,
            calibrated=5.0,
            p10=4.0,
            p90=6.0,
            calibrated_source="passthrough_high",
        )
        for h in range(16, 21)
    ]
    result = render_forecast_chart(forecast, "TAS1")
    assert isinstance(result, bytes)
    assert len(result) > 0


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_render_without_calibration_fields():
    """Intervals without calibrated/p10/p90 should fall back to raw_value."""
    forecast = [
        {
            "nemtime": f"2026-05-01T{h:02d}:30:00+10:00",
            "raw_value": 0.05 + h * 0.005,
        }
        for h in range(7, 17)
    ]
    result = render_forecast_chart(forecast, "QLD1")
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_render_with_negative_p10():
    """Negative p10 values should be handled (y_min extends below zero)."""
    forecast = [
        _make_interval(
            nemtime=f"2026-05-01T{h:02d}:30:00+10:00",
            raw_value=0.02,
            calibrated=0.015,
            p10=-0.01,
            p90=0.04,
        )
        for h in range(7, 17)
    ]
    result = render_forecast_chart(forecast, "QLD1")
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_render_invalid_nemtime_skipped():
    """Intervals with unparseable nemtime should be skipped, not crash."""
    forecast = [
        {"nemtime": "not-a-date", "raw_value": 0.1},
        _make_interval(),
    ]
    result = render_forecast_chart(forecast, "QLD1")
    assert isinstance(result, bytes)
    assert len(result) > 0
