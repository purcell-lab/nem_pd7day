"""
Stage-2 OLS must not be fitted on rows the serving path never asks it about.

Issue #79: the first stage-2 feature is the stage-1 isotonic output, and
``BucketModel.apply_all`` floors that output at 0.0 for every raw forecast
above ``NEGATIVE_PASSTHROUGH_THRESHOLD`` while returning the raw value at or
below it. No value in the open interval (-0.10, 0.0) is attainable, so a
sub-threshold training row lands on the far side of a gap with nothing between
it and the rest of the cluster. In ordinary least squares that makes it a high
leverage point: measured hat leverage for a single such row is 0.92 to 0.98
against a bucket mean near 0.13. One mis-joined deep negative observation was
measured moving the fitted iso_cal coefficient from +1.13 to -0.15, a sign
flip, in a 78 row bucket.

PR #74 stopped the model being consulted below the boundary but left those
rows in the fit. This module pins that they are now dropped from the stage-2
design matrix, that the boundary is one shared definition across the fit and
serve paths, and that the OLS_MIN_OBS floor is counted after exclusion so a
bucket thinned by the filter falls back cleanly instead of fitting a nine
feature model on too few points.

Run with:  python -m pytest tests/test_stage2_negative_row_exclusion.py -v
or simply: python tests/test_stage2_negative_row_exclusion.py
"""
from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

NEM_TZ = timezone(timedelta(hours=10))  # NEM is UTC+10 year-round, no DST
_ANCHOR = datetime.now(NEM_TZ) - timedelta(days=3)

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
RunFeatures = _ce.RunFeatures
StpasaFeatures = _ce.StpasaFeatures
NEGATIVE_PASSTHROUGH_THRESHOLD = _ce.NEGATIVE_PASSTHROUGH_THRESHOLD
OLS_MIN_HORIZON_H = _ce.OLS_MIN_HORIZON_H
OLS_MAX_HORIZON_H = _ce.OLS_MAX_HORIZON_H
OLS_MIN_OBS = _ce.OLS_MIN_OBS
is_negative_passthrough = _ce.is_negative_passthrough
_bucket_key = _ce._bucket_key

# The bucket every test below targets: 36 h ahead, midday, so inside the OLS
# horizon band and in the solar time-of-day bucket where NEM negative prices
# actually occur.
TARGET_H = 36.0
TARGET_HOUR = 12
TARGET = _bucket_key(TARGET_H, TARGET_HOUR)

# Diurnal shape for a single residential premises in SE Queensland: an evening
# peak, a cheap solar middle of the day, a flat shoulder.
_TRUE_SLOPE = 1.10
_TRUE_INTERCEPT = 0.006


def _hourly_base(hour: int) -> float:
    if 16 <= hour <= 21:
        return 0.16
    if 10 <= hour < 16:
        return 0.02
    return 0.08


def _build(n_runs: int = 26, seed: int = 7):
    """Return (observations, stpasa_by_key) with no sub-threshold rows.

    Each run carries near-term rows as well as in-band ones, because
    ``_compute_run_features`` only produces a RunFeatures entry for a run that
    has rows below 24 h, and ``fit_ols_stage2`` skips any row whose run has
    none.
    """
    rng = random.Random(seed)
    obs: list[Observation] = []
    stpasa: dict[str, StpasaFeatures] = {}
    for r in range(n_runs):
        run_dt = (_ANCHOR - timedelta(days=r * 2)).replace(
            hour=3, minute=30, second=0, microsecond=0
        )
        run_at = run_dt.isoformat()
        for h_int in list(range(1, 24, 2)) + list(range(24, 97, 2)):
            interval_dt = run_dt + timedelta(hours=h_int)
            hour = interval_dt.hour
            solar = 10 <= hour < 16
            fc = max(-0.02, _hourly_base(hour) + rng.gauss(0, 0.02))
            interval_iso = interval_dt.isoformat()
            obs.append(Observation(
                interval_time=interval_iso,
                horizon_hours=float(h_int),
                pd7day_forecast=fc,
                actual_rrp=_TRUE_SLOPE * fc + _TRUE_INTERCEPT + rng.gauss(0, 0.008),
                forecast_run_at=run_at,
                hour_of_day=hour,
                day_of_week=interval_dt.weekday(),
                month=interval_dt.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            ))
            stpasa[f"{interval_iso}|{run_at}"] = StpasaFeatures(
                log_surplus=math.log1p(max(0.0, 1400.0 + rng.gauss(0, 250.0))),
                log_solar=math.log1p(
                    max(0.0, (2800.0 if solar else 200.0) + rng.gauss(0, 300.0))
                ),
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


def _promote(obs, k, depth, actual=None):
    """Move the first k in-band target-bucket rows to a sub-threshold forecast."""
    out, taken = [], 0
    for o in obs:
        if taken < k and _in_target(o):
            out.append(o._replace(
                pd7day_forecast=depth,
                actual_rrp=(actual if actual is not None
                            else _TRUE_SLOPE * depth + _TRUE_INTERCEPT),
            ))
            taken += 1
        else:
            out.append(o)
    assert taken == k, f"only promoted {taken} of {k} requested rows"
    return out


def _coef1(models):
    m = models.get(TARGET)
    if m is None or len(m.coef) < 2:
        return None
    return m.coef[1]


# ── The boundary is one definition, shared by both paths ──────────────────────

def test_is_negative_passthrough_boundary():
    """The boundary is inclusive at the threshold and exclusive just above."""
    assert is_negative_passthrough(NEGATIVE_PASSTHROUGH_THRESHOLD), (
        "a forecast exactly at the threshold must bypass calibration"
    )
    assert is_negative_passthrough(NEGATIVE_PASSTHROUGH_THRESHOLD - 1e-9)
    assert not is_negative_passthrough(NEGATIVE_PASSTHROUGH_THRESHOLD + 1e-9)
    assert not is_negative_passthrough(0.0)
    assert not is_negative_passthrough(-0.09)
    print("  PASS: is_negative_passthrough boundary is inclusive at the threshold")


def test_serve_path_agrees_with_the_shared_predicate():
    """apply_all's source must agree with is_negative_passthrough everywhere.

    Sweep rather than point cases: the whole point of the shared helper is
    that the fit filter and the serve bypass cannot drift apart, so the
    agreement is checked across the range instead of at hand-picked values.
    """
    engine = CalibrationEngine()
    obs, _sp = _build()
    result = engine.fit(obs)
    bucket = result.get_bucket(TARGET_H, TARGET_HOUR)
    checked = 0
    for i in range(-400, 401):
        x = i / 1000.0  # -0.400 to +0.400 $/kWh in 0.001 steps
        is_bypass = bucket.apply_all(x)["calibrated_source"] == "passthrough_negative"
        assert is_bypass == is_negative_passthrough(x), (
            f"serve path and predicate disagree at raw {x} $/kWh: "
            f"apply_all bypass={is_bypass}, predicate={is_negative_passthrough(x)}"
        )
        checked += 1
    print(f"  PASS: serve path agrees with the shared predicate over {checked} values")


# ── Rows below the boundary do not enter the stage-2 design matrix ────────────

def test_sub_threshold_rows_are_excluded_from_the_fit():
    """n_train must count only the rows above the boundary."""
    obs, sp = _build()
    base_n = _coef1(CalibrationEngine().fit_ols_stage2(obs, sp))
    assert base_n is not None, "target bucket must be fitted in the baseline"
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train

    k = 6
    promoted = _promote(obs, k, -0.30)
    m = CalibrationEngine().fit_ols_stage2(promoted, sp)[TARGET]
    assert m.n_train == n_before - k, (
        f"expected {n_before - k} training rows after excluding {k} "
        f"sub-threshold rows; got {m.n_train}"
    )
    print(f"  PASS: {k} sub-threshold rows excluded, n_train {n_before} -> "
          f"{m.n_train}")


def test_exclusion_count_sweep_over_depth_and_count():
    """Invariant sweep: n_train falls by exactly the sub-threshold row count.

    Swept over depth as well as count because the filter must key off the
    boundary alone, not off how far below it a row sits.
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    cases = 0
    for depth in (-0.10, -0.11, -0.15, -0.30, -0.60, -1.00, -5.00):
        for k in (1, 2, 5, 11):
            m = CalibrationEngine().fit_ols_stage2(_promote(obs, k, depth), sp)[TARGET]
            assert m.n_train == n_before - k, (
                f"depth {depth} $/kWh, k={k}: expected n_train "
                f"{n_before - k}, got {m.n_train}"
            )
            cases += 1
    print(f"  PASS: n_train falls by exactly the excluded count across {cases} cases")


def test_rows_just_above_the_boundary_are_still_fitted():
    """A mildly negative forecast is served by stage 2, so it must be fitted.

    This is the other half of train and serve consistency. Excluding these
    rows as well would be the skew issue #68 was about: apply() does run the
    stage-2 override for a raw forecast of -0.09 $/kWh, so the fit has to see
    rows like it.
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    just_above = NEGATIVE_PASSTHROUGH_THRESHOLD + 0.01  # -0.09 $/kWh
    m = CalibrationEngine().fit_ols_stage2(_promote(obs, 6, just_above), sp)[TARGET]
    assert m.n_train == n_before, (
        f"rows at {just_above} $/kWh are served by stage 2 and must stay in the "
        f"fit; n_train moved from {n_before} to {m.n_train}"
    )
    print(f"  PASS: rows at {just_above:.2f} $/kWh remain in the fit (n_train "
          f"{m.n_train})")


# ── The leverage this was all about ──────────────────────────────────────────

def test_mis_joined_deep_negative_row_cannot_flip_the_coefficient():
    """A single corrupt sub-threshold row must not invert the iso_cal slope.

    Regression case from issue #79. Before the exclusion filter, one row whose
    actual price was mis-joined onto a deep negative forecast took the fitted
    coefficient from about +1.13 to about -0.15 across five independent seeds,
    because its hat leverage was near 1. A negative iso_cal coefficient of
    -1.879 was observed in the wild on h24_48__shoulder and is pinned by
    test_apply_stpasa_negative_ols_falls_back_to_isotonic in
    tests/test_calibration_engine.py, which is the same failure mode.
    """
    checked = 0
    for seed in (7, 11, 23, 42, 101):
        obs, sp = _build(seed=seed)
        ref = _coef1(CalibrationEngine().fit_ols_stage2(obs, sp))
        assert ref is not None and ref > 0.0, (
            f"seed {seed}: baseline coefficient must be positive, got {ref}"
        )
        # A plausible mis-join: an evening actual price landed on a midday row
        # whose forecast was deeply negative.
        corrupt = _promote(obs, 1, -0.50, actual=0.15)
        got = _coef1(CalibrationEngine().fit_ols_stage2(corrupt, sp))
        assert got is not None, f"seed {seed}: bucket must still be fitted"
        assert got > 0.0, (
            f"seed {seed}: one mis-joined sub-threshold row inverted the iso_cal "
            f"coefficient, {ref:.4f} -> {got:.4f}"
        )
        checked += 1
    print(f"  PASS: mis-joined deep negative row cannot flip the coefficient "
          f"({checked} seeds)")


def test_corrupt_sub_threshold_row_keeps_the_slope_positive():
    """Sweeping a sub-threshold row's actual price must never invert the slope.

    The sign is the property that protects the published value: a negative
    iso_cal coefficient drives the stage-2 prediction non-positive for
    ordinary forecasts, which is the -1.879 failure already pinned in
    tests/test_calibration_engine.py. Before the exclusion filter this sweep
    crossed zero, reaching -0.45 at the top of the range.

    The magnitude is deliberately NOT pinned. Excluding the row from the
    stage-2 design matrix does not remove it from the stage-1 isotonic fit,
    which still sees every observation, and a corrupt actual above its
    neighbours propagates through the pool adjacent violators pooling and
    shifts iso_cal for other rows. That residual channel is measured here at a
    spread of about 3.0 across this sweep, most of it from the extreme 1.20
    $/kWh case alone. It is a real remaining exposure,
    reported rather than asserted away, and narrowing it would mean changing
    the stage-1 training set, which also changes published stage-1 values and
    is out of scope for issue #79.
    """
    obs, sp = _build()
    coefs = {}
    for bad_actual in (-0.55, -0.20, 0.0, 0.05, 0.15, 0.30, 0.60, 1.20):
        c = _coef1(CalibrationEngine().fit_ols_stage2(
            _promote(obs, 1, -0.50, actual=bad_actual), sp
        ))
        assert c is not None, f"bucket must stay fitted at actual {bad_actual}"
        coefs[bad_actual] = c
    inverted = {k: v for k, v in coefs.items() if v <= 0.0}
    assert not inverted, (
        f"a corrupt sub-threshold row inverted the iso_cal slope at these "
        f"actual prices: {inverted}"
    )
    spread = max(coefs.values()) - min(coefs.values())
    print(f"  PASS: slope stays positive across {len(coefs)} corrupt actuals "
          f"(residual spread via the stage-1 isotonic fit: {spread:.4f})")


# ── OLS_MIN_OBS is counted after exclusion ───────────────────────────────────

def test_bucket_thinned_below_ols_min_obs_falls_back_cleanly():
    """A bucket that only clears OLS_MIN_OBS by including excluded rows must
    fall back to an empty OlsModel rather than fit on too few points.
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    assert n_before >= OLS_MIN_OBS, (
        f"fixture must start above the floor; n_train={n_before}, "
        f"OLS_MIN_OBS={OLS_MIN_OBS}"
    )
    # Promote just enough rows to drop the survivors one below the floor.
    k = n_before - OLS_MIN_OBS + 1
    thinned = _promote(obs, k, -0.30)
    models = CalibrationEngine().fit_ols_stage2(thinned, sp)
    assert TARGET in models, (
        "a thinned bucket must still appear in the result for diagnostics"
    )
    m = models[TARGET]
    assert m.coef == [], (
        f"bucket with {n_before - k} surviving rows (floor {OLS_MIN_OBS}) must "
        f"not be fitted; got coef of length {len(m.coef)}"
    )
    assert m.n_train == 0, f"unfitted bucket must report n_train 0, got {m.n_train}"

    # One fewer exclusion leaves it exactly at the floor, which must still fit.
    at_floor = CalibrationEngine().fit_ols_stage2(_promote(obs, k - 1, -0.30), sp)
    assert len(at_floor[TARGET].coef) >= 2, (
        f"bucket sitting exactly at OLS_MIN_OBS={OLS_MIN_OBS} must still fit"
    )
    assert at_floor[TARGET].n_train == OLS_MIN_OBS
    print(f"  PASS: {n_before - k} rows falls back, {OLS_MIN_OBS} rows fits")


def test_thinned_bucket_serves_the_stage_one_result():
    """A bucket that fell back must not publish an isotonic+stpasa value."""
    obs, sp = _build()
    engine = CalibrationEngine()
    n_before = engine.fit_ols_stage2(obs, sp)[TARGET].n_train
    k = n_before - OLS_MIN_OBS + 1
    thinned = _promote(obs, k, -0.30)

    result = CalibrationEngine().fit(thinned)
    result.ols_models = CalibrationEngine().fit_ols_stage2(thinned, sp)

    rf = RunFeatures(run_max_h6_rrp=0.18, run_mean_rrp=0.12, run_spread=0.04)
    sf = StpasaFeatures(
        log_surplus=math.log1p(1400.0),
        log_solar=math.log1p(2800.0),
        log_demand=math.log(8200.0),
        poe_spread_n=0.18,
        stpasa_run_at=_ANCHOR.replace(
            hour=3, minute=30, second=0, microsecond=0
        ).isoformat(),
    )
    out = result.apply(
        0.05, horizon_hours=TARGET_H, hour_of_day=TARGET_HOUR,
        stpasa=sf, run_features=rf,
    )
    assert out["calibrated_source"] != "isotonic+stpasa", (
        f"a bucket below OLS_MIN_OBS must not serve a stage-2 value; got "
        f"source {out['calibrated_source']!r}"
    )
    assert out["calibrated"] is not None, "fallback must still publish a value"
    print(f"  PASS: thinned bucket serves {out['calibrated_source']!r}, not "
          f"'isotonic+stpasa'")


def test_bucket_of_only_sub_threshold_rows_still_appears():
    """A bucket whose every candidate row is excluded must not vanish.

    apply() treats a missing key and an empty coef list identically, but the
    diagnostic surface should still show the bucket rather than silently
    losing it.
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    total_in_target = sum(1 for o in obs if _in_target(o))
    models = CalibrationEngine().fit_ols_stage2(
        _promote(obs, total_in_target, -0.30), sp
    )
    assert TARGET in models, (
        f"bucket {TARGET} disappeared when all {total_in_target} of its "
        f"candidate rows were excluded"
    )
    assert models[TARGET].coef == []
    print(f"  PASS: bucket of {total_in_target} wholly excluded rows still "
          f"present with an empty model (was {n_before} fitted rows)")


def test_other_buckets_are_untouched():
    """Excluding rows in one bucket must not disturb the others.

    The filter is per row, so a bucket with no sub-threshold rows must fit
    identically. Guards against the exclusion accidentally being applied at
    bucket granularity.
    """
    obs, sp = _build()
    before = CalibrationEngine().fit_ols_stage2(obs, sp)
    after = CalibrationEngine().fit_ols_stage2(_promote(obs, 6, -0.30), sp)
    others = [k for k in before if k != TARGET]
    assert others, "fixture must populate more than one bucket"
    for key in others:
        assert after[key].n_train == before[key].n_train, (
            f"bucket {key} row count changed from {before[key].n_train} to "
            f"{after[key].n_train}"
        )
        assert after[key].coef == before[key].coef, (
            f"bucket {key} coefficients moved despite having no excluded rows"
        )
    print(f"  PASS: {len(others)} other buckets fitted identically")


_TESTS = [
    test_is_negative_passthrough_boundary,
    test_serve_path_agrees_with_the_shared_predicate,
    test_sub_threshold_rows_are_excluded_from_the_fit,
    test_exclusion_count_sweep_over_depth_and_count,
    test_rows_just_above_the_boundary_are_still_fitted,
    test_mis_joined_deep_negative_row_cannot_flip_the_coefficient,
    test_corrupt_sub_threshold_row_keeps_the_slope_positive,
    test_bucket_thinned_below_ols_min_obs_falls_back_cleanly,
    test_thinned_bucket_serves_the_stage_one_result,
    test_bucket_of_only_sub_threshold_rows_still_appears,
    test_other_buckets_are_untouched,
]


if __name__ == "__main__":
    for fn in _TESTS:
        fn()
    print(f"\n{len(_TESTS)} tests passed")
