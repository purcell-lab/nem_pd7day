"""Regression tests for issue #69 — published point estimate must lie inside its band.

``CalibrationResult.apply()`` step 7 replaces the point estimate with the
stage-2 STPASA OLS prediction.  It used to keep the quantile band that
``BucketModel.apply_all()`` had already clamped against the *isotonic* value,
so whenever the stage-2 prediction moved past a stage-1 bound the published
triple was inconsistent: value below p10, or above p90.  On a five-region live
snapshot that was 522 of 3075 intervals across 9 sensors, every one of them
``calibrated_source = isotonic+stpasa``.

These tests pin the invariant ``p10 <= calibrated <= p90 <= ...`` for every
calibration path, and pin the two directions of the stage-2 failure with
hand-built models so the arithmetic is exact rather than fit-dependent.

Run with:  python -m pytest tests/test_band_contains_point_estimate.py -v
or simply: python tests/test_band_contains_point_estimate.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

# Allow running from repo root without installing the package.  Import the
# engine module directly to avoid loading the HA-dependent __init__.py.
import importlib.util

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load const first, then nem_time, then calibration_engine — loading const
# before nem_time keeps the relative import in nem_time from pulling in the
# full package __init__.py, which needs Home Assistant.
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
    MIN_OBS,
    SOURCE_ISOTONIC_BELOW_DOMAIN,
    BucketModel,
    CalibrationResult,
    IsotonicRegression,
    OlsModel,
    QuantileCoeff,
    RunFeatures,
    StpasaFeatures,
    _bucket_key,
)

# ── Fixture geometry ─────────────────────────────────────────────────────────
# In the OLS band (22 <= h <= 120) so the stage-2 override is reachable.
HORIZON = 36.0
HOUR = 17
KEY = _bucket_key(HORIZON, HOUR)

# Comfortably above MIN_OBS so no quantile level reads as unfitted.
N_FITTED = MIN_OBS * 10

RUN_FEATURES = RunFeatures(run_max_h6_rrp=0.2, run_mean_rrp=0.1, run_spread=0.05)
STPASA = StpasaFeatures(
    log_surplus=8.0,
    log_solar=8.0,
    log_demand=9.0,
    poe_spread_n=0.1,
    stpasa_run_at="2026-09-02T04:00:00+10:00",
)


def _iso_model() -> IsotonicRegression:
    """Monotone fit mapping forecast to roughly half of it.

    Training x-range is 0.0 to 0.3, so out_of_bounds='clip' pins anything
    below 0.0 to y=0.0 and anything above 0.3 to y=0.15.
    """
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
    """Bucket with hand-set coefficients so every published number is exact."""
    n = N_FITTED if fitted_quantiles else 0
    return BucketModel(
        bucket_key=KEY,
        q10=QuantileCoeff(0.1, a=q10_a, b=q10_b, n=n),
        q50=QuantileCoeff(0.5, a=q50_a, b=q50_b, n=n),
        q90=QuantileCoeff(0.9, a=q90_a, b=q90_b, n=n),
        iso_model=_iso_model() if with_iso else None,
    )


def _result(bucket: BucketModel, prediction: float | None) -> CalibrationResult:
    """CalibrationResult whose stage-2 model returns ``prediction`` for any input.

    The OLS coefficient vector is the intercept followed by eight zeros, so
    ``OlsModel.predict()`` is a constant.  That isolates the band arithmetic
    from the regression fit.
    """
    ols_models = {}
    if prediction is not None:
        ols_models[KEY] = OlsModel(
            bucket_key=KEY,
            coef=[prediction] + [0.0] * 8,
            n_train=100,
            r2=0.5,
        )
    return CalibrationResult(
        fitted_at="2026-09-02T16:05:00+10:00",
        total_observations=1000,
        models={KEY: bucket},
        ols_models=ols_models,
    )


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
    """The published triple must be ordered and must contain the point estimate."""
    _assert_ordered(out, label)
    value = out["calibrated"]
    p10, p90 = out["p10"], out["p90"]
    if p10 is not None:
        assert p10 <= value, (
            f"{label}: value {value} below its own p10 {p10} "
            f"(source {out['calibrated_source']})"
        )
    if p90 is not None:
        assert value <= p90, (
            f"{label}: value {value} above its own p90 {p90} "
            f"(source {out['calibrated_source']})"
        )


# ── Stage-2 override, both directions ────────────────────────────────────────


def test_override_below_stage1_p10_lowers_p10_to_the_point_estimate():
    """Prediction under the fitted p10 must pull p10 down, not publish below it.

    At forecast 0.2 the isotonic value is 0.10 and the fitted band is
    [0.08, 0.10, 0.14].  A stage-2 prediction of 0.03 used to be published
    against that inherited band, 0.05 below its own lower bound.
    """
    res = _result(_bucket(), prediction=0.03)
    out = res.apply(
        0.2,
        horizon_hours=HORIZON,
        hour_of_day=HOUR,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == 0.03
    assert out["p10"] == 0.03, f"expected p10 clamped to the prediction, got {out['p10']}"
    assert out["p50"] == 0.1, f"expected p50 to survive at 0.1, got {out['p50']}"
    assert out["p90"] == 0.14, f"expected p90 untouched at 0.14, got {out['p90']}"
    _assert_consistent(out, "override below p10")
    print("  PASS: override below stage-1 p10 is re-clamped")


def test_override_above_stage1_p90_raises_p90_to_the_point_estimate():
    """Prediction over the fitted p90 must lift p90 up, not publish above it."""
    res = _result(_bucket(), prediction=0.25)
    out = res.apply(
        0.2,
        horizon_hours=HORIZON,
        hour_of_day=HOUR,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == 0.25
    assert out["p10"] == 0.08, f"expected p10 untouched at 0.08, got {out['p10']}"
    assert out["p50"] == 0.1, f"expected p50 to survive at 0.1, got {out['p50']}"
    assert out["p90"] == 0.25, f"expected p90 clamped to the prediction, got {out['p90']}"
    _assert_consistent(out, "override above p90")
    print("  PASS: override above stage-1 p90 is re-clamped")


def test_override_inside_the_band_publishes_the_fitted_band_untouched():
    """A prediction already inside the fitted band must not move the bounds."""
    res = _result(_bucket(), prediction=0.12)
    out = res.apply(
        0.2,
        horizon_hours=HORIZON,
        hour_of_day=HOUR,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert (out["p10"], out["p50"], out["p90"]) == (0.08, 0.1, 0.14)
    _assert_consistent(out, "override inside band")
    print("  PASS: override inside the band leaves it untouched")


def test_override_band_is_derived_from_the_fits_not_the_clamped_stage1_band():
    """Re-clamping must start from the quantile fits, not the stage-1 band.

    Here the isotonic value sits *below* the fitted p10, so stage 1 clamps p10
    down from 0.08 to 0.025.  A stage-2 prediction of 0.12 then belongs in the
    fitted band [0.08, 0.14].  Re-clamping the already-clamped band would
    publish the loose lower bound 0.025 instead, wider than the fits support.
    """
    # Isotonic interpolates 0.05 to 0.025.  Steep quantile lines put the
    # fitted p10 at 1.6 * 0.05 = 0.08, above that isotonic value.
    bucket = _bucket(q10_a=1.6, q50_a=2.0, q90_a=2.8)
    stage1 = bucket.apply_all(0.05)
    assert stage1["calibrated"] == 0.025, stage1
    assert stage1["p10"] == 0.025, f"expected stage 1 to clamp p10 down, got {stage1}"
    # raw_band is unrounded, so compare within float tolerance.
    assert abs(bucket.raw_band(0.05)[0] - 0.08) < 1e-9, (
        "fitted p10 should be 0.08 before clamping"
    )

    out = _result(bucket, prediction=0.12).apply(
        0.05,
        horizon_hours=HORIZON,
        hour_of_day=HOUR,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["p10"] == 0.08, (
        f"p10 must come from the quantile fit (0.08), not the stage-1 clamp "
        f"(0.025); got {out['p10']}"
    )
    assert out["p90"] == 0.14
    _assert_consistent(out, "band derived from fits")
    print("  PASS: re-clamped band derives from the fits, not the stage-1 band")


def test_qld1_collapsed_zero_band_regression():
    """The live QLD1 case: a band that used to collapse to [0, 0].

    Observed on 2026-09-02 at h65.0, forecast -0.07591 $/kWh.  This fixture's
    isotonic domain starts at 0.0, so the forecast is below it: since issue
    #117 that is a below-domain extrapolation, AEMO's value shifted by the
    edge correction (zero here, iso(0.0) = 0.0) with a band from the quantile
    lines clamped to contain it, and stage 2 is never consulted.
    Before #114 and #117 the isotonic value clipped and floored to 0.0, the
    zero floor collapsed the band to p10 = p90 = 0.0, and a stage-2 prediction
    of 0.00182 was published above a p90 of exactly zero.
    """
    forecast = -0.07591
    bucket = _bucket()
    assert bucket.is_below_domain(forecast)

    stage1 = bucket.apply_all(forecast)
    assert stage1["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
    assert stage1["calibrated"] == round(forecast, 6), stage1
    # q10 line 0.4x = -0.0304 sits above the point estimate and is clamped
    # down onto it; q90 line 0.7x = -0.0531 stays as the upper bound.
    assert stage1["p10"] == round(forecast, 6), stage1
    assert stage1["p90"] == round(0.7 * forecast, 6), stage1
    assert stage1["p10"] < stage1["p90"], "the band must no longer collapse"
    _assert_consistent(stage1, "qld1 stage 1")

    out = _result(bucket, prediction=0.00182).apply(
        forecast,
        horizon_hours=HORIZON,
        hour_of_day=HOUR,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    assert out["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
    assert out["calibrated"] == round(forecast, 6)
    assert out["p10"] == stage1["p10"] and out["p90"] == stage1["p90"]
    _assert_consistent(out, "qld1 collapsed band")
    print("  PASS: QLD1 formerly collapsed band is a banded extrapolation")


# ── Unfitted quantiles ───────────────────────────────────────────────────────


def test_unfitted_quantiles_stay_none_after_the_override():
    """A bucket with too few observations must publish None, not an invented band."""
    res = _result(_bucket(fitted_quantiles=False), prediction=0.25)
    out = res.apply(
        0.2,
        horizon_hours=HORIZON,
        hour_of_day=HOUR,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["p10"] is None and out["p50"] is None and out["p90"] is None, out
    _assert_consistent(out, "unfitted quantiles")
    print("  PASS: unfitted quantiles stay None after the override")


# ── The passthrough path is a deliberate exception ────────────────────────────────────────


def test_passthrough_band_is_left_unclamped_on_purpose():
    """With no isotonic model the band is not clamped to the raw forecast.

    This is the one path where the published value may sit outside its own
    band, and it is intentional.  The point estimate is the un-calibrated raw
    forecast; the quantile fits survive serialisation and the isotonic model
    does not, so between a restart and the next engine.fit() a fitted p10
    above the raw forecast is the calibration saying the forecast is too low.
    Clamping p10 down to the forecast would erase that, and would also break
    the warm-start guarantee that the published band equals the stored fits
    (test_engine_serialisation_roundtrip in test_calibration_engine.py).

    Pinned here so a later reading of issue #69 does not extend the clamp to
    this path without weighing that trade-off.
    """
    # All three lines sit entirely above the forecast, in order, so nothing
    # here is an ordering violation — only the containment the clamp would add.
    bucket = _bucket(
        with_iso=False,
        q10_a=1.0,
        q10_b=0.05,
        q50_a=1.0,
        q50_b=0.10,
        q90_a=1.0,
        q90_b=0.15,
    )
    out = bucket.apply_all(0.1)
    assert out["calibrated_source"] == "passthrough"
    assert out["calibrated"] == 0.1
    assert (out["p10"], out["p50"], out["p90"]) == (0.15, 0.2, 0.25), (
        f"the fitted lines must be published as fitted, got {out}"
    )
    assert out["calibrated"] < out["p10"], "this is the tolerated exception"
    _assert_ordered(out, "passthrough")
    print("  PASS: passthrough band is left unclamped on purpose")


def test_override_on_top_of_a_passthrough_bucket_is_still_consistent():
    """Stage 2 can fire on a bucket with no isotonic model; the band must hold.

    The step-2 gate tests horizon and feature availability, not the stage-1
    source, so a passthrough bucket inside the OLS band still reaches the
    override.  Once the point estimate is a calibrated stage-2 value the
    exception above no longer applies and the band must contain it.
    """
    bucket = _bucket(
        with_iso=False,
        q10_a=1.0,
        q10_b=0.05,
        q50_a=1.0,
        q50_b=0.10,
        q90_a=1.0,
        q90_b=0.15,
    )
    out = _result(bucket, prediction=0.4).apply(
        0.1,
        horizon_hours=HORIZON,
        hour_of_day=HOUR,
        stpasa=STPASA,
        run_features=RUN_FEATURES,
    )
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == 0.4
    assert out["p90"] == 0.4, f"p90 must rise to contain the override, got {out['p90']}"
    _assert_consistent(out, "override over passthrough")
    print("  PASS: override on top of a passthrough bucket stays consistent")


# ── Invariant sweep across every calibration path ────────────────────────────


def test_invariant_holds_across_every_source_and_horizon():
    """Sweep the paths and horizons and assert the invariant everywhere.

    Covers isotonic_below_domain, isotonic and isotonic+stpasa, inside and
    outside the OLS horizon band, with quantile lines steep enough to cross
    the isotonic curve in both directions.

    ``passthrough`` is checked for ordering only — see
    test_passthrough_band_is_left_unclamped_on_purpose for why that path is
    allowed to publish a value outside its band.
    """
    forecasts = [-0.5, -0.11, -0.1, -0.076, -0.01, 0.0, 0.02, 0.1, 0.2, 0.35, 3.5]
    horizons = [1.0, 21.9, 22.0, 36.0, 120.0, 120.1, 168.0]
    predictions = [0.001, 0.03, 0.12, 0.25, 4.0]
    seen = set()
    checks = 0

    for with_iso in (True, False):
        for fitted in (True, False):
            for slopes in ((0.4, 0.5, 0.7), (1.6, 2.0, 2.8), (0.05, 0.06, 0.08)):
                bucket = _bucket(
                    with_iso=with_iso,
                    fitted_quantiles=fitted,
                    q10_a=slopes[0],
                    q50_a=slopes[1],
                    q90_a=slopes[2],
                )
                for prediction in predictions:
                    res = _result(bucket, prediction=prediction)
                    for forecast in forecasts:
                        for horizon in horizons:
                            for stp, rf in ((None, None), (STPASA, RUN_FEATURES)):
                                out = res.apply(
                                    forecast,
                                    horizon_hours=horizon,
                                    hour_of_day=HOUR,
                                    stpasa=stp,
                                    run_features=rf,
                                )
                                seen.add(out["calibrated_source"])
                                label = (
                                    f"sweep x={forecast} h={horizon} "
                                    f"pred={prediction} iso={with_iso} "
                                    f"fitted={fitted} slopes={slopes}"
                                )
                                if out["calibrated_source"] == "passthrough":
                                    _assert_ordered(out, label)
                                else:
                                    _assert_consistent(out, label)
                                checks += 1

    expected = {SOURCE_ISOTONIC_BELOW_DOMAIN, "passthrough", "isotonic", "isotonic+stpasa"}
    assert expected <= seen, f"sweep missed a calibration path: {expected - seen}"
    print(f"  PASS: invariant holds over {checks} combinations, sources {sorted(seen)}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} band-consistency tests\n")
    for t in tests:
        t()
    print(f"\nAll {len(tests)} tests passed.")
