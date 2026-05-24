"""Tests for iso_chart module and isotonic summary fields."""
from __future__ import annotations

from unittest.mock import patch


def test_iso_chart_renders_png():
    """iso_chart.render_iso_chart returns valid PNG bytes."""
    from custom_components.nem_pd7day.iso_chart import render_iso_chart
    from custom_components.nem_pd7day.calibration_engine import (
        CalibrationEngine, Observation,
    )
    from datetime import datetime, timedelta

    # Use recent dates so observations fall within the 90-day rolling window
    base = datetime.now().astimezone()
    obs = []
    for i in range(30):
        x = 0.05 + i * 0.005
        y = x * 0.75 + 0.01
        dt = base - timedelta(days=i)
        obs.append(Observation(
            interval_time=dt.isoformat(),
            horizon_hours=4.0,
            pd7day_forecast=x, actual_rrp=y,
            forecast_run_at=(dt - timedelta(hours=4)).isoformat(),
            hour_of_day=14, day_of_week=0, month=1,
            gas_forecast_tj=None, qni_mwflow=None,
            qni_violation_degree=None, is_intervention=False,
        ))

    engine = CalibrationEngine()
    result = engine.fit(obs, region="QLD1")
    png = render_iso_chart(result, iso_history=[], obs_count=30, region="QLD1")
    assert isinstance(png, bytes)
    assert len(png) > 1000
    assert png[:4] == b'\x89PNG'


def test_summary_isotonic_fields():
    """summary() emits isotonic diagnostic fields per bucket."""
    from custom_components.nem_pd7day.calibration_engine import CalibrationEngine, Observation
    from datetime import datetime, timedelta

    # Use recent dates so observations fall within the 90-day rolling window
    base = datetime.now().astimezone()
    obs = []
    for i in range(30):
        x = 0.05 + i * 0.005
        y = x * 0.75
        dt = base - timedelta(days=i)
        obs.append(Observation(
            interval_time=dt.isoformat(),
            horizon_hours=4.0,
            pd7day_forecast=x, actual_rrp=y,
            forecast_run_at=(dt - timedelta(hours=4)).isoformat(),
            hour_of_day=14, day_of_week=0, month=1,
            gas_forecast_tj=None, qni_mwflow=None,
            qni_violation_degree=None, is_intervention=False,
        ))
    engine = CalibrationEngine()
    result = engine.fit(obs, region="QLD1")
    s = result.summary()
    # Find a bucket with data
    fitted = {k: v for k, v in s["buckets"].items() if v["n"] >= 20}
    assert fitted, "Expected at least one fitted bucket"
    bucket = next(iter(fitted.values()))
    for field in ("n", "ols_a", "iso_n_steps", "x_min", "x_max",
                  "compression_ratio", "iso_mae", "spot_010", "spot_020",
                  "q10_a", "q90_a"):
        assert field in bucket, f"Missing field: {field}"
    assert bucket["compression_ratio"] is not None
    assert 0.0 < bucket["compression_ratio"] < 2.0
    assert bucket["spot_010"] is not None
    assert bucket["spot_020"] is not None


# ── Placeholder PNG / matplotlib-missing fallback ─────────────────────────────

def test_iso_chart_placeholder_png_is_valid_png():
    """_placeholder_png() returns bytes with a valid PNG signature."""
    from custom_components.nem_pd7day.iso_chart import _placeholder_png

    data = _placeholder_png()
    assert isinstance(data, bytes)
    assert data[:8] == b'\x89PNG\r\n\x1a\n'


def test_iso_chart_returns_placeholder_when_matplotlib_missing():
    """render_iso_chart falls back to a placeholder PNG when matplotlib is missing."""
    from custom_components.nem_pd7day.iso_chart import render_iso_chart
    from custom_components.nem_pd7day.calibration_engine import (
        CalibrationEngine, Observation,
    )
    from datetime import datetime, timedelta

    base = datetime.now().astimezone()
    obs = []
    for i in range(30):
        x = 0.05 + i * 0.005
        y = x * 0.75 + 0.01
        dt = base - timedelta(days=i)
        obs.append(Observation(
            interval_time=dt.isoformat(),
            horizon_hours=4.0,
            pd7day_forecast=x, actual_rrp=y,
            forecast_run_at=(dt - timedelta(hours=4)).isoformat(),
            hour_of_day=14, day_of_week=0, month=1,
            gas_forecast_tj=None, qni_mwflow=None,
            qni_violation_degree=None, is_intervention=False,
        ))

    engine = CalibrationEngine()
    result = engine.fit(obs, region="QLD1")

    _real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

    def _mock_import(name, *args, **kwargs):
        if name == "matplotlib" or name.startswith("matplotlib."):
            raise ImportError(f"No module named '{name}'")
        return _real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_mock_import):
        png = render_iso_chart(result, iso_history=[], obs_count=30, region="QLD1")
    assert isinstance(png, bytes)
    assert png[:4] == b'\x89PNG'
