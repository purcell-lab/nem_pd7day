"""
Unit tests for CalibrationEngine — pure Python, no HA dependency.

Run with:  python -m pytest tests/test_calibration_engine.py -v
or simply: python tests/test_calibration_engine.py
"""
from __future__ import annotations

import math
import sys
import os
import random

# Allow running from repo root without installing the package.
# Import the engine module directly to avoid loading the HA-dependent __init__.py.
import importlib.util

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# Load const first, then nem_time (no HA deps), then calibration_engine.
# Loading const before nem_time prevents the relative import in nem_time
# from triggering the full package __init__.py (which needs HA).
_const = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

from custom_components.nem_pd7day.calibration_engine import (
    DECAY_LAMBDA,
    MIN_OBS,
    OBSERVATION_WINDOW_DAYS,
    NEGATIVE_PASSTHROUGH_THRESHOLD,
    REGION_COORDS,
    SPIKE_THRESHOLD,
    BucketModel,
    CalibrationEngine,
    LinearCoeff,
    Observation,
    QuantileCoeff,
    _bucket_key,
    _bucket_key_solar,
    _horizon_label,
    _tod_label,
    _tod_label_solar,
    _ols,
    _ols_metrics,
    _quantile_regression,
    all_bucket_keys,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_obs(
    forecast: float,
    actual: float,
    horizon_hours: float = 12.0,
    hour_of_day: int = 14,
    is_intervention: bool = False,
) -> Observation:
    # Build interval_time that matches hour_of_day so solar classification is consistent
    interval_time = f"2026-04-13T{hour_of_day:02d}:00:00+10:00"
    return Observation(
        interval_time=interval_time,
        horizon_hours=horizon_hours,
        pd7day_forecast=forecast,
        actual_rrp=actual,
        forecast_run_at="2026-04-12T03:30:00+10:00",
        hour_of_day=hour_of_day,
        day_of_week=0,
        month=4,
        gas_forecast_tj=75.0,
        qni_mwflow=-150.0,
        qni_violation_degree=0.0,
        is_intervention=is_intervention,
    )


def _pairs(n: int, a: float, b: float, noise: float = 0.0, seed: int = 42) -> list[tuple[float, float]]:
    """Generate n (x, y) pairs where y = a*x + b + noise."""
    rng = random.Random(seed)
    xs = [rng.uniform(0.05, 0.30) for _ in range(n)]
    pairs = [(x, a * x + b + rng.gauss(0, noise)) for x in xs]
    return pairs


# ── Bucket routing tests ──────────────────────────────────────────────────────

def test_horizon_labels():
    assert _horizon_label(0) == "h00_06"
    assert _horizon_label(5.9) == "h00_06"
    assert _horizon_label(6.0) == "h06_12"
    assert _horizon_label(11.9) == "h06_12"
    assert _horizon_label(12.0) == "h12_24"
    assert _horizon_label(23.9) == "h12_24"
    assert _horizon_label(24.0) == "h24_48"
    assert _horizon_label(47.9) == "h24_48"
    assert _horizon_label(48.0) == "h48_96"
    assert _horizon_label(95.9) == "h48_96"
    assert _horizon_label(96.0) == "h96plus"
    assert _horizon_label(200.0) == "h96plus"
    print("  PASS: horizon labels")


def test_tod_labels():
    # With the 3-label system: peak (16-21), solar (10-16), shoulder (everything else)
    assert _tod_label(0) == "shoulder"
    assert _tod_label(6) == "shoulder"
    assert _tod_label(10) == "solar"
    assert _tod_label(15) == "solar"
    assert _tod_label(16) == "peak"
    assert _tod_label(19) == "peak"
    assert _tod_label(20) == "peak"
    assert _tod_label(21) == "shoulder"
    assert _tod_label(22) == "shoulder"
    assert _tod_label(23) == "shoulder"
    print("  PASS: time-of-day labels")


def test_all_bucket_keys():
    keys = all_bucket_keys()
    assert len(keys) == 24   # 6 horizons × 4 tod buckets
    assert "h00_06__peak" in keys
    assert "h96plus__shoulder" in keys
    assert "h12_24__solar" in keys
    assert "h06_12__morning_ramp" in keys
    # offpeak no longer exists
    assert not any("offpeak" in k for k in keys)
    print(f"  PASS: all_bucket_keys — {len(keys)} keys")


# ── OLS tests ─────────────────────────────────────────────────────────────────

def test_ols_perfect_fit():
    """OLS should recover exact coefficients from noise-free data."""
    pairs = _pairs(50, a=1.8, b=0.02, noise=0.0)
    a, b = _ols(pairs)
    assert abs(a - 1.8) < 1e-6, f"a={a}"
    assert abs(b - 0.02) < 1e-6, f"b={b}"
    print("  PASS: OLS perfect fit (a=1.8, b=0.02)")


def test_ols_noisy_fit():
    """OLS should recover approximate coefficients from noisy data."""
    pairs = _pairs(200, a=2.1, b=0.03, noise=0.01, seed=7)
    a, b = _ols(pairs)
    assert abs(a - 2.1) < 0.05, f"a={a} too far from 2.1"
    assert abs(b - 0.03) < 0.02, f"b={b} too far from 0.03"
    print(f"  PASS: OLS noisy fit (a≈{a:.4f}, b≈{b:.5f})")


def test_ols_passthrough_insufficient_data():
    """OLS should return (1,0) passthrough when n < MIN_OBS."""
    pairs = _pairs(MIN_OBS - 1, a=2.0, b=0.1)
    a, b = _ols(pairs)
    assert a == 1.0 and b == 0.0
    print(f"  PASS: OLS passthrough with n={MIN_OBS - 1} (< {MIN_OBS})")


def test_ols_metrics():
    """MAE and RMSE should be zero for a perfect fit."""
    pairs = _pairs(50, a=1.5, b=0.01, noise=0.0)
    a, b = _ols(pairs)
    mae, rmse = _ols_metrics(pairs, a, b)
    assert mae < 1e-8, f"mae={mae}"
    assert rmse < 1e-8, f"rmse={rmse}"
    print(f"  PASS: OLS metrics (MAE={mae:.2e}, RMSE={rmse:.2e})")


def test_ols_positive_intercept():
    """OLS with systematic bias — b should be positive."""
    pairs = _pairs(100, a=1.0, b=0.05, noise=0.002)
    a, b = _ols(pairs)
    assert b > 0.03, f"b={b} should be positive"
    print(f"  PASS: OLS positive intercept (b≈{b:.4f})")


# ── Quantile regression tests ─────────────────────────────────────────────────

def test_quantile_regression_median():
    """
    For symmetric noise, P50 should approximate OLS.
    """
    pairs = _pairs(200, a=1.8, b=0.02, noise=0.01, seed=1)
    a_ols, b_ols = _ols(pairs)
    a_q50, b_q50, pl = _quantile_regression(pairs, 0.5)
    assert abs(a_q50 - a_ols) < 0.1, f"P50 a={a_q50} vs OLS a={a_ols}"
    assert pl < 0.02, f"pinball_loss={pl} unexpectedly high"
    print(f"  PASS: Q50 ≈ OLS (a={a_q50:.4f} vs {a_ols:.4f}, PL={pl:.5f})")


def test_quantile_regression_ordering():
    """
    P10 predictions should always be ≤ P50 ≤ P90 for positive x.
    """
    pairs = _pairs(100, a=2.0, b=0.01, noise=0.03, seed=3)
    a10, b10, _ = _quantile_regression(pairs, 0.1)
    a50, b50, _ = _quantile_regression(pairs, 0.5)
    a90, b90, _ = _quantile_regression(pairs, 0.9)
    for x in [0.05, 0.10, 0.15, 0.20, 0.25]:
        p10 = a10 * x + b10
        p50 = a50 * x + b50
        p90 = a90 * x + b90
        assert p10 <= p50 + 0.001, f"x={x}: P10={p10:.4f} > P50={p50:.4f}"
        assert p50 <= p90 + 0.001, f"x={x}: P50={p50:.4f} > P90={p90:.4f}"
    print("  PASS: quantile ordering P10 ≤ P50 ≤ P90")


def test_quantile_regression_asymmetric_noise():
    """
    With right-skewed noise (like electricity prices), P90 should be
    significantly higher than P50 and OLS.
    """
    rng = random.Random(42)
    pairs = []
    for _ in range(300):
        x = rng.uniform(0.05, 0.20)
        # Right-skewed: occasionally very high actual (spike simulation)
        noise = rng.expovariate(10) * 0.5 if rng.random() < 0.15 else rng.gauss(0, 0.005)
        pairs.append((x, 1.5 * x + 0.01 + noise))

    a90, b90, _ = _quantile_regression(pairs, 0.9)
    a10, b10, _ = _quantile_regression(pairs, 0.1)
    a50, b50, _ = _quantile_regression(pairs, 0.5)

    # At x=0.15, P90 should be meaningfully higher than P10
    x = 0.15
    spread = (a90 * x + b90) - (a10 * x + b10)
    assert spread > 0.01, f"spread={spread:.4f} — quantile bands too narrow for spikey data"
    print(f"  PASS: asymmetric noise — P90-P10 spread at x=0.15 = {spread:.4f}")


def test_quantile_passthrough():
    """Quantile regression should return (1,0) passthrough with insufficient data."""
    pairs = _pairs(MIN_OBS - 1, a=2.0, b=0.01)
    a, b, pl = _quantile_regression(pairs, 0.9)
    assert a == 1.0 and b == 0.0
    assert math.isinf(pl)
    print(f"  PASS: quantile passthrough with n={MIN_OBS - 1}")


# ── Engine integration tests ──────────────────────────────────────────────────

def _make_obs_batch(
    n: int,
    a: float,
    b: float,
    horizon_hours: float,
    hour_of_day: int,
    noise: float = 0.01,
    seed: int = 99,
) -> list[Observation]:
    rng = random.Random(seed)
    obs = []
    for i in range(n):
        fc = rng.uniform(0.05, 0.25)
        actual = a * fc + b + rng.gauss(0, noise)
        obs.append(make_obs(fc, actual, horizon_hours=horizon_hours, hour_of_day=hour_of_day))
    return obs


def test_engine_fit_applies_correctly():
    """
    Fit an engine on synthetic data and verify apply() returns
    calibrated values closer to actuals than raw forecast.
    """
    engine = CalibrationEngine()

    # Generate observations for h12_24 / solar bucket (hour=12)
    observations = _make_obs_batch(
        n=80, a=2.2, b=0.025, horizon_hours=18.0, hour_of_day=12, noise=0.005
    )

    result = engine.fit(observations)

    # Test a midrange forecast value
    test_forecast = 0.10
    true_actual = 2.2 * test_forecast + 0.025   # ≈ 0.245

    calibrated = result.apply(test_forecast, horizon_hours=18.0, hour_of_day=12)

    assert calibrated["calibrated_source"] == "ols", "Expected OLS calibration"
    assert calibrated["p10"] is not None, "Expected P10"
    assert calibrated["p90"] is not None, "Expected P90"

    raw_error = abs(test_forecast - true_actual)
    cal_error = abs(calibrated["calibrated"] - true_actual)
    assert cal_error < raw_error, (
        f"Calibrated error {cal_error:.4f} should be less than raw error {raw_error:.4f}"
    )
    assert calibrated["p10"] < calibrated["p90"]

    print(
        f"  PASS: engine fit/apply — raw_err={raw_error:.4f} cal_err={cal_error:.4f} "
        f"P10={calibrated['p10']:.4f} P90={calibrated['p90']:.4f}"
    )


def test_engine_intervention_skipped():
    """Observations with is_intervention=True should be excluded from fitting."""
    engine = CalibrationEngine()

    # All intervention observations — should produce passthrough
    obs = [
        make_obs(0.10, 0.30, is_intervention=True)
        for _ in range(50)
    ]
    result = engine.fit(obs)
    out = result.apply(0.10, horizon_hours=12.0, hour_of_day=14)
    assert out["calibrated_source"] == "passthrough"
    assert out["calibrated"] == 0.10
    print("  PASS: intervention observations excluded from calibration")


def test_engine_passthrough_below_min_obs():
    """Buckets with < MIN_OBS observations should return passthrough."""
    engine = CalibrationEngine()
    obs = _make_obs_batch(n=MIN_OBS - 1, a=2.5, b=0.05, horizon_hours=12.0, hour_of_day=12)
    result = engine.fit(obs)
    out = result.apply(0.10, horizon_hours=12.0, hour_of_day=12)
    assert out["calibrated_source"] == "passthrough"
    assert out["n_obs"] == MIN_OBS - 1
    print(f"  PASS: passthrough with n={MIN_OBS - 1} (< {MIN_OBS})")


def test_engine_serialisation_roundtrip():
    """to_storage / from_storage should produce identical apply() results."""
    engine = CalibrationEngine()
    observations = _make_obs_batch(
        n=50, a=1.9, b=0.03, horizon_hours=8.0, hour_of_day=17
    )
    result = engine.fit(observations)

    storage = engine.to_storage(result)
    restored = engine.from_storage(storage)

    test_price = 0.12
    orig = result.apply(test_price, horizon_hours=8.0, hour_of_day=17)
    rest = restored.apply(test_price, horizon_hours=8.0, hour_of_day=17)

    assert abs(orig["calibrated"] - rest["calibrated"]) < 1e-9
    assert orig["calibrated_source"] == rest["calibrated_source"]
    if orig["p10"] is not None:
        assert abs(orig["p10"] - rest["p10"]) < 1e-9
    print("  PASS: serialisation roundtrip")


def test_engine_multi_bucket_independence():
    """
    Fitting different true relationships in different buckets should
    produce independent models per bucket.
    """
    engine = CalibrationEngine()

    # h12_24 / solar: a=2.5
    obs_solar = _make_obs_batch(
        n=60, a=2.5, b=0.01, horizon_hours=18.0, hour_of_day=12
    )
    # h12_24 / peak: a=3.5
    obs_peak = _make_obs_batch(
        n=60, a=3.5, b=0.02, horizon_hours=18.0, hour_of_day=17
    )
    result = engine.fit(obs_solar + obs_peak)

    x = 0.10
    solar_cal = result.apply(x, horizon_hours=18.0, hour_of_day=12)
    peak_cal = result.apply(x, horizon_hours=18.0, hour_of_day=17)

    assert solar_cal["calibrated"] < peak_cal["calibrated"], (
        f"Solar ({solar_cal['calibrated']:.4f}) should be < peak ({peak_cal['calibrated']:.4f})"
    )
    print(
        f"  PASS: multi-bucket independence — "
        f"solar={solar_cal['calibrated']:.4f} peak={peak_cal['calibrated']:.4f} at x={x}"
    )


# ── Bug-fix regression tests ─────────────────────────────────────────────────

def test_negative_ols_slope_clamped():
    """
    When OLS produces a negative slope (a < 0), the engine must clamp it to 0.
    A negative slope would invert the forecast: higher raw → lower calibrated.
    """
    engine = CalibrationEngine()

    # Synthetic data where forecast is positively correlated with some baseline
    # but actual goes the opposite direction — OLS would fit a < 0.
    rng = random.Random(123)
    obs = []
    for _ in range(60):
        fc = rng.uniform(0.05, 0.25)
        # actual = -0.5 * fc + 0.20 + noise  → negative true slope
        actual = -0.5 * fc + 0.20 + rng.gauss(0, 0.002)
        obs.append(make_obs(fc, actual, horizon_hours=30.0, hour_of_day=21))

    result = engine.fit(obs)
    bucket = result.get_bucket(horizon_hours=30.0, hour_of_day=21)

    assert bucket.ols.a >= 0.0, (
        f"OLS slope a={bucket.ols.a} is negative — should be clamped to >= 0"
    )
    print(f"  PASS: negative OLS slope clamped (a={bucket.ols.a})")


def test_quantile_slopes_ordered_after_irls():
    """
    After IRLS, quantile slopes must satisfy q10_a <= q50_a <= q90_a.
    Inverted slopes (q10_a > q90_a) would produce nonsensical confidence bands.
    """
    engine = CalibrationEngine()

    # Heavily skewed noise that can cause IRLS to invert quantile slopes.
    rng = random.Random(77)
    obs = []
    for _ in range(80):
        fc = rng.uniform(0.05, 0.25)
        # Mix of normal and extreme spike noise
        if rng.random() < 0.2:
            noise = rng.uniform(0.05, 0.15)  # large positive spike
        else:
            noise = rng.gauss(0, 0.003)
        actual = 1.2 * fc + 0.01 + noise
        obs.append(make_obs(fc, actual, horizon_hours=9.0, hour_of_day=12))

    result = engine.fit(obs)
    bucket = result.get_bucket(horizon_hours=9.0, hour_of_day=12)

    assert bucket.q10.a <= bucket.q90.a, (
        f"Quantile slopes inverted: q10_a={bucket.q10.a} > q90_a={bucket.q90.a}"
    )
    assert bucket.q10.a <= bucket.q50.a <= bucket.q90.a, (
        f"Quantile slopes not ordered: q10_a={bucket.q10.a}, "
        f"q50_a={bucket.q50.a}, q90_a={bucket.q90.a}"
    )
    print(
        f"  PASS: quantile slopes ordered (q10_a={bucket.q10.a}, "
        f"q50_a={bucket.q50.a}, q90_a={bucket.q90.a})"
    )


def test_quantile_slopes_clamped_to_zero():
    """
    When IRLS produces negative quantile slopes, the engine must clamp them to 0.
    Negative quantile slopes produce nonsensical near-zero or negative P10/P90
    bands for positive raw prices.
    """
    engine = CalibrationEngine()

    # Synthetic data where actual decreases as forecast increases — IRLS will
    # try to fit negative slopes for all quantiles.
    rng = random.Random(999)
    obs = []
    for _ in range(80):
        fc = rng.uniform(0.05, 0.25)
        actual = -0.3 * fc + 0.15 + rng.gauss(0, 0.002)
        obs.append(make_obs(fc, actual, horizon_hours=30.0, hour_of_day=20))

    result = engine.fit(obs)
    bucket = result.get_bucket(horizon_hours=30.0, hour_of_day=20)

    assert bucket.q10.a >= 0.0, (
        f"q10 slope a={bucket.q10.a} is negative — should be clamped to >= 0"
    )
    assert bucket.q50.a >= 0.0, (
        f"q50 slope a={bucket.q50.a} is negative — should be clamped to >= 0"
    )
    assert bucket.q90.a >= 0.0, (
        f"q90 slope a={bucket.q90.a} is negative — should be clamped to >= 0"
    )
    print(
        f"  PASS: quantile slopes clamped to >= 0 "
        f"(q10_a={bucket.q10.a}, q50_a={bucket.q50.a}, q90_a={bucket.q90.a})"
    )


def test_negative_raw_passthrough():
    """
    When raw forecast is <= NEGATIVE_PASSTHROUGH_THRESHOLD (-0.10 $/kWh),
    calibration must return the raw value unchanged — genuine negative-price event.
    """
    model = BucketModel(
        bucket_key="h24_48__shoulder",
        ols=LinearCoeff(a=0.8, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=0.6, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=0.8, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.0, b=0.03, n=100),
    )

    result = model.apply_all(-0.15)
    assert result["calibrated"] == round(-0.15, 6), (
        f"Deeply negative raw should pass through unchanged, got {result['calibrated']}"
    )
    assert result["calibrated_source"] == "passthrough_negative"
    print(f"  PASS: negative raw passthrough (calibrated={result['calibrated']})")


def test_mild_negative_raw_uses_ols():
    """
    When raw forecast is mildly negative (> NEGATIVE_PASSTHROUGH_THRESHOLD,
    e.g. -0.03 $/kWh), calibration should apply OLS correction.
    This is common in the solar window where AEMO over-corrects the trough
    but actual prices are near zero.
    """
    model = BucketModel(
        bucket_key="h12_24__solar",
        ols=LinearCoeff(a=0.928, b=0.012, n=70, mae=0.010, rmse=0.012),
        q10=QuantileCoeff(quantile=0.1, a=0.920, b=0.012, n=70),
        q50=QuantileCoeff(quantile=0.5, a=0.928, b=0.012, n=70),
        q90=QuantileCoeff(quantile=0.9, a=0.931, b=0.012, n=70),
    )

    result = model.apply_all(-0.03)
    # OLS: 0.928 * -0.03 + 0.012 = -0.01584 — closer to zero than raw
    expected = round(0.928 * -0.03 + 0.012, 6)
    assert result["calibrated"] == expected, (
        f"Mild negative raw should use OLS, got {result['calibrated']}, expected {expected}"
    )
    assert result["calibrated_source"] == "ols"
    print(f"  PASS: mild negative raw uses OLS (raw=-0.03, calibrated={result['calibrated']})")


def test_zero_raw_uses_ols():
    """
    When raw forecast is exactly 0.0, calibration should apply OLS
    (above the -0.10 passthrough threshold).
    """
    model = BucketModel(
        bucket_key="h24_48__shoulder",
        ols=LinearCoeff(a=0.8, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=0.6, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=0.8, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.0, b=0.03, n=100),
    )

    result = model.apply_all(0.0)
    # OLS: 0.8 * 0.0 + 0.02 = 0.02
    expected = round(0.8 * 0.0 + 0.02, 6)
    assert result["calibrated"] == expected, (
        f"Zero raw should use OLS, got {result['calibrated']}, expected {expected}"
    )
    assert result["calibrated_source"] == "ols"
    print(f"  PASS: zero raw uses OLS (calibrated={result['calibrated']})")


def test_threshold_boundary_exact():
    """
    Raw exactly at NEGATIVE_PASSTHROUGH_THRESHOLD (-0.10) must pass through.
    """
    model = BucketModel(
        bucket_key="h12_24__solar",
        ols=LinearCoeff(a=0.928, b=0.012, n=70, mae=0.010, rmse=0.012),
        q10=QuantileCoeff(quantile=0.1, a=0.920, b=0.012, n=70),
        q50=QuantileCoeff(quantile=0.5, a=0.928, b=0.012, n=70),
        q90=QuantileCoeff(quantile=0.9, a=0.931, b=0.012, n=70),
    )

    result = model.apply_all(-0.10)
    assert result["calibrated"] == round(-0.10, 6)
    assert result["calibrated_source"] == "passthrough_negative"
    print(f"  PASS: threshold boundary exact passthrough (raw=-0.10)")


# ── Spike passthrough tests ──────────────────────────────────────────────────

def test_spike_passthrough_above_threshold():
    """
    When raw forecast >= SPIKE_THRESHOLD (3.00 $/kWh), calibration must
    return the raw value unchanged with calibrated_source="passthrough_high".
    """
    model = BucketModel(
        bucket_key="h12_24__peak",
        ols=LinearCoeff(a=1.5, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=1.2, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=1.5, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.8, b=0.03, n=100),
    )

    result = model.apply_all(3.50)
    assert result["calibrated"] == round(3.50, 6), (
        f"Spike raw should pass through unchanged, got {result['calibrated']}"
    )
    assert result["calibrated_source"] == "passthrough_high"
    assert result["p10"] == round(3.50, 6)
    assert result["p50"] == round(3.50, 6)
    assert result["p90"] == round(3.50, 6)
    print(f"  PASS: spike passthrough above threshold (raw=3.50, source={result['calibrated_source']})")


def test_below_spike_threshold_uses_ols():
    """
    When raw forecast < SPIKE_THRESHOLD (e.g. 2.50 $/kWh), calibration should
    proceed through the normal OLS path.
    """
    model = BucketModel(
        bucket_key="h12_24__peak",
        ols=LinearCoeff(a=1.5, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=1.2, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=1.5, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.8, b=0.03, n=100),
    )

    result = model.apply_all(0.25)
    # OLS: 1.5 * 0.25 + 0.02 = 0.395
    expected_calibrated = round(1.5 * 0.25 + 0.02, 6)
    assert result["calibrated"] == expected_calibrated, (
        f"Expected OLS calibrated={expected_calibrated}, got {result['calibrated']}"
    )
    assert result["calibrated_source"] == "ols"
    assert result["p10"] is not None
    assert result["p90"] is not None
    print(f"  PASS: below spike threshold uses OLS (raw=0.25, calibrated={result['calibrated']})")


# ── Rolling observation window tests ─────────────────────────────────────────

def test_rolling_window_filters_old_observations():
    """
    Observations older than OBSERVATION_WINDOW_DAYS are excluded from the fit.
    Create 100 days of observations; only the most recent 90 should be used.
    """
    from datetime import datetime, timedelta, timezone

    engine = CalibrationEngine()

    # Create observations spanning 100 days — 1 per day, all in the same bucket
    # (h12_24 / solar, horizon=18, hour=12)
    rng = random.Random(42)
    now = datetime.now(timezone.utc)

    all_obs = []
    for day_offset in range(100):
        obs_date = now - timedelta(days=99 - day_offset)  # oldest first
        # Fix hour to 12:00 NEM so solar classification → "solar" bucket
        iso_str = obs_date.strftime("%Y-%m-%dT") + "12:00:00+10:00"
        fc = rng.uniform(0.05, 0.25)
        actual = 2.0 * fc + 0.01 + rng.gauss(0, 0.003)
        all_obs.append(Observation(
            interval_time=iso_str,
            horizon_hours=18.0,
            pd7day_forecast=fc,
            actual_rrp=actual,
            forecast_run_at=iso_str,
            hour_of_day=12,
            day_of_week=0,
            month=4,
            gas_forecast_tj=None,
            qni_mwflow=None,
            qni_violation_degree=None,
            is_intervention=False,
        ))

    result = engine.fit(all_obs)

    # observations_in_window should be ~90 (within the rolling window)
    assert result.observations_in_window <= 91, (
        f"observations_in_window should be ~90, got {result.observations_in_window}"
    )
    assert result.observations_in_window >= 89, (
        f"observations_in_window should be ~90, got {result.observations_in_window}"
    )

    # total_observations counts non-intervention obs in window (all are non-intervention here)
    assert result.total_observations == result.observations_in_window, (
        f"total_observations should equal observations_in_window for non-intervention data, "
        f"got {result.total_observations} vs {result.observations_in_window}"
    )

    # The bucket should have n reflecting the windowed count, not 100
    bucket = result.get_bucket(horizon_hours=18.0, hour_of_day=12)
    assert bucket.ols.n <= 91, (
        f"Bucket n should be ~90, got {bucket.ols.n}"
    )
    assert bucket.ols.n >= 89, (
        f"Bucket n should be ~90, got {bucket.ols.n}"
    )
    print(
        f"  PASS: rolling window filters old observations "
        f"(input=100, in_window={result.observations_in_window}, bucket_n={bucket.ols.n})"
    )


def test_rolling_window_storage_unchanged():
    """
    The rolling window is a fit-time filter. All observations remain
    available in the input list — the engine does not mutate or trim them.
    """
    from datetime import datetime, timedelta, timezone

    engine = CalibrationEngine()

    now = datetime.now(timezone.utc)
    rng = random.Random(123)

    all_obs = []
    for day_offset in range(100):
        obs_date = now - timedelta(days=99 - day_offset)
        # Fix hour to 12:00 NEM so solar classification is consistent
        iso_str = obs_date.strftime("%Y-%m-%dT") + "12:00:00+10:00"
        fc = rng.uniform(0.05, 0.25)
        actual = 2.0 * fc + 0.01
        all_obs.append(Observation(
            interval_time=iso_str,
            horizon_hours=18.0,
            pd7day_forecast=fc,
            actual_rrp=actual,
            forecast_run_at=iso_str,
            hour_of_day=12,
            day_of_week=0,
            month=4,
            gas_forecast_tj=None,
            qni_mwflow=None,
            qni_violation_degree=None,
            is_intervention=False,
        ))

    original_len = len(all_obs)
    result = engine.fit(all_obs)

    # Engine must NOT mutate or trim the input list
    assert len(all_obs) == original_len, (
        f"Engine mutated input list: was {original_len}, now {len(all_obs)}"
    )
    # observations_in_window should be less than the total input (100)
    assert result.observations_in_window < original_len, (
        f"observations_in_window ({result.observations_in_window}) should be < "
        f"input length ({original_len}) — old observations should be excluded from fit"
    )
    # All 100 observations still in the input list (storage unchanged)
    assert len(all_obs) == 100, (
        f"Input list should still have 100 entries, got {len(all_obs)}"
    )
    print(
        f"  PASS: rolling window storage unchanged "
        f"(input_len={len(all_obs)}, "
        f"in_window={result.observations_in_window})"
    )


# ── Solar elevation ToD tests ────────────────────────────────────────────────

def test_tod_label_solar_peak_window():
    """Peak window (16-21 NEM) is always classified as 'peak' regardless of elevation."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    for hour in (16, 17, 18, 19, 20):
        dt = datetime(2026, 1, 15, hour, 0, tzinfo=nem_tz)
        for region in REGION_COORDS:
            label = _tod_label_solar(dt, region, "fallback")
            assert label == "peak", (
                f"Hour {hour} in {region} should be peak, got {label}"
            )
    print("  PASS: peak window 16-21 NEM hardcoded for all regions")


def test_tod_label_solar_noon_all_regions():
    """At noon NEM in all regions, solar elevation > 15° → 'solar'."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 3, 15, 12, 0, tzinfo=nem_tz)  # noon, March (autumn)
    for region in REGION_COORDS:
        label = _tod_label_solar(dt, region, "fallback")
        assert label == "solar", (
            f"Noon March in {region} should be solar, got {label}"
        )
    print("  PASS: noon NEM → solar for all regions")


def test_tod_label_solar_midnight_all_regions():
    """At midnight NEM in all regions, sun is below horizon → 'shoulder'."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 6, 15, 0, 0, tzinfo=nem_tz)  # midnight, June (winter)
    for region in REGION_COORDS:
        label = _tod_label_solar(dt, region, "fallback")
        assert label == "shoulder", (
            f"Midnight June in {region} should be shoulder, got {label}"
        )
    print("  PASS: midnight NEM → shoulder for all regions")


def test_tod_label_solar_unknown_region_fallback():
    """Unknown region falls back to raw_label."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 3, 15, 12, 0, tzinfo=nem_tz)
    assert _tod_label_solar(dt, "UNKNOWN", "my_fallback") == "my_fallback"
    print("  PASS: unknown region falls back to raw_label")


def test_tod_label_solar_brisbane_summer_morning():
    """Brisbane summer morning 8am — sun should be above 15° → solar."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 1, 15, 8, 0, tzinfo=nem_tz)  # summer 8am
    label = _tod_label_solar(dt, "QLD1", "shoulder")
    assert label == "solar", f"Brisbane summer 8am should be solar, got {label}"
    print("  PASS: Brisbane summer 8am → solar")


def test_tod_label_solar_hobart_winter_early_morning():
    """Hobart winter 7am — sun is very low → shoulder."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 7, 15, 7, 0, tzinfo=nem_tz)  # winter 7am
    label = _tod_label_solar(dt, "TAS1", "shoulder")
    assert label == "shoulder", f"Hobart winter 7am should be shoulder, got {label}"
    print("  PASS: Hobart winter 7am → shoulder")


def test_tod_label_solar_morning_ramp_brisbane_april():
    """Brisbane April 7am — sun above horizon but low (el ~11°) → morning_ramp."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 4, 15, 7, 0, tzinfo=nem_tz)
    label = _tod_label_solar(dt, "QLD1", "shoulder")
    assert label == "morning_ramp", f"Brisbane April 7am should be morning_ramp, got {label}"
    print("  PASS: Brisbane April 7am → morning_ramp")


def test_tod_label_solar_predawn_brisbane_april():
    """Brisbane April 6am — sun below horizon (el ~ -2°) → shoulder."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 4, 15, 6, 0, tzinfo=nem_tz)
    label = _tod_label_solar(dt, "QLD1", "shoulder")
    assert label == "shoulder", f"Brisbane April 6am should be shoulder, got {label}"
    print("  PASS: Brisbane April 6am → shoulder")


def test_tod_label_solar_overnight_brisbane():
    """Brisbane 11pm — well below horizon → shoulder."""
    from datetime import datetime, timezone, timedelta
    nem_tz = timezone(timedelta(hours=10))
    dt = datetime(2026, 4, 15, 23, 0, tzinfo=nem_tz)
    label = _tod_label_solar(dt, "QLD1", "shoulder")
    assert label == "shoulder", f"Brisbane 11pm should be shoulder, got {label}"
    print("  PASS: Brisbane 11pm → shoulder")


def test_region_coords_all_regions():
    """All 5 NEM regions have coordinates."""
    expected = {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}
    assert set(REGION_COORDS.keys()) == expected
    for region, (lat, lon) in REGION_COORDS.items():
        assert -50 < lat < -20, f"{region} latitude {lat} out of range"
        assert 130 < lon < 160, f"{region} longitude {lon} out of range"
    print("  PASS: all 5 NEM regions have valid coordinates")


# ── Weighted OLS tests ──────────────────────────────────────────────────────

def test_weighted_ols_uniform_weights_match_unweighted():
    """Uniform weights should produce the same result as unweighted OLS."""
    pairs = _pairs(50, a=1.8, b=0.02, noise=0.005)
    a_uw, b_uw = _ols(pairs)
    weights = [1.0] * len(pairs)
    a_w, b_w = _ols(pairs, weights=weights)
    assert abs(a_uw - a_w) < 1e-6, f"Weighted a={a_w} != unweighted a={a_uw}"
    assert abs(b_uw - b_w) < 1e-6, f"Weighted b={b_w} != unweighted b={b_uw}"
    print("  PASS: uniform weights match unweighted OLS")


def test_weighted_ols_recent_obs_higher_weight():
    """
    Recent observations with higher weight should dominate the fit.
    Create two groups: old (a=1.0) and new (a=2.0). With decay, fit
    should be closer to 2.0 than 1.0.
    """
    rng = random.Random(42)
    pairs = []
    weights = []
    # Old observations (low weight): y = 1.0 * x + 0.0
    for _ in range(30):
        x = rng.uniform(0.05, 0.25)
        pairs.append((x, 1.0 * x + 0.0))
        weights.append(math.exp(-DECAY_LAMBDA * 80))  # 80 days ago
    # Recent observations (high weight): y = 2.0 * x + 0.0
    for _ in range(30):
        x = rng.uniform(0.05, 0.25)
        pairs.append((x, 2.0 * x + 0.0))
        weights.append(math.exp(-DECAY_LAMBDA * 5))   # 5 days ago
    a, b = _ols(pairs, weights=weights)
    assert a > 1.5, f"Weighted OLS a={a} should be > 1.5 (closer to recent a=2.0)"
    print(f"  PASS: weighted OLS favors recent (a={a:.4f}, closer to 2.0 than 1.0)")


def test_weighted_ols_passthrough_insufficient_data():
    """Weighted OLS should still return passthrough when n < MIN_OBS."""
    pairs = _pairs(MIN_OBS - 1, a=2.0, b=0.1)
    weights = [1.0] * len(pairs)
    a, b = _ols(pairs, weights=weights)
    assert a == 1.0 and b == 0.0
    print(f"  PASS: weighted OLS passthrough with n={MIN_OBS - 1}")


def test_weighted_ols_decay_correctness():
    """Verify weight formula: weight = exp(-0.033 * days_ago)."""
    w0 = math.exp(-DECAY_LAMBDA * 0)
    w21 = math.exp(-DECAY_LAMBDA * 21)
    w90 = math.exp(-DECAY_LAMBDA * 90)
    assert abs(w0 - 1.0) < 1e-10, f"Weight at day 0 should be 1.0, got {w0}"
    assert abs(w21 - 0.5) < 0.02, f"Weight at day 21 (half-life) should be ~0.5, got {w21}"
    assert w90 < 0.06, f"Weight at day 90 should be < 0.06, got {w90}"
    print(f"  PASS: decay weights correct (day0={w0:.3f}, day21={w21:.3f}, day90={w90:.4f})")


def test_engine_weighted_fit_produces_result():
    """Engine fit with region parameter should produce a valid CalibrationResult."""
    engine = CalibrationEngine()
    observations = _make_obs_batch(
        n=50, a=2.0, b=0.01, horizon_hours=18.0, hour_of_day=12
    )
    result = engine.fit(observations, region="QLD1")
    assert result.observations_in_window == 50
    assert result.total_observations == 50
    bucket = result.get_bucket(horizon_hours=18.0, hour_of_day=12)
    assert bucket.ols.n == 50
    assert bucket.ols.a > 0
    print(f"  PASS: engine weighted fit produces valid result (n={bucket.ols.n})")


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    # Bucket routing
    test_horizon_labels,
    test_tod_labels,
    test_all_bucket_keys,
    # OLS
    test_ols_perfect_fit,
    test_ols_noisy_fit,
    test_ols_passthrough_insufficient_data,
    test_ols_metrics,
    test_ols_positive_intercept,
    # Quantile regression
    test_quantile_regression_median,
    test_quantile_regression_ordering,
    test_quantile_regression_asymmetric_noise,
    test_quantile_passthrough,
    # Engine integration
    test_engine_fit_applies_correctly,
    test_engine_intervention_skipped,
    test_engine_passthrough_below_min_obs,
    test_engine_serialisation_roundtrip,
    test_engine_multi_bucket_independence,
    # Bug-fix regressions
    test_negative_ols_slope_clamped,
    test_quantile_slopes_ordered_after_irls,
    test_quantile_slopes_clamped_to_zero,
    test_negative_raw_passthrough,
    test_mild_negative_raw_uses_ols,
    test_zero_raw_uses_ols,
    test_threshold_boundary_exact,
    # Spike passthrough
    test_spike_passthrough_above_threshold,
    test_below_spike_threshold_uses_ols,

    # Rolling observation window
    test_rolling_window_filters_old_observations,
    test_rolling_window_storage_unchanged,
    # Solar elevation ToD
    test_tod_label_solar_peak_window,
    test_tod_label_solar_noon_all_regions,
    test_tod_label_solar_midnight_all_regions,
    test_tod_label_solar_unknown_region_fallback,
    test_tod_label_solar_brisbane_summer_morning,
    test_tod_label_solar_hobart_winter_early_morning,
    test_tod_label_solar_morning_ramp_brisbane_april,
    test_tod_label_solar_predawn_brisbane_april,
    test_tod_label_solar_overnight_brisbane,
    test_region_coords_all_regions,
    # Weighted OLS
    test_weighted_ols_uniform_weights_match_unweighted,
    test_weighted_ols_recent_obs_higher_weight,
    test_weighted_ols_passthrough_insufficient_data,
    test_weighted_ols_decay_correctness,
    test_engine_weighted_fit_produces_result,
]


def run_all():
    passed = 0
    failed = 0
    print(f"\nRunning {len(TESTS)} calibration engine tests\n{'='*50}")
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
