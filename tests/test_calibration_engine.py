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

# Load nem_time first (no HA deps), then calibration_engine
_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

from custom_components.nem_pd7day.calibration_engine import (
    MIN_OBS,
    BucketModel,
    CalibrationEngine,
    LinearCoeff,
    Observation,
    QuantileCoeff,
    _bucket_key,
    _horizon_label,
    _tod_label,
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
    return Observation(
        interval_time="2026-04-13T14:00:00",
        horizon_hours=horizon_hours,
        pd7day_forecast=forecast,
        actual_rrp=actual,
        forecast_run_at="2026-04-12T03:30:00",
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
    assert _tod_label(0) == "offpeak"
    assert _tod_label(6) == "offpeak"   # shoulder start not defined separately
    assert _tod_label(10) == "solar"
    assert _tod_label(15) == "solar"
    assert _tod_label(16) == "peak"
    assert _tod_label(19) == "peak"
    assert _tod_label(20) == "shoulder"
    assert _tod_label(21) == "shoulder"
    assert _tod_label(22) == "offpeak"
    assert _tod_label(23) == "offpeak"
    print("  PASS: time-of-day labels")


def test_all_bucket_keys():
    keys = all_bucket_keys()
    assert len(keys) == 24   # 6 horizons × 4 tod buckets
    assert "h00_06__peak" in keys
    assert "h96plus__offpeak" in keys
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
    When raw forecast is negative, calibration must return the raw value
    unchanged. The model is trained on positive prices only — extrapolating
    through positive intercepts would actively worsen the forecast.
    """
    model = BucketModel(
        bucket_key="h24_48__shoulder",
        ols=LinearCoeff(a=0.8, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=0.6, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=0.8, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.0, b=0.03, n=100),
    )

    result = model.apply_all(-0.005)
    assert result["calibrated"] == round(-0.005, 6), (
        f"Negative raw should pass through unchanged, got {result['calibrated']}"
    )
    assert result["calibrated_source"] == "passthrough_negative"
    print(f"  PASS: negative raw passthrough (calibrated={result['calibrated']})")


def test_zero_raw_passthrough():
    """
    When raw forecast is exactly 0.0, calibration must return 0.0 unchanged.
    """
    model = BucketModel(
        bucket_key="h24_48__shoulder",
        ols=LinearCoeff(a=0.8, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=0.6, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=0.8, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.0, b=0.03, n=100),
    )

    result = model.apply_all(0.0)
    assert result["calibrated"] == 0.0, (
        f"Zero raw should pass through unchanged, got {result['calibrated']}"
    )
    assert result["calibrated_source"] == "passthrough_negative"
    print(f"  PASS: zero raw passthrough (calibrated={result['calibrated']})")


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
    test_zero_raw_passthrough,
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
