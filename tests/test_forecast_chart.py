"""Tests for forecast_chart — 7-day forecast chart rendering."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from custom_components.nem_pd7day.forecast_chart import (
    render_forecast_chart,
    _is_spike_callout_eligible,
    _placeholder_png,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_interval(
    nemtime: str = "2026-05-01T08:00:00+10:00",
    raw_value: float = 0.08,
    calibrated: float = 0.075,
    p10: float = 0.06,
    p90: float = 0.09,
    calibrated_source: str = "ols",
    horizon_hours: float = 1.0,
    forecast_run_at: str | None = None,
    spike_first_run: bool = True,
) -> dict:
    d = {
        "nemtime": nemtime,
        "raw_value": raw_value,
        "calibrated": calibrated,
        "p10": p10,
        "p90": p90,
        "calibrated_source": calibrated_source,
        "horizon_hours": horizon_hours,
        "spike_first_run": spike_first_run,
    }
    if forecast_run_at is not None:
        d["forecast_run_at"] = forecast_run_at
    return d


def _make_forecast(n: int = 10, base_hour: int = 7, forecast_run_at: str | None = None) -> list[dict]:
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
            forecast_run_at=forecast_run_at,
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


# ── Isotonic source regression tests ────────────────────────────────────────

def test_forecast_chart_daily_minmax_with_isotonic_source():
    """Daily min/max dots must render when calibrated_source is 'isotonic' (not 'ols')."""
    from datetime import datetime, timezone, timedelta

    NEM_TZ = timezone(timedelta(hours=10))
    base = datetime(2026, 5, 12, 18, 0, tzinfo=NEM_TZ)

    # Build 48 half-hourly intervals (2 days) with isotonic source
    data = []
    for i in range(48):
        t = base + timedelta(minutes=30 * i)
        data.append({
            "nemtime": t.isoformat(),
            "raw_value": 0.08 + (i % 10) * 0.005,
            "calibrated": 0.07 + (i % 10) * 0.004,
            "p10": 0.05,
            "p90": 0.12,
            "calibrated_source": "isotonic",
        })

    png = render_forecast_chart(data, region="QLD1")
    assert isinstance(png, bytes)
    assert len(png) > 1000
    assert png[:4] == b'\x89PNG'


# ── Rec 1: Horizon-gated spike callout tests ────────────────────────────────

def test_spike_callout_suppressed_beyond_48h():
    """Spike callouts at horizon >= 48h must be suppressed regardless of value."""
    eligible, style = _is_spike_callout_eligible(20.30, horizon_hours=72.0, spike_first_run=False)
    assert not eligible
    eligible, style = _is_spike_callout_eligible(20.30, horizon_hours=168.0, spike_first_run=False)
    assert not eligible


def test_spike_callout_within_24h_lower_threshold():
    """Within 24h, raw >= $1.50 qualifies; below $1.50 does not."""
    eligible, _ = _is_spike_callout_eligible(1.50, horizon_hours=10.0, spike_first_run=False)
    assert eligible
    eligible, _ = _is_spike_callout_eligible(1.49, horizon_hours=10.0, spike_first_run=False)
    assert not eligible


def test_spike_callout_24_48h_higher_threshold():
    """At 24-48h horizon, raw >= $3.00 qualifies; below $3.00 does not."""
    eligible, _ = _is_spike_callout_eligible(3.00, horizon_hours=36.0, spike_first_run=False)
    assert eligible
    eligible, _ = _is_spike_callout_eligible(2.99, horizon_hours=36.0, spike_first_run=False)
    assert not eligible


def test_spike_callout_at_48h_boundary_suppressed():
    """Exactly 48h horizon is NOT eligible (>= 48 suppressed)."""
    eligible, _ = _is_spike_callout_eligible(20.30, horizon_hours=48.0, spike_first_run=False)
    assert not eligible


# ── Rec 4: Spike persistence scoring tests ──────────────────────────────────

def test_spike_first_run_is_candidate():
    """A spike appearing for the first time (spike_first_run=True) is a candidate, not confirmed."""
    eligible, style = _is_spike_callout_eligible(5.0, horizon_hours=10.0, spike_first_run=True)
    assert eligible
    assert style == "candidate"


def test_spike_prior_run_is_confirmed():
    """A spike that appeared in the prior run (spike_first_run=False) is confirmed."""
    eligible, style = _is_spike_callout_eligible(5.0, horizon_hours=10.0, spike_first_run=False)
    assert eligible
    assert style == "confirmed"


# ── Rec 5: Visual confidence tier tests ──────────────────────────────────────

def test_chart_with_forecast_run_at_renders_zones():
    """Chart with forecast_run_at metadata should render with confidence zones."""
    from datetime import datetime, timezone, timedelta

    NEM_TZ = timezone(timedelta(hours=10))
    run_at = datetime(2026, 5, 15, 7, 30, tzinfo=NEM_TZ)
    run_at_str = run_at.isoformat()

    data = []
    # 336 intervals = 7 days of 30-min intervals
    for i in range(336):
        t = run_at + timedelta(minutes=30 * i)
        h = i * 0.5
        data.append(_make_interval(
            nemtime=t.isoformat(),
            raw_value=0.08 + (i % 20) * 0.003,
            calibrated=0.07 + (i % 20) * 0.002,
            p10=0.05,
            p90=0.12,
            horizon_hours=h,
            forecast_run_at=run_at_str,
        ))

    png = render_forecast_chart(data, region="QLD1")
    assert isinstance(png, bytes)
    assert len(png) > 1000
    assert png[:4] == b'\x89PNG'


def test_zone_boundaries_split_data_correctly():
    """Verify that zone_a/zone_b/zone_c partition covers all forecast data."""
    from datetime import datetime, timezone, timedelta
    import numpy as np

    NEM_TZ = timezone(timedelta(hours=10))
    run_at = datetime(2026, 5, 15, 7, 30, tzinfo=NEM_TZ)
    zone_24h = run_at + timedelta(hours=24)
    zone_72h = run_at + timedelta(hours=72)

    # Simulate 336 intervals (7 days)
    times = [run_at + timedelta(minutes=30 * i) for i in range(336)]
    zone_a = [t < zone_24h for t in times]
    zone_b = [zone_24h <= t < zone_72h for t in times]
    zone_c = [t >= zone_72h for t in times]

    # Every interval must be in exactly one zone
    for i in range(len(times)):
        zones = [zone_a[i], zone_b[i], zone_c[i]]
        assert sum(zones) == 1, f"Interval {i} (h={i*0.5}) in {sum(zones)} zones"

    # Check boundary counts
    assert sum(zone_a) == 48   # 24h / 0.5h = 48 intervals
    assert sum(zone_b) == 96   # (72-24)h / 0.5h = 96 intervals
    assert sum(zone_c) == 192  # (168-72)h / 0.5h = 192 intervals


def test_chart_renders_passthrough_high_with_horizon_gating():
    """passthrough_high intervals beyond 48h should not produce spike callouts."""
    from datetime import datetime, timezone, timedelta

    NEM_TZ = timezone(timedelta(hours=10))
    run_at = datetime(2026, 5, 15, 7, 30, tzinfo=NEM_TZ)
    run_at_str = run_at.isoformat()

    # Build normal forecast + a passthrough_high at 72h horizon
    data = _make_forecast(10, forecast_run_at=run_at_str)
    data.append(_make_interval(
        nemtime=(run_at + timedelta(hours=72)).isoformat(),
        raw_value=8.99,
        calibrated=8.99,
        p10=7.50,
        p90=10.00,
        calibrated_source="passthrough_high",
        horizon_hours=72.0,
        forecast_run_at=run_at_str,
        spike_first_run=False,
    ))

    # Should render without crash — the 72h spike won't produce a callout
    result = render_forecast_chart(data, "QLD1")
    assert isinstance(result, bytes)
    assert len(result) > 0


# ── Rec 2: Covariate gate tests (unit-level) ────────────────────────────────

def test_covariate_constants_exist():
    """Verify spike covariate constants are defined in const.py."""
    from custom_components.nem_pd7day.const import (
        SPIKE_GAS_THRESHOLD_TJ,
        SPIKE_QNI_THRESHOLD_MW,
        SPIKE_COVARIATE_BYPASS_HORIZON_H,
        SPIKE_COVARIATE_CAP,
        SPIKE_COVARIATE_RAW_FLOOR,
    )
    assert SPIKE_GAS_THRESHOLD_TJ == 150.0
    assert SPIKE_QNI_THRESHOLD_MW == -300.0
    assert SPIKE_COVARIATE_BYPASS_HORIZON_H == 12.0
    assert SPIKE_COVARIATE_CAP == 0.50
    assert SPIKE_COVARIATE_RAW_FLOOR == 1.00


# ── Placeholder PNG / matplotlib-missing fallback ─────────────────────────────

def test_placeholder_png_is_valid_png():
    """_placeholder_png() returns bytes with a valid PNG signature."""
    data = _placeholder_png()
    assert isinstance(data, bytes)
    assert data[:8] == b'\x89PNG\r\n\x1a\n'


def test_render_forecast_chart_no_matplotlib():
    """render_forecast_chart falls back to a placeholder PNG when matplotlib is missing."""
    _real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _mock_import(name, *args, **kwargs):
        if name == "matplotlib" or name.startswith("matplotlib."):
            raise ImportError(f"No module named '{name}'")
        return _real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_mock_import):
        result = render_forecast_chart([], "QLD1")
    assert isinstance(result, bytes)
    assert result[:4] == b'\x89PNG'


# ── Font weight ───────────────────────────────────────────────────────────────
#
# The extremes annotations used fontweight='semibold'. matplotlib maps that to
# numeric weight 600, which the bundled DejaVu Sans does not ship, so every
# render that hit the annotation path logged:
#
#     findfont: Failed to find font weight semibold, now using 700.
#
# 700 is 'bold', which is what the rest of the chart already asks for, so the
# rendered image was never affected. Only the log was.

def test_render_emits_no_findfont_warning():
    """Chart rendering must not ask for a font weight the bundled font lacks."""
    import logging

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    # findfont results are memoised per process and the warning is logged only
    # on a cache miss, so an earlier render in the same session would otherwise
    # let a regression through here unnoticed.
    from matplotlib import font_manager

    for name in ("_findfont_cached", "findfont"):
        cached = getattr(font_manager, name, None)
        cache_clear = getattr(cached, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
    fm = getattr(font_manager, "fontManager", None)
    for name in ("_findfont_cached", "findfont"):
        cached = getattr(fm, name, None)
        cache_clear = getattr(cached, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()

    logger = logging.getLogger("matplotlib.font_manager")
    handler = _Capture()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        # 48 intervals is enough to exercise the min and max annotations, which
        # are the two call sites that requested the unsupported weight.
        result = render_forecast_chart(_make_forecast(48), "QLD1")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert result[:4] == b'\x89PNG'
    findfont = [m for m in records if "findfont" in m]
    assert not findfont, f"matplotlib could not resolve a requested font: {findfont}"


@pytest.mark.parametrize(
    "module_name",
    ["forecast_chart", "bias_chart", "iso_chart", "tod_stats"],
)
def test_chart_modules_request_only_supported_font_weights(module_name):
    """Guard every chart module, not just the one that regressed."""
    import ast
    import os

    # DejaVu Sans, which matplotlib bundles and these charts use, ships regular
    # and bold only. Anything else silently falls back and logs a warning.
    supported = {"normal", "regular", "bold"}

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components", "nem_pd7day", f"{module_name}.py",
    )
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in ("fontweight", "weight"):
                continue
            if not isinstance(keyword.value, ast.Constant):
                continue
            value = keyword.value.value
            if isinstance(value, str) and value not in supported:
                offenders.append(f"line {node.lineno}: {keyword.arg}={value!r}")

    assert not offenders, (
        f"{module_name}.py requests unsupported font weights: {offenders}"
    )
