"""
Stage-2 STPASA override must not fire on top of a passthrough_negative result.

Issue #73: the step-2 gate in CalibrationResult.apply never inspected
calibrated_source, so a deeply negative raw forecast that reached the
deliberate negative bypass was still eligible for the OLS override. The only
protection was the later ``prediction <= 0.0`` guard, which fails exactly when
the OLS prediction is positive, that is when the published value flips sign
from "paid to consume" to "pay to consume".

Run with:  python -m pytest tests/test_stage2_negative_passthrough.py -v
or simply: python tests/test_stage2_negative_passthrough.py
"""
from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

NEM_TZ = timezone(timedelta(hours=10))  # NEM is UTC+10 year-round, no DST
_ANCHOR = datetime.now(NEM_TZ) - timedelta(days=2)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load const then nem_time then calibration_engine, so the relative import in
# nem_time does not pull in the HA-dependent package __init__.py.
_load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_ce = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

CalibrationEngine = _ce.CalibrationEngine
Observation = _ce.Observation
OlsModel = _ce.OlsModel
RunFeatures = _ce.RunFeatures
StpasaFeatures = _ce.StpasaFeatures
NEGATIVE_PASSTHROUGH_THRESHOLD = _ce.NEGATIVE_PASSTHROUGH_THRESHOLD
OLS_MIN_HORIZON_H = _ce.OLS_MIN_HORIZON_H
OLS_MAX_HORIZON_H = _ce.OLS_MAX_HORIZON_H
_bucket_key = _ce._bucket_key


_RF = RunFeatures(run_max_h6_rrp=0.20, run_mean_rrp=0.12, run_spread=0.05)
_SF = StpasaFeatures(
    log_surplus=math.log1p(1200.0),
    log_solar=math.log1p(2500.0),
    log_demand=math.log(8500.0),
    poe_spread_n=0.2,
    stpasa_run_at=_ANCHOR.replace(hour=3, minute=30, second=0, microsecond=0).isoformat(),
)


def _obs_batch(n, horizon_hours, hour_of_day, seed=3):
    """Observations at one horizon and hour, all positive raw forecasts.

    Enough rows for the isotonic fit in the target bucket; the OLS model is
    injected by the caller so the test controls the coefficients exactly.
    """
    rng = random.Random(seed)
    run_at = (_ANCHOR - timedelta(days=1)).replace(
        hour=3, minute=30, second=0, microsecond=0
    ).isoformat()
    obs = []
    for i in range(n):
        interval = (_ANCHOR - timedelta(days=i % 30)).replace(
            hour=hour_of_day, minute=(i % 2) * 30, second=(i % 55), microsecond=0
        )
        fc = rng.uniform(0.03, 0.25)
        obs.append(
            Observation(
                interval_time=interval.isoformat(),
                horizon_hours=horizon_hours,
                pd7day_forecast=fc,
                actual_rrp=max(0.0, 1.15 * fc + 0.01 + rng.gauss(0, 0.01)),
                forecast_run_at=run_at,
                hour_of_day=hour_of_day,
                day_of_week=interval.weekday(),
                month=interval.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )
    return obs


def _result_with_ols(coef, horizon_hours=36.0, hour_of_day=12):
    """A fitted CalibrationResult carrying one hand-specified OLS model."""
    engine = CalibrationEngine()
    result = engine.fit(_obs_batch(80, horizon_hours, hour_of_day))
    key = _bucket_key(horizon_hours, hour_of_day)
    result.ols_models[key] = OlsModel(
        bucket_key=key, coef=list(coef), n_train=120, r2=0.8
    )
    return result


# Coefficient layout: intercept, then iso_cal, run_max_h6, run_mean, run_spread,
# horizon/168, log_surplus, log_solar, log_demand, poe_spread_n.
# This vector is shaped like a real weak-raw-signal fit: a modest slope on the
# stage-1 value and a large positive demand term, so the prediction at a deeply
# negative forecast comes out positive. At raw -0.15 it predicts about +0.04,
# which is the exact scenario described in issue #73.
_SIGN_FLIP_COEF = [
    -1.72, 0.84, -0.37, -0.31, -0.21, -0.38, -0.003, -0.004, 0.245, -0.36,
]


def test_positive_prediction_does_not_override_negative_passthrough():
    """The regression case from #73: -0.15 must stay -0.15, not become positive."""
    result = _result_with_ols(_SIGN_FLIP_COEF)
    raw = -0.15

    # Confirm the fixture really does reproduce the hazard: the OLS model
    # predicts a positive value for this input, so a fired override would flip
    # the sign. Without that the test would pass vacuously.
    key = _bucket_key(36.0, 12)
    feature_vec = [
        raw,
        _RF.run_max_h6_rrp,
        _RF.run_mean_rrp,
        _RF.run_spread,
        36.0 / 168.0,
        _SF.log_surplus,
        _SF.log_solar,
        _SF.log_demand,
        _SF.poe_spread_n,
    ]
    prediction = result.ols_models[key].predict(feature_vec)
    assert prediction > 0.0, (
        f"fixture is not exercising the hazard: OLS prediction {prediction:.4f} "
        "is not positive"
    )

    out = result.apply(
        raw, horizon_hours=36.0, hour_of_day=12, stpasa=_SF, run_features=_RF
    )

    assert out["calibrated_source"] == "passthrough_negative", (
        f"expected the negative bypass to survive, got {out['calibrated_source']}"
    )
    assert out["calibrated"] == round(raw, 6), (
        f"expected raw {raw} published untouched, got {out['calibrated']}"
    )
    assert "stpasa_run_at" not in out, (
        "stpasa_run_at must be absent when the override is skipped"
    )
    print(
        "  PASS: positive OLS prediction does not override passthrough_negative "
        f"(raw={raw}, blocked prediction={prediction:+.4f})"
    )


def test_no_sign_flip_sweep():
    """Sweep raw forecasts, horizons, hours and coefficient sets for sign flips.

    The invariant: whenever stage 1 returns passthrough_negative, apply must
    republish the raw value with that source and must never publish a value of
    the opposite sign.
    """
    coef_sets = [
        _SIGN_FLIP_COEF,
        # Large positive intercept: prediction is positive for any input.
        [0.50, 0.20, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        # Negative slope on the stage-1 value: a negative input raises the
        # prediction, which is the worst case for a negative raw forecast.
        [0.02, -1.50, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        # Well behaved fit: prediction tracks the stage-1 value closely.
        [0.005, 1.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    ]
    raws = [-3.0, -1.0, -0.5, -0.25, -0.15, -0.1001, -0.10]
    horizons = [22.0, 30.0, 36.0, 60.0, 100.0, 120.0]
    hours = [3, 11, 13, 18, 22]

    checked = 0
    for coef in coef_sets:
        for horizon in horizons:
            for hour in hours:
                result = _result_with_ols(coef, horizon_hours=horizon, hour_of_day=hour)
                for raw in raws:
                    out = result.apply(
                        raw,
                        horizon_hours=horizon,
                        hour_of_day=hour,
                        stpasa=_SF,
                        run_features=_RF,
                    )
                    assert out["calibrated_source"] == "passthrough_negative", (
                        f"raw={raw} h={horizon} hour={hour} coef={coef[:2]}: "
                        f"source {out['calibrated_source']}"
                    )
                    assert out["calibrated"] == round(raw, 6), (
                        f"raw={raw} h={horizon} hour={hour}: "
                        f"published {out['calibrated']}"
                    )
                    assert out["calibrated"] < 0.0, (
                        f"raw={raw} h={horizon} hour={hour}: sign flipped to "
                        f"{out['calibrated']}"
                    )
                    checked += 1
    print(f"  PASS: no sign flip across {checked} negative bypass combinations")


def test_override_still_fires_above_the_threshold():
    """The fix must not disturb the normal in-band override path."""
    result = _result_with_ols([0.02, 1.10, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    out = result.apply(
        0.12, horizon_hours=36.0, hour_of_day=12, stpasa=_SF, run_features=_RF
    )
    assert out["calibrated_source"] == "isotonic+stpasa", (
        f"expected the override to still fire, got {out['calibrated_source']}"
    )
    assert out["calibrated"] > 0.0
    assert out["stpasa_run_at"] == _SF.stpasa_run_at
    print(
        "  PASS: override still fires above the threshold "
        f"(calibrated={out['calibrated']:.4f})"
    )


def test_mild_negative_above_threshold_is_still_calibrated():
    """Mild negatives above the threshold keep going through stage 1 and stage 2.

    The threshold comment records that AEMO often forecasts mild negatives
    during the solar window and that the isotonic step maps those usefully
    toward zero. That path must be untouched: only the deep bypass is protected.
    """
    result = _result_with_ols([0.02, 1.10, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    raw = -0.03
    assert raw > NEGATIVE_PASSTHROUGH_THRESHOLD
    out = result.apply(
        raw, horizon_hours=36.0, hour_of_day=12, stpasa=_SF, run_features=_RF
    )
    assert out["calibrated_source"] != "passthrough_negative", (
        "a mild negative must not reach the deep bypass"
    )
    assert out["calibrated"] >= 0.0, (
        f"stage 1 floors its output at zero, got {out['calibrated']}"
    )
    print(
        "  PASS: mild negative above the threshold still calibrated "
        f"(source={out['calibrated_source']}, calibrated={out['calibrated']:.4f})"
    )


def test_fitted_model_without_negative_training_rows_would_flip():
    """Evidence case: a genuinely fitted stage-2 model, no deep negative rows.

    fit_ols_stage2 filters on intervention, horizon band and spike threshold
    only, so it never excludes negative forecasts. What excludes them in
    practice is that the store rarely holds any: the first feature is the
    stage-1 output, which is floored at zero above the threshold, so the
    training set has no support in the negative range unless deeply negative
    intervals were both forecast and matched to STPASA features. This test
    fits stage 2 on a training set with zero such rows, confirms the fitted
    model still predicts a positive value at a deeply negative forecast, and
    confirms apply refuses to publish it.
    """
    rng = random.Random(17)
    run_at = (_ANCHOR - timedelta(days=1)).replace(
        hour=3, minute=30, second=0, microsecond=0
    ).isoformat()
    obs = []
    stpasa_by_key = {}

    # Near-term rows so _compute_run_features has a run to summarise.
    for j in range(8):
        near = (_ANCHOR - timedelta(days=1)).replace(
            hour=4 + j, minute=0, second=0, microsecond=0
        )
        obs.append(
            Observation(
                interval_time=near.isoformat(),
                horizon_hours=2.0 + j,
                pd7day_forecast=rng.uniform(0.05, 0.25),
                actual_rrp=rng.uniform(0.05, 0.30),
                forecast_run_at=run_at,
                hour_of_day=near.hour,
                day_of_week=near.weekday(),
                month=near.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )

    # In-band rows: the raw forecast is only weakly informative and the demand
    # feature carries the signal, which is the regime stage 2 exists for.
    for i in range(300):
        interval = (_ANCHOR - timedelta(days=i % 60)).replace(
            hour=12, minute=(i % 2) * 30, second=(i % 50), microsecond=0
        )
        fc = rng.uniform(-0.0999, 0.30)
        surplus = rng.uniform(500.0, 5000.0)
        solar = rng.uniform(0.0, 4000.0)
        demand50 = rng.uniform(5000.0, 9000.0)
        actual = max(
            -0.02,
            0.25 * fc + 0.00004 * (demand50 - 6000.0) - 6e-6 * solar
            + rng.gauss(0, 0.02),
        )
        obs.append(
            Observation(
                interval_time=interval.isoformat(),
                horizon_hours=36.0,
                pd7day_forecast=fc,
                actual_rrp=actual,
                forecast_run_at=run_at,
                hour_of_day=12,
                day_of_week=interval.weekday(),
                month=interval.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )
        stpasa_by_key[f"{interval.isoformat()}|{run_at}"] = StpasaFeatures(
            log_surplus=math.log1p(surplus),
            log_solar=math.log1p(solar),
            log_demand=math.log(max(demand50, 1.0)),
            poe_spread_n=0.2,
            stpasa_run_at=run_at,
        )

    deep = [
        o for o in obs
        if o.pd7day_forecast <= NEGATIVE_PASSTHROUGH_THRESHOLD
        and OLS_MIN_HORIZON_H <= o.horizon_hours <= OLS_MAX_HORIZON_H
    ]
    assert not deep, "fixture should contain no deep negative in-band rows"

    engine = CalibrationEngine()
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)
    key = _bucket_key(36.0, 12)
    model = result.ols_models.get(key)
    assert model is not None and len(model.coef) >= 2, "stage 2 did not fit"

    raw = -0.101
    feature_vec = [
        raw,
        _RF.run_max_h6_rrp,
        _RF.run_mean_rrp,
        _RF.run_spread,
        36.0 / 168.0,
        _SF.log_surplus,
        _SF.log_solar,
        _SF.log_demand,
        _SF.poe_spread_n,
    ]
    prediction = model.predict(feature_vec)
    assert prediction > 0.0, (
        "expected the fitted model to extrapolate positive at a deeply negative "
        f"forecast, got {prediction:.4f}"
    )

    out = result.apply(
        raw, horizon_hours=36.0, hour_of_day=12, stpasa=_SF, run_features=_RF
    )
    assert out["calibrated_source"] == "passthrough_negative", (
        f"expected the bypass to hold, got {out['calibrated_source']}"
    )
    assert out["calibrated"] == round(raw, 6)
    print(
        "  PASS: fitted model with no negative training rows extrapolates to "
        f"{prediction:+.4f} and is correctly refused"
    )


if __name__ == "__main__":
    test_positive_prediction_does_not_override_negative_passthrough()
    test_no_sign_flip_sweep()
    test_override_still_fires_above_the_threshold()
    test_mild_negative_above_threshold_is_still_calibrated()
    test_fitted_model_without_negative_training_rows_would_flip()
    print("\nAll stage-2 negative passthrough tests passed.")
