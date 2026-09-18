"""
CalibrationEngine, stage 1: bucket routing, the OLS and quantile fits, the
isotonic apply path, the published band, and negative prices.

Stage 2 (fit_ols_stage2 and the STPASA override in CalibrationResult.apply)
is tests/test_calibration_stage2.py; the STPASA serving-side feature lookup
is tests/test_calibration_inputs.py.

Run with:  python -m pytest tests/test_calibration_engine.py -v
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from support import NEM_TZ, load_chain

_const, _nem_time, _engine_mod = load_chain("const", "nem_time", "calibration_engine")

from custom_components.nem_pd7day.calibration_engine import (  # noqa: E402
    DECAY_LAMBDA,
    ISO_FEATURE_KEY,
    MIN_OBS,
    REGION_COORDS,
    SOURCE_ISOTONIC_BELOW_DOMAIN,
    BucketModel,
    CalibrationEngine,
    IsotonicRegression,
    LinearCoeff,
    Observation,
    QuantileCoeff,
    _bucket_key,
    _clamp_band,
    _horizon_label,
    _ols,
    _ols_metrics,
    _quantile_regression,
    _tod_label,
    _tod_label_solar,
    all_bucket_keys,
)
from custom_components.nem_pd7day.const import MARKET_PRICE_FLOOR  # noqa: E402

# ── Fixture date anchoring ────────────────────────────────────────────────────
# fit() only trains on observations newer than OBSERVATION_WINDOW_DAYS (90).
# Fixture dates are relative to "now": a hard-coded 2026-04-13 anchor aged out
# of the window on 2026-07-12, after which every observation was silently
# dropped, all buckets fitted empty, and 17 tests failed with "expected
# isotonic, got passthrough". test_fixture_observations_are_inside_training_window
# guards the anchor.
_OBS_ANCHOR = datetime.now(NEM_TZ) - timedelta(days=2)


def _obs_day(offset_days: int = 0) -> datetime:
    """The fixture base date, optionally offset, at midnight NEM time."""
    return (_OBS_ANCHOR + timedelta(days=offset_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _obs_iso(offset_days: int = 0, hour: int = 0, minute: int = 0) -> str:
    return _obs_day(offset_days).replace(hour=hour, minute=minute).isoformat()


def make_obs(
    forecast: float,
    actual: float,
    horizon_hours: float = 12.0,
    hour_of_day: int = 14,
    is_intervention: bool = False,
) -> Observation:
    """One observation whose interval_time matches hour_of_day, inside the window."""
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
    """n (x, y) pairs where y = a*x + b + noise."""
    rng = random.Random(seed)
    xs = [rng.uniform(0.05, 0.30) for _ in range(n)]
    return [(x, a * x + b + rng.gauss(0, noise)) for x in xs]


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
    for _ in range(n):
        fc = rng.uniform(0.05, 0.25)
        obs.append(make_obs(fc, a * fc + b + rng.gauss(0, noise), horizon_hours, hour_of_day))
    return obs


def _windowed_obs(days: int, seed: int) -> list[Observation]:
    """One observation per day for ``days`` days back from now, all h12_24 / solar."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    obs = []
    for day_offset in range(days):
        obs_date = now - timedelta(days=days - 1 - day_offset)  # oldest first
        iso_str = obs_date.strftime("%Y-%m-%dT") + "12:00:00+10:00"
        fc = rng.uniform(0.05, 0.25)
        obs.append(Observation(
            interval_time=iso_str,
            horizon_hours=18.0,
            pd7day_forecast=fc,
            actual_rrp=2.0 * fc + 0.01 + rng.gauss(0, 0.003),
            forecast_run_at=iso_str,
            hour_of_day=12,
            day_of_week=0,
            month=4,
            gas_forecast_tj=None,
            qni_mwflow=None,
            qni_violation_degree=None,
            is_intervention=False,
        ))
    return obs


# ── Bucket routing ────────────────────────────────────────────────────────────

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


def test_tod_labels():
    # Three labels: peak (16-21), solar (10-16), shoulder (everything else).
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


def test_all_bucket_keys():
    keys = all_bucket_keys()
    assert len(keys) == 24   # 6 horizons x 4 tod buckets
    assert "h00_06__peak" in keys
    assert "h96plus__shoulder" in keys
    assert "h12_24__solar" in keys
    assert "h06_12__morning_ramp" in keys
    assert not any("offpeak" in k for k in keys)


# ── Solar-elevation time-of-day labels ───────────────────────────────────────

@pytest.mark.parametrize("region", sorted(REGION_COORDS))
def test_tod_label_solar_fixed_windows_hold_in_every_region(region):
    """Peak 16-21 is hardcoded whatever the elevation; noon is solar and
    midnight shoulder everywhere on the NEM."""
    for hour in (16, 17, 18, 19, 20):
        dt = datetime(2026, 1, 15, hour, 0, tzinfo=NEM_TZ)
        assert _tod_label_solar(dt, region, "fallback") == "peak", (hour, region)
    noon_march = datetime(2026, 3, 15, 12, 0, tzinfo=NEM_TZ)
    assert _tod_label_solar(noon_march, region, "fallback") == "solar", region
    midnight_june = datetime(2026, 6, 15, 0, 0, tzinfo=NEM_TZ)
    assert _tod_label_solar(midnight_june, region, "fallback") == "shoulder", region


@pytest.mark.parametrize("dt, region, expected", [
    (datetime(2026, 1, 15, 8, 0), "QLD1", "solar"),         # Brisbane summer 8am
    (datetime(2026, 7, 15, 7, 0), "TAS1", "shoulder"),      # Hobart winter 7am, sun very low
    (datetime(2026, 4, 15, 7, 0), "QLD1", "morning_ramp"),  # Brisbane April 7am, el ~11 deg
    (datetime(2026, 4, 15, 6, 0), "QLD1", "shoulder"),      # Brisbane April 6am, el ~ -2 deg
    (datetime(2026, 4, 15, 23, 0), "QLD1", "shoulder"),     # Brisbane 11pm
])
def test_tod_label_solar_by_elevation(dt, region, expected):
    # raw_label is not "shoulder", so a shoulder answer cannot come from the
    # unknown-region fallback.
    assert _tod_label_solar(dt.replace(tzinfo=NEM_TZ), region, "fallback") == expected


def test_tod_label_solar_unknown_region_fallback():
    dt = datetime(2026, 3, 15, 12, 0, tzinfo=NEM_TZ)
    assert _tod_label_solar(dt, "UNKNOWN", "my_fallback") == "my_fallback"


def test_region_coords_all_regions():
    assert set(REGION_COORDS) == {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}
    for region, (lat, lon) in REGION_COORDS.items():
        assert -50 < lat < -20, f"{region} latitude {lat} out of range"
        assert 130 < lon < 160, f"{region} longitude {lon} out of range"


# ── OLS ───────────────────────────────────────────────────────────────────────

def test_ols_perfect_fit():
    a, b = _ols(_pairs(50, a=1.8, b=0.02, noise=0.0))
    assert abs(a - 1.8) < 1e-6, f"a={a}"
    assert abs(b - 0.02) < 1e-6, f"b={b}"


def test_ols_noisy_fit():
    a, b = _ols(_pairs(200, a=2.1, b=0.03, noise=0.01, seed=7))
    assert abs(a - 2.1) < 0.05, f"a={a} too far from 2.1"
    assert abs(b - 0.03) < 0.02, f"b={b} too far from 0.03"


@pytest.mark.parametrize("weighted", [False, True])
def test_ols_passthrough_insufficient_data(weighted):
    """(1, 0) passthrough when n < MIN_OBS, with or without weights."""
    pairs = _pairs(MIN_OBS - 1, a=2.0, b=0.1)
    weights = [1.0] * len(pairs) if weighted else None
    a, b = _ols(pairs, weights=weights)
    assert a == 1.0 and b == 0.0


def test_ols_metrics():
    pairs = _pairs(50, a=1.5, b=0.01, noise=0.0)
    a, b = _ols(pairs)
    mae, rmse = _ols_metrics(pairs, a, b)
    assert mae < 1e-8, f"mae={mae}"
    assert rmse < 1e-8, f"rmse={rmse}"


def test_weighted_ols_uniform_weights_match_unweighted():
    pairs = _pairs(50, a=1.8, b=0.02, noise=0.005)
    a_uw, b_uw = _ols(pairs)
    a_w, b_w = _ols(pairs, weights=[1.0] * len(pairs))
    assert abs(a_uw - a_w) < 1e-6, f"Weighted a={a_w} != unweighted a={a_uw}"
    assert abs(b_uw - b_w) < 1e-6, f"Weighted b={b_w} != unweighted b={b_uw}"


def test_weighted_ols_recent_obs_higher_weight():
    """Old (a=1.0, 80 days) against recent (a=2.0, 5 days): the fit follows the recent."""
    rng = random.Random(42)
    pairs, weights = [], []
    for _ in range(30):
        x = rng.uniform(0.05, 0.25)
        pairs.append((x, 1.0 * x))
        weights.append(math.exp(-DECAY_LAMBDA * 80))
    for _ in range(30):
        x = rng.uniform(0.05, 0.25)
        pairs.append((x, 2.0 * x))
        weights.append(math.exp(-DECAY_LAMBDA * 5))
    a, _b = _ols(pairs, weights=weights)
    assert a > 1.5, f"Weighted OLS a={a} should be > 1.5 (closer to recent a=2.0)"


def test_weighted_ols_decay_constant():
    """weight = exp(-DECAY_LAMBDA * days_ago): a 21 day half-life, ~0 at 90 days."""
    w0 = math.exp(-DECAY_LAMBDA * 0)
    w21 = math.exp(-DECAY_LAMBDA * 21)
    w90 = math.exp(-DECAY_LAMBDA * 90)
    assert abs(w0 - 1.0) < 1e-10
    assert abs(w21 - 0.5) < 0.02, f"Weight at day 21 (half-life) should be ~0.5, got {w21}"
    assert w90 < 0.06, f"Weight at day 90 should be < 0.06, got {w90}"


# ── Quantile regression ───────────────────────────────────────────────────────

def test_quantile_regression_median():
    """For symmetric noise, P50 approximates OLS."""
    pairs = _pairs(200, a=1.8, b=0.02, noise=0.01, seed=1)
    a_ols, _ = _ols(pairs)
    a_q50, _, pl = _quantile_regression(pairs, 0.5)
    assert abs(a_q50 - a_ols) < 0.1, f"P50 a={a_q50} vs OLS a={a_ols}"
    assert pl < 0.02, f"pinball_loss={pl} unexpectedly high"


def test_quantile_regression_ordering():
    """P10 <= P50 <= P90 for positive x."""
    pairs = _pairs(100, a=2.0, b=0.01, noise=0.03, seed=3)
    a10, b10, _ = _quantile_regression(pairs, 0.1)
    a50, b50, _ = _quantile_regression(pairs, 0.5)
    a90, b90, _ = _quantile_regression(pairs, 0.9)
    for x in [0.05, 0.10, 0.15, 0.20, 0.25]:
        p10, p50, p90 = a10 * x + b10, a50 * x + b50, a90 * x + b90
        assert p10 <= p50 + 0.001, f"x={x}: P10={p10:.4f} > P50={p50:.4f}"
        assert p50 <= p90 + 0.001, f"x={x}: P50={p50:.4f} > P90={p90:.4f}"


def test_quantile_regression_asymmetric_noise():
    """Right-skewed noise (spikes): P90 well above P10."""
    rng = random.Random(42)
    pairs = []
    for _ in range(300):
        x = rng.uniform(0.05, 0.20)
        noise = rng.expovariate(10) * 0.5 if rng.random() < 0.15 else rng.gauss(0, 0.005)
        pairs.append((x, 1.5 * x + 0.01 + noise))
    a90, b90, _ = _quantile_regression(pairs, 0.9)
    a10, b10, _ = _quantile_regression(pairs, 0.1)
    x = 0.15
    spread = (a90 * x + b90) - (a10 * x + b10)
    assert spread > 0.01, f"spread={spread:.4f}: quantile bands too narrow for spikey data"


def test_quantile_passthrough():
    a, b, pl = _quantile_regression(_pairs(MIN_OBS - 1, a=2.0, b=0.01), 0.9)
    assert a == 1.0 and b == 0.0
    assert math.isinf(pl)


def _right_skewed_pairs(n: int = 400, seed: int = 103) -> list[tuple[float, float]]:
    """y = x plus gaussian noise, with a lognormal spike on 15% of rows: the
    shape on which an expectile fit and a quantile fit disagree most."""
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
    """Issue #103: about tau of the observations fall below the tau line.
    The sign-only IRLS weights this replaced converged to the expectile and
    put ~23% under the P10 line and ~60% under the P50 line."""
    pairs = _right_skewed_pairs()
    a, b, _ = _quantile_regression(pairs, quantile)
    below = _fraction_below(pairs, a, b)
    assert abs(below - quantile) < 0.04, (
        f"tau={quantile}: {below:.3f} of observations fall below the fitted "
        f"line, expected about {quantile}"
    )


def test_quantile_regression_expectile_weights_would_fail_coverage():
    """Guard that the coverage test is discriminating: the pre-#103 weighting
    (sign-only, no 1/|r| divisor) misses the nominal level by far more than
    the tolerance on the same fixture."""
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
    pairs = _right_skewed_pairs(n=200, seed=7)
    a0, b0, pl0 = _quantile_regression(pairs, 0.9)
    a1, b1, pl1 = _quantile_regression(pairs, 0.9, weights=[0.37] * len(pairs))
    assert abs(a0 - a1) < 1e-6 and abs(b0 - b1) < 1e-6
    assert abs(pl0 - pl1) < 1e-6


def test_quantile_regression_weights_shift_the_fit():
    """Weighting one half of the sample heavily moves the median line to it."""
    rng = random.Random(11)
    low = [(x, x + 0.00 + rng.gauss(0, 0.003)) for x in (rng.uniform(0.05, 0.25) for _ in range(150))]
    high = [(x, x + 0.05 + rng.gauss(0, 0.003)) for x in (rng.uniform(0.05, 0.25) for _ in range(150))]
    pairs = low + high
    _, b_low, _ = _quantile_regression(pairs, 0.5, weights=[1.0] * 150 + [0.01] * 150)
    _, b_high, _ = _quantile_regression(pairs, 0.5, weights=[0.01] * 150 + [1.0] * 150)
    assert b_high - b_low > 0.03, f"weights did not move the median line ({b_low=}, {b_high=})"


# ── Engine fit and apply ──────────────────────────────────────────────────────

def test_engine_fit_applies_correctly():
    """A fitted bucket calibrates closer to the actual than the raw forecast."""
    observations = _make_obs_batch(n=80, a=2.2, b=0.025, horizon_hours=18.0, hour_of_day=12, noise=0.005)
    result = CalibrationEngine().fit(observations)
    test_forecast = 0.10
    true_actual = 2.2 * test_forecast + 0.025
    calibrated = result.apply(test_forecast, horizon_hours=18.0, hour_of_day=12)
    assert calibrated["calibrated_source"] == "isotonic"
    assert calibrated["p10"] is not None and calibrated["p90"] is not None
    raw_error = abs(test_forecast - true_actual)
    cal_error = abs(calibrated["calibrated"] - true_actual)
    assert cal_error < raw_error, f"Calibrated error {cal_error:.4f} should be below raw {raw_error:.4f}"
    assert calibrated["p10"] < calibrated["p90"]


def test_engine_intervention_skipped():
    obs = [make_obs(0.10, 0.30, is_intervention=True) for _ in range(50)]
    out = CalibrationEngine().fit(obs).apply(0.10, horizon_hours=12.0, hour_of_day=14)
    assert out["calibrated_source"] == "passthrough"
    assert out["calibrated"] == 0.10


def test_engine_passthrough_below_min_obs():
    obs = _make_obs_batch(n=MIN_OBS - 1, a=2.5, b=0.05, horizon_hours=12.0, hour_of_day=12)
    out = CalibrationEngine().fit(obs).apply(0.10, horizon_hours=12.0, hour_of_day=12)
    assert out["calibrated_source"] == "passthrough"
    assert out["n_obs"] == MIN_OBS - 1


def test_engine_serialisation_roundtrip():
    """to_storage / from_storage preserves the quantile coefficients.

    The isotonic model is not JSON-serialisable and is NOT persisted: after
    from_storage, apply() is passthrough until the next engine.fit(). Storage
    is a warm start for the OLS diagnostics and quantile intervals only.
    """
    engine = CalibrationEngine()
    result = engine.fit(_make_obs_batch(n=50, a=1.9, b=0.03, horizon_hours=8.0, hour_of_day=17))
    restored = engine.from_storage(engine.to_storage(result))

    test_price = 0.12
    orig = result.apply(test_price, horizon_hours=8.0, hour_of_day=17)
    rest = restored.apply(test_price, horizon_hours=8.0, hour_of_day=17)

    assert orig["calibrated_source"] == "isotonic"
    assert orig["calibrated"] != round(test_price, 6), "Isotonic must alter the forecast"
    assert rest["calibrated_source"] == "passthrough"
    assert rest["calibrated"] == round(test_price, 6)

    assert orig["p10"] is not None and orig["p90"] is not None
    assert rest["p10"] is not None and rest["p90"] is not None
    assert math.isclose(orig["p10"], rest["p10"], rel_tol=1e-6)
    assert math.isclose(orig["p90"], rest["p90"], rel_tol=1e-6)


def test_engine_multi_bucket_independence():
    obs_solar = _make_obs_batch(n=60, a=2.5, b=0.01, horizon_hours=18.0, hour_of_day=12)
    obs_peak = _make_obs_batch(n=60, a=3.5, b=0.02, horizon_hours=18.0, hour_of_day=17)
    result = CalibrationEngine().fit(obs_solar + obs_peak)
    solar_cal = result.apply(0.10, horizon_hours=18.0, hour_of_day=12)
    peak_cal = result.apply(0.10, horizon_hours=18.0, hour_of_day=17)
    assert solar_cal["calibrated"] < peak_cal["calibrated"], (solar_cal, peak_cal)


def test_engine_weighted_fit_produces_result():
    """fit() with a region routes by solar elevation and still fills the bucket."""
    result = CalibrationEngine().fit(
        _make_obs_batch(n=50, a=2.0, b=0.01, horizon_hours=18.0, hour_of_day=12), region="QLD1"
    )
    assert result.observations_in_window == 50
    assert result.total_observations == 50
    bucket = result.get_bucket(horizon_hours=18.0, hour_of_day=12)
    assert bucket.ols.n == 50
    assert bucket.ols.a > 0


# ── Slope clamps after the fit ────────────────────────────────────────────────

def test_negative_ols_slope_clamped():
    """A negative OLS slope would invert the forecast; it is clamped to 0."""
    rng = random.Random(123)
    obs = []
    for _ in range(60):
        fc = rng.uniform(0.05, 0.25)
        obs.append(make_obs(fc, -0.5 * fc + 0.20 + rng.gauss(0, 0.002), horizon_hours=30.0, hour_of_day=21))
    bucket = CalibrationEngine().fit(obs).get_bucket(horizon_hours=30.0, hour_of_day=21)
    assert bucket.ols.a >= 0.0, f"OLS slope a={bucket.ols.a} is negative"


def test_quantile_slopes_ordered_after_irls():
    """Heavily skewed noise must not leave q10_a > q90_a."""
    rng = random.Random(77)
    obs = []
    for _ in range(80):
        fc = rng.uniform(0.05, 0.25)
        noise = rng.uniform(0.05, 0.15) if rng.random() < 0.2 else rng.gauss(0, 0.003)
        obs.append(make_obs(fc, 1.2 * fc + 0.01 + noise, horizon_hours=9.0, hour_of_day=12))
    bucket = CalibrationEngine().fit(obs).get_bucket(horizon_hours=9.0, hour_of_day=12)
    assert bucket.q10.a <= bucket.q50.a <= bucket.q90.a, (bucket.q10.a, bucket.q50.a, bucket.q90.a)


def test_quantile_slopes_clamped_to_zero():
    rng = random.Random(999)
    obs = []
    for _ in range(80):
        fc = rng.uniform(0.05, 0.25)
        obs.append(make_obs(fc, -0.3 * fc + 0.15 + rng.gauss(0, 0.002), horizon_hours=30.0, hour_of_day=20))
    bucket = CalibrationEngine().fit(obs).get_bucket(horizon_hours=30.0, hour_of_day=20)
    for q in (bucket.q10, bucket.q50, bucket.q90):
        assert q.a >= 0.0, f"quantile {q.quantile} slope a={q.a} is negative"


# ── Band ordering and containment on the stage-1 path ────────────────────────

def test_p10_p90_never_outside_calibrated():
    """P10 <= calibrated <= P90 on every fitted bucket of a real fit."""
    obs = _make_obs_batch(n=40, a=0.75, b=0.0, horizon_hours=4.0, hour_of_day=10, noise=0.01, seed=42)
    result = CalibrationEngine().fit(obs, region="QLD1")
    checked = 0
    violations = []
    for key, model in result.models.items():
        if model.ols.n < MIN_OBS:
            continue
        for x_test in [0.05, 0.10, 0.15, 0.20]:
            out = model.apply_all(x_test)
            # 0.05 can sit just below the fitted domain; both paths clamp.
            assert out["calibrated_source"] in ("isotonic", SOURCE_ISOTONIC_BELOW_DOMAIN), out
            checked += 1
            cal, p10, p90 = out["calibrated"], out["p10"], out["p90"]
            if p10 is not None and p10 > cal + 1e-9:
                violations.append(f"{key} x={x_test}: p10={p10} > cal={cal}")
            if p90 is not None and p90 < cal - 1e-9:
                violations.append(f"{key} x={x_test}: p90={p90} < cal={cal}")
    assert checked, "the fit must populate at least one bucket"
    assert not violations, f"P10/P90 violations: {violations}"


def test_p50_within_confidence_band():
    """P10 <= P50 <= P90 even when the fitted intercepts cross.

    IRLS orders the slopes but not the intercepts, so at small x the P10 line
    (high intercept, low slope) can sit above P90. apply_all enforces the
    full ordering.
    """
    model = BucketModel(bucket_key="h00_06__solar")
    model.ols = LinearCoeff(a=1.0, b=0.0, n=30, mae=0.01, rmse=0.015)
    model.q10 = QuantileCoeff(0.1, a=0.5, b=0.10, n=30)
    model.q50 = QuantileCoeff(0.5, a=0.8, b=0.06, n=30)
    model.q90 = QuantileCoeff(0.9, a=1.2, b=0.01, n=30)
    iso = IsotonicRegression()
    iso.fit(np.array([0.05, 0.10, 0.15, 0.20, 0.25]), np.array([0.08, 0.12, 0.16, 0.20, 0.24]))
    model.iso_model = iso

    violations = []
    # At x=0.05: P10 = 0.125, P90 = 0.07, P50 = 0.10, all crossed without the clamp.
    for x_test in [0.05, 0.08, 0.10, 0.15, 0.20, 0.25]:
        out = model.apply_all(x_test)
        assert out["calibrated_source"] == "isotonic"
        p10, p50, p90 = out["p10"], out["p50"], out["p90"]
        if p10 > p50 + 1e-9:
            violations.append(f"x={x_test}: p10={p10} > p50={p50}")
        if p50 > p90 + 1e-9:
            violations.append(f"x={x_test}: p50={p50} > p90={p90}")
    assert not violations, f"P10/P50/P90 ordering violated with crossed intercepts: {violations}"


@pytest.mark.parametrize("x", [0.1, 0.0, -0.02, -0.15])
def test_passthrough_band_is_left_unclamped_on_purpose(x):
    """With no isotonic model the band is not clamped to the raw forecast.

    The one path where the published value may sit outside its own band, and
    it is intentional: the point estimate is the un-calibrated raw forecast,
    the quantile fits survive serialisation and the isotonic model does not,
    so between a restart and the next engine.fit() a fitted p10 above the raw
    forecast is the calibration saying the forecast is too low. Clamping
    would erase that and break the warm-start guarantee
    (test_engine_serialisation_roundtrip). Pinned so a later reading of issue
    #69 does not extend the clamp here. A negative raw forecast takes this
    path too: without a model there is no domain, so no below-domain clip.
    The stage-2 feature on this path is the raw forecast (#85).
    """
    bucket = BucketModel(
        bucket_key=_bucket_key(36.0, 17),
        q10=QuantileCoeff(0.1, a=1.0, b=0.05, n=MIN_OBS * 10),
        q50=QuantileCoeff(0.5, a=1.0, b=0.10, n=MIN_OBS * 10),
        q90=QuantileCoeff(0.9, a=1.0, b=0.15, n=MIN_OBS * 10),
    )
    assert bucket.domain_min is None
    out = bucket.apply_all(x)
    assert out["calibrated_source"] == "passthrough"
    assert out["calibrated"] == x
    assert out[ISO_FEATURE_KEY] == x
    assert (out["p10"], out["p50"], out["p90"]) == (
        round(x + 0.05, 6), round(x + 0.10, 6), round(x + 0.15, 6)
    ), f"the fitted lines must be published as fitted, got {out}"
    assert out["calibrated"] < out["p10"], "this is the tolerated exception"
    assert out["p10"] <= out["p50"] <= out["p90"]


# ── Spike inputs and spike observations ───────────────────────────────────────

@pytest.mark.parametrize("raw", [3.50, 8.999])
def test_spike_input_no_iso_model_returns_passthrough(raw):
    """raw >= SPIKE_THRESHOLD with no iso_model (< MIN_OBS) is plain passthrough:
    the passthrough_high source no longer exists."""
    model = BucketModel(
        bucket_key="h12_24__peak",
        ols=LinearCoeff(a=1.5, b=0.02, n=100, mae=0.01, rmse=0.02),
        q10=QuantileCoeff(quantile=0.1, a=1.2, b=0.01, n=100),
        q50=QuantileCoeff(quantile=0.5, a=1.5, b=0.02, n=100),
        q90=QuantileCoeff(quantile=0.9, a=1.8, b=0.03, n=100),
    )
    assert model.iso_model is None
    out = model.apply_all(raw)
    assert out["calibrated"] == round(raw, 6)
    assert out["calibrated_source"] == "passthrough"


def test_spike_input_with_iso_model_clips_to_the_training_maximum():
    """A spike input goes through isotonic with out_of_bounds='clip': the
    result is the training-range maximum, well below the raw spike, with the
    quantile band and n_obs of the fitted bucket."""
    obs = _make_obs_batch(n=60, a=1.5, b=0.02, horizon_hours=18.0, hour_of_day=17)
    result = CalibrationEngine().fit(obs).apply(8.999, horizon_hours=18.0, hour_of_day=17)
    assert result["calibrated_source"] == "isotonic"
    cal_val = result["calibrated"]
    assert isinstance(cal_val, float)
    assert 0.0 <= cal_val < 3.0, f"Isotonic clip should map the spike to the training max, got {cal_val}"
    assert result["p10"] is not None and result["p90"] is not None
    assert result["n_obs"] >= 60


@pytest.mark.parametrize("spike_field", ["actual", "forecast"])
def test_spike_observations_excluded_from_fit(spike_field):
    """Observations with actual_rrp or pd7day_forecast >= SPIKE_THRESHOLD never
    enter a bucket. Spike actuals follow a different distribution and collapse
    the slope; spike forecasts are high-leverage x-values that do the same
    even when the actual stayed moderate (spike forecast, no spike)."""
    rng = random.Random(555 if spike_field == "actual" else 777)
    normal = []
    for _ in range(60):
        fc = rng.uniform(0.05, 0.25)
        normal.append(make_obs(fc, 2.0 * fc + 0.01 + rng.gauss(0, 0.003), horizon_hours=18.0, hour_of_day=12))
    spikes = []
    for _ in range(15):
        if spike_field == "actual":
            spikes.append(make_obs(rng.uniform(0.05, 0.25), rng.uniform(8.0, 15.0), horizon_hours=18.0, hour_of_day=12))
        else:
            spikes.append(make_obs(rng.uniform(3.0, 15.0), rng.uniform(0.05, 0.30), horizon_hours=18.0, hour_of_day=12))
    result = CalibrationEngine().fit(normal + spikes)
    bucket = result.get_bucket(horizon_hours=18.0, hour_of_day=12)
    assert bucket.ols.n == 60, f"spike observations leaked into the fit: n={bucket.ols.n}"
    assert bucket.ols.a > 0.1, f"OLS slope a={bucket.ols.a} collapsed"
    assert abs(bucket.ols.a - 2.0) < 0.3, f"OLS slope a={bucket.ols.a} too far from 2.0"
    assert result.total_observations == 60


def test_isotonic_mae_beats_raw_baseline():
    """Isotonic calibration beats the raw forecast on a held-out set of a
    piecewise-monotone relationship that flattens at higher forecasts, the
    shape observed in QLD1 PD7DAY at h24_48+."""
    rng = random.Random(42)

    def make_actual(fc: float) -> float:
        if fc < 0.05:
            base = 0.80 * fc + 0.010
        elif fc < 0.15:
            base = 0.40 * fc + 0.030
        else:
            base = 0.20 * fc + 0.060
        return max(base + rng.gauss(0, 0.003), 0.0)

    forecasts = [rng.uniform(0.01, 0.25) for _ in range(60)]
    all_obs = [make_obs(fc, make_actual(fc), horizon_hours=36.0, hour_of_day=17) for fc in forecasts]
    train_obs, test_obs = all_obs[:48], all_obs[48:]
    result = CalibrationEngine().fit(train_obs)
    mae_raw = sum(abs(o.actual_rrp - o.pd7day_forecast) for o in test_obs) / len(test_obs)
    mae_cal = sum(
        abs(o.actual_rrp - result.apply(o.pd7day_forecast, o.horizon_hours, o.hour_of_day)["calibrated"])
        for o in test_obs
    ) / len(test_obs)
    assert mae_cal < mae_raw, f"Isotonic MAE {mae_cal:.4f} should be < raw MAE {mae_raw:.4f}"
    assert mae_cal < 0.035, f"Calibrated MAE {mae_cal:.4f} exceeds the quality gate 0.035"


# ── Rolling observation window ────────────────────────────────────────────────

def test_rolling_window_filters_old_observations_without_trimming_the_input():
    """Of 100 daily observations only the most recent ~90 are fitted; the
    input list is a fit-time filter's input, not mutated or trimmed."""
    all_obs = _windowed_obs(days=100, seed=42)
    result = CalibrationEngine().fit(all_obs)
    assert len(all_obs) == 100, "Engine mutated the input list"
    assert 89 <= result.observations_in_window <= 91, result.observations_in_window
    assert result.total_observations == result.observations_in_window
    bucket = result.get_bucket(horizon_hours=18.0, hour_of_day=12)
    assert 89 <= bucket.ols.n <= 91, bucket.ols.n


def test_stale_observations_yield_empty_buckets():
    """Observations older than OBSERVATION_WINDOW_DAYS are discarded by fit():
    empty buckets and a passthrough with n_obs=0. This is the failure mode the
    fixture guard below protects against."""
    stale_day = datetime.now(NEM_TZ) - timedelta(days=_engine_mod.OBSERVATION_WINDOW_DAYS + 30)
    stale_obs = [
        Observation(
            interval_time=stale_day.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(),
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
    out = CalibrationEngine().fit(stale_obs).apply(0.10, horizon_hours=18.0, hour_of_day=12)
    assert out["calibrated_source"] == "passthrough"
    assert out["n_obs"] == 0
    assert out["calibrated"] == 0.10, "stale fit must pass the raw value through"


def test_fixture_observations_are_inside_training_window():
    """Fixture dates must be recent enough for fit() to train on them, in the
    past, and have run_at no later than the interval."""
    now = datetime.now(NEM_TZ)
    cutoff = now - timedelta(days=_engine_mod.OBSERVATION_WINDOW_DAYS)
    assert _OBS_ANCHOR < now
    for label, obs in (
        ("make_obs", [make_obs(0.10, 0.20)]),
        ("_make_obs_batch", _make_obs_batch(n=5, a=2.0, b=0.02, horizon_hours=18.0, hour_of_day=12)),
    ):
        for o in obs:
            interval = datetime.fromisoformat(o.interval_time)
            assert cutoff <= interval <= now, (
                f"{label}() produced an observation dated {o.interval_time}, "
                f"outside the {_engine_mod.OBSERVATION_WINDOW_DAYS}-day window. "
                "Fixture dates must be anchored to datetime.now()."
            )
            assert datetime.fromisoformat(o.forecast_run_at) <= interval, (
                f"{label}() forecast_run_at {o.forecast_run_at} is after interval_time {o.interval_time}"
            )


# ── Below the fitted domain, and negative prices ─────────────────────────────
# Issue #114: the published isotonic value used to be floored at 0.0 and the
# band's lower bound with it, so a mildly negative NEM price, the normal solar
# trough state, was published as "free" on about one interval in nine on the
# live install. Issues #117, #120, #123: a forecast below every forecast the
# bucket was fitted on has no settled actual near it, so the bucket answers
# as if the forecast were at its floor, point and band. Issue #144: the
# stage-2 feature is the published (market-floored) price on every path.
#
# The fixture bucket's isotonic map is 1.10 * x + 0.006 on x in [-0.08, 0.32],
# so it is negative below a forecast of about -0.0055 $/kWh. Built without
# engine.fit on purpose: fit applies wall-clock decay weights, so a fit is not
# reproducible and could not carry the golden table below.

_ISO_XS = [round(-0.08 + 0.02 * i, 6) for i in range(21)]  # -0.08 to 0.32
_ISO_YS = [round(1.10 * x + 0.006, 6) for x in _ISO_XS]


def _negative_bucket(bucket_key: str = "h24_48__solar") -> BucketModel:
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray(_ISO_XS, dtype=float), np.asarray(_ISO_YS, dtype=float))
    return BucketModel(
        bucket_key=bucket_key,
        ols=LinearCoeff(a=1.10, b=0.006, n=120, mae=0.0075, rmse=0.0095),
        q10=QuantileCoeff(quantile=0.1, a=1.00, b=-0.010, n=120),
        q50=QuantileCoeff(quantile=0.5, a=1.05, b=0.000, n=120),
        q90=QuantileCoeff(quantile=0.9, a=1.10, b=0.020, n=120),
        iso_model=iso,
    )


def _raw_iso(bucket: BucketModel, x: float) -> float:
    """The unfloored isotonic prediction, read straight off the model."""
    return float(bucket.iso_model.predict(np.asarray([x], dtype=float))[0])


def test_mildly_negative_forecast_publishes_a_negative_calibrated_price():
    bucket = _negative_bucket()
    for x in (-0.07, -0.05, -0.02):
        assert not bucket.is_below_domain(x)
        out = bucket.apply_all(x)
        assert out["calibrated_source"] == "isotonic"
        assert out["calibrated"] == round(_raw_iso(bucket, x), 6)
        assert out["calibrated"] < 0.0, f"{x}: {out}"


def test_lower_bound_can_sit_below_a_negative_point_estimate():
    """p10 is not clamped up onto a negative point estimate any more."""
    out = _negative_bucket().apply_all(-0.05)
    assert out["p10"] < out["calibrated"] < out["p90"], out
    assert out["p10"] == round(1.00 * -0.05 - 0.010, 6)


def test_clamp_band_floors_at_the_market_floor_not_zero():
    p10, p50, p90 = _clamp_band(-0.3, -1.5, -0.4, -0.1)
    assert p10 == MARKET_PRICE_FLOOR
    assert p50 == -0.4 and p90 == -0.1
    p10, p50, p90 = _clamp_band(0.05, -0.02, 0.01, 0.09)
    assert p10 == -0.02, "a negative lower bound below a positive estimate is kept"


def test_point_estimate_and_feature_are_floored_at_the_market_floor():
    """A corrupt fit below -$1000/MWh publishes the floor, not the step, and
    the stage-2 feature is that same floored price (#144): every apply_all
    branch publishes ISO_FEATURE_KEY equal to "calibrated", so stage 2 is
    never fed a feature more extreme than the price shown beside it."""
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray([0.001, 0.002, 0.003], dtype=float), np.asarray([-3.2, -3.1, -3.0], dtype=float))
    bucket = _negative_bucket()
    bucket.iso_model = iso
    out = bucket.apply_all(0.002)
    assert out["calibrated"] == MARKET_PRICE_FLOOR
    assert out["p10"] is None or out["p10"] <= out["calibrated"]
    assert out[ISO_FEATURE_KEY] == out["calibrated"] == MARKET_PRICE_FLOOR, (
        f"iso_feature {out[ISO_FEATURE_KEY]} must equal the floored price {out['calibrated']}"
    )


def test_below_domain_clips_to_the_edge_with_the_floor_band():
    """Below the domain the bucket answers at its floor: iso(x_min) and the
    quantile band at x_min. The fixture's domain starts at -0.08 where iso is
    -0.082; the raw -0.25 plays no part."""
    bucket = _negative_bucket()
    assert bucket.domain_min == -0.08
    assert abs(bucket.edge_value + 0.082) < 1e-9
    assert bucket.is_below_domain(-0.25) and not bucket.is_below_domain(-0.08)
    out = bucket.apply_all(-0.25)
    assert out["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
    assert out["calibrated"] == -0.082
    # q10 1.00*(-0.08) - 0.010 = -0.09; q90 1.10*(-0.08) + 0.020 = -0.068.
    assert out["p10"] == -0.09 and out["p90"] == -0.068, out
    assert out["p10"] <= out["p50"] <= out["p90"]
    assert out["band_source"] == "stage1_quantile"


def test_below_domain_is_continuous_at_the_floor():
    """No step at the domain floor: just below it the published triple meets
    the isotonic curve, and however far below, it stays there (#120, #123)."""
    bucket = _negative_bucket()
    at_floor = bucket.apply_all(-0.08)
    just_below = bucket.apply_all(-0.08 - 1e-6)
    far_below = bucket.apply_all(-5.0)
    assert at_floor["calibrated_source"] == "isotonic"
    assert just_below["calibrated_source"] == far_below["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
    for key in ("calibrated", "p10", "p50", "p90", ISO_FEATURE_KEY):
        assert at_floor[key] == just_below[key] == far_below[key], (
            key, at_floor[key], just_below[key], far_below[key]
        )


# ── The published stage-1 output is pinned ────────────────────────────────────
# Issue #85 pinned this table so an internal feature change (the stage-2
# feature was being read floored) could not move a displayed price. Issue
# #114 then moved the negative region on purpose and #123 made the
# below-domain path a clip to the domain edge, so the table was recaptured;
# rows at 0.02 and above are unchanged from the pre-#85 capture at 0b35e55,
# which pins the positive region across all five changes. Regenerate with
# _capture() after a deliberate change to the published numbers.

_GOLDEN_XS = [
    -0.5, -0.30, -0.1500, -0.1000, -0.0999, -0.09, -0.0864, -0.05, -0.02,
    -0.0055, -0.0054, -0.001, 0.0, 0.001, 0.02, 0.05, 0.08, 0.12, 0.20,
    0.32, 0.75, 3.50,
]

_GOLDEN_PUBLISHED = {
    -0.5: (-0.082, -0.09, -0.084, -0.068, 'isotonic_below_domain'),
    -0.3: (-0.082, -0.09, -0.084, -0.068, 'isotonic_below_domain'),
    -0.15: (-0.082, -0.09, -0.084, -0.068, 'isotonic_below_domain'),
    -0.1: (-0.082, -0.09, -0.084, -0.068, 'isotonic_below_domain'),
    -0.0999: (-0.082, -0.09, -0.084, -0.068, 'isotonic_below_domain'),
    -0.09: (-0.082, -0.09, -0.084, -0.068, 'isotonic_below_domain'),
    -0.0864: (-0.082, -0.09, -0.084, -0.068, 'isotonic_below_domain'),
    -0.05: (-0.049, -0.06, -0.0525, -0.035, 'isotonic'),
    -0.02: (-0.016, -0.03, -0.021, -0.002, 'isotonic'),
    -0.0055: (-5e-05, -0.0155, -0.005775, 0.01395, 'isotonic'),
    -0.0054: (6e-05, -0.0154, -0.00567, 0.01406, 'isotonic'),
    -0.001: (0.0049, -0.011, -0.00105, 0.0189, 'isotonic'),
    0.0: (0.006, -0.01, 0.0, 0.02, 'isotonic'),
    0.001: (0.0071, -0.009, 0.00105, 0.0211, 'isotonic'),
    0.02: (0.028, 0.01, 0.021, 0.042, 'isotonic'),
    0.05: (0.061, 0.04, 0.0525, 0.075, 'isotonic'),
    0.08: (0.094, 0.07, 0.084, 0.108, 'isotonic'),
    0.12: (0.138, 0.11, 0.126, 0.152, 'isotonic'),
    0.2: (0.226, 0.19, 0.21, 0.24, 'isotonic'),
    0.32: (0.358, 0.31, 0.336, 0.372, 'isotonic'),
    0.75: (0.358, 0.358, 0.7875, 0.845, 'isotonic'),
    3.5: (0.358, 0.358, 3.675, 3.87, 'isotonic'),
}


def _published(bucket: BucketModel, x: float) -> tuple:
    out = bucket.apply_all(x)
    return (out["calibrated"], out["p10"], out["p50"], out["p90"], out["calibrated_source"])


def _capture() -> None:
    """Print the golden table in the form above."""
    bucket = _negative_bucket()
    print("_GOLDEN_PUBLISHED = {")
    for x in _GOLDEN_XS:
        print(f"    {x!r}: {_published(bucket, x)!r},")
    print("}")


def test_published_stage_one_output_matches_the_golden_table():
    bucket = _negative_bucket()
    for x in _GOLDEN_XS:
        assert _published(bucket, x) == _GOLDEN_PUBLISHED[x], (
            f"published stage-1 output changed at raw forecast {x} $/kWh: "
            f"{_published(bucket, x)} against golden {_GOLDEN_PUBLISHED[x]}"
        )


def test_published_price_sweep_is_the_unfloored_isotonic_value_and_the_feature():
    """The invariant behind the golden table, at 1601 forecasts from -0.400
    to +0.400 $/kWh: inside the domain the published price is exactly
    round(max(iso(x), MARKET_PRICE_FLOOR), 6), below it the edge value, and
    on both paths the stage-2 feature is the published price (#85, #144).
    The only floor is the market floor, which this fixture never reaches.
    """
    bucket = _negative_bucket()
    negatives = 0
    for i in range(1601):
        x = round(-0.400 + i * 0.0005, 6)
        out = bucket.apply_all(x)
        if bucket.is_below_domain(x):
            assert out["calibrated"] == round(max(bucket.edge_value, MARKET_PRICE_FLOOR), 6)
            assert out["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
        else:
            expected = round(max(_raw_iso(bucket, x), MARKET_PRICE_FLOOR), 6)
            assert out["calibrated"] == expected, (
                f"published price at {x} is {out['calibrated']}, expected the isotonic value {expected}"
            )
            assert out["calibrated_source"] == "isotonic"
            if out["calibrated"] < 0.0:
                negatives += 1
        assert out[ISO_FEATURE_KEY] == out["calibrated"], (
            f"feature and published price disagree at {x}: {out[ISO_FEATURE_KEY]} against {out['calibrated']}"
        )
    assert negatives > 80, "the sweep must exercise the in-domain negative region"
