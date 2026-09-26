"""
CalibrationEngine, stage 2: fit_ols_stage2 and the STPASA override in
CalibrationResult.apply.

Stage 2 replaces the stage-1 point estimate with a nine-feature OLS
prediction (the stage-1 isotonic value, three run features, the horizon and
four STPASA features) for horizons in the OLS band [22 h, 120 h]. These tests
pin the fit (which rows enter it, the leverage screen, the residual quantiles
and feature ranges it stores), the serving gates (horizon and feature
presence, the below-domain clip, feature-range extrapolation, sign agreement,
the market floor) and the band published beside a stage-2 value.

Stage 1 is tests/test_calibration_engine.py; the STPASA serving-side feature
lookup is tests/test_calibration_inputs.py.

Run with:  python -m pytest tests/test_calibration_stage2.py -v
"""
from __future__ import annotations

import copy
import math
import random
from datetime import datetime, timedelta

import numpy as np
import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()  # stpasa_client imports aiohttp
load_chain("const", "nem_time", "calibration_engine", "stpasa_client")

from custom_components.nem_pd7day.calibration_engine import (  # noqa: E402
    BAND_SOURCE_KEY,
    BAND_SOURCE_PASSTHROUGH,
    BAND_SOURCE_STAGE1,
    BAND_SOURCE_STAGE1_RAW,
    BAND_SOURCE_STAGE2,
    BAND_SOURCE_STAGE2_FALLBACK,
    ISO_FEATURE_KEY,
    MIN_OBS,
    OLS_MAX_HORIZON_H,
    OLS_MIN_HORIZON_H,
    OLS_MIN_OBS,
    SOURCE_ISOTONIC_BELOW_DOMAIN,
    SPIKE_THRESHOLD,
    STAGE2_FEATURE_NAMES,
    STPASA_DEMAND_FLOOR_MW,
    BucketModel,
    CalibrationEngine,
    CalibrationResult,
    IsotonicRegression,
    LinearCoeff,
    Observation,
    OlsModel,
    QuantileCoeff,
    ResidualQuantiles,
    RunFeatures,
    StpasaFeatures,
    _bucket_key,
    _compute_run_features,
    _conformal_index,
    _loo_residuals,
    stage2_iso_feature,
    stpasa_feature_values,
)
from custom_components.nem_pd7day.const import ATTR_CAL_BAND_SOURCE, MARKET_PRICE_FLOOR  # noqa: E402
from custom_components.nem_pd7day.stpasa_client import StpasaInterval  # noqa: E402

# Fixture dates are relative to now so they stay inside the engine's 90-day
# training window (see test_calibration_engine.py).
_ANCHOR = datetime.now(NEM_TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=3)


def _obs(
    interval_dt: datetime,
    horizon_hours: float,
    forecast: float,
    actual: float,
    run_at: str,
    *,
    hour_of_day: int | None = None,
) -> Observation:
    return Observation(
        interval_time=interval_dt.isoformat(),
        horizon_hours=float(horizon_hours),
        pd7day_forecast=forecast,
        actual_rrp=actual,
        forecast_run_at=run_at,
        hour_of_day=interval_dt.hour if hour_of_day is None else hour_of_day,
        day_of_week=interval_dt.weekday(),
        month=interval_dt.month,
        gas_forecast_tj=75.0,
        qni_mwflow=-150.0,
        qni_violation_degree=0.0,
        is_intervention=False,
    )


def _feature_vec(iso_feature: float, rf: RunFeatures, sf: StpasaFeatures, horizon: float) -> list[float]:
    """The nine-feature vector OlsModel.predict takes, in the engine's order."""
    return [
        float(iso_feature),
        rf.run_max_h6_rrp,
        rf.run_mean_rrp,
        rf.run_spread,
        horizon / 168.0,
        sf.log_surplus,
        sf.log_solar,
        sf.log_demand,
        sf.poe_spread_n,
    ]


# ── Hand-built geometry ───────────────────────────────────────────────────────
# Inside the OLS horizon band so the stage-2 override is reached. Coefficients
# are hand-set so every published number is exact rather than fit-dependent.

HORIZON = 36.0
HOUR = 17
KEY = _bucket_key(HORIZON, HOUR)
N_FITTED = MIN_OBS * 10

RUN_FEATURES = RunFeatures(run_max_h6_rrp=0.2, run_mean_rrp=0.1, run_spread=0.05)
STPASA = StpasaFeatures(
    log_surplus=8.0,
    log_solar=8.0,
    log_demand=9.0,
    poe_spread_n=0.1,
    stpasa_run_at="2026-09-03T04:00:00+10:00",
)


def _iso_model() -> IsotonicRegression:
    """Monotone fit mapping a forecast to roughly half of it, x-range 0 to 0.3:
    out_of_bounds='clip' pins anything below 0.0 to 0.0 and above 0.3 to 0.15."""
    return IsotonicRegression().fit(
        np.asarray([0.0, 0.1, 0.2, 0.3], dtype=float),
        np.asarray([0.0, 0.05, 0.10, 0.15], dtype=float),
    )


def _bucket(
    *,
    with_iso: bool = True,
    fitted_quantiles: bool = True,
    q10_a: float = 0.4,
    q10_b: float = 0.0,
    q50_a: float = 0.5,
    q50_b: float = 0.0,
    q90_a: float = 0.7,
    q90_b: float = 0.0,
) -> BucketModel:
    """Stage-1 bucket with hand-set lines, by default p10 = 0.4x, p50 = 0.5x, p90 = 0.7x."""
    n = N_FITTED if fitted_quantiles else 0
    return BucketModel(
        bucket_key=KEY,
        q10=QuantileCoeff(0.1, a=q10_a, b=q10_b, n=n),
        q50=QuantileCoeff(0.5, a=q50_a, b=q50_b, n=n),
        q90=QuantileCoeff(0.9, a=q90_a, b=q90_b, n=n),
        iso_model=_iso_model() if with_iso else None,
    )


def _resid(q10=-0.02, q50=-0.001, q90=0.03, n=OLS_MIN_OBS * 2, key=KEY) -> ResidualQuantiles:
    return ResidualQuantiles(bucket_key=key, q10=q10, q50=q50, q90=q90, n=n)


def _result(
    bucket: BucketModel,
    prediction: float | None = None,
    resid: ResidualQuantiles | None = None,
    *,
    coef: list[float] | None = None,
) -> CalibrationResult:
    """Result whose stage-2 model returns ``prediction`` for any input (the
    intercept followed by zeros), or carries ``coef`` verbatim. No model at all
    when both are None. Keyed on the bucket's own key."""
    key = bucket.bucket_key
    ols_models = {}
    if coef is not None:
        ols_models[key] = OlsModel(bucket_key=key, coef=list(coef), n_train=100, r2=0.5, resid=resid)
    elif prediction is not None:
        ols_models[key] = OlsModel(
            bucket_key=key, coef=[prediction] + [0.0] * 8, n_train=100, r2=0.5, resid=resid
        )
    return CalibrationResult(
        fitted_at="2026-09-03T07:30:00+10:00",
        total_observations=1000,
        models={key: bucket},
        ols_models=ols_models,
    )


def _apply(
    res: CalibrationResult,
    forecast: float,
    horizon: float = HORIZON,
    hour: int = HOUR,
    stpasa: StpasaFeatures | None = STPASA,
    run_features: RunFeatures | None = RUN_FEATURES,
) -> dict:
    return res.apply(forecast, horizon_hours=horizon, hour_of_day=hour, stpasa=stpasa, run_features=run_features)


def _assert_ordered(out: dict, label: str) -> None:
    """p10 <= p50 <= p90, which must hold on every path without exception."""
    p10, p50, p90 = out["p10"], out["p50"], out["p90"]
    if p10 is not None and p90 is not None:
        assert p10 <= p90, f"{label}: p10 {p10} above p90 {p90}"
    if p50 is not None:
        if p10 is not None:
            assert p10 <= p50, f"{label}: p50 {p50} below p10 {p10}"
        if p90 is not None:
            assert p50 <= p90, f"{label}: p50 {p50} above p90 {p90}"


def _assert_consistent(out: dict, label: str) -> None:
    """The published triple is ordered and contains the point estimate."""
    _assert_ordered(out, label)
    value = out["calibrated"]
    p10, p90 = out["p10"], out["p90"]
    if p10 is not None:
        assert p10 <= value, f"{label}: value {value} below its own p10 {p10} (source {out['calibrated_source']})"
    if p90 is not None:
        assert value <= p90, f"{label}: value {value} above its own p90 {p90} (source {out['calibrated_source']})"


# The negative-region bucket of test_calibration_engine.py: isotonic map
# 1.10 * x + 0.006 on x in [-0.08, 0.32], negative below about -0.0055 $/kWh,
# keyed on the midday solar bucket where NEM negative prices occur.
NEG_HOUR = 12
NEG_KEY = _bucket_key(HORIZON, NEG_HOUR)
ZERO_STPASA = StpasaFeatures(
    log_surplus=0.0, log_solar=0.0, log_demand=0.0, poe_spread_n=0.0,
    stpasa_run_at="2026-09-04T04:00:00+10:00",
)
ZERO_RUN = RunFeatures(run_max_h6_rrp=0.0, run_mean_rrp=0.0, run_spread=0.0)
_ISO_XS = [round(-0.08 + 0.02 * i, 6) for i in range(21)]
_ISO_YS = [round(1.10 * x + 0.006, 6) for x in _ISO_XS]


def _negative_bucket() -> BucketModel:
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray(_ISO_XS, dtype=float), np.asarray(_ISO_YS, dtype=float))
    return BucketModel(
        bucket_key=NEG_KEY,
        ols=LinearCoeff(a=1.10, b=0.006, n=120, mae=0.0075, rmse=0.0095),
        q10=QuantileCoeff(quantile=0.1, a=1.00, b=-0.010, n=120),
        q50=QuantileCoeff(quantile=0.5, a=1.05, b=0.000, n=120),
        q90=QuantileCoeff(quantile=0.9, a=1.10, b=0.020, n=120),
        iso_model=iso,
    )


def _raw_iso(bucket: BucketModel, x: float) -> float:
    return float(bucket.iso_model.predict(np.asarray([x], dtype=float))[0])


def _neg_apply(res: CalibrationResult, forecast: float) -> dict:
    return _apply(res, forecast, hour=NEG_HOUR, stpasa=ZERO_STPASA, run_features=ZERO_RUN)


# ── Real fits ─────────────────────────────────────────────────────────────────

# A real isotonic plus a real stage-2 fit on two peak cells. The actual
# carries a Gaussian innovation and a latent driver never placed in the design
# matrix, so the fit has genuine irreducible error and the residual quantiles
# are fitted on something. NOT a coverage measurement: its noise process is
# chosen here. Several training runs, not one: _compute_run_features only
# builds an entry for a run holding a horizon < 24 row, and the run features
# are constant within a run, so a single-run fixture has zero-width training
# ranges there and the #147 gate would refuse every served run but that one.
_CELLS = ((30.0, 17), (60.0, 17))
# (base, width) of each training run's near-term raw prices: run_max_h6 runs
# 0.04 to 0.59, run_mean 0.04 to 0.55, run_spread 0.03 to 0.40, bracketing
# RUN_FEATURES.
_RUN_SHAPES = (
    (0.02, 0.04), (0.05, 0.10), (0.10, 0.16), (0.16, 0.24), (0.22, 0.36), (0.30, 0.50),
)


def _peak_fit() -> CalibrationResult:
    rng = random.Random(720)
    engine = CalibrationEngine()
    observations: list[Observation] = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}

    run_ats = []
    for k, (base, width) in enumerate(_RUN_SHAPES):
        run_dt = (_ANCHOR - timedelta(days=40 + k)).replace(hour=3, minute=30)
        run_at = run_dt.isoformat()
        run_ats.append(run_at)
        for j in range(8):
            observations.append(_obs(
                run_dt + timedelta(hours=1 + j), 1.0 + j, base + width * j / 7.0,
                rng.uniform(0.05, 0.30), run_at,
            ))

    for horizon, hour in _CELLS:
        for i in range(90):
            day_off = i % 45 + (0 if horizon < 48 else 45)
            interval_dt = (_ANCHOR - timedelta(days=day_off)).replace(hour=hour, minute=(i % 2) * 30)
            run_at = run_ats[i % len(run_ats)]
            forecast = rng.uniform(0.03, 0.28)
            surplus = rng.uniform(400.0, 5200.0)
            solar = rng.uniform(0.0, 4200.0)
            demand50 = rng.uniform(5000.0, 9200.0)
            actual = (
                1.05 * forecast + 0.02 - 6.0e-6 * solar + 3.0e-6 * (demand50 - 7000.0)
                + 0.012 * rng.gauss(0.0, 1.0) + rng.gauss(0.0, 0.008)
            )
            o = _obs(interval_dt, horizon, forecast, actual, run_at)
            observations.append(o)
            stpasa_by_key[f"{o.interval_time}|{run_at}"] = StpasaFeatures(
                log_surplus=math.log1p(surplus),
                log_solar=math.log1p(solar),
                log_demand=math.log(max(demand50, 1.0)),
                poe_spread_n=rng.uniform(0.08, 0.30),  # spans the 0.1 to 0.22 served (#147)
                stpasa_run_at=STPASA.stpasa_run_at,
            )

    result = engine.fit(observations)
    result.ols_models = engine.fit_ols_stage2(observations, stpasa_by_key)
    return result


# The diurnal fixture of issues #79, #85 and #117: a single residential
# premises in SE Queensland, an evening peak, a cheap solar middle of the day
# and a flat shoulder. Every forecast is clipped at _FIXTURE_FLOOR, so that is
# the lowest training forecast in every bucket and each bucket's domain floor.
# Every test targets 36 h ahead at midday: inside the OLS band, in the solar
# bucket where NEM negative prices occur.
TARGET_H = HORIZON
TARGET_HOUR = NEG_HOUR
TARGET = NEG_KEY
_TRUE_SLOPE = 1.10
_TRUE_INTERCEPT = 0.006
_FIXTURE_FLOOR = -0.02
_FLOORED_DEPTH = -0.09


def _hourly_base(hour: int) -> float:
    if 16 <= hour <= 21:
        return 0.16
    if 10 <= hour < 16:
        return 0.02
    return 0.08


def _build(n_runs: int = 26, seed: int = 7):
    """(observations, stpasa_by_key) with no forecast below _FIXTURE_FLOOR.

    Each run carries near-term rows as well as in-band ones, because
    _compute_run_features only produces an entry for a run with rows below
    24 h and fit_ols_stage2 skips any row whose run has none.
    """
    rng = random.Random(seed)
    obs: list[Observation] = []
    stpasa: dict[str, StpasaFeatures] = {}
    for r in range(n_runs):
        run_dt = (_ANCHOR - timedelta(days=r * 2)).replace(hour=3, minute=30, second=0, microsecond=0)
        run_at = run_dt.isoformat()
        for h_int in list(range(1, 24, 2)) + list(range(24, 97, 2)):
            interval_dt = run_dt + timedelta(hours=h_int)
            hour = interval_dt.hour
            solar = 10 <= hour < 16
            fc = max(_FIXTURE_FLOOR, _hourly_base(hour) + rng.gauss(0, 0.02))
            o = _obs(interval_dt, h_int, fc, _TRUE_SLOPE * fc + _TRUE_INTERCEPT + rng.gauss(0, 0.008), run_at)
            obs.append(o)
            stpasa[f"{o.interval_time}|{run_at}"] = StpasaFeatures(
                log_surplus=math.log1p(max(0.0, 1400.0 + rng.gauss(0, 250.0))),
                log_solar=math.log1p(max(0.0, (2800.0 if solar else 200.0) + rng.gauss(0, 300.0))),
                log_demand=math.log(max(1.0, 8200.0 + rng.gauss(0, 400.0))),
                poe_spread_n=0.18 + rng.gauss(0, 0.03),
                stpasa_run_at=run_at,
            )
    return obs, stpasa


def _in_target(o: Observation) -> bool:
    return (
        OLS_MIN_HORIZON_H <= o.horizon_hours <= OLS_MAX_HORIZON_H
        and _bucket_key(o.horizon_hours, o.hour_of_day) == TARGET
    )


def _promote(obs, k, depth=_FLOORED_DEPTH, actual=None):
    """Move the first k target-bucket rows to a forecast of ``depth``.

    The actual comes from the same relation as every other row unless given,
    so any coefficient movement is attributable to the feature and not to a
    different relationship in the negative region.
    """
    out, taken = [], 0
    for o in obs:
        if taken < k and _in_target(o):
            out.append(o._replace(
                pd7day_forecast=depth,
                actual_rrp=(actual if actual is not None else _TRUE_SLOPE * depth + _TRUE_INTERCEPT),
            ))
            taken += 1
        else:
            out.append(o)
    assert taken == k, f"only promoted {taken} of {k} requested rows"
    return out


def _fit_stage2_with_domain_from(reference: CalibrationResult, promoted, sp):
    """Fit stage 2 on ``promoted`` with stage 1 pinned to ``reference``.

    fit_ols_stage2 refits stage 1 on the rows it is given, so a promoted row
    widens the bucket's domain and is never below it. Real data can disagree
    between the two stages: stage 1 is fitted on the full observation window,
    stage 2 only on rows with STPASA and run features, and the store can hold
    a row whose forecast was never seen by the stage-1 fit current at serve
    time. Pinning the stage-1 result reproduces that deterministically.
    """
    engine = CalibrationEngine()
    engine.fit = lambda observations, region=None: reference
    return engine.fit_ols_stage2(promoted, sp)


def _coef1(models: dict[str, OlsModel]) -> float | None:
    """The fitted iso_cal coefficient of the target bucket, or None when unfitted."""
    m = models.get(TARGET)
    if m is None or len(m.coef) < 2:
        return None
    return m.coef[1]


def _stage2(obs, sp) -> dict[str, OlsModel]:
    return CalibrationEngine().fit_ols_stage2(obs, sp)


# A fitted stage-1 result at one horizon and hour carrying one hand-specified
# OLS model (issue #73). Raw forecasts reach -0.06 so the bucket's fitted
# domain does, which makes the sweep's deep negatives below-domain and -0.03
# inside it.
_RF = RunFeatures(run_max_h6_rrp=0.20, run_mean_rrp=0.12, run_spread=0.05)
_SF = StpasaFeatures(
    log_surplus=math.log1p(1200.0),
    log_solar=math.log1p(2500.0),
    log_demand=math.log(8500.0),
    poe_spread_n=0.2,
    stpasa_run_at=(_ANCHOR - timedelta(days=1)).replace(hour=3, minute=30).isoformat(),
)


def _obs_batch(n, horizon_hours, hour_of_day, seed=3):
    rng = random.Random(seed)
    run_at = (_ANCHOR - timedelta(days=1)).replace(hour=3, minute=30).isoformat()
    obs = []
    for i in range(n):
        interval = (_ANCHOR - timedelta(days=i % 30)).replace(hour=hour_of_day, minute=(i % 2) * 30, second=(i % 55))
        fc = rng.uniform(-0.06, 0.25)
        obs.append(_obs(interval, horizon_hours, fc, max(0.0, 1.15 * fc + 0.01 + rng.gauss(0, 0.01)), run_at,
                        hour_of_day=hour_of_day))
    return obs


def _result_with_ols(coef, horizon_hours=36.0, hour_of_day=12) -> CalibrationResult:
    result = CalibrationEngine().fit(_obs_batch(80, horizon_hours, hour_of_day))
    key = _bucket_key(horizon_hours, hour_of_day)
    result.ols_models[key] = OlsModel(bucket_key=key, coef=list(coef), n_train=120, r2=0.8)
    return result


# ── The serving gates: horizon band, feature presence ────────────────────────

@pytest.mark.parametrize("horizon, stpasa, run_features", [
    (20.0, STPASA, RUN_FEATURES),      # below the OLS band
    (130.0, STPASA, RUN_FEATURES),     # above the OLS band
    (HORIZON, None, RUN_FEATURES),     # no STPASA features
    (HORIZON, STPASA, None),           # no run features
])
def test_stage2_is_skipped_outside_the_horizon_band_or_without_features(horizon, stpasa, run_features):
    """Outside [22 h, 120 h], or without both feature groups, apply() serves
    stage 1 whatever the fitted OLS model would predict."""
    keys = {_bucket_key(h, HOUR) for h in (20.0, HORIZON, 130.0)}
    res = CalibrationResult(
        fitted_at="2026-09-03T07:30:00+10:00",
        total_observations=1000,
        models={k: _bucket() for k in keys},
        ols_models={
            k: OlsModel(bucket_key=k, coef=[0.12] + [0.0] * 8, n_train=100, r2=0.5, resid=_resid(key=k))
            for k in keys
        },
    )
    served = _apply(res, 0.2)
    assert served["calibrated_source"] == "isotonic+stpasa", "control: the in-band call must be served"
    out = _apply(res, 0.2, horizon=horizon, stpasa=stpasa, run_features=run_features)
    assert out["calibrated_source"] == "isotonic", out
    assert out["calibrated"] == 0.1  # the isotonic value at 0.2
    assert "stpasa_run_at" not in out


# ── The sign and floor gates ──────────────────────────────────────────────────

def test_negative_stage2_prediction_at_a_positive_stage1_value_falls_back():
    """The case observed in the wild on h24_48__shoulder: an iso_cal
    coefficient of -1.879 drove predictions <= 0 for typical forecasts and
    the prior code published calibrated: 0. The correct answer is the
    isotonic result, not labelled isotonic+stpasa."""
    result = _result_with_ols([0.05, -2.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    out = result.apply(0.15, horizon_hours=36.0, hour_of_day=12, stpasa=_SF, run_features=_RF)
    assert out["calibrated"] > 0.0, f"fallback isotonic result must be positive; got {out['calibrated']}"
    assert out["calibrated_source"] == "isotonic"
    assert "stpasa_run_at" not in out


@pytest.mark.parametrize("x, prediction", [(-0.05, +0.02), (0.08, -0.02)])
def test_stage_two_will_not_flip_the_sign_of_the_stage_one_value(x, prediction):
    """Before issue #114 the rule was ``prediction <= 0.0``, which also stopped
    stage 2 ever publishing a negative. It is now sign disagreement (#73): a
    negative stage-1 value is not flipped positive ("paid to consume" must
    not become "pay to consume") and a positive one is not flipped negative."""
    bucket = _negative_bucket()
    out = _neg_apply(_result(bucket, prediction), x)
    assert out["calibrated_source"] == "isotonic"
    assert out["calibrated"] == round(_raw_iso(bucket, x), 6)
    if x < 0.0:
        assert out["calibrated"] < 0.0
    else:
        assert out["calibrated"] > 0.0


def test_stage_two_serves_a_negative_prediction_when_stage_one_is_negative():
    bucket = _negative_bucket()
    x = -0.05
    assert _raw_iso(bucket, x) < 0.0
    out = _neg_apply(_result(bucket, -0.03, _resid(-0.02, -0.001, 0.03, key=NEG_KEY)), x)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == -0.03
    assert out["p10"] == -0.05 and out["p90"] == 0.0, out
    assert out["p10"] < out["calibrated"] < out["p90"]


def test_stage_two_refuses_a_prediction_below_the_market_floor():
    out = _neg_apply(_result(_negative_bucket(), -1.5), -0.05)
    assert out["calibrated_source"] == "isotonic"


# ── The point estimate lies inside its band ───────────────────────────────────
# Issue #69: apply() step 7 replaced the point estimate with the stage-2
# prediction but kept the band apply_all had clamped against the isotonic
# value, so the published triple was inconsistent whenever the prediction
# moved past a stage-1 bound: 522 of 3075 intervals across 9 sensors on a
# five-region live snapshot, every one isotonic+stpasa.

def test_override_band_is_derived_from_the_fits_not_the_clamped_stage1_band():
    """Re-clamping starts from the quantile fits, not the stage-1 band.

    The isotonic value sits below the fitted p10, so stage 1 clamps p10 down
    from 0.08 to 0.025. A stage-2 prediction of 0.12 belongs in the fitted
    band [0.08, 0.14]; re-clamping the already-clamped band would publish the
    loose 0.025 instead.
    """
    bucket = _bucket(q10_a=1.6, q50_a=2.0, q90_a=2.8)
    stage1 = bucket.apply_all(0.05)
    assert stage1["calibrated"] == 0.025, stage1
    assert stage1["p10"] == 0.025, f"expected stage 1 to clamp p10 down, got {stage1}"
    assert abs(bucket.raw_band(0.05)[0] - 0.08) < 1e-9
    out = _apply(_result(bucket, prediction=0.12), 0.05)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["p10"] == 0.08, f"p10 must come from the quantile fit (0.08), not the stage-1 clamp; got {out['p10']}"
    assert out["p90"] == 0.14
    _assert_consistent(out, "band derived from fits")


def test_qld1_collapsed_zero_band_regression():
    """The live QLD1 case, 2026-09-02 at h65.0, forecast -0.07591 $/kWh.

    This fixture's isotonic domain starts at 0.0, so the forecast is below it:
    since #117 (refined by #123) a below-domain clip, the bucket's value at
    its floor with the quantile band there, and stage 2 is never consulted.
    Before #114 and #117 the isotonic value clipped and floored to 0.0, the
    zero floor collapsed the band to p10 = p90 = 0.0, and a stage-2
    prediction of 0.00182 was published above a p90 of exactly zero.
    """
    forecast = -0.07591
    bucket = _bucket(q10_b=-0.01, q90_b=0.01)
    assert bucket.is_below_domain(forecast)
    stage1 = bucket.apply_all(forecast)
    assert stage1["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
    assert stage1["calibrated"] == 0.0, stage1
    assert stage1["p10"] == -0.01 and stage1["p90"] == 0.01, stage1
    _assert_consistent(stage1, "qld1 stage 1")

    out = _apply(_result(bucket, prediction=0.00182), forecast)
    assert out["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
    assert out["calibrated"] == 0.0
    assert out["p10"] == stage1["p10"] and out["p90"] == stage1["p90"]
    _assert_consistent(out, "qld1 collapsed band")


def test_unfitted_quantiles_stay_none_after_the_override():
    """Too few observations publish None, not an invented band."""
    out = _apply(_result(_bucket(fitted_quantiles=False), prediction=0.25), 0.2)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["p10"] is None and out["p50"] is None and out["p90"] is None, out
    _assert_consistent(out, "unfitted quantiles")


def test_override_on_top_of_a_passthrough_bucket_is_still_consistent():
    """Stage 2 can fire on a bucket with no isotonic model (the gate tests
    horizon and features, not the stage-1 source). The passthrough band is
    deliberately unclamped, but once the point estimate is a calibrated
    stage-2 value the band must contain it."""
    bucket = _bucket(with_iso=False, q10_a=1.0, q10_b=0.05, q50_a=1.0, q50_b=0.10, q90_a=1.0, q90_b=0.15)
    out = _apply(_result(bucket, prediction=0.4), 0.1)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == 0.4
    assert out["p90"] == 0.4, f"p90 must rise to contain the override, got {out['p90']}"
    _assert_consistent(out, "override over passthrough")


def test_invariant_holds_across_every_source_and_horizon():
    """Sweep paths and horizons: isotonic_below_domain, isotonic and
    isotonic+stpasa, inside and outside the OLS band, with lines steep enough
    to cross the isotonic curve both ways. passthrough is checked for
    ordering only (test_calibration_engine.py pins why it may publish a value
    outside its band)."""
    forecasts = [-0.5, -0.11, -0.1, -0.076, -0.01, 0.0, 0.02, 0.1, 0.2, 0.35, 3.5]
    horizons = [1.0, 21.9, 22.0, 36.0, 120.0, 120.1, 168.0]
    predictions = [0.001, 0.03, 0.12, 0.25, 4.0]
    seen = set()
    checks = 0
    for with_iso in (True, False):
        for fitted in (True, False):
            for slopes in ((0.4, 0.5, 0.7), (1.6, 2.0, 2.8), (0.05, 0.06, 0.08)):
                bucket = _bucket(
                    with_iso=with_iso, fitted_quantiles=fitted,
                    q10_a=slopes[0], q50_a=slopes[1], q90_a=slopes[2],
                )
                for prediction in predictions:
                    res = _result(bucket, prediction=prediction)
                    for forecast in forecasts:
                        for horizon in horizons:
                            for stp, rf in ((None, None), (STPASA, RUN_FEATURES)):
                                out = _apply(res, forecast, horizon=horizon, stpasa=stp, run_features=rf)
                                seen.add(out["calibrated_source"])
                                label = (
                                    f"sweep x={forecast} h={horizon} pred={prediction} "
                                    f"iso={with_iso} fitted={fitted} slopes={slopes}"
                                )
                                if out["calibrated_source"] == "passthrough":
                                    _assert_ordered(out, label)
                                else:
                                    _assert_consistent(out, label)
                                checks += 1
    expected = {SOURCE_ISOTONIC_BELOW_DOMAIN, "passthrough", "isotonic", "isotonic+stpasa"}
    assert expected <= seen, f"sweep missed a calibration path: {expected - seen}"


# ── The stage-2 band is the prediction plus its residual quantiles ────────────
# Issue #72: the band beside a stage-2 value came from the stage-1 quantile
# lines, which have never seen the nine features. PR #71 re-clamped them
# around the stage-2 value, which bought containment by pulling the nearer
# bound onto the point estimate. First live measurement, QLD1, the run at
# 2026-09-03T07:30:00+10:00, 330 intervals: containment violations 56 to 0,
# bounds collapsed onto the point estimate 36 to 98, of which 82 onto p10
# against 16 onto p90. A collapsed bound reports zero uncertainty on one
# side. The band is now the prediction plus the 10th, 50th and 90th
# percentile of the bucket's leave-one-out residuals: centred on the
# prediction by construction, so it cannot collapse.

def test_loo_residual_equals_an_explicit_refit_without_that_row():
    """``e_i / (1 - h_ii)`` is the error of a fit that excluded row i, checked
    per row against a refit: ten coefficients on sixty rows, the shape the
    stage-2 fit runs at, with a deliberately leveraged final row."""
    rng = np.random.default_rng(72)
    n, p = 60, 10
    X = np.column_stack([np.ones(n), rng.normal(size=(n, p - 1))])
    X[-1, 1:] *= 6.0
    beta = rng.normal(size=p)
    y = X @ beta + rng.normal(scale=0.05, size=n)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    got = _loo_residuals(X, y, coef)
    for i in range(n):
        keep = [j for j in range(n) if j != i]
        coef_i, *_ = np.linalg.lstsq(X[keep], y[keep], rcond=None)
        expected = y[i] - X[i] @ coef_i
        assert abs(got[i] - expected) < 1e-8, f"row {i}: closed form {got[i]:.9f} against refit {expected:.9f}"
    in_sample = y - X @ coef
    assert np.mean(np.abs(got)) > np.mean(np.abs(in_sample)) * 1.05, (
        "leave-one-out residuals should be materially larger than in-sample ones"
    )


def test_conformal_index_errs_wide_and_stays_in_range():
    """The tail indices bracket the plain empirical ones and never escape,
    at every sample size a bucket can plausibly reach."""
    for n in range(OLS_MIN_OBS, 400):
        lo = _conformal_index(n, 0.1)
        hi = _conformal_index(n, 0.9)
        assert 0 <= lo < n and 0 <= hi < n, f"n={n}: index out of range"
        assert lo < hi, f"n={n}: lower index {lo} not below upper {hi}"
        assert lo <= int(round(0.1 * (n - 1))), f"n={n}: lower index {lo} above empirical"
        assert hi >= int(round(0.9 * (n - 1))), f"n={n}: upper index {hi} below empirical"
    assert _conformal_index(50, 0.1) == 4
    assert _conformal_index(50, 0.9) == 45


def test_stage2_band_is_the_prediction_plus_its_residual_quantiles():
    out = _apply(_result(_bucket(), prediction=0.12, resid=_resid(-0.02, -0.001, 0.03)), 0.2)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == 0.12
    assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2
    assert out["p10"] == 0.1, f"expected 0.12 - 0.02, got {out['p10']}"
    assert out["p50"] == 0.119, f"expected 0.12 - 0.001, got {out['p50']}"
    assert out["p90"] == 0.15, f"expected 0.12 + 0.03, got {out['p90']}"
    # The stage-1 lines at 0.2 would have given 0.08 and 0.14.
    assert out["p10"] != 0.08 and out["p90"] != 0.14


def test_stage2_band_never_collapses_onto_the_point_estimate():
    """Sweep the prediction far outside the stage-1 lines: with re-clamped
    lines every prediction below 0.08 collapsed p10 and above 0.14 collapsed
    p90 at this forecast; a residual band sits a fixed distance away."""
    resid = _resid(-0.02, 0.0, 0.03)
    collapsed = []
    for forecast in (0.01, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0):
        for prediction in (0.001, 0.02, 0.05, 0.09, 0.13, 0.2, 0.4, 0.9, 2.5):
            out = _apply(_result(_bucket(), prediction, resid), forecast)
            assert out["calibrated_source"] == "isotonic+stpasa", out
            assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2
            v, p10, p90 = out["calibrated"], out["p10"], out["p90"]
            assert p10 <= v <= p90, f"{forecast}/{prediction}: {out}"
            if p90 == v or (p10 == v and p10 > 0.0):
                collapsed.append((forecast, prediction, out))
    assert not collapsed, f"{len(collapsed)} collapsed bounds, first {collapsed[0]}"


def test_lower_bound_reaching_below_zero_is_published():
    """Before #114 the lower bound was floored at 0.0; negative prices are a
    normal NEM state, so the band keeps the model's own lower bound."""
    out = _apply(_result(_bucket(), 0.004, _resid(-0.02, -0.001, 0.03)), 0.2)
    assert out["p10"] == -0.016, f"expected the residual lower bound, got {out['p10']}"
    assert out["p90"] == 0.034
    assert out["p10"] < out["calibrated"] < out["p90"]


def test_lower_bound_is_floored_at_the_market_floor():
    out = _apply(_result(_bucket(), 0.004, _resid(-1.2, -0.001, 0.03)), 0.2)
    assert out["p10"] == MARKET_PRICE_FLOOR, f"expected the market floor, got {out['p10']}"
    assert out["p10"] < out["calibrated"], "the floor must not collapse the bound"


def test_a_wide_residual_band_is_published_in_full():
    out = _apply(_result(_bucket(), 0.5, _resid(-0.3, 0.01, 0.9)), 0.2)
    assert out["p10"] == 0.2 and out["p50"] == 0.51 and out["p90"] == 1.4


def test_residual_quantiles_are_rejected_when_they_do_not_bracket_zero():
    """A residual band excluding zero would exclude its own point estimate:
    the v3.4.0 band is published instead of 0.13 to 0.15 around 0.12."""
    bad = _resid(0.01, 0.02, 0.03)
    assert not bad.is_fitted
    out = _apply(_result(_bucket(), 0.12, bad), 0.2)
    assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK
    assert (out["p10"], out["p50"], out["p90"]) == (0.08, 0.1, 0.14)


def test_residual_quantiles_are_rejected_below_the_observation_floor():
    assert not _resid(n=OLS_MIN_OBS - 1).is_fitted
    assert _resid(n=OLS_MIN_OBS).is_fitted
    assert not ResidualQuantiles(bucket_key=KEY, q10=None, q50=0.0, q90=0.1, n=99).is_fitted
    assert not _resid(q10=-0.01, q50=0.05, q90=0.02).is_fitted, "unordered"


def test_fallback_reproduces_the_v340_band_and_says_so():
    """With no residual quantiles the band is exactly what v3.4.0 gave, both
    directions of the old collapse, kept deliberately: withholding the
    stage-2 point estimate to protect the band would move the published price
    on a path that is otherwise working. The label makes it non-silent."""
    below = _apply(_result(_bucket(), 0.03, None), 0.2)
    assert below[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK
    assert (below["p10"], below["p50"], below["p90"]) == (0.03, 0.1, 0.14)
    _assert_consistent(below, "fallback below p10")

    above = _apply(_result(_bucket(), 0.25, None), 0.2)
    assert above[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK
    assert (above["p10"], above["p50"], above["p90"]) == (0.08, 0.1, 0.25)
    _assert_consistent(above, "fallback above p90")


def test_the_old_collapse_side_follows_the_displacement_sign():
    """Which bound collapsed was decided by the data, not the machinery. The
    two measurements on #72 disagreed (synthetic 14 p90 against 7 p10, live
    82 p10 against 16 p90) and both are consistent with this mechanism: the
    ratio reports the sign of the stage-2 minus stage-1 displacement, so a
    synthetic ratio carries no information about the market."""
    bucket = _bucket()
    p10_fit, _p50_fit, p90_fit = bucket.raw_band(0.2)
    assert (round(p10_fit, 6), round(p90_fit, 6)) == (0.08, 0.14)
    for prediction in (0.01, 0.03, 0.06, 0.079):
        out = _apply(_result(bucket, prediction, None), 0.2)
        assert out["p10"] == out["calibrated"], f"{prediction} should collapse p10"
        assert out["p90"] != out["calibrated"]
    for prediction in (0.141, 0.2, 0.5):
        out = _apply(_result(bucket, prediction, None), 0.2)
        assert out["p90"] == out["calibrated"], f"{prediction} should collapse p90"
        assert out["p10"] != out["calibrated"]


def test_band_source_is_published_on_every_path():
    deep_negative = -0.15  # below this fixture's domain (0.0 to 0.3)
    cases = [
        (_result(_bucket()), deep_negative, BAND_SOURCE_STAGE1),
        (_result(_bucket(fitted_quantiles=False)), deep_negative, BAND_SOURCE_PASSTHROUGH),
        (_result(_bucket(with_iso=False)), 0.2, BAND_SOURCE_STAGE1_RAW),
        (_result(_bucket()), 0.2, BAND_SOURCE_STAGE1),
        (_result(_bucket(), 0.12, _resid()), 0.2, BAND_SOURCE_STAGE2),
        (_result(_bucket(), 0.12, None), 0.2, BAND_SOURCE_STAGE2_FALLBACK),
    ]
    for res, forecast, expected in cases:
        out = _apply(res, forecast)
        assert out[BAND_SOURCE_KEY] == expected, f"forecast {forecast}: expected {expected}, got {out.get(BAND_SOURCE_KEY)}"
    # The engine key and the sensor attribute name are one string, so they
    # cannot drift the way the calibration inputs did in issue #66.
    assert BAND_SOURCE_KEY == ATTR_CAL_BAND_SOURCE == "band_source"


def test_stage1_publications_are_byte_for_byte_unchanged():
    """Residual quantiles must not move a single stage-1 number on any path
    that does not reach stage 2."""
    with_resid = _result(_bucket(), 0.12, _resid())
    without = copy.deepcopy(with_resid)
    for m in without.ols_models.values():
        m.resid = None
    fields = ("calibrated", "p10", "p50", "p90", "calibrated_source", "n_obs")
    for forecast in (-0.2, -0.05, 0.0, 0.02, 0.1, 0.2, 0.4, 3.5):
        # Horizon below the OLS band, so stage 2 is gated off entirely.
        a = with_resid.apply(forecast, horizon_hours=4.0, hour_of_day=HOUR)
        b = without.apply(forecast, horizon_hours=4.0, hour_of_day=HOUR)
        for f in fields:
            assert a[f] == b[f], f"forecast {forecast}: {f} moved, {a[f]} vs {b[f]}"
        if forecast < 0.0:  # the below-domain bypass, gated inside the band too
            c = _apply(with_resid, forecast)
            assert c["calibrated"] == round(_bucket().edge_value, 6)
            assert c["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN


def test_a_real_stage2_fit_produces_usable_residual_quantiles():
    result = _peak_fit()
    fitted = [m for m in result.ols_models.values() if len(m.coef) >= 2]
    assert fitted, f"expected fitted OLS buckets, got {result.ols_models}"
    for m in fitted:
        r = m.resid
        assert r is not None, f"{m.bucket_key}: no residual quantiles"
        assert r.is_fitted, f"{m.bucket_key}: residual quantiles unusable, {r}"
        assert r.n == m.n_train, f"{m.bucket_key}: {r.n} residuals for {m.n_train} rows"
        assert r.q10 < 0.0 < r.q90, f"{m.bucket_key}: {r}"
        assert r.q10 <= r.q50 <= r.q90
        assert r.bucket_key == m.bucket_key


def test_real_fit_sweep_has_no_collapsed_stage2_bound():
    """On unfixed main this sweep collapses a bound on a large minority of
    the intervals it reaches."""
    result = _peak_fit()
    stage2 = 0
    collapsed = []
    for horizon, hour in _CELLS:
        for i in range(120):
            sf = StpasaFeatures(
                log_surplus=math.log1p(400.0 + i * 40.0),
                log_solar=math.log1p(i * 35.0),
                log_demand=math.log(5000.0 + i * 35.0),
                poe_spread_n=0.1 + i * 0.001,
                stpasa_run_at=STPASA.stpasa_run_at,
            )
            out = result.apply(0.005 + i * 0.0045, horizon_hours=horizon, hour_of_day=hour, stpasa=sf, run_features=RUN_FEATURES)
            if out["calibrated_source"] != "isotonic+stpasa":
                continue
            stage2 += 1
            assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2, out
            v, p10, p50, p90 = out["calibrated"], out["p10"], out["p50"], out["p90"]
            assert p10 <= p50 <= p90, out
            assert p10 <= v <= p90, out
            # The zero floor is the one legitimate way p10 stops tracking the
            # prediction, and it cannot equal a strictly positive prediction.
            if p90 == v or (p10 == v and p10 > 0.0):
                collapsed.append(out)
    assert stage2 >= 100, f"fixture reached only {stage2} stage-2 intervals"
    assert not collapsed, f"{len(collapsed)} of {stage2} stage-2 intervals collapsed a bound, first {collapsed[0]}"


def test_stage2_band_width_is_the_bucket_residual_spread():
    """Away from the floor the width is q90 - q10 of the bucket alone: the
    additive assumption stated plainly. A reviewer who disagrees with it
    should read this test as where to change it."""
    result = _peak_fit()
    for key, m in result.ols_models.items():
        if len(m.coef) < 2 or m.resid is None or not m.resid.is_fitted:
            continue
        expected = round(m.resid.q90, 6) - round(m.resid.q10, 6)
        horizon = 30.0 if key.startswith("h24_48") else 60.0
        seen = 0
        for i in range(40):
            out = result.apply(0.05 + i * 0.005, horizon_hours=horizon, hour_of_day=17, stpasa=STPASA, run_features=RUN_FEATURES)
            if out["calibrated_source"] != "isotonic+stpasa" or out["p10"] == 0.0:
                continue
            seen += 1
            width = out["p90"] - out["p10"]
            assert abs(width - expected) < 2e-6, f"{key}: width {width:.6f} against residual spread {expected:.6f}"
        assert seen, f"{key}: no unfloored stage-2 interval to check"


# ── Storage ───────────────────────────────────────────────────────────────────

def test_stage2_models_survive_the_storage_round_trip():
    """Coefficients, n_train, r2, residual quantiles (#72) and feature ranges
    (#147) all outlive a restart. The isotonic model is not persisted, so on
    the passthrough path a restart lands on stage 2 still overrides the point
    estimate; without the residual quantiles every such interval would
    publish the old re-clamped stage-1 band."""
    engine = CalibrationEngine()
    result = _peak_fit()
    stored = engine.to_storage(result)
    assert "ols_models" in stored
    restored = engine.from_storage(stored)
    fitted = 0
    for key, m in result.ols_models.items():
        r = restored.ols_models[key]
        assert r.coef == m.coef, f"{key}: coef changed"
        assert r.n_train == m.n_train and abs(r.r2 - m.r2) < 1e-9
        assert r.feature_min == m.feature_min and r.feature_max == m.feature_max
        if len(m.coef) >= 2:
            fitted += 1
            assert stored["ols_models"][key]["feature_min"] == m.feature_min
            assert stored["ols_models"][key]["feature_max"] == m.feature_max
        if m.resid is None:
            assert r.resid is None
            continue
        assert r.resid is not None, f"{key}: residual quantiles lost in storage"
        assert (r.resid.q10, r.resid.q50, r.resid.q90, r.resid.n) == (m.resid.q10, m.resid.q50, m.resid.q90, m.resid.n)
    assert fitted >= 1

    for key in restored.models:
        restored.models[key].iso_model = None
    out = restored.apply(0.18, horizon_hours=_CELLS[0][0], hour_of_day=_CELLS[0][1], stpasa=STPASA, run_features=RUN_FEATURES)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2
    assert out["p10"] < out["calibrated"] < out["p90"]


def test_legacy_storage_payloads_load_and_degrade_cleanly():
    """Payloads written before each change degrade rather than raise."""
    engine = CalibrationEngine()
    result = _peak_fit()
    key = _bucket_key(*_CELLS[0])

    # Before stage 2 existed: no ols_models key at all.
    stored = engine.to_storage(result)
    stored.pop("ols_models", None)
    assert engine.from_storage(stored).ols_models == {}

    # Before #72: no residual quantiles, so the fallback band, labelled.
    stored = engine.to_storage(result)
    for md in stored["ols_models"].values():
        md.pop("resid", None)
    restored = engine.from_storage(stored)
    for m in restored.ols_models.values():
        assert m.resid is None
    out = restored.apply(0.18, horizon_hours=_CELLS[0][0], hour_of_day=_CELLS[0][1], stpasa=STPASA, run_features=RUN_FEATURES)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK

    # Before #147: no feature ranges, so the range gate passes.
    stored = engine.to_storage(result)
    for md in stored["ols_models"].values():
        md.pop("feature_min", None)
        md.pop("feature_max", None)
    legacy = engine.from_storage(stored).ols_models[key]
    assert legacy.feature_min == [] and legacy.feature_max == []
    assert legacy.serves([0.0] * 9)


# ── The stage-2 feature is the unfloored stage-1 value ────────────────────────
# Issue #85: the first stage-2 feature was taken from apply_all()["calibrated"],
# which then floored the isotonic prediction at 0.0. For a raw forecast inside
# the fitted domain but below the crossing, the feature read 0.0 while the
# settled actual was negative, and the fitted iso_cal coefficient absorbed the
# error: one such row in a 78 row bucket moved it by +8.1 percent and sixteen
# by +87.4 percent, sign consistent and monotone at every count and seed. The
# feature is now read through stage2_iso_feature on both the fit and the serve
# path, one definition so the two cannot drift apart (the #68 bug class).

def test_stage2_iso_feature_helper_is_monotone_unfloored_and_gap_free():
    bucket = _negative_bucket()
    previous = None
    for i in range(1601):
        x = round(-0.400 + i * 0.0005, 6)
        got = stage2_iso_feature(bucket.apply_all(x), x)
        if bucket.is_below_domain(x):
            assert got == round(bucket.edge_value, 6)
        else:
            assert got == round(_raw_iso(bucket, x), 6)
        if previous is not None:
            assert got >= previous - 1e-9, f"the feature decreased between {x - 0.0005} and {x}"
        previous = got
    # No gap: the feature takes values inside the old (-0.10, 0.0) hole.
    attained = [
        stage2_iso_feature(bucket.apply_all(round(-0.0999 + i * 0.001, 6)), round(-0.0999 + i * 0.001, 6))
        for i in range(95)
    ]
    assert any(-0.10 < v < 0.0 for v in attained), "the feature has no attainable value between the threshold and zero"


def test_stage2_iso_feature_falls_back_to_the_published_value():
    """A dict without the key (an older shaped dict during a rolling upgrade)
    degrades to the published value, then the raw, rather than raising."""
    assert stage2_iso_feature({"calibrated": 0.05}, 0.07) == 0.05
    assert stage2_iso_feature({}, 0.07) == 0.07
    assert stage2_iso_feature({ISO_FEATURE_KEY: None, "calibrated": 0.05}, 0.07) == 0.05


def test_floored_rows_no_longer_bias_the_fitted_coefficient():
    """The regression case at every count the issue reports, two seeds.

    On main the coefficient rose monotonically with the floored row count.
    Unfloored, the shift against the same dataset with no floored rows is
    under 4 percent at every count and not consistently signed; the bar is 5
    percent, which main fails at k=1 already. The shape is pinned too: a
    coefficient that climbs with every extra floored row is the regression
    absorbing the error into the slope, and a single count could pass by luck.
    """
    for seed in (7, 42):
        base_obs, stpasa = _build(seed=seed)
        reference = _coef1(_stage2(base_obs, stpasa))
        assert reference is not None, "the target bucket was not fitted"
        coefs = []
        for k in (1, 2, 4, 8, 16):
            coef = _coef1(_stage2(_promote(base_obs, k), stpasa))
            assert coef is not None
            coefs.append(coef)
            shift = (coef - reference) / abs(reference)
            assert abs(shift) < 0.05, (
                f"seed {seed}, {k} floored rows of 78 moved the iso_cal coefficient by "
                f"{shift * 100:+.1f} percent, from {reference:.4f} to {coef:.4f}"
            )
        assert not all(b > a for a, b in zip(coefs, coefs[1:])), (
            f"seed {seed}: the coefficient still climbs with the floored row count: {coefs}"
        )
        assert max(coefs) < reference * 1.10, (
            f"seed {seed}: sixteen floored rows still inflate the coefficient: {reference:.4f}, {coefs}"
        )


def test_fit_path_uses_the_same_unfloored_feature_as_serving():
    """The fitted coefficients match a hand-built unfloored design matrix to
    1e-6; a fit that still floored would differ in the first coefficient by
    the biases measured above."""
    observations, stpasa_by_key = _build(seed=11)
    observations = _promote(observations, 8)
    iso_result = CalibrationEngine().fit(observations, region="QLD1")
    run_features = _compute_run_features(observations)
    rows = []
    for obs in observations:
        if obs.is_intervention:
            continue
        if obs.horizon_hours < OLS_MIN_HORIZON_H or obs.horizon_hours > OLS_MAX_HORIZON_H:
            continue
        if obs.actual_rrp >= SPIKE_THRESHOLD or obs.pd7day_forecast >= SPIKE_THRESHOLD:
            continue
        sf = stpasa_by_key.get(f"{obs.interval_time}|{obs.forecast_run_at}")
        rf = run_features.get(obs.forecast_run_at)
        if sf is None or rf is None:
            continue
        if _bucket_key(obs.horizon_hours, obs.hour_of_day) != TARGET:
            continue
        bucket = iso_result.get_bucket(obs.horizon_hours, obs.hour_of_day)
        if bucket.is_below_domain(obs.pd7day_forecast):
            continue
        feature = round(_raw_iso(bucket, obs.pd7day_forecast), 6)
        rows.append(([1.0] + _feature_vec(feature, rf, sf, obs.horizon_hours), obs.actual_rrp))
    assert len(rows) >= OLS_MIN_OBS
    X = np.array([r[0] for r in rows], dtype=float)
    y = np.array([r[1] for r in rows], dtype=float)
    expected, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted = CalibrationEngine().fit_ols_stage2(observations, stpasa_by_key, region="QLD1")[TARGET].coef
    assert len(fitted) == len(expected)
    for i, (got, want) in enumerate(zip(fitted, expected)):
        assert abs(got - want) < 1e-6, f"coefficient {i} is {got}, an unfloored design gives {want}"
    # The negative feature values really are in the matrix: the eight promoted
    # rows sit at an isotonic prediction of -0.093, and the generator also
    # produces a few mildly negative ordinary rows main floored to 0.0.
    promoted = int(np.isclose(X[:, 1], -0.093).sum())
    negatives = int((X[:, 1] < 0.0).sum())
    assert promoted == 8, f"expected the eight promoted rows at a feature of -0.093, found {promoted}"
    assert negatives >= promoted, "the floored rows did not reach the design matrix as negatives"


def test_serve_path_feeds_the_unfloored_feature_to_the_ols_model():
    """With a coefficient vector that is zero everywhere but iso_cal, the
    stage-2 prediction is a direct read of the feature, so it must reflect
    the negative isotonic value, not 0.0. A negative intercept keeps the
    prediction on the same side of zero as stage 1 (#114)."""
    bucket = _negative_bucket()
    result = _result(bucket, coef=[-0.02, 1.0] + [0.0] * 8)
    for x in (-0.07, -0.05, -0.02):
        out = _neg_apply(result, x)
        assert out["calibrated_source"] == "isotonic+stpasa"
        want = round(-0.02 + round(_raw_iso(bucket, x), 6), 6)
        assert abs(out["calibrated"] - want) < 1e-6, f"stage 2 at {x} published {out['calibrated']}, an unfloored feature gives {want}"
        assert abs(out["calibrated"] - (-0.02)) > 1e-4, "a floored feature would have produced exactly -0.02"


# ── Which rows enter the fit, and the leverage screen ─────────────────────────
# Issue #79: at the time apply_all returned the raw value for any forecast at
# or below a fixed -0.10 $/kWh, so such a row sat on the far side of a gap from
# the cluster and was a high leverage point in OLS: measured hat leverage 0.92
# to 0.98 against a bucket mean near 0.13, and one mis-joined deep negative
# observation moved the fitted iso_cal coefficient from +1.13 to -0.15 in a 78
# row bucket. PR #80 dropped those rows. Issue #117 replaced the fixed boundary
# with the bucket's fitted domain, read from BucketModel.is_below_domain by
# both the fit filter and the serve bypass, and made the leverage protection
# explicit: rows whose hat leverage exceeds STAGE2_LEVERAGE_MULTIPLE times the
# mean p/n are dropped and the bucket refitted once, whatever their price.
# OLS_MIN_OBS is counted after both filters.

def test_is_below_domain_boundary():
    """Inclusive at the lowest training forecast; a bucket with no isotonic
    model has no domain and never reports below it."""
    obs, _sp = _build()
    bucket = CalibrationEngine().fit(obs).get_bucket(TARGET_H, TARGET_HOUR)
    assert bucket.domain_min == _FIXTURE_FLOOR, bucket.domain_min
    assert not bucket.is_below_domain(_FIXTURE_FLOOR)
    assert bucket.is_below_domain(_FIXTURE_FLOOR - 1e-9)
    assert not bucket.is_below_domain(_FIXTURE_FLOOR + 1e-9)
    assert not bucket.is_below_domain(0.0)
    assert bucket.is_below_domain(-0.09)
    assert bucket.is_below_domain(-5.0)
    empty = BucketModel(bucket_key=TARGET)
    assert empty.domain_min is None
    assert not empty.is_below_domain(-5.0)


def test_serve_path_agrees_with_the_shared_predicate():
    """apply_all's source agrees with is_below_domain across the range, not
    at hand-picked values: the point of the shared predicate is that the fit
    filter and the serve bypass cannot drift apart."""
    obs, _sp = _build()
    bucket = CalibrationEngine().fit(obs).get_bucket(TARGET_H, TARGET_HOUR)
    for i in range(-400, 401):
        x = i / 1000.0
        is_bypass = bucket.apply_all(x)["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
        assert is_bypass == bucket.is_below_domain(x), (
            f"serve path and predicate disagree at raw {x} $/kWh: "
            f"apply_all bypass={is_bypass}, predicate={bucket.is_below_domain(x)}"
        )


def test_below_domain_rows_are_excluded_by_exactly_their_count():
    """n_train falls by exactly the number of below-domain rows, keyed off the
    domain floor alone and not off how far below it a row sits."""
    obs, sp = _build()
    n_before = _stage2(obs, sp)[TARGET].n_train
    assert n_before >= OLS_MIN_OBS, "target bucket must be fitted in the baseline"
    reference = CalibrationEngine().fit(obs)
    for depth in (-0.021, -0.10, -5.00):
        for k in (1, 2, 11):
            m = _fit_stage2_with_domain_from(reference, _promote(obs, k, depth), sp)[TARGET]
            assert m.n_train == n_before - k, f"depth {depth} $/kWh, k={k}: expected n_train {n_before - k}, got {m.n_train}"


def test_rows_inside_the_domain_are_still_fitted():
    """A mildly negative forecast inside the domain is served by stage 2, so it
    must be fitted; excluding it would be the skew issue #68 was about. Under
    the domain rule that holds at any forecast the bucket was trained on,
    including a cluster of deep negatives."""
    obs, sp = _build()
    n_before = _stage2(obs, sp)[TARGET].n_train
    for depth, k in ((-0.01, 6), (-0.10, 11), (-0.30, 11), (-1.00, 11)):
        m = _stage2(_promote(obs, k, depth), sp)[TARGET]
        assert m.n_train == n_before, f"{k} rows at {depth} $/kWh are inside the domain; n_train moved from {n_before} to {m.n_train}"


def test_isolated_row_is_screened_by_leverage_whatever_its_price():
    """One or two rows far from the cluster are dropped at either end; a
    cluster is kept. The old boundary got exactly this wrong: an oversupplied
    SA1 solar afternoon is many rows near -0.19 $/kWh, all genuine."""
    obs, sp = _build()
    n_before = _stage2(obs, sp)[TARGET].n_train
    for depth in (-0.30, -5.00, 0.90):
        for k in (1, 2):
            m = _stage2(_promote(obs, k, depth), sp)[TARGET]
            assert m.n_train == n_before - k, f"{k} isolated row(s) at {depth} $/kWh should be screened; n_train {m.n_train}"
        m = _stage2(_promote(obs, 5, depth), sp)[TARGET]
        assert m.n_train == n_before, f"a cluster of 5 rows at {depth} $/kWh supports itself and must be kept; n_train {m.n_train}"


def test_corrupt_deep_negative_row_cannot_flip_the_coefficient():
    """A single mis-joined deep negative row must not invert the iso_cal slope,
    whatever actual price landed on it and whatever the seed.

    Before any filter, one row whose actual was mis-joined onto a deep
    negative forecast took the coefficient from about +1.13 to about -0.15
    across five seeds, and the sweep over its actual price crossed zero,
    reaching -0.45 at 1.20 $/kWh. The row now widens the domain rather than
    falling outside it, so the leverage screen carries this guarantee. The
    magnitude is deliberately NOT pinned: the stage-1 isotonic fit still sees
    the row, and a corrupt actual propagates through the pooling to the
    stage-1 feature of other rows, a spread of about 0.85 across this sweep,
    most of it from the 1.20 case. A real remaining exposure, reported rather
    than asserted away; narrowing it would change published stage-1 values.
    """
    obs, sp = _build(seed=7)
    ref = _coef1(_stage2(obs, sp))
    assert ref is not None and ref > 0.0, f"baseline coefficient must be positive, got {ref}"
    coefs = {}
    for bad_actual in (-0.55, 0.0, 0.15, 0.60, 1.20):
        c = _coef1(_stage2(_promote(obs, 1, -0.50, actual=bad_actual), sp))
        assert c is not None, f"bucket must stay fitted at actual {bad_actual}"
        coefs[bad_actual] = c
    inverted = {k: v for k, v in coefs.items() if v <= 0.0}
    assert not inverted, f"a corrupt deep negative row inverted the iso_cal slope at these actual prices: {inverted}"
    # The plausible mis-join (an evening actual on a midday row) across seeds.
    for seed in (42, 101):
        obs, sp = _build(seed=seed)
        ref = _coef1(_stage2(obs, sp))
        assert ref is not None and ref > 0.0, f"seed {seed}: baseline coefficient must be positive, got {ref}"
        got = _coef1(_stage2(_promote(obs, 1, -0.50, actual=0.15), sp))
        assert got is not None, f"seed {seed}: bucket must still be fitted"
        assert got > 0.0, f"seed {seed}: one mis-joined deep negative row inverted the iso_cal coefficient, {ref:.4f} -> {got:.4f}"


def test_bucket_thinned_below_ols_min_obs_falls_back_cleanly():
    """A bucket that only clears OLS_MIN_OBS by including excluded rows falls
    back to an empty OlsModel; one sitting exactly at the floor still fits."""
    obs, sp = _build()
    n_before = _stage2(obs, sp)[TARGET].n_train
    assert n_before >= OLS_MIN_OBS, f"fixture must start above the floor; n_train={n_before}"
    reference = CalibrationEngine().fit(obs)
    k = n_before - OLS_MIN_OBS + 1
    models = _fit_stage2_with_domain_from(reference, _promote(obs, k, -0.30), sp)
    assert TARGET in models, "a thinned bucket must still appear in the result for diagnostics"
    assert models[TARGET].coef == [], f"bucket with {n_before - k} surviving rows must not be fitted"
    assert models[TARGET].n_train == 0
    at_floor = _fit_stage2_with_domain_from(reference, _promote(obs, k - 1, -0.30), sp)
    assert len(at_floor[TARGET].coef) >= 2, f"bucket sitting exactly at OLS_MIN_OBS={OLS_MIN_OBS} must still fit"
    assert at_floor[TARGET].n_train == OLS_MIN_OBS


def test_leverage_screen_counts_ols_min_obs_after_dropping_rows():
    """A bucket exactly at the floor that loses a screened row falls back.

    k rows below the pinned domain leave exactly OLS_MIN_OBS survivors; one of
    those is moved to an isolated spike just under SPIKE_THRESHOLD, which
    survives the domain filter but not the leverage screen (at n = 50 and
    p = 10 the screen fires above a leverage of 0.6).
    """
    obs, sp = _build()
    n_before = _stage2(obs, sp)[TARGET].n_train
    thinned = _promote(obs, n_before - OLS_MIN_OBS, -0.30)
    spiked, taken = [], 0
    for o in thinned:
        if taken < 1 and _in_target(o) and o.pd7day_forecast > -0.30:
            spiked.append(o._replace(pd7day_forecast=2.90, actual_rrp=_TRUE_SLOPE * 2.90 + _TRUE_INTERCEPT))
            taken += 1
        else:
            spiked.append(o)
    assert taken == 1
    m = _fit_stage2_with_domain_from(CalibrationEngine().fit(obs), spiked, sp)[TARGET]
    assert m.coef == [] and m.n_train == 0, f"a bucket left with {OLS_MIN_OBS - 1} rows after the leverage screen must fall back; got n_train {m.n_train}"


def test_thinned_bucket_serves_the_stage_one_result():
    obs, sp = _build()
    n_before = _stage2(obs, sp)[TARGET].n_train
    result = CalibrationEngine().fit(obs)
    result.ols_models = _fit_stage2_with_domain_from(result, _promote(obs, n_before - OLS_MIN_OBS + 1, -0.30), sp)
    sf = StpasaFeatures(
        log_surplus=math.log1p(1400.0), log_solar=math.log1p(2800.0), log_demand=math.log(8200.0),
        poe_spread_n=0.18, stpasa_run_at=_ANCHOR.replace(hour=3, minute=30).isoformat(),
    )
    out = result.apply(0.05, horizon_hours=TARGET_H, hour_of_day=TARGET_HOUR, stpasa=sf, run_features=_RF)
    assert out["calibrated_source"] != "isotonic+stpasa", f"a bucket below OLS_MIN_OBS must not serve a stage-2 value; got {out['calibrated_source']!r}"
    assert out["calibrated"] is not None


def test_bucket_of_only_below_domain_rows_still_appears():
    """apply() treats a missing key and an empty coef list the same, but the
    diagnostic surface must still show the bucket."""
    obs, sp = _build()
    total_in_target = sum(1 for o in obs if _in_target(o))
    models = _fit_stage2_with_domain_from(CalibrationEngine().fit(obs), _promote(obs, total_in_target, -0.30), sp)
    assert TARGET in models, f"bucket {TARGET} disappeared when all {total_in_target} of its rows were excluded"
    assert models[TARGET].coef == []


def test_other_buckets_are_untouched():
    """Both filters are per row: a bucket with no excluded or screened rows
    fits identically."""
    obs, sp = _build()
    before = _stage2(obs, sp)
    after = _stage2(_promote(obs, 1, -0.30), sp)
    others = [k for k in before if k != TARGET]
    assert others, "fixture must populate more than one bucket"
    assert after[TARGET].n_train == before[TARGET].n_train - 1
    for key in others:
        assert after[key].n_train == before[key].n_train, key
        assert after[key].coef == before[key].coef, f"bucket {key} coefficients moved despite having no screened rows"


def test_ols_min_obs_is_fifty():
    """A 9-feature fit on 10 rows (the old MIN_OBS) was a severe over-fit."""
    assert OLS_MIN_OBS == 50


# ── A below-domain clip is never overridden ───────────────────────────────────
# Issue #73: the step-2 gate never inspected calibrated_source, so a deeply
# negative raw forecast that reached the deliberate negative bypass was still
# eligible for the OLS override. The only protection was the later
# ``prediction <= 0.0`` guard, which fails exactly when the prediction is
# positive: the published value flips sign from "paid to consume" to "pay to
# consume".

# Shaped like a real weak-raw-signal fit: a modest slope on the stage-1 value
# and a large positive demand term, so the prediction at a deeply negative
# forecast comes out positive (about +0.04 at raw -0.15, the issue's case).
_SIGN_FLIP_COEF = [-1.72, 0.84, -0.37, -0.31, -0.21, -0.38, -0.003, -0.004, 0.245, -0.36]


def test_positive_prediction_does_not_override_negative_passthrough():
    result = _result_with_ols(_SIGN_FLIP_COEF)
    raw = -0.15
    # The fixture must reproduce the hazard, or the test passes vacuously.
    prediction = result.ols_models[_bucket_key(36.0, 12)].predict(_feature_vec(raw, _RF, _SF, 36.0))
    assert prediction > 0.0, f"fixture is not exercising the hazard: OLS prediction {prediction:.4f} is not positive"
    out = result.apply(raw, horizon_hours=36.0, hour_of_day=12, stpasa=_SF, run_features=_RF)
    assert out["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN, out["calibrated_source"]
    assert out["calibrated"] == result.get_bucket(36.0, 12).apply_all(raw)["calibrated"]
    assert out["calibrated"] <= 0.0
    assert "stpasa_run_at" not in out


def test_no_sign_flip_sweep():
    """Whenever stage 1 returns isotonic_below_domain, apply republishes that
    value with that source and never a value of the opposite sign."""
    coef_sets = [
        _SIGN_FLIP_COEF,
        [0.50, 0.20, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],   # positive for any input
        [0.02, -1.50, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # negative slope: worst case
        [0.005, 1.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # well behaved
    ]
    raws = [-3.0, -1.0, -0.5, -0.25, -0.15, -0.1001, -0.10]
    for coef in coef_sets:
        for horizon in (22.0, 36.0, 120.0):       # both band edges and the middle
            for hour in (3, 13, 18):              # shoulder, solar, peak
                result = _result_with_ols(coef, horizon_hours=horizon, hour_of_day=hour)
                edge = result.get_bucket(horizon, hour).edge_value
                for raw in raws:
                    out = result.apply(raw, horizon_hours=horizon, hour_of_day=hour, stpasa=_SF, run_features=_RF)
                    label = f"raw={raw} h={horizon} hour={hour} coef={coef[:2]}"
                    assert out["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN, f"{label}: source {out['calibrated_source']}"
                    # The edge level (#123), clamped only by the market floor (#114).
                    assert out["calibrated"] == round(max(edge, MARKET_PRICE_FLOOR), 6), f"{label}: published {out['calibrated']}"
                    assert out["calibrated"] <= 0.0, f"{label}: sign flipped to {out['calibrated']}"


def test_fitted_model_without_negative_training_rows_would_flip():
    """Evidence case: a genuinely fitted stage-2 model on a training set with
    no in-band row at or below -0.10 still predicts a positive value at a
    deeply negative forecast, and apply refuses to publish it."""
    rng = random.Random(17)
    run_at = (_ANCHOR - timedelta(days=1)).replace(hour=3, minute=30).isoformat()
    obs = []
    stpasa_by_key = {}
    for j in range(8):  # near-term rows so _compute_run_features has a run
        near = (_ANCHOR - timedelta(days=1)).replace(hour=4 + j)
        obs.append(_obs(near, 2.0 + j, rng.uniform(0.05, 0.25), rng.uniform(0.05, 0.30), run_at))
    # In-band rows: the raw forecast is weakly informative and demand carries
    # the signal, the regime stage 2 exists for.
    for i in range(300):
        interval = (_ANCHOR - timedelta(days=i % 60)).replace(hour=12, minute=(i % 2) * 30, second=(i % 50))
        fc = rng.uniform(-0.0999, 0.30)
        surplus = rng.uniform(500.0, 5000.0)
        solar = rng.uniform(0.0, 4000.0)
        demand50 = rng.uniform(5000.0, 9000.0)
        actual = max(-0.02, 0.25 * fc + 0.00004 * (demand50 - 6000.0) - 6e-6 * solar + rng.gauss(0, 0.02))
        o = _obs(interval, 36.0, fc, actual, run_at, hour_of_day=12)
        obs.append(o)
        stpasa_by_key[f"{o.interval_time}|{run_at}"] = StpasaFeatures(
            log_surplus=math.log1p(surplus), log_solar=math.log1p(solar),
            log_demand=math.log(max(demand50, 1.0)), poe_spread_n=0.2, stpasa_run_at=run_at,
        )
    assert not [
        o for o in obs
        if o.pd7day_forecast <= -0.10 and OLS_MIN_HORIZON_H <= o.horizon_hours <= OLS_MAX_HORIZON_H
    ], "fixture should contain no in-band rows at or below -0.10"

    engine = CalibrationEngine()
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)
    model = result.ols_models.get(_bucket_key(36.0, 12))
    assert model is not None and len(model.coef) >= 2, "stage 2 did not fit"

    raw = -0.101
    prediction = model.predict(_feature_vec(raw, _RF, _SF, 36.0))
    assert prediction > 0.0, f"expected the fitted model to extrapolate positive at a deeply negative forecast, got {prediction:.4f}"
    out = result.apply(raw, horizon_hours=36.0, hour_of_day=12, stpasa=_SF, run_features=_RF)
    assert out["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN, out["calibrated_source"]
    assert out["calibrated"] == result.get_bucket(36.0, 12).apply_all(raw)["calibrated"]
    assert out["calibrated"] <= 0.0


# ── Stage 2 is served only inside the feature range it was fitted on ─────────
# Issue #147: SA1, run 07:30 on 8 September 2026, Saturday 12 September 12:30
# (horizon 101 h, bucket h96plus__solar). STPASA had demand50 at 24 MW, giving
# log_demand 3.18 against a training range of about 5.7 to 7.3, and the
# regression extrapolated to publish -$0.864/kWh for a raw -$0.10 that stage 1
# put at -$0.012; the sign and floor gates both passed. Neighbouring rows had
# negative demand50 (log_demand 0, poe_spread_n 2 to 6) and were refused only
# because their predictions happened to land below the floor. OlsModel now
# carries each feature's training min and max, and stage 2 is refused when the
# extrapolation cost, sum of |coef_i| times the distance each feature lies
# outside its range, exceeds half the bucket's residual spread (#153: the bare
# range test refused 19 SA1 rows whose cost was 0.0003 $/kWh against a 0.037
# allowance). stpasa_feature_values returns None for demand50 below the
# transform's floor on both the training and the serving side.

_FD_HORIZON = 101.0
_FD_HOUR = 12
_FD_KEY = _bucket_key(_FD_HORIZON, _FD_HOUR)
_FD_RUN_AT = (_ANCHOR - timedelta(days=1)).replace(hour=7, minute=30).isoformat()
# Training demand in the hundreds to over a thousand MW, as on the live
# install; the serve-time cases sit inside and far below that range.
_TRAIN_DEMAND_MW = (300.0, 1500.0)
_LIVE_DEMAND_MW = 24.0
_IN_RANGE_DEMAND_MW = 500.0


def _stpasa_at_demand(demand50: float, surplus: float = 1800.0, solar: float = 900.0) -> StpasaFeatures:
    """Features from the shared transform at a chosen demand50."""
    values = stpasa_feature_values(surplus, solar, demand50, demand50 * 1.1, demand50 * 0.9)
    assert values is not None
    log_surplus, log_solar, log_demand, poe_spread_n = values
    return StpasaFeatures(
        log_surplus=log_surplus, log_solar=log_solar, log_demand=log_demand,
        poe_spread_n=poe_spread_n, stpasa_run_at=_FD_RUN_AT,
    )


def _low_demand_fit(seed: int = 147):
    """(engine, result, run_features): a stage-1 and stage-2 fit on midday
    rows with demand 300 to 1500 MW. run_features are those computed from the
    training run itself, so a serve-time vector built from them differs from
    the training rows only in what the test varies."""
    rng = random.Random(seed)
    obs: list[Observation] = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}
    for j in range(8):
        near = (_ANCHOR - timedelta(days=1)).replace(hour=8 + j)
        obs.append(_obs(near, 0.5 + j, rng.uniform(0.05, 0.25), rng.uniform(0.05, 0.30), _FD_RUN_AT))
    # The actual tracks the raw forecast with a modest demand term, so the
    # fitted stage 2 is well behaved inside its range.
    for i in range(300):
        interval = (_ANCHOR - timedelta(days=i % 60)).replace(hour=_FD_HOUR, minute=(i % 2) * 30, second=(i % 50))
        fc = rng.uniform(-0.24, 0.30)
        surplus = rng.uniform(800.0, 3000.0)
        solar = rng.uniform(300.0, 1500.0)
        demand50 = rng.uniform(*_TRAIN_DEMAND_MW)
        actual = 0.9 * fc + 0.00003 * (demand50 - 900.0) + rng.gauss(0, 0.01)
        o = _obs(interval, _FD_HORIZON, fc, actual, _FD_RUN_AT, hour_of_day=_FD_HOUR)
        obs.append(o)
        stpasa_by_key[f"{o.interval_time}|{_FD_RUN_AT}"] = _stpasa_at_demand(demand50, surplus=surplus, solar=solar)
    engine = CalibrationEngine()
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)
    model = result.ols_models.get(_FD_KEY)
    assert model is not None and len(model.coef) >= 2, "stage 2 did not fit"
    assert len(model.feature_min) == len(STAGE2_FEATURE_NAMES)
    assert len(model.feature_max) == len(STAGE2_FEATURE_NAMES)
    return engine, result, _compute_run_features(obs)[_FD_RUN_AT]


def _fd_vec(result: CalibrationResult, raw: float, rf: RunFeatures, sf: StpasaFeatures) -> list[float]:
    iso_feature = stage2_iso_feature(result.get_bucket(_FD_HORIZON, _FD_HOUR).apply_all(raw), raw)
    return _feature_vec(iso_feature, rf, sf, _FD_HORIZON)


def _fd_apply(result: CalibrationResult, raw: float, rf: RunFeatures, sf: StpasaFeatures) -> dict:
    return result.apply(raw, horizon_hours=_FD_HORIZON, hour_of_day=_FD_HOUR, stpasa=sf, run_features=rf)


def test_demand_far_below_training_range_falls_back_to_stage1():
    """The live case: 24 MW demand against a 300 to 1500 MW training range.
    log_demand is the one feature outside its range, its extrapolation cost
    exceeds the allowance, and stage 1 is published untouched."""
    _engine, result, rf = _low_demand_fit()
    raw = 0.12
    model = result.ols_models[_FD_KEY]
    vec = _fd_vec(result, raw, rf, _stpasa_at_demand(_LIVE_DEMAND_MW))
    outside = model.out_of_domain_features(vec)
    assert [STAGE2_FEATURE_NAMES[i] for i in outside] == ["log_demand"], outside
    assert not model.in_feature_domain(vec)
    cost = model.extrapolation_cost(vec)
    assert cost is not None and cost > model.extrapolation_allowance, (cost, model.extrapolation_allowance)
    assert not model.serves(vec)

    out = _fd_apply(result, raw, rf, _stpasa_at_demand(_LIVE_DEMAND_MW))
    stage1 = result.get_bucket(_FD_HORIZON, _FD_HOUR).apply_all(raw)
    assert out["calibrated_source"] == "isotonic", out["calibrated_source"]
    assert out["calibrated"] == stage1["calibrated"]
    assert out["p10"] == stage1["p10"] and out["p90"] == stage1["p90"]
    assert "stpasa_run_at" not in out


def test_demand_inside_training_range_is_still_served_by_stage2():
    _engine, result, rf = _low_demand_fit()
    model = result.ols_models[_FD_KEY]
    vec = _fd_vec(result, 0.12, rf, _stpasa_at_demand(_IN_RANGE_DEMAND_MW))
    assert model.in_feature_domain(vec), model.out_of_domain_features(vec)
    out = _fd_apply(result, 0.12, rf, _stpasa_at_demand(_IN_RANGE_DEMAND_MW))
    assert out["calibrated_source"] == "isotonic+stpasa", out["calibrated_source"]
    assert out["stpasa_run_at"] == _FD_RUN_AT


def test_negative_raw_inside_domain_at_low_demand_falls_back():
    """The published shape of #147: raw -0.10 inside the stage-1 domain, which
    reached stage 2 before the gate."""
    _engine, result, rf = _low_demand_fit()
    raw = -0.10
    bucket = result.get_bucket(_FD_HORIZON, _FD_HOUR)
    assert not bucket.is_below_domain(raw), "fixture: -0.10 must be inside the domain"
    out = _fd_apply(result, raw, rf, _stpasa_at_demand(_LIVE_DEMAND_MW))
    assert out["calibrated_source"] == "isotonic"
    assert out["calibrated"] == bucket.apply_all(raw)["calibrated"]


def test_gate_weighs_each_excursion_by_its_coefficient():
    """For every feature the model uses: an excursion costing twice the
    allowance is refused, one costing half of it is served, and the bounds
    themselves are served (#153)."""
    _engine, result, rf = _low_demand_fit()
    model = result.ols_models[_FD_KEY]
    base = _fd_vec(result, 0.12, rf, _stpasa_at_demand(_IN_RANGE_DEMAND_MW))
    assert model.serves(base)
    assert model.extrapolation_cost(base) == 0.0
    allowance = model.extrapolation_allowance
    assert allowance is not None and allowance > 0.0
    checked = 0
    for i, name in enumerate(STAGE2_FEATURE_NAMES):
        lo, hi = model.feature_min[i], model.feature_max[i]
        for probe in (lo, hi):
            vec = list(base)
            vec[i] = probe
            assert model.serves(vec), f"{name} at its bound {probe} refused"
        c = abs(model.coef[i + 1])
        if c < 1e-9:
            continue  # a feature the model does not use cannot be extrapolated on
        checked += 1
        for factor, expect in ((2.0, False), (0.5, True)):
            excess = factor * allowance / c
            for probe in (lo - excess, hi + excess):
                vec = list(base)
                vec[i] = probe
                cost = model.extrapolation_cost(vec)
                assert abs(cost - factor * allowance) < 1e-9 * max(1.0, allowance), (name, cost)
                assert model.serves(vec) is expect, f"{name}={probe} cost={cost:.5f} allowance={allowance:.5f}"
    assert checked >= 5, checked


def test_hairline_excursion_on_a_light_feature_is_served():
    """poe_spread_n a hundredth below a narrow range: refused on the live
    install by the bare range test, served under the weighed one."""
    _engine, result, rf = _low_demand_fit()
    model = result.ols_models[_FD_KEY]
    i = STAGE2_FEATURE_NAMES.index("poe_spread_n")
    sf = _stpasa_at_demand(_IN_RANGE_DEMAND_MW)
    sf.poe_spread_n = model.feature_min[i] - 0.01
    vec = _fd_vec(result, 0.12, rf, sf)
    assert model.out_of_domain_features(vec) == [i]
    cost = model.extrapolation_cost(vec)
    assert cost is not None and cost < model.extrapolation_allowance, (cost, model.extrapolation_allowance)
    out = _fd_apply(result, 0.12, rf, sf)
    assert out["calibrated_source"] == "isotonic+stpasa", out["calibrated_source"]


def test_no_residual_quantiles_falls_back_to_the_strict_range_test():
    """With ranges but no usable band there is nothing to size an allowance from."""
    m = OlsModel(bucket_key="x", coef=[0.0, 1.0, 0.001], feature_min=[0.0, 0.0], feature_max=[1.0, 1.0])
    assert m.extrapolation_allowance is None
    assert m.serves([0.5, 0.5])
    assert not m.serves([0.5, 1.001]), "a light feature's hairline excursion is refused without a band"
    m.resid = ResidualQuantiles(bucket_key="x", q10=-0.02, q50=0.0, q90=0.02, n=OLS_MIN_OBS)
    assert m.extrapolation_allowance == 0.02
    assert m.serves([0.5, 1.001])       # cost 0.001 * 0.001
    assert m.serves([1.019, 0.5])       # cost 0.019
    assert not m.serves([1.021, 0.5])   # cost 0.021


def test_in_feature_domain_tolerates_float_jitter_and_rejects_bad_ranges():
    m = OlsModel(bucket_key="x", coef=[0.0, 1.0], feature_min=[1.0], feature_max=[2.0])
    assert m.in_feature_domain([1.0 - 1e-12])
    assert m.in_feature_domain([2.0 + 1e-12])
    assert not m.in_feature_domain([0.999])
    assert not m.in_feature_domain([2.001])
    # A range list of the wrong length is not evidence; fail closed.
    bad = OlsModel(bucket_key="x", coef=[0.0, 1.0, 1.0], feature_min=[1.0], feature_max=[2.0])
    assert not bad.in_feature_domain([1.5, 1.5])
    assert bad.out_of_domain_features([1.5, 1.5]) == []


def test_legacy_model_without_ranges_serves_as_before():
    """A store written before #147 has no ranges; the gate must pass."""
    _engine, result, rf = _low_demand_fit()
    result.ols_models[_FD_KEY] = OlsModel(
        bucket_key=_FD_KEY, coef=[0.02, 1.10, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], n_train=120, r2=0.8,
    )
    assert result.ols_models[_FD_KEY].serves([0.0] * 9)
    out = _fd_apply(result, 0.12, rf, _stpasa_at_demand(_LIVE_DEMAND_MW))
    assert out["calibrated_source"] == "isotonic+stpasa"


def test_ranges_come_from_rows_that_survived_the_leverage_screen():
    """The training range is that of the rows that fitted the coefficients."""
    _engine, result, _rf = _low_demand_fit()
    m = result.ols_models[_FD_KEY]
    lo, hi = m.feature_min[7], m.feature_max[7]  # log_demand
    assert math.log(_TRAIN_DEMAND_MW[0]) - 1e-6 <= lo < hi <= math.log(_TRAIN_DEMAND_MW[1]) + 1e-6
    assert lo > math.log(_LIVE_DEMAND_MW) + 1.0


def test_summary_publishes_stage2_diagnostics():
    _engine, result, _rf = _low_demand_fit()
    s = result.summary()
    assert "stage2" in s
    entry = s["stage2"][_FD_KEY]
    m = result.ols_models[_FD_KEY]
    assert entry["n_train"] == m.n_train
    assert entry["r2"] == m.r2
    assert entry["coef"] == m.coef and len(entry["coef"]) == 1 + len(STAGE2_FEATURE_NAMES)
    assert entry["resid_q10"] == m.resid.q10
    assert entry["resid_q90"] == m.resid.q90
    assert set(entry["feature_min"]) == set(STAGE2_FEATURE_NAMES)
    assert entry["feature_min"]["log_demand"] == m.feature_min[7]
    assert entry["feature_max"]["log_demand"] == m.feature_max[7]
    for key, model in result.ols_models.items():
        if len(model.coef) < 2:
            assert key not in s["stage2"], "buckets without a fitted model are not listed"


# ── The STPASA feature transform ──────────────────────────────────────────────

def _stpasa_interval(**overrides) -> StpasaInterval:
    fields = dict(
        interval_datetime="2026-06-17T04:30:00+10:00",
        run_datetime="2026-06-16T12:00:00+10:00",
        demand10=5500.0,
        demand50=6000.0,
        demand90=6500.0,
        surpluscapacity=1200.0,
        ss_solar_uigf=800.0,
        ss_wind_uigf=400.0,
    )
    fields.update(overrides)
    return StpasaInterval(**fields)


def test_demand_below_floor_yields_no_features():
    """A degenerate transform yields None, on the shared helper and from
    from_interval; at the floor and above, the plain transform with no
    clamped divisor, so poe_spread_n is the ratio it claims to be."""
    for demand50 in (-27.0, -13.0, 0.0, 0.5, STPASA_DEMAND_FLOOR_MW - 1e-9):
        assert stpasa_feature_values(1200.0, 800.0, demand50, demand50 + 5.0, demand50 - 5.0) is None, demand50
        interval = _stpasa_interval(demand10=demand50 + 5.0, demand50=demand50, demand90=demand50 - 5.0)
        assert StpasaFeatures.from_interval(interval) is None, demand50
    values = stpasa_feature_values(1200.0, 800.0, 24.0, 29.0, 19.0)
    assert values is not None
    log_surplus, log_solar, log_demand, poe_spread_n = values
    assert abs(log_demand - math.log(24.0)) < 1e-12
    assert abs(poe_spread_n - 10.0 / 24.0) < 1e-12
    assert abs(log_surplus - math.log1p(1200.0)) < 1e-12
    assert abs(log_solar - math.log1p(800.0)) < 1e-12
    assert stpasa_feature_values(None, 800.0, 24.0, 29.0, 19.0) is None


def test_stpasa_features_from_interval_returns_none_when_an_input_is_missing():
    """Issue #43: an incomplete interval is None, not features derived from a
    substituted zero."""
    assert StpasaFeatures.from_interval(_stpasa_interval()) is not None
    for field_name in ("demand10", "demand50", "demand90", "surpluscapacity", "ss_solar_uigf"):
        assert StpasaFeatures.from_interval(_stpasa_interval(**{field_name: None})) is None, (
            f"a missing {field_name} must yield None, not derived features"
        )


def test_stpasa_features_tolerate_missing_wind_and_genuine_zeros():
    """Wind is not an OLS input, so its absence must not drop the interval,
    and a real zero must produce features rather than read as missing."""
    feats = StpasaFeatures.from_interval(_stpasa_interval(surpluscapacity=0.0, ss_solar_uigf=0.0, ss_wind_uigf=None))
    assert feats is not None
    assert feats.log_solar == 0.0
    assert feats.log_surplus == 0.0
    assert abs(feats.log_demand - math.log(6000.0)) < 1e-9
