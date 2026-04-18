"""
Integration tests for CalibrationStore — pure Python, no HA dependency.

Covers the bugs found in production:
  - forecast_history keyed by datetime vs str (type mismatch)
  - Duplicate observations from repeated Amber state changes
  - 5-min Amber readings averaged into 30-min trading interval actuals
  - Sanity guard in calibration_engine rejecting corrupt OLS fits
  - Observation accumulator rebuilt correctly after restart

Run with:  python -m pytest tests/test_calibration_store.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone

# ── Module loader (avoids HA import chain) ────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load(name, path, deps=None):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

# Stub out all HA modules so CalibrationStore can be imported without HA installed
_ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", _ha_mock)
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.helpers.event"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.util"] = MagicMock()
sys.modules["homeassistant.util.dt"] = MagicMock()

_store_mod = _load(
    "custom_components.nem_pd7day.calibration_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"),
)

from custom_components.nem_pd7day.nem_time import NEM_TZ, to_nem_iso, current_nem_interval
from custom_components.nem_pd7day.calibration_engine import (
    CalibrationEngine, Observation, MAX_INTERCEPT_ABS, MAX_CALIBRATED_RATIO
)
from custom_components.nem_pd7day.calibration_store import CalibrationStore

# ── Helpers ───────────────────────────────────────────────────────────────────

NEM_TZ = timezone(timedelta(hours=10))

def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")

def make_price_period(nemtime_dt: datetime, value: float = 0.10):
    """Create a minimal PricePeriod-like object with str time fields."""
    start_dt = nemtime_dt - timedelta(minutes=30)
    return MagicMock(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),       # str, not datetime
        value=value,
    )

def make_price_data(run_at_dt: datetime, periods):
    """Create a minimal PD7DayData-like object."""
    return MagicMock(
        forecast_generated_at=nem_iso(run_at_dt),
        forecast=periods,
    )

def make_store() -> CalibrationStore:
    """Create a CalibrationStore with mocked HA storage."""
    hass = MagicMock()
    store = CalibrationStore.__new__(CalibrationStore)
    store._hass = hass
    store._obs_store = AsyncMock()
    store._obs_store.async_load = AsyncMock(return_value=None)
    store._obs_store.async_save = AsyncMock()
    store._coeff_store = AsyncMock()
    store._coeff_store.async_load = AsyncMock(return_value=None)
    store._coeff_store.async_save = AsyncMock()
    store._fh_store = AsyncMock()
    store._fh_store.async_load = AsyncMock(return_value=None)
    store._fh_store.async_save = AsyncMock()
    store._engine = CalibrationEngine()
    store._observations = []
    store._calibration = None
    store._forecast_history = {}
    store._actual_accum = {}
    return store

BASE_DT = datetime(2026, 4, 14, 18, 0, tzinfo=NEM_TZ)  # 18:00 NEM forecast run


# ── Tests: forecast_history key type ──────────────────────────────────────────

def test_forecast_history_keyed_by_str():
    """
    BUG: ingest_forecast() was using period.time (str) directly as dict key,
    but async_record_actual() looked up by ISO string from current_nem_interval().
    Both sides must be str — verify the key type is str not datetime.
    """
    store = make_store()
    run_dt = BASE_DT
    interval_end = BASE_DT + timedelta(hours=3, minutes=30)  # nemtime
    period = make_price_period(interval_end, value=0.108)

    run_async(store.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, [period]),
        interconnectors={},
        case=None,
    ))

    assert len(store._forecast_history) == 1
    key = list(store._forecast_history.keys())[0]
    assert isinstance(key, str), f"Expected str key, got {type(key)}: {key!r}"
    assert key.endswith("+10:00"), f"Key missing +10:00 suffix: {key!r}"
    # Key must be the interval START (period.time), not the nemtime end
    expected_start = nem_iso(interval_end - timedelta(minutes=30))
    assert key == expected_start, f"Key {key!r} != expected {expected_start!r}"


def test_forecast_history_matches_current_nem_interval():
    """
    The forecast_history key must match the output of current_nem_interval()
    for the same point in time — this is what async_record_actual() uses to
    look up the forecast.
    """
    store = make_store()
    # Simulate a forecast for an interval starting at 21:00 NEM
    interval_start_dt = datetime(2026, 4, 14, 21, 0, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.10)

    run_async(store.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(BASE_DT, [period]),
        interconnectors={},
        case=None,
    ))

    # current_nem_interval() at 21:15 should return "2026-04-14T21:00:00+10:00"
    key_from_store = list(store._forecast_history.keys())[0]
    expected_key = nem_iso(interval_start_dt)
    assert key_from_store == expected_key, (
        f"Store key {key_from_store!r} doesn't match current_nem_interval() output {expected_key!r}"
    )


# ── Tests: observation deduplication and averaging ────────────────────────────

import asyncio

def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_first_amber_reading_creates_observation():
    """First Amber reading for an interval must create exactly one observation."""
    store = make_store()
    interval_start_dt = datetime(2026, 4, 14, 21, 0, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.108)

    run_async(store.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(BASE_DT, [period]),
        interconnectors={},
        case=None,
    ))

    interval_iso = nem_iso(interval_start_dt)
    run_async(store.async_record_actual(interval_iso, 0.0956))

    assert len(store._observations) == 1
    assert store._observations[0]["actual_rrp"] == 0.0956
    assert store._observations[0]["pd7day_forecast"] == 0.108


def test_duplicate_amber_readings_averaged_not_duplicated():
    """
    BUG: Amber fires 6 times per 30-min interval (5-min dispatch).
    Each call to async_record_actual must update the running average
    in-place rather than appending new rows.
    After 6 readings the observation count must still be 1.
    """
    store = make_store()
    interval_start_dt = datetime(2026, 4, 14, 21, 0, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.108)

    run_async(store.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(BASE_DT, [period]),
        interconnectors={},
        case=None,
    ))

    interval_iso = nem_iso(interval_start_dt)
    readings = [0.090, 0.092, 0.094, 0.091, 0.093, 0.095]
    for r in readings:
        run_async(store.async_record_actual(interval_iso, r))

    # Must still be exactly one observation row
    assert len(store._observations) == 1, (
        f"Expected 1 observation, got {len(store._observations)} — duplicate rows!"
    )

    # actual_rrp must be the average of all 6 readings
    expected_avg = sum(readings) / len(readings)
    actual = store._observations[0]["actual_rrp"]
    assert abs(actual - expected_avg) < 1e-6, (
        f"Expected avg {expected_avg:.6f}, got {actual:.6f}"
    )


def test_different_intervals_create_separate_observations():
    """Separate 30-min intervals must each get their own observation row."""
    store = make_store()
    run_dt = BASE_DT

    # Two consecutive 30-min intervals
    for i in range(3):
        interval_start_dt = datetime(2026, 4, 14, 21, 0, tzinfo=NEM_TZ) + timedelta(minutes=30*i)
        interval_end_dt = interval_start_dt + timedelta(minutes=30)
        period = make_price_period(interval_end_dt, value=0.10 + i * 0.01)
        run_async(store.ingest_forecast(
            region="QLD1",
            price_data=make_price_data(run_dt, [period]),
            interconnectors={},
            case=None,
        ))

    for i in range(3):
        interval_start_dt = datetime(2026, 4, 14, 21, 0, tzinfo=NEM_TZ) + timedelta(minutes=30*i)
        run_async(store.async_record_actual(nem_iso(interval_start_dt), 0.095 + i * 0.005))

    assert len(store._observations) == 3, (
        f"Expected 3 observations (one per interval), got {len(store._observations)}"
    )


def test_no_match_when_no_forecast_history():
    """
    async_record_actual must return 0 and not create observations
    when no forecast history exists for the interval.
    """
    store = make_store()
    result = run_async(store.async_record_actual("2026-04-14T21:00:00+10:00", 0.095))
    assert result == 0
    assert len(store._observations) == 0


def test_accumulator_rebuilt_from_loaded_observations():
    """
    When observations are loaded from storage, _actual_accum must be rebuilt
    so that subsequent Amber readings for already-logged intervals are averaged
    correctly rather than creating duplicate rows.
    """
    store = make_store()

    # Simulate loading a persisted observation
    stored_obs = {
        "interval_time": "2026-04-14T21:00:00+10:00",
        "horizon_hours": 3.0,
        "pd7day_forecast": 0.108,
        "actual_rrp": 0.092,
        "forecast_run_at": "2026-04-14T18:00:00+10:00",
        "hour_of_day": 21,
        "day_of_week": 1,
        "month": 4,
        "gas_forecast_tj": None,
        "qni_mwflow": None,
        "qni_violation_degree": None,
        "is_intervention": False,
    }
    store._observations = [stored_obs]
    store._actual_accum = {
        ("2026-04-14T21:00:00+10:00", "2026-04-14T18:00:00+10:00"): {
            "sum": 0.092,
            "count": 1,
            "obs_idx": 0,
        }
    }
    # Also rebuild forecast history so the lookup succeeds
    store._forecast_history["2026-04-14T21:00:00+10:00"] = [{
        "run_at": "2026-04-14T18:00:00+10:00",
        "forecast_price": 0.108,
        "gas_tj": None,
        "qni_mwflow": None,
        "qni_violation": None,
        "is_intervention": False,
        "region": "QLD1",
    }]

    # New Amber reading arrives for the same interval post-restart
    run_async(store.async_record_actual("2026-04-14T21:00:00+10:00", 0.100))

    # Must still be one row, with updated average
    assert len(store._observations) == 1, "Restart created a duplicate observation row"
    expected_avg = (0.092 + 0.100) / 2
    actual = store._observations[0]["actual_rrp"]
    assert abs(actual - expected_avg) < 1e-6, (
        f"Expected avg {expected_avg:.6f} after restart, got {actual:.6f}"
    )


# ── Tests: sanity guard in calibration engine ─────────────────────────────────

def _make_obs(forecast, actual, horizon=3.0, hour=21):
    return Observation(
        interval_time="2026-04-14T21:00:00+10:00",
        horizon_hours=horizon,
        pd7day_forecast=forecast,
        actual_rrp=actual,
        forecast_run_at="2026-04-14T18:00:00+10:00",
        hour_of_day=hour,
        day_of_week=1,
        month=4,
        gas_forecast_tj=None,
        qni_mwflow=None,
        qni_violation_degree=None,
        is_intervention=False,
    )


def test_sanity_guard_rejects_large_intercept():
    """
    BUG: Duplicate observations caused OLS intercepts of -3.15 and +75.
    The sanity guard must fall back to passthrough when |intercept| > MAX_INTERCEPT_ABS (1.0).
    Simulate the real corrupt case: tiny near-identical forecasts, large negative actuals.
    """
    import random
    rng = random.Random(42)
    # Near-constant forecast (~0.003), actual = -3.15 → OLS gives b ≈ -3.15
    # This replicates the h00_06__offpeak bucket that had b=-3.145
    obs = [
        _make_obs(forecast=rng.uniform(0.001, 0.005), actual=-3.15 + rng.gauss(0, 0.01))
        for _ in range(30)
    ]
    engine = CalibrationEngine()
    result = engine.fit(obs)
    out = result.apply(0.003, horizon_hours=3.0, hour_of_day=21)

    # With b ≈ -3.15 exceeding MAX_INTERCEPT_ABS=1.0, must fall back to passthrough
    assert out["calibrated_source"] in ("passthrough", "passthrough_sanity"), (
        f"Expected passthrough for corrupt bucket, got {out['calibrated_source']} "
        f"with calibrated={out['calibrated']:.4f}"
    )


def test_sanity_guard_rejects_large_ratio():
    """
    If calibrated/raw ratio exceeds MAX_CALIBRATED_RATIO, fall back to passthrough.
    Simulate with observations that produce a valid intercept but wildly wrong ratio
    for small raw values: slope=50, intercept=0.05 → at raw=0.10, calibrated=5.05 (50x).
    """
    import random
    rng = random.Random(7)
    # High slope, moderate intercept — intercept passes guard but ratio fails for large x
    # actual = 50 * forecast + 0.05 → at forecast=0.10, calibrated=5.05, ratio=50.5x
    obs = [
        _make_obs(
            forecast=rng.uniform(0.08, 0.12),
            actual=50 * rng.uniform(0.08, 0.12) + 0.05 + rng.gauss(0, 0.01),
            horizon=3.0, hour=21
        )
        for _ in range(40)
    ]
    engine = CalibrationEngine()
    result = engine.fit(obs)
    # At raw=0.10, calibrated ≈ 5.05, ratio ≈ 50x >> MAX_CALIBRATED_RATIO=5
    out = result.apply(0.10, horizon_hours=3.0, hour_of_day=21)
    assert out["calibrated_source"] in ("passthrough", "passthrough_sanity"), (
        f"Expected passthrough_sanity for large ratio, got {out['calibrated_source']} "
        f"calibrated={out['calibrated']:.4f} vs raw=0.10"
    )
    assert abs(out["calibrated"] - 0.10) < 1e-9, "Passthrough must return raw value unchanged"


def test_sanity_guard_passes_normal_values():
    """Normal OLS output within plausible range must NOT be caught by the guard."""
    import random
    rng = random.Random(1)
    obs = [
        _make_obs(
            forecast=rng.uniform(0.05, 0.25),
            actual=rng.uniform(0.06, 0.28),
            horizon=3.0, hour=21
        )
        for _ in range(40)
    ]
    engine = CalibrationEngine()
    result = engine.fit(obs)
    out = result.apply(0.10, horizon_hours=3.0, hour_of_day=21)
    # With normal data the guard must not interfere
    assert out["calibrated_source"] == "ols", (
        f"Sanity guard incorrectly rejected a valid bucket: {out}"
    )


# ── Tests: horizon calculation ────────────────────────────────────────────────

def test_horizon_hours_calculated_from_nemtime():
    """
    horizon_hours = interval_time(start) - run_at.
    With run_at=18:00 and interval_start=21:00, horizon must be 3.0h.
    """
    store = make_store()
    run_dt = datetime(2026, 4, 14, 18, 0, tzinfo=NEM_TZ)
    interval_end_dt = datetime(2026, 4, 14, 21, 30, tzinfo=NEM_TZ)  # nemtime
    interval_start_dt = interval_end_dt - timedelta(minutes=30)      # time = 21:00

    period = make_price_period(interval_end_dt, value=0.108)
    run_async(store.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, [period]),
        interconnectors={},
        case=None,
    ))

    run_async(store.async_record_actual(nem_iso(interval_start_dt), 0.095))

    assert len(store._observations) == 1
    obs = store._observations[0]
    assert abs(obs["horizon_hours"] - 3.0) < 0.01, (
        f"Expected horizon 3.0h, got {obs['horizon_hours']}"
    )


def test_negative_horizon_skipped():
    """Forecasts for intervals in the past (horizon < 0) must not be logged."""
    store = make_store()
    run_dt = datetime(2026, 4, 14, 21, 0, tzinfo=NEM_TZ)
    # Interval START is before run_at — negative horizon
    interval_end_dt = datetime(2026, 4, 14, 20, 30, tzinfo=NEM_TZ)
    interval_start_dt = interval_end_dt - timedelta(minutes=30)  # 20:00, before run_at 21:00

    period = make_price_period(interval_end_dt, value=0.10)
    run_async(store.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, [period]),
        interconnectors={},
        case=None,
    ))

    run_async(store.async_record_actual(nem_iso(interval_start_dt), 0.09))
    assert len(store._observations) == 0, (
        f"Negative horizon observation must be skipped, got {len(store._observations)}"
    )


# ── Tests: multiple forecast runs per interval ────────────────────────────────

def test_multiple_forecast_runs_create_multiple_observations():
    """
    A single interval may be covered by multiple AEMO forecast runs
    (e.g. the 13:00 and 18:00 publishes both forecast tomorrow 06:00).
    Each (interval, forecast_run) pair must produce a separate observation.
    """
    store = make_store()
    interval_end_dt = datetime(2026, 4, 15, 6, 30, tzinfo=NEM_TZ)
    interval_start_dt = interval_end_dt - timedelta(minutes=30)

    run1_dt = datetime(2026, 4, 14, 13, 0, tzinfo=NEM_TZ)  # 13:00 publish
    run2_dt = datetime(2026, 4, 14, 18, 0, tzinfo=NEM_TZ)  # 18:00 publish

    for run_dt in [run1_dt, run2_dt]:
        period = make_price_period(interval_end_dt, value=0.118)
        run_async(store.ingest_forecast(
            region="QLD1",
            price_data=make_price_data(run_dt, [period]),
            interconnectors={},
            case=None,
        ))

    run_async(store.async_record_actual(nem_iso(interval_start_dt), 0.095))

    # Two forecast runs → two observations for the same interval
    assert len(store._observations) == 2, (
        f"Expected 2 observations (one per forecast run), got {len(store._observations)}"
    )
    horizons = sorted(o["horizon_hours"] for o in store._observations)
    assert horizons[0] < horizons[1], "Second run should have shorter horizon"


# ── Tests: ingest deduplication (v1.8.0 regression) ─────────────────────────

def test_reingest_same_run_at_does_not_duplicate_forecast_history():
    """
    BUG (v1.8.0): ingest_forecast called twice with the same run_at (e.g. HA
    restart + refetch of same AEMO file) appended duplicate entries to
    _forecast_history.  Each Amber reading then iterated all duplicates and
    called async_record_actual update path multiple times, corrupting the
    running average by counting each Amber sample N times instead of once.

    After dedup fix: second ingest of same run_at must be silently ignored.
    """
    store = make_store()
    run_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)  # 07:30 NEM publish
    interval_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period = make_price_period(interval_end_dt, value=0.110)
    price_data = make_price_data(run_dt, [period])

    # Ingest the same forecast twice (restart scenario)
    run_async(store.ingest_forecast("QLD1", price_data, {}, None))
    run_async(store.ingest_forecast("QLD1", price_data, {}, None))  # same run_at

    # History must have exactly one entry per interval key
    key = nem_iso(interval_end_dt - timedelta(minutes=30))
    assert key in store._forecast_history
    assert len(store._forecast_history[key]) == 1, (
        f"Expected 1 history entry after dedup, got {len(store._forecast_history[key])}"
    )

    # Record an Amber reading — must produce exactly one observation
    run_async(store.async_record_actual(key, 0.095))
    assert len(store._observations) == 1, (
        f"Duplicate forecast history caused {len(store._observations)} obs (expected 1)"
    )


def test_reingest_different_run_at_adds_new_entry():
    """
    Two genuine AEMO publish runs (different run_at timestamps) covering the
    same interval must both be stored — they produce distinct observations
    with different horizons.
    """
    store = make_store()
    interval_end_dt = datetime(2026, 4, 15, 18, 0, tzinfo=NEM_TZ)
    interval_start_str = nem_iso(interval_end_dt - timedelta(minutes=30))

    run1_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    run2_dt = datetime(2026, 4, 15, 13, 0, tzinfo=NEM_TZ)

    for run_dt in [run1_dt, run2_dt]:
        period = make_price_period(interval_end_dt, value=0.110)
        run_async(store.ingest_forecast("QLD1", make_price_data(run_dt, [period]), {}, None))

    assert len(store._forecast_history[interval_start_str]) == 2, (
        "Two distinct run_at timestamps must produce two history entries"
    )

    run_async(store.async_record_actual(interval_start_str, 0.095))
    assert len(store._observations) == 2, (
        "Two forecast runs covering one interval must produce two observations"
    )


def test_horizon_uses_interval_start_not_nemtime():
    """
    BUG (v1.8.0): sensor._calibrate_period used period.nemtime (interval END)
    for horizon calculation, but calibration_store.async_record_actual used
    period.time (interval START).  The horizon stored in observations was thus
    30 minutes shorter than the horizon used for bucket lookup — causing
    misrouting near bucket boundaries.

    This test verifies the store uses interval START (period.time) for horizon.
    With run_at=07:30 and interval_start=14:00, horizon must be 6.5h → h06_12.
    If nemtime (14:30) were used, horizon=7.0h → still h06_12 in this case,
    so we use a boundary case: run_at=07:30, interval_start=13:30 (horizon=6.0h
    → h06_12), nemtime=14:00 (horizon=6.5h → also h06_12).
    Use a case that crosses the 6h boundary: run_at=08:00, interval_start=14:00
    (horizon=6.0h exactly, on the boundary between h00_06 and h06_12).
    """
    store = make_store()
    # run_at = 08:00 NEM; interval START = 14:00 NEM → horizon = 6.0h exactly
    # The bucket boundary is at 6h: horizon < 6 → h00_06, horizon >= 6 → h06_12.
    run_dt = datetime(2026, 4, 15, 8, 0, tzinfo=NEM_TZ)
    interval_start_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)  # 14:30 NEM

    period = make_price_period(interval_end_dt, value=0.12)
    run_async(store.ingest_forecast("QLD1", make_price_data(run_dt, [period]), {}, None))

    run_async(store.async_record_actual(nem_iso(interval_start_dt), 0.095))

    assert len(store._observations) == 1
    obs = store._observations[0]
    # Horizon from interval START: (14:00 - 08:00) = 6.0h
    assert abs(obs["horizon_hours"] - 6.0) < 0.01, (
        f"Expected horizon 6.0h (using interval START), got {obs['horizon_hours']}h. "
        f"If 6.5h, the store is incorrectly using nemtime (interval END)."
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
