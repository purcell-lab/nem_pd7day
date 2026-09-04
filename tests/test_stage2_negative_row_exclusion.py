"""
Stage-2 OLS training rows agree with the serve path, and an isolated row
cannot steer the fit.

Issue #79: the first stage-2 feature is the stage-1 isotonic output, and at
the time ``BucketModel.apply_all`` returned the raw value for any forecast at
or below a fixed -0.10 $/kWh boundary. Such a row sat on the far side of a gap
with nothing between it and the rest of the cluster, so in ordinary least
squares it was a high leverage point: measured hat leverage for a single such
row was 0.92 to 0.98 against a bucket mean near 0.13, and one mis-joined deep
negative observation moved the fitted iso_cal coefficient from +1.13 to -0.15,
a sign flip, in a 78 row bucket. PR #80 dropped those rows from the fit.

Issue #117 replaced the fixed boundary with the bucket's fitted domain: a
forecast below the lowest training forecast is passed through, and a row the
serving path would pass through is dropped from the stage-2 design matrix by
the same ``BucketModel.is_below_domain`` predicate. The leverage protection
the fixed boundary gave by accident is now explicit: rows whose hat leverage
exceeds ``STAGE2_LEVERAGE_MULTIPLE`` times the mean p/n are dropped and the
bucket refitted once, whatever their price. This module pins both halves and
that ``OLS_MIN_OBS`` is counted after both filters.

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
BucketModel = _ce.BucketModel
Observation = _ce.Observation
RunFeatures = _ce.RunFeatures
StpasaFeatures = _ce.StpasaFeatures
SOURCE_ISOTONIC_BELOW_DOMAIN = _ce.SOURCE_ISOTONIC_BELOW_DOMAIN
OLS_MIN_HORIZON_H = _ce.OLS_MIN_HORIZON_H
OLS_MAX_HORIZON_H = _ce.OLS_MAX_HORIZON_H
OLS_MIN_OBS = _ce.OLS_MIN_OBS
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

# The fixture clips every forecast at this value, so it is the lowest training
# forecast in every bucket and therefore each bucket's domain floor.
_FIXTURE_FLOOR = -0.02


def _hourly_base(hour: int) -> float:
    if 16 <= hour <= 21:
        return 0.16
    if 10 <= hour < 16:
        return 0.02
    return 0.08


def _build(n_runs: int = 26, seed: int = 7):
    """Return (observations, stpasa_by_key) with no forecast below -0.02.

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
            fc = max(_FIXTURE_FLOOR, _hourly_base(hour) + rng.gauss(0, 0.02))
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
    """Move the first k target-bucket rows to a forecast of ``depth``."""
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


def _fit_stage2_with_domain_from(reference_obs, promoted, sp):
    """Fit stage 2 on ``promoted`` with stage 1 fitted on ``reference_obs``.

    ``fit_ols_stage2`` refits stage 1 on the rows it is given, so a promoted
    row widens the bucket's domain and is never below it. Real data can
    disagree between the two stages: stage 1 is fitted on the full
    observation window, stage 2 only on rows with STPASA and run features,
    and the store can hold a row whose forecast was never seen by the
    stage-1 fit that is current at serve time. Pinning the stage-1 result
    to the reference rows reproduces that case deterministically.
    """
    engine = CalibrationEngine()
    reference = CalibrationEngine().fit(reference_obs)
    engine.fit = lambda observations, region=None: reference
    return engine.fit_ols_stage2(promoted, sp)


def _coef1(models):
    m = models.get(TARGET)
    if m is None or len(m.coef) < 2:
        return None
    return m.coef[1]


# ── The boundary is one definition, shared by both paths ──────────────────────

def test_is_below_domain_boundary():
    """The domain floor is inclusive: at the lowest training forecast the
    bucket is calibrated, just below it the forecast is passed through."""
    obs, _sp = _build()
    bucket = CalibrationEngine().fit(obs).get_bucket(TARGET_H, TARGET_HOUR)
    assert bucket.domain_min == _FIXTURE_FLOOR, (
        f"the fixture clips forecasts at {_FIXTURE_FLOOR}, so that must be the "
        f"domain floor; got {bucket.domain_min}"
    )
    assert not bucket.is_below_domain(_FIXTURE_FLOOR), (
        "a forecast exactly at the lowest training forecast is inside the domain"
    )
    assert bucket.is_below_domain(_FIXTURE_FLOOR - 1e-9)
    assert not bucket.is_below_domain(_FIXTURE_FLOOR + 1e-9)
    assert not bucket.is_below_domain(0.0)
    assert bucket.is_below_domain(-0.09)
    assert bucket.is_below_domain(-5.0)

    empty = BucketModel(bucket_key=TARGET)
    assert empty.domain_min is None
    assert not empty.is_below_domain(-5.0), (
        "a bucket with no isotonic model has no domain and never reports below it"
    )
    print(f"  PASS: is_below_domain boundary is inclusive at {_FIXTURE_FLOOR}")


def test_serve_path_agrees_with_the_shared_predicate():
    """apply_all's source must agree with is_below_domain everywhere.

    Sweep rather than point cases: the whole point of the shared predicate is
    that the fit filter and the serve bypass cannot drift apart, so the
    agreement is checked across the range instead of at hand-picked values.
    """
    obs, _sp = _build()
    bucket = CalibrationEngine().fit(obs).get_bucket(TARGET_H, TARGET_HOUR)
    checked = 0
    for i in range(-400, 401):
        x = i / 1000.0  # -0.400 to +0.400 $/kWh in 0.001 steps
        is_bypass = (
            bucket.apply_all(x)["calibrated_source"] == SOURCE_ISOTONIC_BELOW_DOMAIN
        )
        assert is_bypass == bucket.is_below_domain(x), (
            f"serve path and predicate disagree at raw {x} $/kWh: "
            f"apply_all bypass={is_bypass}, predicate={bucket.is_below_domain(x)}"
        )
        checked += 1
    print(f"  PASS: serve path agrees with the shared predicate over {checked} values")


# ── Rows below the domain do not enter the stage-2 design matrix ──────────────

def test_below_domain_rows_are_excluded_from_the_fit():
    """n_train must count only the rows inside the stage-1 domain."""
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    assert n_before >= OLS_MIN_OBS, "target bucket must be fitted in the baseline"

    k = 6
    m = _fit_stage2_with_domain_from(obs, _promote(obs, k, -0.30), sp)[TARGET]
    assert m.n_train == n_before - k, (
        f"expected {n_before - k} training rows after excluding {k} "
        f"below-domain rows; got {m.n_train}"
    )
    print(f"  PASS: {k} below-domain rows excluded, n_train {n_before} -> "
          f"{m.n_train}")


def test_exclusion_count_sweep_over_depth_and_count():
    """Invariant sweep: n_train falls by exactly the below-domain row count.

    Swept over depth as well as count because the filter must key off the
    domain floor alone, not off how far below it a row sits.
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    cases = 0
    for depth in (-0.021, -0.05, -0.10, -0.30, -1.00, -5.00):
        for k in (1, 2, 5, 11):
            m = _fit_stage2_with_domain_from(obs, _promote(obs, k, depth), sp)[TARGET]
            assert m.n_train == n_before - k, (
                f"depth {depth} $/kWh, k={k}: expected n_train "
                f"{n_before - k}, got {m.n_train}"
            )
            cases += 1
    print(f"  PASS: n_train falls by exactly the excluded count across {cases} cases")


def test_rows_inside_the_domain_are_still_fitted():
    """A mildly negative forecast inside the domain is served by stage 2, so
    it must be fitted.

    This is the other half of train and serve consistency. Excluding these
    rows as well would be the skew issue #68 was about: apply() does run the
    stage-2 override for a raw forecast of -0.01 $/kWh in this fixture, so
    the fit has to see rows like it. Under the old fixed boundary this held
    at -0.09; under the domain rule it holds at any forecast the bucket was
    trained on, including a cluster of deep negatives.
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    for depth, k in ((-0.01, 6), (-0.10, 11), (-0.30, 11), (-1.00, 11)):
        m = CalibrationEngine().fit_ols_stage2(_promote(obs, k, depth), sp)[TARGET]
        assert m.n_train == n_before, (
            f"{k} rows at {depth} $/kWh are inside the domain and must stay in "
            f"the fit; n_train moved from {n_before} to {m.n_train}"
        )
    print(f"  PASS: in-domain negative rows remain in the fit (n_train {n_before})")


# ── The leverage this was all about ──────────────────────────────────────────

def test_isolated_row_is_screened_by_leverage_whatever_its_price():
    """One or two rows far from the cluster are dropped; a cluster is kept.

    The fixed -0.10 boundary protected the fit from a lone deep negative row
    only by accident, and not at all from a lone spike. The hat-leverage
    screen (#117) is keyed on where a row sits in the design, so it drops an
    isolated row at either end and keeps a group of rows that support each
    other, which is exactly the case the old boundary got wrong (an
    oversupplied SA1 solar afternoon is many rows near -0.19 $/kWh, all
    genuine).
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    cases = 0
    for depth in (-0.30, -1.00, -5.00, 0.60, 0.90):
        for k in (1, 2):
            m = CalibrationEngine().fit_ols_stage2(_promote(obs, k, depth), sp)[TARGET]
            assert m.n_train == n_before - k, (
                f"{k} isolated row(s) at {depth} $/kWh should be screened; "
                f"n_train {m.n_train}, expected {n_before - k}"
            )
            cases += 1
        for k in (5, 11):
            m = CalibrationEngine().fit_ols_stage2(_promote(obs, k, depth), sp)[TARGET]
            assert m.n_train == n_before, (
                f"a cluster of {k} rows at {depth} $/kWh supports itself and "
                f"must be kept; n_train {m.n_train}, expected {n_before}"
            )
            cases += 1
    print(f"  PASS: isolated rows screened, clusters kept, across {cases} cases")


def test_mis_joined_deep_negative_row_cannot_flip_the_coefficient():
    """A single corrupt deep negative row must not invert the iso_cal slope.

    Regression case from issue #79. Before any filter, one row whose actual
    price was mis-joined onto a deep negative forecast took the fitted
    coefficient from about +1.13 to about -0.15 across five independent
    seeds, because its hat leverage was near 1. A negative iso_cal
    coefficient of -1.879 was observed in the wild on h24_48__shoulder and is
    pinned by test_apply_stpasa_negative_ols_falls_back_to_isotonic in
    tests/test_calibration_engine.py, which is the same failure mode. The row
    now widens the domain rather than falling outside it, so the leverage
    screen is what carries this guarantee.
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
            f"seed {seed}: one mis-joined deep negative row inverted the iso_cal "
            f"coefficient, {ref:.4f} -> {got:.4f}"
        )
        checked += 1
    print(f"  PASS: mis-joined deep negative row cannot flip the coefficient "
          f"({checked} seeds)")


def test_corrupt_deep_negative_row_keeps_the_slope_positive():
    """Sweeping an isolated row's actual price must never invert the slope.

    The sign is the property that protects the published value: a negative
    iso_cal coefficient drives the stage-2 prediction non-positive for
    ordinary forecasts, which is the -1.879 failure already pinned in
    tests/test_calibration_engine.py. Before any filter this sweep crossed
    zero, reaching -0.45 at the top of the range.

    The magnitude is deliberately NOT pinned. Screening the row out of the
    stage-2 design matrix does not remove it from the stage-1 isotonic fit,
    which still sees every observation, and a corrupt actual above its
    neighbours propagates through the pool adjacent violators pooling and
    shifts the stage-1 feature of other rows. That residual channel is
    measured here at a spread of about 0.85 across this sweep, most of it
    from the extreme 1.20 $/kWh case alone. It is a real remaining exposure,
    reported rather than asserted away, and narrowing it would mean changing
    the stage-1 training set, which also changes published stage-1 values.
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
        f"a corrupt deep negative row inverted the iso_cal slope at these "
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
    models = _fit_stage2_with_domain_from(obs, _promote(obs, k, -0.30), sp)
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
    at_floor = _fit_stage2_with_domain_from(obs, _promote(obs, k - 1, -0.30), sp)
    assert len(at_floor[TARGET].coef) >= 2, (
        f"bucket sitting exactly at OLS_MIN_OBS={OLS_MIN_OBS} must still fit"
    )
    assert at_floor[TARGET].n_train == OLS_MIN_OBS
    print(f"  PASS: {n_before - k} rows falls back, {OLS_MIN_OBS} rows fits")


def test_leverage_screen_counts_ols_min_obs_after_dropping_rows():
    """A bucket exactly at the floor that loses a screened row falls back."""
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    k = n_before - OLS_MIN_OBS
    # k rows below the pinned domain leave exactly OLS_MIN_OBS survivors; one
    # of those is then moved to an isolated spike, which survives the domain
    # filter but not the leverage screen, so the refit has too few rows. At
    # n = 50 and p = 10 the screen fires above a leverage of 0.6, so the spike
    # sits just under SPIKE_THRESHOLD to be far enough from the cluster.
    thinned = _promote(obs, k, -0.30)
    spiked, taken = [], 0
    for o in thinned:
        if taken < 1 and _in_target(o) and o.pd7day_forecast > -0.30:
            spiked.append(o._replace(
                pd7day_forecast=2.90,
                actual_rrp=_TRUE_SLOPE * 2.90 + _TRUE_INTERCEPT,
            ))
            taken += 1
        else:
            spiked.append(o)
    assert taken == 1
    m = _fit_stage2_with_domain_from(obs, spiked, sp)[TARGET]
    assert m.coef == [] and m.n_train == 0, (
        f"a bucket left with {OLS_MIN_OBS - 1} rows after the leverage screen "
        f"must fall back; got n_train {m.n_train}"
    )
    print("  PASS: OLS_MIN_OBS is counted after the leverage screen")


def test_thinned_bucket_serves_the_stage_one_result():
    """A bucket that fell back must not publish an isotonic+stpasa value."""
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    k = n_before - OLS_MIN_OBS + 1
    thinned = _promote(obs, k, -0.30)

    result = CalibrationEngine().fit(obs)
    result.ols_models = _fit_stage2_with_domain_from(obs, thinned, sp)

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


def test_bucket_of_only_below_domain_rows_still_appears():
    """A bucket whose every candidate row is excluded must not vanish.

    apply() treats a missing key and an empty coef list identically, but the
    diagnostic surface should still show the bucket rather than silently
    losing it.
    """
    obs, sp = _build()
    n_before = CalibrationEngine().fit_ols_stage2(obs, sp)[TARGET].n_train
    total_in_target = sum(1 for o in obs if _in_target(o))
    models = _fit_stage2_with_domain_from(
        obs, _promote(obs, total_in_target, -0.30), sp
    )
    assert TARGET in models, (
        f"bucket {TARGET} disappeared when all {total_in_target} of its "
        f"candidate rows were excluded"
    )
    assert models[TARGET].coef == []
    print(f"  PASS: bucket of {total_in_target} wholly excluded rows still "
          f"present with an empty model (was {n_before} fitted rows)")


def test_other_buckets_are_untouched():
    """Screening a row in one bucket must not disturb the others.

    Both filters are per row, so a bucket with no excluded or screened rows
    must fit identically. Guards against either being applied at bucket
    granularity.
    """
    obs, sp = _build()
    before = CalibrationEngine().fit_ols_stage2(obs, sp)
    after = CalibrationEngine().fit_ols_stage2(_promote(obs, 1, -0.30), sp)
    others = [k for k in before if k != TARGET]
    assert others, "fixture must populate more than one bucket"
    assert after[TARGET].n_train == before[TARGET].n_train - 1, (
        "the promoted row should have been screened from the target bucket"
    )
    for key in others:
        assert after[key].n_train == before[key].n_train, (
            f"bucket {key} row count changed from {before[key].n_train} to "
            f"{after[key].n_train}"
        )
        assert after[key].coef == before[key].coef, (
            f"bucket {key} coefficients moved despite having no screened rows"
        )
    print(f"  PASS: {len(others)} other buckets fitted identically")


_TESTS = [
    test_is_below_domain_boundary,
    test_serve_path_agrees_with_the_shared_predicate,
    test_below_domain_rows_are_excluded_from_the_fit,
    test_exclusion_count_sweep_over_depth_and_count,
    test_rows_inside_the_domain_are_still_fitted,
    test_isolated_row_is_screened_by_leverage_whatever_its_price,
    test_mis_joined_deep_negative_row_cannot_flip_the_coefficient,
    test_corrupt_deep_negative_row_keeps_the_slope_positive,
    test_bucket_thinned_below_ols_min_obs_falls_back_cleanly,
    test_leverage_screen_counts_ols_min_obs_after_dropping_rows,
    test_thinned_bucket_serves_the_stage_one_result,
    test_bucket_of_only_below_domain_rows_still_appears,
    test_other_buckets_are_untouched,
]

if __name__ == "__main__":
    failed = 0
    for fn in _TESTS:
        print(f"{fn.__name__}:")
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL: {exc}")
    sys.exit(1 if failed else 0)
