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

import pytest
from datetime import datetime, timedelta, timezone

# ── Fixture date anchoring ────────────────────────────────────────────────────
# CalibrationEngine.fit() only trains on observations newer than
# OBSERVATION_WINDOW_DAYS (90).  Fixture dates must therefore be relative to
# "now", not a fixed calendar date: the previous hard-coded 2026-04-13 anchor
# aged out of the window on 2026-07-12, after which every observation was
# silently dropped, all buckets fitted empty, and apply() returned
# "passthrough" instead of "isotonic" — turning 17 tests red with no code
# change.  Anchoring to now keeps these tests valid indefinitely.
NEM_TZ = timezone(timedelta(hours=10))  # NEM is UTC+10 year-round (no DST)
_OBS_ANCHOR = datetime.now(NEM_TZ) - timedelta(days=2)


def _obs_day(offset_days: int = 0) -> datetime:
    """Return the fixture base date, optionally offset, at midnight NEM time."""
    return (_OBS_ANCHOR + timedelta(days=offset_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _obs_iso(offset_days: int = 0, hour: int = 0, minute: int = 0) -> str:
    """ISO-8601 NEM timestamp for a fixture interval, relative to the anchor."""
    return _obs_day(offset_days).replace(hour=hour, minute=minute).isoformat()

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
    REGION_COORDS,
    BucketModel,
    CalibrationEngine,
    LinearCoeff,
    Observation,
    OlsModel,
    QuantileCoeff,
    RunFeatures,
    StpasaFeatures,
    _bucket_key,
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
    # Build interval_time that matches hour_of_day so solar classification is
    # consistent.  Anchored to the fixture base date (see _OBS_ANCHOR) so the
    # observation always falls inside the engine's rolling training window.
    interval_dt = _obs_day().replace(hour=hour_of_day)
    return Observation(
        interval_time=interval_dt.isoformat(),
        horizon_hours=horizon_hours,
        pd7day_forecast=forecast,
        actual_rrp=actual,
        forecast_run_at=_obs_iso(offset_days=-1, hour=3, minute=30),
        hour_of_day=hour_of_day,
        day_of_week=interval_dt.weekday(),
        month=interval_dt.month,
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


def _right_skewed_pairs(n: int = 400, seed: int = 103) -> list[tuple[float, float]]:
    """y = x plus gaussian noise, with a lognormal spike on 15% of rows.

    A crude stand-in for the NEM price regime: heavy right tail, nothing on
    the left. This is the shape on which an expectile fit and a quantile fit
    disagree most.
    """
    rng = random.Random(seed)
    pairs = []
    for _ in range(n):
        x = rng.uniform(0.0, 0.3)
        noise = rng.gauss(0, 0.02)
        if rng.random() < 0.15:
            noise += rng.lognormvariate(-3, 1)
        pairs.append((x, x + noise))
    return pairs


def _fraction_below(pairs, a, b) -> float:
    return sum(1 for x, y in pairs if y < a * x + b) / len(pairs)


@pytest.mark.parametrize("quantile", [0.1, 0.5, 0.9])
def test_quantile_regression_coverage_matches_nominal_level(quantile):
    """
    Issue #103: the fitted line for level tau must have about tau of the
    observations below it. The sign-only IRLS weights this replaced converged
    to the expectile instead, and on this fixture put ~23% of points under
    the P10 line and ~60% under the P50 line.
    """
    pairs = _right_skewed_pairs()
    a, b, _ = _quantile_regression(pairs, quantile)
    below = _fraction_below(pairs, a, b)
    assert abs(below - quantile) < 0.04, (
        f"tau={quantile}: {below:.3f} of observations fall below the fitted "
        f"line, expected about {quantile}"
    )


def test_quantile_regression_expectile_weights_would_fail_coverage():
    """
    Guard that the coverage test above is testing something real: rerun the
    pre-#103 weighting (sign-only, no 1/|r| divisor) and confirm it misses
    the nominal level by far more than the tolerance.
    """
    pairs = _right_skewed_pairs()
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    a, b = _ols(pairs)
    for _ in range(15):
        w = [0.1 if ys[i] - (a * xs[i] + b) >= 0 else 0.9 for i in range(len(pairs))]
        sw = sum(w)
        swx = sum(w[i] * xs[i] for i in range(len(pairs)))
        swy = sum(w[i] * ys[i] for i in range(len(pairs)))
        swxx = sum(w[i] * xs[i] * xs[i] for i in range(len(pairs)))
        swxy = sum(w[i] * xs[i] * ys[i] for i in range(len(pairs)))
        denom = sw * swxx - swx * swx
        a = (sw * swxy - swx * swy) / denom
        b = (swy - a * swx) / sw
    below = _fraction_below(pairs, a, b)
    assert below > 0.18, f"expectile weighting unexpectedly covered {below:.3f}"


def test_quantile_regression_uniform_weights_match_unweighted():
    """Sample weights all equal must reproduce the unweighted fit."""
    pairs = _right_skewed_pairs(n=200, seed=7)
    a0, b0, pl0 = _quantile_regression(pairs, 0.9)
    a1, b1, pl1 = _quantile_regression(pairs, 0.9, weights=[0.37] * len(pairs))
    assert abs(a0 - a1) < 1e-6 and abs(b0 - b1) < 1e-6
    assert abs(pl0 - pl1) < 1e-6


def test_quantile_regression_weights_shift_the_fit():
    """Weighting one half of the sample heavily must move the fitted line
    towards that half; otherwise the weights are not reaching the solver."""
    rng = random.Random(11)
    low = [(x, x + 0.00 + rng.gauss(0, 0.003)) for x in (rng.uniform(0.05, 0.25) for _ in range(150))]
    high = [(x, x + 0.05 + rng.gauss(0, 0.003)) for x in (rng.uniform(0.05, 0.25) for _ in range(150))]
    pairs = low + high
    w_low = [1.0] * 150 + [0.01] * 150
    w_high = [0.01] * 150 + [1.0] * 150
    _, b_low, _ = _quantile_regression(pairs, 0.5, weights=w_low)
    _, b_high, _ = _quantile_regression(pairs, 0.5, weights=w_high)
    assert b_high - b_low > 0.03, f"weights did not move the median line ({b_low=}, {b_high=})"


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

    assert calibrated["calibrated_source"] == "isotonic", "Expected isotonic calibration"
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
    """to_storage / from_storage preserves OLS/quantile coefficients for warm-start.

    Note: the isotonic model (sklearn IsotonicRegression) is not JSON-serialisable
    and is NOT persisted to storage.  After from_storage, apply() returns
    "passthrough" (iso_model is None) until the next engine.fit() call re-populates
    the isotonic models.  This is by design: storage provides a warm-start for OLS
    diagnostics and quantile intervals; the isotonic model is always re-fitted on
    startup or when force_refit is called.
    """
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

    # Pre-serialisation: isotonic must be active (not passthrough)
    assert orig["calibrated_source"] == "isotonic", (
        f"Expected isotonic before serialisation, got {orig['calibrated_source']}"
    )
    assert orig["calibrated"] != round(test_price, 6), (
        "Isotonic must alter the forecast — raw passthrough before serialisation"
    )

    # Post-serialisation: iso_model not persisted → passthrough
    assert rest["calibrated_source"] == "passthrough", (
        f"Expected passthrough after serialisation (iso not persisted), got {rest['calibrated_source']}"
    )
    assert rest["calibrated"] == round(test_price, 6), (
        "Post-serialisation passthrough must return raw price unchanged"
    )

    # Quantile intervals survive roundtrip
    assert orig["p10"] is not None
    assert orig["p90"] is not None
    assert rest["p10"] is not None
    assert rest["p90"] is not None
    assert math.isclose(orig["p10"], rest["p10"], rel_tol=1e-6), (
        f"P10 changed after serialisation: {orig['p10']} vs {rest['p10']}"
    )
    assert math.isclose(orig["p90"], rest["p90"], rel_tol=1e-6), (
        f"P90 changed after serialisation: {orig['p90']} vs {rest['p90']}"
    )
    print("  PASS: serialisation roundtrip (isotonic active pre, passthrough post, quantiles survive)")


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
    # Mild negative (> NEGATIVE_PASSTHROUGH_THRESHOLD) must NOT use passthrough_negative.
    # With no iso_model set, a manually-constructed BucketModel returns "passthrough"
    # (insufficient data path); in production engine.fit() would populate iso_model.
    assert result["calibrated_source"] != "passthrough_negative", (
        f"Mild negative should not use negative passthrough, got {result['calibrated_source']}"
    )
    print(f"  PASS: mild negative raw bypasses passthrough_negative (raw=-0.03, source={result['calibrated_source']})")


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
    # Zero raw (above NEGATIVE_PASSTHROUGH_THRESHOLD=-0.10) must NOT passthrough_negative.
    # Without iso_model in a manually-constructed bucket, returns "passthrough";
    # in production engine.fit() populates iso_model and returns "isotonic".
    assert result["calibrated_source"] != "passthrough_negative", (
        f"Zero raw should not use negative passthrough, got {result['calibrated_source']}"
    )
    print(f"  PASS: zero raw bypasses passthrough_negative (calibrated={result['calibrated']}, source={result['calibrated_source']})")


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
    print("  PASS: threshold boundary exact passthrough (raw=-0.10)")


# ── Spike passthrough tests ──────────────────────────────────────────────────

def test_spike_input_no_iso_model_returns_passthrough():
    """
    When raw >= SPIKE_THRESHOLD and iso_model is None (< MIN_OBS),
    calibration must return 'passthrough' (not passthrough_high — that
    source no longer exists).  The raw value passes through unchanged.
    """
    model = BucketModel(
        bucket_key="h12_24__peak",
        ols=LinearCoeff(a=1.5, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=1.2, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=1.5, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.8, b=0.03, n=100),
    )
    # No iso_model set → None by default
    assert model.iso_model is None

    result = model.apply_all(3.50)
    assert result["calibrated"] == round(3.50, 6), (
        f"Spike raw with no iso_model should pass through unchanged, got {result['calibrated']}"
    )
    assert result["calibrated_source"] == "passthrough"
    print("  PASS: spike input no iso_model returns passthrough (raw=3.50)")


def test_spike_input_with_iso_model_returns_isotonic():
    """
    When raw >= SPIKE_THRESHOLD and iso_model is fitted, the isotonic model
    must return a clipped value (out_of_bounds='clip') with source 'isotonic'.
    The clipped value is the training-range maximum — well below the raw spike.
    """
    # Build a fitted engine so iso_model exists
    obs = _make_obs_batch(n=60, a=1.5, b=0.02, horizon_hours=18.0, hour_of_day=17)
    engine = CalibrationEngine()
    result_fitted = engine.fit(obs)

    # Apply a spike value well above training range
    result = result_fitted.apply(8.999, horizon_hours=18.0, hour_of_day=17)
    assert result["calibrated_source"] == "isotonic", (
        f"Expected isotonic for spike input with iso_model, got {result['calibrated_source']}"
    )
    cal_val = result["calibrated"]
    assert isinstance(cal_val, float), "calibrated must be a float"
    assert cal_val >= 0.0, "calibrated must be non-negative"
    # Isotonic uses out_of_bounds='clip' — for x far above training range,
    # the result is the clipped maximum, which should be well below 8.999
    assert cal_val < 8.999, (
        f"Isotonic clip should be below raw spike, got {cal_val}"
    )
    print(f"  PASS: spike input with iso_model returns isotonic (raw=8.999, cal={cal_val})")


def test_spike_input_no_iso_model_passthrough():
    """
    When raw >= SPIKE_THRESHOLD and iso_model is None, passthrough is returned.
    """
    model = BucketModel(
        bucket_key="h12_24__peak",
        ols=LinearCoeff(a=1.5, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=1.2, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=1.5, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.8, b=0.03, n=100),
    )
    assert model.iso_model is None

    result = model.apply_all(8.999)
    assert result["calibrated_source"] == "passthrough"
    print("  PASS: spike input without iso_model (passthrough)")


def test_below_spike_threshold_uses_ols():
    """
    When raw forecast < SPIKE_THRESHOLD (e.g. 0.25 $/kWh), calibration should
    proceed through the isotonic path (not spike passthrough).
    Uses engine.fit() to produce a real iso_model.
    """
    # Build observations with known relationship for h12_24 / peak bucket
    obs = _make_obs_batch(n=60, a=1.5, b=0.02, horizon_hours=18.0, hour_of_day=17)
    engine = CalibrationEngine()
    result_fitted = engine.fit(obs)
    result = result_fitted.apply(0.25, horizon_hours=18.0, hour_of_day=17)

    assert result["calibrated_source"] == "isotonic", (
        f"Below-spike forecast should use isotonic, got {result['calibrated_source']}"
    )
    assert result["calibrated"] != round(0.25, 6), (
        "Isotonic must alter a sub-threshold forecast from the raw value"
    )
    assert result["p10"] is not None
    assert result["p90"] is not None
    print(f"  PASS: below spike threshold uses isotonic (raw=0.25, calibrated={result['calibrated']})")


def test_spike_actuals_excluded_from_ols_buckets():
    """
    Observations where actual_rrp >= SPIKE_THRESHOLD must be excluded from
    OLS training buckets.  Spike actuals ($3,000+/MWh) follow a different
    distribution and collapse OLS slopes to near zero when included.
    """
    engine = CalibrationEngine()
    rng = random.Random(555)

    # 60 normal observations: actual ≈ 2.0 * forecast + 0.01
    normal_obs = []
    for _ in range(60):
        fc = rng.uniform(0.05, 0.25)
        actual = 2.0 * fc + 0.01 + rng.gauss(0, 0.003)
        normal_obs.append(make_obs(fc, actual, horizon_hours=18.0, hour_of_day=12))

    # 15 spike observations: actual_rrp >= SPIKE_THRESHOLD (e.g. $8-$15/kWh)
    spike_obs = []
    for _ in range(15):
        fc = rng.uniform(0.05, 0.25)
        actual = rng.uniform(8.0, 15.0)  # $8,000-$15,000/MWh spike
        spike_obs.append(make_obs(fc, actual, horizon_hours=18.0, hour_of_day=12))

    # Fit with both normal + spike observations
    result = engine.fit(normal_obs + spike_obs)
    bucket = result.get_bucket(horizon_hours=18.0, hour_of_day=12)

    # Spike observations must NOT have entered the bucket
    assert bucket.ols.n == 60, (
        f"Bucket n should be 60 (normal only), got {bucket.ols.n} — "
        f"spike observations leaked into the OLS fit"
    )

    # OLS slope should be healthy (~2.0), not collapsed
    assert bucket.ols.a > 0.1, (
        f"OLS slope a={bucket.ols.a} is near zero — spike observations "
        f"likely corrupted the fit"
    )
    assert abs(bucket.ols.a - 2.0) < 0.3, (
        f"OLS slope a={bucket.ols.a} too far from expected ~2.0 — "
        f"spike observations may have affected the fit"
    )

    # total_observations should also exclude spike obs
    assert result.total_observations == 60, (
        f"total_observations should be 60 (normal only), got {result.total_observations}"
    )

    print(
        f"  PASS: spike actuals excluded from OLS buckets "
        f"(n={bucket.ols.n}, a={bucket.ols.a:.4f}, total={result.total_observations})"
    )


def test_spike_forecasts_excluded_from_ols_buckets():
    """
    Observations where pd7day_forecast >= SPIKE_THRESHOLD must also be excluded
    from OLS training buckets.  During spike events the passthrough path serves
    the raw AEMO forecast unchanged, so pd7day_forecast can be $8-$15/kWh.
    These extreme x-values are high-leverage points that collapse the OLS slope
    to near zero even when actual_rrp is also high (since actual is also excluded
    by the actual_rrp guard).  The key risk is forecasts with spike-range x but
    moderate y (e.g. spike forecast issued but actual didn't materialise).
    """
    engine = CalibrationEngine()
    rng = random.Random(777)

    # 60 normal observations: actual ≈ 1.5 * forecast + 0.02
    normal_obs = []
    for _ in range(60):
        fc = rng.uniform(0.05, 0.25)
        actual = 1.5 * fc + 0.02 + rng.gauss(0, 0.003)
        normal_obs.append(make_obs(fc, actual, horizon_hours=36.0, hour_of_day=17))

    # 15 spike-forecast observations: pd7day_forecast >= SPIKE_THRESHOLD
    # (spike forecast issued, actual moderated — the classic leverage poison case)
    spike_fc_obs = []
    for _ in range(15):
        fc = rng.uniform(3.0, 15.0)  # spike-range forecast
        actual = rng.uniform(0.05, 0.30)  # actual stayed normal
        spike_fc_obs.append(make_obs(fc, actual, horizon_hours=36.0, hour_of_day=17))

    result = engine.fit(normal_obs + spike_fc_obs)
    bucket = result.get_bucket(horizon_hours=36.0, hour_of_day=17)

    # Spike-forecast observations must NOT have entered the bucket
    assert bucket.ols.n == 60, (
        f"Bucket n should be 60 (normal only), got {bucket.ols.n} — "
        f"spike-forecast observations leaked into the OLS fit"
    )

    # OLS slope should be healthy (~1.5), not collapsed
    assert bucket.ols.a > 0.1, (
        f"OLS slope a={bucket.ols.a} is near zero — spike-forecast observations "
        f"likely corrupted the fit via leverage"
    )
    assert abs(bucket.ols.a - 1.5) < 0.3, (
        f"OLS slope a={bucket.ols.a} too far from expected ~1.5"
    )

    assert result.total_observations == 60, (
        f"total_observations should be 60 (normal only), got {result.total_observations}"
    )

    print(
        f"  PASS: spike forecasts excluded from OLS buckets "
        f"(n={bucket.ols.n}, a={bucket.ols.a:.4f}, total={result.total_observations})"
    )


def test_isotonic_mae_beats_raw_baseline():
    """
    IsotonicRegression calibration must produce lower MAE than the raw
    PD7DAY forecast on a held-out test set.

    Uses a synthetic piecewise-monotone dataset where a linear (OLS) model
    would underfit but isotonic regression fits well — the relationship
    between forecast and actual flattens at higher forecast values, which
    is exactly the behaviour observed in QLD1 PD7DAY at h24_48+ horizons.

    Piecewise relationship:
      fc < 0.05:  actual ≈ 0.80 * fc + 0.010
      fc < 0.15:  actual ≈ 0.40 * fc + 0.030  (slope flattens)
      fc ≥ 0.15:  actual ≈ 0.20 * fc + 0.060  (further flattening)
    """
    import random as _rng_mod
    rng = _rng_mod.Random(42)

    def make_actual(fc: float) -> float:
        """Piecewise monotone transform with Gaussian noise."""
        if fc < 0.05:
            base = 0.80 * fc + 0.010
        elif fc < 0.15:
            base = 0.40 * fc + 0.030
        else:
            base = 0.20 * fc + 0.060
        return max(base + rng.gauss(0, 0.003), 0.0)

    # 60 observations, all in h24_48 / peak bucket (horizon=36h, hour=17)
    all_obs = [
        make_obs(
            forecast=rng.uniform(0.01, 0.25),
            actual=0.0,  # placeholder, overwritten below
            horizon_hours=36.0,
            hour_of_day=17,
        )
        for _ in range(60)
    ]
    # Assign actuals using the piecewise transform
    all_obs = [
        make_obs(
            forecast=o.pd7day_forecast,
            actual=make_actual(o.pd7day_forecast),
            horizon_hours=36.0,
            hour_of_day=17,
        )
        for o in all_obs
    ]

    # 80/20 chronological split
    train_obs = all_obs[:48]
    test_obs  = all_obs[48:]

    engine = CalibrationEngine()
    result = engine.fit(train_obs)

    # Compute MAE on test set
    mae_raw = sum(
        abs(o.actual_rrp - o.pd7day_forecast) for o in test_obs
    ) / len(test_obs)

    mae_cal = sum(
        abs(
            o.actual_rrp
            - result.apply(o.pd7day_forecast, o.horizon_hours, o.hour_of_day)["calibrated"]
        )
        for o in test_obs
    ) / len(test_obs)

    assert mae_cal < mae_raw, (
        f"Isotonic calibration MAE {mae_cal:.4f} should be < raw MAE {mae_raw:.4f}"
    )
    assert mae_cal < 0.035, (
        f"Calibrated MAE {mae_cal:.4f} exceeds absolute quality gate 0.035"
    )

    print(
        f"  PASS: isotonic MAE={mae_cal:.4f} < raw MAE={mae_raw:.4f}"
    )


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


def test_p10_p90_never_outside_calibrated():
    """P10 must be <= calibrated and P90 must be >= calibrated after apply_all."""
    from datetime import datetime, timezone, timedelta

    NEM_TZ = timezone(timedelta(hours=10))
    random.seed(42)
    obs = []
    for i in range(40):
        x = 0.05 + random.uniform(0, 0.15)
        y = x * 0.75
        obs.append(Observation(
            interval_time=datetime(2026, 3, i % 28 + 1, 10, 0, tzinfo=NEM_TZ).isoformat(),
            horizon_hours=4.0, pd7day_forecast=x, actual_rrp=y,
            forecast_run_at=datetime(2026, 3, i % 28 + 1, 6, 0, tzinfo=NEM_TZ).isoformat(),
            hour_of_day=10, day_of_week=0, month=3,
            gas_forecast_tj=None, qni_mwflow=None,
            qni_violation_degree=None, is_intervention=False,
        ))
    engine = CalibrationEngine()
    result = engine.fit(obs, region="QLD1")

    violations = []
    for key, model in result.models.items():
        if model.ols.n < 10:
            continue
        for x_test in [0.05, 0.10, 0.15, 0.20]:
            out = model.apply_all(x_test)
            if out["calibrated_source"] not in ("isotonic", "ols"):
                continue
            cal = out["calibrated"]
            p10 = out.get("p10")
            p90 = out.get("p90")
            if p10 is not None and p10 > cal + 1e-9:
                violations.append(f"{key} x={x_test}: p10={p10} > cal={cal}")
            if p90 is not None and p90 < cal - 1e-9:
                violations.append(f"{key} x={x_test}: p90={p90} < cal={cal}")

    assert not violations, f"P10/P90 violations: {violations}"
    print("  PASS: P10 <= calibrated <= P90 for all buckets and test inputs")


def test_p50_within_confidence_band():
    """
    P50 must satisfy P10 <= P50 <= P90 after apply_all.

    Quantile IRLS re-orders slopes (q10_a <= q50_a <= q90_a) but does not
    re-order intercepts.  When intercepts cross, P50 can fall outside the
    [P10, P90] band at the prediction point.  The post-fit clamp in apply_all
    must enforce the full monotone ordering P10 <= P50 <= P90.

    This test injects a BucketModel with deliberately crossed intercepts
    (q10_b > q90_b) and verifies apply_all still returns a valid ordering.
    """
    from custom_components.nem_pd7day.calibration_engine import (
        BucketModel, LinearCoeff, QuantileCoeff, IsotonicRegression,
    )
    import numpy as np

    # Build a bucket with crossed intercepts: slopes ordered but b values inverted.
    # At small x the P10 line (high intercept, low slope) will be ABOVE P90.
    model = BucketModel(bucket_key="h00_06__solar")
    n_fit = 30
    model.ols = LinearCoeff(a=1.0, b=0.0, n=n_fit, mae=0.01, rmse=0.015)
    # Slopes correctly ordered: q10_a < q50_a < q90_a
    # Intercepts deliberately inverted: b10 > b50 > b90
    model.q10 = QuantileCoeff(0.1, a=0.5, b=0.10, n=n_fit)
    model.q50 = QuantileCoeff(0.5, a=0.8, b=0.06, n=n_fit)
    model.q90 = QuantileCoeff(0.9, a=1.2, b=0.01, n=n_fit)

    # Fit a trivial isotonic model so calibrated_source == "isotonic"
    iso = IsotonicRegression()
    xs = np.array([0.05, 0.10, 0.15, 0.20, 0.25])
    ys = np.array([0.08, 0.12, 0.16, 0.20, 0.24])
    iso.fit(xs, ys)
    model.iso_model = iso

    violations = []
    # At small x (x=0.05): P10 = 0.5*0.05+0.10=0.125; P90 = 1.2*0.05+0.01=0.07
    # Without clamping: P50=0.8*0.05+0.06=0.10 which is outside [0.07, 0.08_calibrated]
    for x_test in [0.05, 0.08, 0.10, 0.15, 0.20, 0.25]:
        out = model.apply_all(x_test)
        if out["calibrated_source"] != "isotonic":
            continue
        p10 = out["p10"]
        p50 = out["p50"]
        p90 = out["p90"]
        if p10 is not None and p50 is not None and p10 > p50 + 1e-9:
            violations.append(f"x={x_test}: p10={p10} > p50={p50}")
        if p50 is not None and p90 is not None and p50 > p90 + 1e-9:
            violations.append(f"x={x_test}: p50={p50} > p90={p90}")

    assert not violations, (
        f"P10/P50/P90 ordering violated with crossed intercepts: {violations}"
    )
    print("  PASS: P50 clamped within [P10, P90] even with crossed intercepts")


def test_spike_input_uses_isotonic():
    """
    Spike inputs (>= SPIKE_THRESHOLD) proceed through isotonic calibration.
    The isotonic clip produces a large divergence from raw (intentional) —
    out_of_bounds='clip' maps spike forecasts to training-range max.
    """
    obs = _make_obs_batch(n=60, a=1.5, b=0.02, horizon_hours=18.0, hour_of_day=17)
    engine = CalibrationEngine()
    result_fitted = engine.fit(obs)

    result = result_fitted.apply(8.999, horizon_hours=18.0, hour_of_day=17)
    assert result["calibrated_source"] == "isotonic", (
        f"Spike input must use isotonic, got {result['calibrated_source']}"
    )
    assert result["calibrated"] < 3.0, (
        f"Isotonic clip should map spike to training-range max, got {result['calibrated']}"
    )
    print(f"  PASS: spike input uses isotonic (raw=8.999, cal={result['calibrated']})")


def test_spike_input_quantiles_none_when_isotonic():
    """
    When a spike input goes through isotonic clip, quantile coefficients
    still apply. Verify the full result structure.
    """
    obs = _make_obs_batch(n=60, a=1.5, b=0.02, horizon_hours=18.0, hour_of_day=17)
    engine = CalibrationEngine()
    result_fitted = engine.fit(obs)

    result = result_fitted.apply(8.999, horizon_hours=18.0, hour_of_day=17)
    assert result["calibrated_source"] == "isotonic"
    # p10/p50/p90 should be present (quantile regressions are fitted)
    assert result["p10"] is not None, "p10 should be present for fitted bucket"
    assert result["p90"] is not None, "p90 should be present for fitted bucket"
    assert "n_obs" in result, "n_obs must be in result"
    assert result["n_obs"] >= 60, f"n_obs should reflect training count, got {result['n_obs']}"
    print("  PASS: spike input isotonic result has full structure")


# ── STPASA OLS stage2 tests ─────────────────────────────────────────────────

def _make_stpasa_obs(
    n: int,
    horizon_hours: float,
    hour_of_day: int,
    a: float = 1.2,
    b: float = 0.02,
    seed: int = 7,
) -> tuple[list[Observation], dict[str, StpasaFeatures]]:
    """
    Build n observations at a given horizon with distinct interval_time keys
    plus a matching stpasa_by_key feature map.  Each obs gets unique STPASA
    features so the OLS fit sees real variation.
    """
    rng = random.Random(seed)
    run_at = _obs_iso(offset_days=-1, hour=3, minute=30)
    obs: list[Observation] = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}

    # Seed a handful of near-term (h<24) observations sharing the same run_at so
    # _compute_run_features populates RunFeatures for this run.  A real PD7DAY
    # run always contains near-term intervals; the in-band rows need them.
    for j in range(6):
        near_dt = _obs_day(offset_days=-1).replace(hour=4 + j)
        obs.append(
            Observation(
                interval_time=near_dt.isoformat(),
                horizon_hours=2.0 + j,
                pd7day_forecast=rng.uniform(0.05, 0.25),
                actual_rrp=rng.uniform(0.05, 0.30),
                forecast_run_at=run_at,
                hour_of_day=4 + j,
                day_of_week=near_dt.weekday(),
                month=near_dt.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )

    for i in range(n):
        # Distinct interval_time per obs (vary the day so keys are unique).
        # Days step backwards from the anchor to stay inside the training window.
        interval_time = _obs_day(offset_days=-(i % 15)).replace(
            hour=hour_of_day, minute=(i % 2) * 30
        ).isoformat()
        fc = rng.uniform(0.05, 0.25)
        # Higher surplus / solar pushes actual price down.
        surplus = rng.uniform(500.0, 5000.0)
        solar = rng.uniform(0.0, 4000.0)
        demand50 = rng.uniform(5000.0, 9000.0)
        actual = max(0.0, a * fc + b - solar * 1e-5 + rng.gauss(0, 0.005))
        o = Observation(
            interval_time=interval_time,
            horizon_hours=horizon_hours,
            pd7day_forecast=fc,
            actual_rrp=actual,
            forecast_run_at=run_at,
            hour_of_day=hour_of_day,
            day_of_week=0,
            month=4,
            gas_forecast_tj=75.0,
            qni_mwflow=-150.0,
            qni_violation_degree=0.0,
            is_intervention=False,
        )
        obs.append(o)
        key = f"{interval_time}|{run_at}"
        stpasa_by_key[key] = StpasaFeatures(
            log_surplus=math.log1p(surplus),
            log_solar=math.log1p(solar),
            log_demand=math.log(max(demand50, 1.0)),
            poe_spread_n=(demand50 * 1.1 - demand50 * 0.9) / demand50,
            stpasa_run_at=_obs_iso(offset_days=-1, hour=3),
        )
    return obs, stpasa_by_key


def test_fit_ols_stage2_basic():
    """fit_ols_stage2 returns OlsModel with non-empty coef for in-band buckets."""
    engine = CalibrationEngine()
    # h24_48 bucket (in OLS band) and h48_96 bucket.
    # Use n=60 (>= OLS_MIN_OBS=50) to ensure buckets are fitted.
    obs1, sp1 = _make_stpasa_obs(n=60, horizon_hours=30.0, hour_of_day=17, seed=1)
    obs2, sp2 = _make_stpasa_obs(n=60, horizon_hours=60.0, hour_of_day=17, seed=2)
    observations = obs1 + obs2
    stpasa_by_key = {**sp1, **sp2}

    ols_models = engine.fit_ols_stage2(observations, stpasa_by_key)
    key1 = _bucket_key(30.0, 17)
    key2 = _bucket_key(60.0, 17)
    assert key1 in ols_models, f"missing bucket {key1}"
    assert key2 in ols_models, f"missing bucket {key2}"
    assert len(ols_models[key1].coef) >= 2, "expected fitted coef for h24_48"
    assert len(ols_models[key2].coef) >= 2, "expected fitted coef for h48_96"
    print("  PASS: fit_ols_stage2 basic")


def test_apply_with_stpasa_improves_high_surplus():
    """apply() with high-surplus/solar STPASA features shifts the point estimate."""
    engine = CalibrationEngine()
    obs, stpasa_by_key = _make_stpasa_obs(n=60, horizon_hours=36.0, hour_of_day=17, seed=3)
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)

    rf = RunFeatures(run_max_h6_rrp=0.2, run_mean_rrp=0.1, run_spread=0.05)
    high = StpasaFeatures(
        log_surplus=math.log1p(5000.0),
        log_solar=math.log1p(4000.0),
        log_demand=math.log(6000.0),
        poe_spread_n=0.2,
        stpasa_run_at=_obs_iso(offset_days=-1, hour=3),
    )
    iso_only = result.apply(0.15, horizon_hours=36.0, hour_of_day=17)
    with_stpasa = result.apply(
        0.15, horizon_hours=36.0, hour_of_day=17, stpasa=high, run_features=rf
    )
    # When the OLS bucket fitted, the source flips and the value can differ.
    key = _bucket_key(36.0, 17)
    if len(result.ols_models.get(key, OlsModel(bucket_key=key)).coef) >= 2:
        assert with_stpasa["calibrated_source"] == "isotonic+stpasa"
        assert with_stpasa["stpasa_run_at"] == high.stpasa_run_at
    else:
        assert with_stpasa["calibrated_source"] == "isotonic"
    assert iso_only["calibrated_source"] == "isotonic"
    print("  PASS: apply with stpasa high surplus")


def test_apply_stpasa_skipped_below_h22():
    """horizon < 22h must fall through to isotonic regardless of STPASA features."""
    engine = CalibrationEngine()
    obs, stpasa_by_key = _make_stpasa_obs(n=60, horizon_hours=30.0, hour_of_day=17, seed=4)
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)

    rf = RunFeatures(run_max_h6_rrp=0.2, run_mean_rrp=0.1, run_spread=0.05)
    sf = StpasaFeatures(
        log_surplus=8.0, log_solar=8.0, log_demand=9.0, poe_spread_n=0.1,
        stpasa_run_at=_obs_iso(offset_days=-1, hour=3),
    )
    out = result.apply(0.15, horizon_hours=20.0, hour_of_day=17, stpasa=sf, run_features=rf)
    assert out["calibrated_source"] != "isotonic+stpasa", out["calibrated_source"]
    assert "stpasa_run_at" not in out
    print("  PASS: stpasa skipped below h22")


def test_apply_stpasa_skipped_above_h120():
    """horizon > 120h must fall through to isotonic regardless of STPASA features."""
    engine = CalibrationEngine()
    obs, stpasa_by_key = _make_stpasa_obs(n=60, horizon_hours=36.0, hour_of_day=17, seed=5)
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)

    rf = RunFeatures(run_max_h6_rrp=0.2, run_mean_rrp=0.1, run_spread=0.05)
    sf = StpasaFeatures(
        log_surplus=8.0, log_solar=8.0, log_demand=9.0, poe_spread_n=0.1,
        stpasa_run_at=_obs_iso(offset_days=-1, hour=3),
    )
    out = result.apply(0.15, horizon_hours=130.0, hour_of_day=17, stpasa=sf, run_features=rf)
    assert out["calibrated_source"] != "isotonic+stpasa", out["calibrated_source"]
    assert "stpasa_run_at" not in out
    print("  PASS: stpasa skipped above h120")


def test_ols_serialisation_round_trip():
    """to_storage → from_storage preserves OlsModel coef, n_train, r2."""
    engine = CalibrationEngine()
    obs, stpasa_by_key = _make_stpasa_obs(n=50, horizon_hours=36.0, hour_of_day=17, seed=6)
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)

    stored = engine.to_storage(result)
    assert "ols_models" in stored
    restored = engine.from_storage(stored)

    for key, model in result.ols_models.items():
        assert key in restored.ols_models, f"missing ols bucket {key} after round-trip"
        assert restored.ols_models[key].coef == model.coef, "coef changed"
        assert restored.ols_models[key].n_train == model.n_train
        assert abs(restored.ols_models[key].r2 - model.r2) < 1e-9
    print("  PASS: ols serialisation round trip")


def test_from_storage_missing_ols_key():
    """from_storage on an old-format dict (no ols_models) yields empty dict, no error."""
    engine = CalibrationEngine()
    obs = _make_obs_batch(n=30, a=1.5, b=0.02, horizon_hours=36.0, hour_of_day=17)
    result = engine.fit(obs)
    stored = engine.to_storage(result)
    # Simulate an old install: strip the ols_models key entirely.
    stored.pop("ols_models", None)
    restored = engine.from_storage(stored)
    assert restored.ols_models == {}, "expected empty ols_models for legacy storage"
    print("  PASS: from_storage missing ols key")


def test_apply_stpasa_negative_ols_falls_back_to_isotonic():
    """When OLS predicts a non-positive value, apply() must return the isotonic
    result rather than clamping to 0.

    Regression test for the bug observed with h24_48__shoulder:
    OLS coef[0] (iso_calibrated coefficient) was negative (-1.879 in the
    wild), causing predictions of <=0 for typical forecast values.  Prior code
    returned ``calibrated: 0``; the correct behaviour is to fall back to the
    isotonic result and NOT emit ``calibrated_source: isotonic+stpasa``.
    """
    from custom_components.nem_pd7day.calibration_engine import (
        OlsModel,
        RunFeatures,
        StpasaFeatures,
        _bucket_key,
    )

    # Build a result with enough obs for isotonic but inject a broken OLS model
    # whose intercept alone drives predictions below zero.
    engine = CalibrationEngine()
    obs = _make_obs_batch(n=60, a=1.2, b=0.02, horizon_hours=36.0, hour_of_day=10)
    result = engine.fit(obs)

    # Inject an OLS model with a strongly negative iso_calibrated coefficient
    # (coef layout: [intercept, iso_cal, run_max_h6, run_mean, run_spread,
    #                 h_norm, log_surplus, log_solar, log_demand, poe_spread]).
    key = _bucket_key(36.0, 10)
    negative_ols = OlsModel(
        bucket_key=key,
        coef=[0.05, -2.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        n_train=15,
        r2=0.99,
    )
    result.ols_models[key] = negative_ols

    rf = RunFeatures(run_max_h6_rrp=0.18, run_mean_rrp=0.12, run_spread=0.04)
    sf = StpasaFeatures(
        log_surplus=0.3,
        log_solar=0.8,
        log_demand=0.5,
        poe_spread_n=0.1,
        stpasa_run_at="2026-06-16T05:00:00+10:00",
    )

    out = result.apply(
        0.15, horizon_hours=36.0, hour_of_day=10, stpasa=sf, run_features=rf
    )

    assert out["calibrated"] != 0.0, (
        f"calibrated must not be 0 when OLS predicts negative; got {out['calibrated']}"
    )
    assert out["calibrated"] > 0.0, (
        f"fallback isotonic result must be positive; got {out['calibrated']}"
    )
    assert out["calibrated_source"] != "isotonic+stpasa", (
        f"calibrated_source must not be 'isotonic+stpasa' on negative-OLS fallback; "
        f"got {out['calibrated_source']}"
    )
    assert "stpasa_run_at" not in out, (
        "stpasa_run_at must be absent when falling back to isotonic"
    )
    print(
        f"  PASS: negative OLS falls back to isotonic "
        f"(calibrated={out['calibrated']:.4f}, source={out['calibrated_source']})"
    )


def test_apply_stpasa_zero_ols_falls_back_to_isotonic():
    """An OLS prediction of exactly 0.0 must also trigger the isotonic fallback."""
    from custom_components.nem_pd7day.calibration_engine import (
        OlsModel,
        RunFeatures,
        StpasaFeatures,
        _bucket_key,
    )

    engine = CalibrationEngine()
    obs = _make_obs_batch(n=60, a=1.2, b=0.02, horizon_hours=36.0, hour_of_day=10)
    result = engine.fit(obs)

    key = _bucket_key(36.0, 10)
    # All-zero coefs: predict() returns exactly 0.0 regardless of features.
    zero_ols = OlsModel(
        bucket_key=key,
        coef=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        n_train=60,
        r2=0.0,
    )
    result.ols_models[key] = zero_ols

    rf = RunFeatures(run_max_h6_rrp=0.0, run_mean_rrp=0.0, run_spread=0.0)
    sf = StpasaFeatures(
        log_surplus=0.0,
        log_solar=0.0,
        log_demand=0.0,
        poe_spread_n=0.0,
        stpasa_run_at="2026-06-16T05:00:00+10:00",
    )

    out = result.apply(
        0.15, horizon_hours=36.0, hour_of_day=10, stpasa=sf, run_features=rf
    )

    assert out["calibrated"] != 0.0, (
        f"exact-zero OLS must fall back to isotonic; got calibrated={out['calibrated']}"
    )
    assert out["calibrated_source"] != "isotonic+stpasa"
    print(
        f"  PASS: zero OLS falls back to isotonic "
        f"(calibrated={out['calibrated']:.4f}, source={out['calibrated_source']})"
    )


def test_min_obs_is_fifty():
    """OLS_MIN_OBS must be 50 to prevent OLS over-fit with 9-feature models."""
    from custom_components.nem_pd7day.const import OLS_MIN_OBS
    assert OLS_MIN_OBS == 50, (
        f"OLS_MIN_OBS must be 50 (9-feature OLS rule-of-thumb guard); got {OLS_MIN_OBS}"
    )
    print(f"  PASS: OLS_MIN_OBS == {OLS_MIN_OBS}")


def test_ols_stage2_requires_min_obs_for_fit():
    """fit_ols_stage2() must return an empty-coef OlsModel for buckets with
    fewer than OLS_MIN_OBS observations (previously MIN_OBS=10 was too low
    for the 9-feature model and caused severe over-fit)."""
    from custom_components.nem_pd7day.const import OLS_MIN_OBS

    engine = CalibrationEngine()
    # Use exactly OLS_MIN_OBS - 1 observations: must NOT produce a fitted OLS model.
    obs, stpasa_by_key = _make_stpasa_obs(
        n=OLS_MIN_OBS - 1, horizon_hours=36.0, hour_of_day=17, seed=99
    )
    ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)
    for key, model in ols_models.items():
        assert len(model.coef) < 2, (
            f"Bucket {key!r} must not have a fitted OLS model with n < OLS_MIN_OBS ({OLS_MIN_OBS}); "
            f"got coef={model.coef}"
        )
    print(f"  PASS: fit_ols_stage2 skips buckets with n < OLS_MIN_OBS ({OLS_MIN_OBS})")


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
    # Spike input (isotonic, no more passthrough_high)
    test_spike_input_no_iso_model_returns_passthrough,
    test_spike_input_with_iso_model_returns_isotonic,
    test_spike_input_no_iso_model_passthrough,
    test_below_spike_threshold_uses_ols,
    test_spike_actuals_excluded_from_ols_buckets,
    test_spike_forecasts_excluded_from_ols_buckets,
    test_isotonic_mae_beats_raw_baseline,

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
    # P10/P50/P90 clamping
    test_p10_p90_never_outside_calibrated,
    test_p50_within_confidence_band,
    # Spike isotonic behaviour
    test_spike_input_uses_isotonic,
    test_spike_input_quantiles_none_when_isotonic,
    # STPASA OLS stage2
    test_fit_ols_stage2_basic,
    test_apply_with_stpasa_improves_high_surplus,
    test_apply_stpasa_skipped_below_h22,
    test_apply_stpasa_skipped_above_h120,
    test_ols_serialisation_round_trip,
    test_from_storage_missing_ols_key,
    # OLS stage2 negative-prediction fallback
    test_apply_stpasa_negative_ols_falls_back_to_isotonic,
    test_apply_stpasa_zero_ols_falls_back_to_isotonic,
    test_min_obs_is_fifty,
    test_ols_stage2_requires_min_obs_for_fit,
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


# ── Guard: fixture observations must stay inside the training window ──────────
# These tests exist because the suite silently rotted once before: fixture
# observations were pinned to a fixed calendar date (2026-04-13), which aged
# past OBSERVATION_WINDOW_DAYS on 2026-07-12.  Every observation was then
# discarded by fit(), all buckets fitted empty, and 17 tests failed with
# confusing "expected isotonic, got passthrough" assertions rather than
# pointing at the real cause.  These guards fail loudly and specifically.

def test_fixture_observations_are_inside_training_window():
    """Fixture dates must be recent enough for fit() to train on them."""
    window_days = _engine_mod.OBSERVATION_WINDOW_DAYS
    now = datetime.now(NEM_TZ)
    cutoff = now - timedelta(days=window_days)

    for label, obs in (
        ("make_obs", [make_obs(0.10, 0.20)]),
        ("_make_obs_batch", _make_obs_batch(
            n=5, a=2.0, b=0.02, horizon_hours=18.0, hour_of_day=12
        )),
    ):
        for o in obs:
            interval = datetime.fromisoformat(o.interval_time)
            age_days = (now - interval).days
            assert interval >= cutoff, (
                f"{label}() produced an observation dated {o.interval_time} "
                f"({age_days}d old), outside the engine's "
                f"{window_days}-day training window. Fixture dates must be "
                f"anchored to datetime.now(), not a fixed calendar date."
            )
            run_at = datetime.fromisoformat(o.forecast_run_at)
            assert run_at <= interval, (
                f"{label}() forecast_run_at {o.forecast_run_at} must not be "
                f"after interval_time {o.interval_time}"
            )


def test_fixture_anchor_is_not_in_the_future():
    """The anchor must be in the past, or horizons/actuals are nonsensical."""
    now = datetime.now(NEM_TZ)
    assert _OBS_ANCHOR < now, (
        f"Fixture anchor {_OBS_ANCHOR.isoformat()} is in the future relative "
        f"to {now.isoformat()}"
    )


def test_stale_observations_yield_empty_buckets():
    """
    Document the failure mode the guards above protect against: observations
    older than OBSERVATION_WINDOW_DAYS are discarded by fit(), producing empty
    buckets and a "passthrough" result with n_obs=0.
    """
    window_days = _engine_mod.OBSERVATION_WINDOW_DAYS
    stale_day = datetime.now(NEM_TZ) - timedelta(days=window_days + 30)

    stale_obs = [
        Observation(
            interval_time=stale_day.replace(
                hour=12, minute=0, second=0, microsecond=0
            ).isoformat(),
            horizon_hours=18.0,
            pd7day_forecast=fc,
            actual_rrp=2.2 * fc + 0.025,
            forecast_run_at=(stale_day - timedelta(days=1)).replace(
                hour=3, minute=30, second=0, microsecond=0
            ).isoformat(),
            hour_of_day=12,
            day_of_week=stale_day.weekday(),
            month=stale_day.month,
            gas_forecast_tj=75.0,
            qni_mwflow=-150.0,
            qni_violation_degree=0.0,
            is_intervention=False,
        )
        for fc in [0.05 + 0.0025 * i for i in range(80)]
    ]

    result = CalibrationEngine().fit(stale_obs)
    out = result.apply(0.10, horizon_hours=18.0, hour_of_day=12)

    assert out["calibrated_source"] == "passthrough"
    assert out["n_obs"] == 0
    assert out["calibrated"] == 0.10, "stale fit must pass the raw value through"


# ── StpasaFeatures skips incomplete intervals (issue #43) ──────────────────

def test_stpasa_features_from_interval_returns_none_when_an_input_is_missing():
    """
    from_interval must report an incomplete interval as None rather than
    deriving features from a substituted zero. Issue #43.
    """
    from custom_components.nem_pd7day.calibration_engine import StpasaFeatures
    from custom_components.nem_pd7day.stpasa_client import StpasaInterval

    base = dict(
        interval_datetime="2026-06-17T04:30:00+10:00",
        run_datetime="2026-06-16T12:00:00+10:00",
        demand10=5500.0,
        demand50=6000.0,
        demand90=6500.0,
        surpluscapacity=1200.0,
        ss_solar_uigf=800.0,
        ss_wind_uigf=400.0,
    )

    assert StpasaFeatures.from_interval(StpasaInterval(**base)) is not None

    for field_name in (
        "demand10", "demand50", "demand90", "surpluscapacity", "ss_solar_uigf"
    ):
        incomplete = dict(base)
        incomplete[field_name] = None
        assert StpasaFeatures.from_interval(StpasaInterval(**incomplete)) is None, (
            f"a missing {field_name} must yield None, not derived features"
        )


def test_stpasa_features_tolerate_missing_wind_and_genuine_zeros():
    """
    Wind is not an OLS input, so its absence must not drop the interval, and
    a real zero must produce features rather than being read as missing.
    """
    import math
    from custom_components.nem_pd7day.calibration_engine import StpasaFeatures
    from custom_components.nem_pd7day.stpasa_client import StpasaInterval

    feats = StpasaFeatures.from_interval(StpasaInterval(
        interval_datetime="2026-06-17T04:30:00+10:00",
        run_datetime="2026-06-16T12:00:00+10:00",
        demand10=5500.0,
        demand50=6000.0,
        demand90=6500.0,
        surpluscapacity=0.0,
        ss_solar_uigf=0.0,
        ss_wind_uigf=None,
    ))

    assert feats is not None
    assert feats.log_solar == 0.0
    assert feats.log_surplus == 0.0
    assert abs(feats.log_demand - math.log(6000.0)) < 1e-9
