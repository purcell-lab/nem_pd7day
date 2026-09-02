"""Stage-2 predictions get a stage-2 band, issue #72.

The stage-2 STPASA override replaces the point estimate with an OLS prediction
on nine features.  The band published beside it came from the stage-1 quantile
lines, which are single-variable regressions of the actual on the RAW PD7DAY
forecast and have never seen any of those features.  PR #71 made the triple
self-consistent by re-clamping those lines around the stage-2 value, which
bought containment by pulling the nearer bound onto the point estimate.

On the first live measurement, a single residential premises in SE Queensland,
QLD1, the run at 2026-09-03T07:30:00+10:00, 330 intervals scored by both
versions across the restart: containment violations 56 to 0, and bounds
collapsed onto the point estimate 36 to 98, of which 82 onto p10 against 16
onto p90.  A collapsed bound reports zero uncertainty on one side, which is not
a claim the model has any basis to make.

These tests pin the replacement: the band is the stage-2 prediction plus the
10th, 50th and 90th percentile of that bucket's leave-one-out residuals, so it
is centred on the prediction by construction and cannot collapse.  They also
pin the closed form of the leave-one-out residual against an explicit refit,
the fallback when a bucket has no usable residual quantiles, the storage round
trip, and that nothing on the stage-1 paths moves.

Run with:  python -m pytest tests/test_stage2_residual_band.py -v
or simply: python tests/test_stage2_residual_band.py
"""
from __future__ import annotations

import copy
import importlib.util
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Same loader order as tests/test_calibration_engine.py: const before nem_time
# keeps the relative import out of the HA-dependent package __init__.py.
_load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

from custom_components.nem_pd7day.calibration_engine import (  # noqa: E402
    BAND_SOURCE_KEY,
    BAND_SOURCE_PASSTHROUGH,
    BAND_SOURCE_STAGE1,
    BAND_SOURCE_STAGE1_RAW,
    BAND_SOURCE_STAGE2,
    BAND_SOURCE_STAGE2_FALLBACK,
    MIN_OBS,
    NEGATIVE_PASSTHROUGH_THRESHOLD,
    OLS_MIN_OBS,
    BucketModel,
    CalibrationEngine,
    CalibrationResult,
    IsotonicRegression,
    Observation,
    OlsModel,
    QuantileCoeff,
    ResidualQuantiles,
    RunFeatures,
    StpasaFeatures,
    _bucket_key,
    _conformal_index,
    _loo_residuals,
)
from custom_components.nem_pd7day.const import ATTR_CAL_BAND_SOURCE  # noqa: E402

# ── Hand-built fixture geometry ──────────────────────────────────────────────
# Inside the OLS horizon band (22 to 120 h) so the stage-2 override is reached.
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
    """Monotone fit mapping a forecast to roughly half of it, x-range 0 to 0.3."""
    return IsotonicRegression().fit(
        np.asarray([0.0, 0.1, 0.2, 0.3], dtype=float),
        np.asarray([0.0, 0.05, 0.10, 0.15], dtype=float),
    )


def _bucket(*, with_iso: bool = True, fitted_quantiles: bool = True) -> BucketModel:
    """Stage-1 bucket with hand-set lines: p10 = 0.4x, p50 = 0.5x, p90 = 0.7x."""
    n = N_FITTED if fitted_quantiles else 0
    return BucketModel(
        bucket_key=KEY,
        q10=QuantileCoeff(0.1, a=0.4, b=0.0, n=n),
        q50=QuantileCoeff(0.5, a=0.5, b=0.0, n=n),
        q90=QuantileCoeff(0.9, a=0.7, b=0.0, n=n),
        iso_model=_iso_model() if with_iso else None,
    )


def _result(
    bucket: BucketModel,
    prediction: float | None,
    resid: ResidualQuantiles | None = None,
) -> CalibrationResult:
    """Result whose stage-2 model returns ``prediction`` for any input."""
    ols_models = {}
    if prediction is not None:
        ols_models[KEY] = OlsModel(
            bucket_key=KEY,
            coef=[prediction] + [0.0] * 8,
            n_train=100,
            r2=0.5,
            resid=resid,
        )
    return CalibrationResult(
        fitted_at="2026-09-03T07:30:00+10:00",
        total_observations=1000,
        models={KEY: bucket},
        ols_models=ols_models,
    )


def _resid(q10=-0.02, q50=-0.001, q90=0.03, n=OLS_MIN_OBS * 2) -> ResidualQuantiles:
    return ResidualQuantiles(bucket_key=KEY, q10=q10, q50=q50, q90=q90, n=n)


def _apply(res: CalibrationResult, forecast: float, horizon=HORIZON, hour=HOUR) -> dict:
    return res.apply(
        forecast,
        horizon_hours=horizon,
        hour_of_day=hour,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )


# ── The closed form of the leave-one-out residual ────────────────────────────


def test_loo_residual_equals_an_explicit_refit_without_that_row():
    """``e_i / (1 - h_ii)`` must be the error of a fit that excluded row i.

    This is the only claim in the change that is a piece of mathematics rather
    than a policy choice, so it is checked against the thing it claims to be:
    for every row, drop it, refit by least squares on the remaining rows, and
    predict the dropped row.  Ten coefficients on sixty rows, the same shape the
    stage-2 fit runs at, with a deliberately leveraged final row.
    """
    rng = np.random.default_rng(72)
    n, p = 60, 10
    X = np.column_stack([np.ones(n), rng.normal(size=(n, p - 1))])
    # One high-leverage row, so the test covers the case where the correction
    # matters most rather than only the well-behaved middle of the design.
    X[-1, 1:] *= 6.0
    beta = rng.normal(size=p)
    y = X @ beta + rng.normal(scale=0.05, size=n)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)

    got = _loo_residuals(X, y, coef)
    for i in range(n):
        keep = [j for j in range(n) if j != i]
        coef_i, *_ = np.linalg.lstsq(X[keep], y[keep], rcond=None)
        expected = y[i] - X[i] @ coef_i
        assert abs(got[i] - expected) < 1e-8, (
            f"row {i}: closed form {got[i]:.9f} against refit {expected:.9f}"
        )

    # And it is genuinely different from the in-sample residual, which is the
    # reason for using it: the in-sample version is systematically smaller.
    in_sample = y - X @ coef
    assert np.mean(np.abs(got)) > np.mean(np.abs(in_sample)) * 1.05, (
        "leave-one-out residuals should be materially larger than in-sample ones"
    )
    print("  PASS: leave-one-out residuals match an explicit refit per row")


def test_conformal_index_errs_wide_and_stays_in_range():
    """The tail indices must bracket the plain empirical ones, and never escape.

    Swept over every sample size a bucket can plausibly reach rather than
    checked at one point, because the index is a floor and a ceiling and those
    are exactly where an off-by-one hides.
    """
    for n in range(OLS_MIN_OBS, 400):
        lo = _conformal_index(n, 0.1)
        hi = _conformal_index(n, 0.9)
        assert 0 <= lo < n and 0 <= hi < n, f"n={n}: index out of range"
        assert lo < hi, f"n={n}: lower index {lo} not below upper {hi}"
        emp_lo = int(round(0.1 * (n - 1)))
        emp_hi = int(round(0.9 * (n - 1)))
        assert lo <= emp_lo, f"n={n}: lower index {lo} above empirical {emp_lo}"
        assert hi >= emp_hi, f"n={n}: upper index {hi} below empirical {emp_hi}"
    # At 50 rows the documented answer is the 5th and the 46th smallest.
    assert _conformal_index(50, 0.1) == 4
    assert _conformal_index(50, 0.9) == 45
    print("  PASS: conformal indices err wide and stay inside the sample")


# ── The band itself ──────────────────────────────────────────────────────────


def test_stage2_band_is_the_prediction_plus_its_residual_quantiles():
    """Exact arithmetic: band = prediction + (q10, q50, q90)."""
    res = _result(_bucket(), prediction=0.12, resid=_resid(-0.02, -0.001, 0.03))
    out = _apply(res, 0.2)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == 0.12
    assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2
    assert out["p10"] == 0.1, f"expected 0.12 - 0.02, got {out['p10']}"
    assert out["p50"] == 0.119, f"expected 0.12 - 0.001, got {out['p50']}"
    assert out["p90"] == 0.15, f"expected 0.12 + 0.03, got {out['p90']}"
    # The stage-1 lines at forecast 0.2 would have given 0.08 and 0.14, so the
    # published band is demonstrably not those lines.
    assert out["p10"] != 0.08 and out["p90"] != 0.14
    print("  PASS: stage-2 band is the prediction plus its residual quantiles")


def test_stage2_band_never_collapses_onto_the_point_estimate():
    """Sweep the prediction far outside the stage-1 lines: no bound may collapse.

    This is the issue in one test.  With the stage-1 lines re-clamped, every
    prediction below 0.08 collapsed p10 and every prediction above 0.14
    collapsed p90 at this forecast.  With a residual band the bound sits a fixed
    distance from the prediction wherever the prediction lands.
    """
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
    print("  PASS: no collapsed bound anywhere in the prediction sweep")


def test_lower_bound_is_still_floored_at_zero():
    """A residual band reaching below zero is floored, as every other band is.

    The floor is the one thing that can still put p10 at a fixed distance from
    the market rather than from the model, and it is deliberate: the rest of the
    engine does not publish a negative lower bound above the negative
    passthrough boundary.  It cannot produce an exact collapse because stage 2
    only publishes a strictly positive prediction.
    """
    out = _apply(_result(_bucket(), 0.004, _resid(-0.02, -0.001, 0.03)), 0.2)
    assert out["p10"] == 0.0, f"expected the zero floor, got {out['p10']}"
    assert out["p90"] == 0.034
    assert out["p10"] < out["calibrated"], "the floor must not collapse the bound"
    print("  PASS: the residual lower bound is floored at zero, not collapsed")


def test_a_wide_residual_band_is_published_in_full():
    """Nothing narrows the residual band toward the stage-1 lines."""
    out = _apply(_result(_bucket(), 0.5, _resid(-0.3, 0.01, 0.9)), 0.2)
    assert out["p10"] == 0.2 and out["p50"] == 0.51 and out["p90"] == 1.4
    print("  PASS: a wide residual band survives intact")


# ── Validity and the fallback ────────────────────────────────────────────────


def test_residual_quantiles_are_rejected_when_they_do_not_bracket_zero():
    """A residual band excluding zero would exclude its own point estimate."""
    bad = _resid(0.01, 0.02, 0.03)
    assert not bad.is_fitted
    out = _apply(_result(_bucket(), 0.12, bad), 0.2)
    assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK
    # Falls back to the v3.4.0 band rather than publishing 0.13 to 0.15 around
    # a point estimate of 0.12.
    assert (out["p10"], out["p50"], out["p90"]) == (0.08, 0.1, 0.14)
    print("  PASS: residual quantiles that exclude zero are not published")


def test_residual_quantiles_are_rejected_below_the_observation_floor():
    """Fewer than OLS_MIN_OBS residuals is not a band."""
    assert not _resid(n=OLS_MIN_OBS - 1).is_fitted
    assert _resid(n=OLS_MIN_OBS).is_fitted
    assert not ResidualQuantiles(bucket_key=KEY, q10=None, q50=0.0, q90=0.1, n=99).is_fitted
    assert not _resid(q10=-0.01, q50=0.05, q90=0.02).is_fitted, "unordered"
    print("  PASS: short or malformed residual samples are treated as unfitted")


def test_fallback_reproduces_the_v340_band_and_says_so():
    """With no residual quantiles the published band is exactly what v3.4.0 gave.

    Both directions of the old collapse, kept deliberately rather than replaced
    by something safer: withholding the stage-2 point estimate to protect the
    band would move the published price on a path that is otherwise working.
    The label is what makes it non-silent.
    """
    below = _apply(_result(_bucket(), 0.03, None), 0.2)
    assert below[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK
    assert (below["p10"], below["p50"], below["p90"]) == (0.03, 0.1, 0.14)

    above = _apply(_result(_bucket(), 0.25, None), 0.2)
    assert above[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK
    assert (above["p10"], above["p50"], above["p90"]) == (0.08, 0.1, 0.25)
    print("  PASS: the fallback is the v3.4.0 re-clamp and is labelled as such")


def test_the_old_collapse_side_follows_the_displacement_sign():
    """Which bound collapsed was decided by the data, not by the machinery.

    Worth pinning because the two measurements on issue #72 disagreed: a
    synthetic sweep found 14 p90 collapses against 7 p10, and the live install
    found 82 p10 against 16 p90.  Both are consistent with this mechanism.  A
    prediction below the fitted p10 collapses p10 and a prediction above the
    fitted p90 collapses p90, so the ratio just reports the sign of the stage-2
    minus stage-1 displacement in whatever data produced it.  A fixture chooses
    that sign when it chooses its generator, so the synthetic ratio carries no
    information about the market and only the live one does.
    """
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
    print("  PASS: the old collapse side follows the sign of the displacement")


# ── The stage-1 paths must not move ──────────────────────────────────────────


def test_band_source_is_published_on_every_path():
    """Every published triple says which model produced it."""
    deep_negative = NEGATIVE_PASSTHROUGH_THRESHOLD - 0.05
    cases = [
        (_result(_bucket(), None), deep_negative, BAND_SOURCE_PASSTHROUGH),
        (_result(_bucket(with_iso=False), None), 0.2, BAND_SOURCE_STAGE1_RAW),
        (_result(_bucket(), None), 0.2, BAND_SOURCE_STAGE1),
        (_result(_bucket(), 0.12, _resid()), 0.2, BAND_SOURCE_STAGE2),
        (_result(_bucket(), 0.12, None), 0.2, BAND_SOURCE_STAGE2_FALLBACK),
    ]
    for res, forecast, expected in cases:
        out = _apply(res, forecast)
        assert out[BAND_SOURCE_KEY] == expected, (
            f"forecast {forecast}: expected {expected}, got {out.get(BAND_SOURCE_KEY)}"
        )
    # The engine key and the sensor attribute name are one string, so they
    # cannot drift the way the calibration inputs did in issue #66.
    assert BAND_SOURCE_KEY == ATTR_CAL_BAND_SOURCE == "band_source"
    print("  PASS: band_source is published on all five paths")


def test_stage1_publications_are_byte_for_byte_unchanged():
    """Adding residual quantiles must not move a single stage-1 number.

    The same fitted result is applied twice, once with residual quantiles and
    once with them stripped, on every path that does not reach stage 2.  Every
    published field must match.
    """
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
        # And the deep-negative bypass, which is gated inside the OLS band too.
        if forecast <= NEGATIVE_PASSTHROUGH_THRESHOLD:
            c = _apply(with_resid, forecast)
            assert c["calibrated"] == round(forecast, 6)
            assert c["calibrated_source"] == "passthrough_negative"
    print("  PASS: stage-1 published values and bands are unchanged")


# ── A real fit, end to end ───────────────────────────────────────────────────

_NEM_TZ = timezone(timedelta(hours=10))
_ANCHOR = datetime.now(_NEM_TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
_TRAIN_RUN_AT = (_ANCHOR - timedelta(days=40)).replace(hour=3, minute=30).strftime(
    "%Y-%m-%dT%H:%M:%S+10:00"
)
_CELLS = ((30.0, 17), (60.0, 17))


def _nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def _fitted_result() -> CalibrationResult:
    """A real isotonic plus a real stage-2 OLS fit, seeded.

    The actual carries a Gaussian innovation and a latent driver that is never
    placed in the design matrix, so the fit has genuine irreducible error and
    the residual quantiles are fitted on something.  This fixture is NOT a
    coverage measurement: its noise process is chosen here, so any coverage
    number it produced would describe that choice.  It is here to prove the
    residual quantiles are produced, persisted and applied by real code.
    """
    rng = random.Random(720)
    engine = CalibrationEngine()
    observations: list[Observation] = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}

    # _compute_run_features only builds an entry for a run holding at least one
    # horizon < 24 row, and fit_ols_stage2 skips rows whose run has no entry.
    for j in range(8):
        near = (_ANCHOR - timedelta(days=40)).replace(hour=4 + j, minute=0)
        observations.append(
            Observation(
                interval_time=_nem_iso(near),
                horizon_hours=2.0 + j,
                pd7day_forecast=rng.uniform(0.05, 0.25),
                actual_rrp=rng.uniform(0.05, 0.30),
                forecast_run_at=_TRAIN_RUN_AT,
                hour_of_day=4 + j,
                day_of_week=near.weekday(),
                month=near.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )

    for horizon, hour in _CELLS:
        for i in range(90):
            day_off = i % 45 + (0 if horizon < 48 else 45)
            interval_dt = (_ANCHOR - timedelta(days=day_off)).replace(
                hour=hour, minute=(i % 2) * 30
            )
            forecast = rng.uniform(0.03, 0.28)
            surplus = rng.uniform(400.0, 5200.0)
            solar = rng.uniform(0.0, 4200.0)
            demand50 = rng.uniform(5000.0, 9200.0)
            latent = rng.gauss(0.0, 1.0)
            actual = (
                1.05 * forecast
                + 0.02
                - 6.0e-6 * solar
                + 3.0e-6 * (demand50 - 7000.0)
                + 0.012 * latent
                + rng.gauss(0.0, 0.008)
            )
            observations.append(
                Observation(
                    interval_time=_nem_iso(interval_dt),
                    horizon_hours=horizon,
                    pd7day_forecast=forecast,
                    actual_rrp=actual,
                    forecast_run_at=_TRAIN_RUN_AT,
                    hour_of_day=hour,
                    day_of_week=interval_dt.weekday(),
                    month=interval_dt.month,
                    gas_forecast_tj=75.0,
                    qni_mwflow=-150.0,
                    qni_violation_degree=0.0,
                    is_intervention=False,
                )
            )
            stpasa_by_key[f"{_nem_iso(interval_dt)}|{_TRAIN_RUN_AT}"] = StpasaFeatures(
                log_surplus=math.log1p(surplus),
                log_solar=math.log1p(solar),
                log_demand=math.log(max(demand50, 1.0)),
                poe_spread_n=0.2,
                stpasa_run_at=STPASA.stpasa_run_at,
            )

    result = engine.fit(observations)
    result.ols_models = engine.fit_ols_stage2(observations, stpasa_by_key)
    return result


def test_a_real_stage2_fit_produces_usable_residual_quantiles():
    """fit_ols_stage2 must attach residual quantiles to every fitted bucket."""
    result = _fitted_result()
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
    print(f"  PASS: {len(fitted)} real stage-2 buckets carry residual quantiles")


def test_real_fit_sweep_has_no_collapsed_stage2_bound():
    """The invariant sweep: 1000-odd real stage-2 intervals, no collapsed bound.

    A sweep rather than point cases because the collapse depends on where the
    prediction lands relative to lines it has never seen, which is exactly the
    thing hand-picked cases miss.  On unfixed main this sweep collapses a bound
    on a large minority of the intervals it reaches.
    """
    result = _fitted_result()
    stage2 = 0
    collapsed = []
    for horizon, hour in _CELLS:
        for i in range(120):
            forecast = 0.005 + i * 0.0045
            sf = StpasaFeatures(
                log_surplus=math.log1p(400.0 + i * 40.0),
                log_solar=math.log1p(i * 35.0),
                log_demand=math.log(5000.0 + i * 35.0),
                poe_spread_n=0.1 + i * 0.001,
                stpasa_run_at=STPASA.stpasa_run_at,
            )
            out = result.apply(
                forecast,
                horizon_hours=horizon,
                hour_of_day=hour,
                stpasa=sf,
                run_features=RUN_FEATURES,
            )
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
    assert not collapsed, (
        f"{len(collapsed)} of {stage2} stage-2 intervals collapsed a bound, "
        f"first {collapsed[0]}"
    )
    print(f"  PASS: {stage2} real stage-2 intervals, no collapsed bound")


def test_residual_quantiles_survive_the_storage_round_trip():
    """They must outlive a restart, as the stage-1 quantile coefficients do.

    This matters more than it looks.  The isotonic model is not serialisable and
    is deliberately not persisted, but the OLS coefficients are, so after a
    restart and before the next fit stage 2 still overrides the point estimate.
    If the residual quantiles did not survive, every one of those intervals
    would publish the old re-clamped stage-1 band.
    """
    engine = CalibrationEngine()
    result = _fitted_result()
    restored = engine.from_storage(engine.to_storage(result))
    for key, m in result.ols_models.items():
        r0, r1 = m.resid, restored.ols_models[key].resid
        if r0 is None:
            assert r1 is None
            continue
        assert r1 is not None, f"{key}: residual quantiles lost in storage"
        assert (r1.q10, r1.q50, r1.q90, r1.n) == (r0.q10, r0.q50, r0.q90, r0.n)

    # And the band is identical after the round trip, on the passthrough path a
    # restart actually lands on: no isotonic model, stage 2 still applying.
    for key in restored.models:
        restored.models[key].iso_model = None
    out = restored.apply(
        0.18,
        horizon_hours=_CELLS[0][0],
        hour_of_day=_CELLS[0][1],
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    if out["calibrated_source"] == "isotonic+stpasa":
        assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2
        assert out["p10"] < out["calibrated"] < out["p90"]
    print("  PASS: residual quantiles survive to_storage and from_storage")


def test_storage_without_residuals_loads_as_unfitted():
    """A payload written before this change must degrade, not raise."""
    engine = CalibrationEngine()
    stored = engine.to_storage(_fitted_result())
    for md in stored["ols_models"].values():
        md.pop("resid", None)
    restored = engine.from_storage(stored)
    for m in restored.ols_models.values():
        assert m.resid is None
    out = restored.apply(
        0.18,
        horizon_hours=_CELLS[0][0],
        hour_of_day=_CELLS[0][1],
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    if out["calibrated_source"] == "isotonic+stpasa":
        assert out[BAND_SOURCE_KEY] == BAND_SOURCE_STAGE2_FALLBACK
    print("  PASS: a pre-#72 storage payload loads and falls back cleanly")


def test_stage2_band_width_is_the_bucket_residual_spread():
    """The published width must equal q90 - q10 for that bucket, not the lines.

    Away from the zero floor the width is a property of the bucket alone, which
    is the additive assumption stated plainly: it does not vary with the price
    level within a bucket.  A reviewer who disagrees with that assumption should
    read this test as where to change it.
    """
    result = _fitted_result()
    for key, m in result.ols_models.items():
        if len(m.coef) < 2 or m.resid is None or not m.resid.is_fitted:
            continue
        expected = round(m.resid.q90, 6) - round(m.resid.q10, 6)
        horizon = 30.0 if key.startswith("h24_48") else 60.0
        seen = 0
        for i in range(40):
            out = result.apply(
                0.05 + i * 0.005,
                horizon_hours=horizon,
                hour_of_day=17,
                stpasa=STPASA,
                run_features=RUN_FEATURES,
            )
            if out["calibrated_source"] != "isotonic+stpasa":
                continue
            if out["p10"] == 0.0:
                continue  # floored, so the width is not the residual spread
            seen += 1
            width = out["p90"] - out["p10"]
            assert abs(width - expected) < 2e-6, (
                f"{key}: width {width:.6f} against residual spread {expected:.6f}"
            )
        assert seen, f"{key}: no unfloored stage-2 interval to check"
    print("  PASS: published stage-2 width is the bucket residual spread")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nAll stage-2 residual band tests passed.")
