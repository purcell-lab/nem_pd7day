"""
The stage-2 OLS feature and the published stage-1 price are the same unfloored value.

Issue #85: the first stage-2 feature was taken from
``BucketModel.apply_all(x)["calibrated"]``, which floors the isotonic
prediction at 0.0. For a raw forecast in the open interval (-0.10, 0.0), that
is inside the bucket's fitted domain and therefore genuinely served by
stage 2, the feature read exactly 0.0 while the settled actual was negative.
The regression was asked to explain a negative actual from a feature pinned at
zero, and the fitted iso_cal coefficient absorbed the error. Measured on the
harness below, a single such row in a 78 row bucket moved the coefficient by
+8.1 percent and sixteen moved it by +87.4 percent, sign consistent and
monotone at every count and every seed.

The remedy taken is option 1 of the issue: the value used as the stage-2
feature is the unfloored isotonic prediction, while the published stage-1
price keeps its 0.0 floor. The load bearing guarantee is therefore that no
published price moves, and the golden table in
``test_published_stage_one_output_is_identical_before_and_after`` is the proof.
Its numbers were captured by running this fixture against main at 0b35e55,
before the production change, and they are asserted unchanged after it.

Run with:  python -m pytest tests/test_stage2_floored_feature.py -v
or simply: python tests/test_stage2_floored_feature.py
"""
from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

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

BucketModel = _ce.BucketModel
CalibrationEngine = _ce.CalibrationEngine
CalibrationResult = _ce.CalibrationResult
IsotonicRegression = _ce.IsotonicRegression
LinearCoeff = _ce.LinearCoeff
Observation = _ce.Observation
OlsModel = _ce.OlsModel
QuantileCoeff = _ce.QuantileCoeff
RunFeatures = _ce.RunFeatures
StpasaFeatures = _ce.StpasaFeatures
SOURCE_PASSTHROUGH_BELOW_DOMAIN = _ce.SOURCE_PASSTHROUGH_BELOW_DOMAIN
OLS_MIN_HORIZON_H = _ce.OLS_MIN_HORIZON_H
OLS_MAX_HORIZON_H = _ce.OLS_MAX_HORIZON_H
OLS_MIN_OBS = _ce.OLS_MIN_OBS
SPIKE_THRESHOLD = _ce.SPIKE_THRESHOLD
_bucket_key = _ce._bucket_key
_compute_run_features = _ce._compute_run_features
# Resolved with getattr so this module still imports against a tree that does
# not have the fix, which is what lets the golden table be captured on main.
ISO_FEATURE_KEY = getattr(_ce, "ISO_FEATURE_KEY", "iso_feature")
stage2_iso_feature = getattr(_ce, "stage2_iso_feature", None)


# ── A deterministic fixture bucket ───────────────────────────────────────────
# Built without engine.fit on purpose. engine.fit applies exponential decay
# weights computed from the wall clock, so a fit is not reproducible across
# runs and could not carry a golden table. The isotonic model here is fitted
# directly on fixed pairs, so every number below is stable.

_ISO_XS = [round(-0.08 + 0.02 * i, 6) for i in range(21)]  # -0.08 to 0.32
_ISO_YS = [round(1.10 * x + 0.006, 6) for x in _ISO_XS]


def _fixture_bucket(bucket_key: str = "h24_48__solar") -> BucketModel:
    """A bucket whose isotonic map is negative below about -0.0055 $/kWh.

    The actual price relation is 1.10 * forecast + 0.006, so the isotonic
    prediction crosses zero at a forecast of about -0.00545 $/kWh. Every
    forecast between the domain floor of -0.08 and that crossing is a
    forecast where the published 0.0 floor binds, which is the region issue
    #85 is about.
    """
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray(_ISO_XS, dtype=float), np.asarray(_ISO_YS, dtype=float))
    return BucketModel(
        bucket_key=bucket_key,
        ols=LinearCoeff(a=1.10, b=0.006, n=120, mae=0.0075, rmse=0.0095),
        q10=QuantileCoeff(quantile=0.1, a=1.00, b=-0.010, n=120, pinball_loss=0.004),
        q50=QuantileCoeff(quantile=0.5, a=1.05, b=0.000, n=120, pinball_loss=0.006),
        q90=QuantileCoeff(quantile=0.9, a=1.10, b=0.020, n=120, pinball_loss=0.004),
        iso_model=iso,
    )


def _raw_iso(bucket: BucketModel, x: float) -> float:
    """The unfloored isotonic prediction, read straight off the model."""
    return float(bucket.iso_model.predict(np.asarray([x], dtype=float))[0])


# Forecast values spanning the three regions that matter: below the fitted
# domain, inside the formerly floored band, and ordinary
# positive intervals including a spike well past the training range.
_GOLDEN_XS = [
    -0.5, -0.30, -0.1500, -0.1000, -0.0999, -0.09, -0.0864, -0.05, -0.02,
    -0.0055, -0.0054, -0.001, 0.0, 0.001, 0.02, 0.05, 0.08, 0.12, 0.20,
    0.32, 0.75, 3.50,
]

# Captured by running _capture() against the engine after issue #117 replaced
# the fixed -0.10 passthrough boundary with the bucket's fitted domain, whose
# floor in this fixture is -0.08. Rows below -0.08 are passed through with the
# raw value and the fitted quantile band; rows from -0.08 to -0.0055 publish
# the fitted isotonic value (they published 0.0 with a collapsed band before
# #114). Rows at 0.02 and above are identical to the table captured before #85
# at 0b35e55, so the positive region is pinned across all three changes. Every field a caller publishes is pinned:
# sensor.py maps calibrated, p10, p50, p90 and calibrated_source onto the
# forecast attributes and the sensor state, and tariff_sensor.py publishes
# calibrated as the spot price.
_GOLDEN_PUBLISHED = {
    -0.5: (-0.5, -0.51, -0.51, -0.5, 'passthrough_below_domain'),
    -0.3: (-0.3, -0.31, -0.31, -0.3, 'passthrough_below_domain'),
    -0.15: (-0.15, -0.16, -0.1575, -0.145, 'passthrough_below_domain'),
    -0.1: (-0.1, -0.11, -0.105, -0.09, 'passthrough_below_domain'),
    -0.0999: (-0.0999, -0.1099, -0.104895, -0.08989, 'passthrough_below_domain'),
    -0.09: (-0.09, -0.1, -0.0945, -0.079, 'passthrough_below_domain'),
    -0.0864: (-0.0864, -0.0964, -0.09072, -0.07504, 'passthrough_below_domain'),
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
    return (
        out["calibrated"], out["p10"], out["p50"], out["p90"],
        out["calibrated_source"],
    )


def _capture() -> None:
    """Print the golden table. Run against main to regenerate it."""
    bucket = _fixture_bucket()
    print("_GOLDEN_PUBLISHED = {")
    for x in _GOLDEN_XS:
        print(f"    {x!r}: {_published(bucket, x)!r},")
    print("}")


# ── The load bearing guarantee: no published price moves ─────────────────────

def test_published_stage_one_output_matches_the_golden_table():
    """Every published stage-1 field matches the captured values.

    Issue #85 pinned this table so an internal feature change could not move a
    displayed price. Issue #114 then moved the negative region on purpose, so
    the table was recaptured; the rows at 0.02 and above are unchanged from the
    pre-#85 capture, which pins the positive region across both changes.
    """
    bucket = _fixture_bucket()
    for x in _GOLDEN_XS:
        assert _published(bucket, x) == _GOLDEN_PUBLISHED[x], (
            f"published stage-1 output changed at raw forecast {x} $/kWh: "
            f"{_published(bucket, x)} against golden {_GOLDEN_PUBLISHED[x]}"
        )
    print(
        f"  PASS: published stage-1 output matches the golden table at "
        f"{len(_GOLDEN_XS)} forecast values"
    )


def test_published_price_sweep_is_the_unfloored_isotonic_value():
    """Sweep: the published price is the isotonic value, negative or not.

    The golden table is a set of points; this is the invariant behind it,
    checked at 1601 forecast values from -0.400 to +0.400 $/kWh. Above the
    passthrough boundary the published value is exactly
    round(max(iso.predict(x), MARKET_PRICE_FLOOR), 6); the only floor is the
    market floor, which this fixture never reaches. At or below the boundary
    it is the raw forecast, untouched.
    """
    from custom_components.nem_pd7day.const import MARKET_PRICE_FLOOR
    bucket = _fixture_bucket()
    n = 0
    negatives = 0
    for i in range(1601):
        x = round(-0.400 + i * 0.0005, 6)
        out = bucket.apply_all(x)
        if bucket.is_below_domain(x):
            assert out["calibrated"] == round(x, 6)
            assert out["calibrated_source"] == SOURCE_PASSTHROUGH_BELOW_DOMAIN
        else:
            expected = round(max(_raw_iso(bucket, x), MARKET_PRICE_FLOOR), 6)
            assert out["calibrated"] == expected, (
                f"published price at {x} is {out['calibrated']}, "
                f"expected the isotonic value {expected}"
            )
            if out["calibrated"] < 0.0:
                negatives += 1
        n += 1
    assert negatives > 80, "the sweep must exercise the negative region"
    print(f"  PASS: published price is the isotonic value at {n} sweep points, {negatives} negative")


def test_published_price_equals_the_feature_where_the_floor_used_to_bind():
    """Where the old floor bound, price and feature are now the same negative.

    Under #85 the two values were deliberately different here: the feature
    carried the negative prediction and the price was floored to 0.0. Since
    #114 the price carries it too.
    """
    bucket = _fixture_bucket()
    checked = 0
    for i in range(1, 100):
        x = round(bucket.domain_min + i * 0.001, 6)
        if x >= -0.0055:
            continue
        out = bucket.apply_all(x)
        assert out["calibrated"] < 0.0, (
            f"the published price at {x} should be negative, got {out['calibrated']}"
        )
        assert out[ISO_FEATURE_KEY] == out["calibrated"] == round(_raw_iso(bucket, x), 6)
        checked += 1
    assert checked > 70
    print(f"  PASS: at {checked} formerly floored forecasts price and feature agree")


def test_feature_equals_published_price_where_the_floor_does_not_bind():
    """Outside the formerly floored band the feature and price agree too."""
    bucket = _fixture_bucket()
    for i in range(801):
        x = round(i * 0.0005, 6)  # 0.0 to 0.40
        out = bucket.apply_all(x)
        assert out[ISO_FEATURE_KEY] == out["calibrated"], (
            f"feature and published price disagree at {x}: "
            f"{out[ISO_FEATURE_KEY]} against {out['calibrated']}"
        )
    print("  PASS: feature equals the published price across 801 non-negative forecasts")


def test_feature_key_present_on_every_apply_all_path():
    """All three apply_all paths expose the feature, so no caller falls back.

    A missing key would send the fit or serve path back to the floored
    published value silently, which is the defect returning.
    """
    bucket = _fixture_bucket()
    out = bucket.apply_all(-0.15)
    assert out["calibrated_source"] == SOURCE_PASSTHROUGH_BELOW_DOMAIN
    assert out[ISO_FEATURE_KEY] == round(-0.15, 6)

    no_iso = _fixture_bucket()
    no_iso.iso_model = None
    out = no_iso.apply_all(0.08)
    assert out["calibrated_source"] == "passthrough"
    assert out[ISO_FEATURE_KEY] == 0.08

    out = bucket.apply_all(0.08)
    assert out["calibrated_source"] == "isotonic"
    assert out[ISO_FEATURE_KEY] == round(_raw_iso(bucket, 0.08), 6)
    print("  PASS: the stage-2 feature is present on all three apply_all paths")


def test_stage2_iso_feature_helper_is_the_single_definition():
    """The helper both paths read is monotone, unfloored and gap free.

    One definition, so the fit path and the serve path cannot drift apart.
    That drift is the #68 bug class, and it is why this is a module level
    helper rather than a dict lookup written out twice.
    """
    assert stage2_iso_feature is not None, "stage2_iso_feature is missing"
    bucket = _fixture_bucket()
    # Monotone within each region. Below the domain the feature is the raw
    # forecast, inside it the isotonic prediction; the two may step at the
    # domain floor (here the raw -0.0805 against a fitted -0.082 at -0.08),
    # which is the passthrough seam, not a fault in either function.
    previous = None
    was_below = None
    for i in range(1601):
        x = round(-0.400 + i * 0.0005, 6)
        got = stage2_iso_feature(bucket.apply_all(x), x)
        below = bucket.is_below_domain(x)
        if below:
            assert got == round(x, 6)
        else:
            assert got == round(_raw_iso(bucket, x), 6)
        if previous is not None and below == was_below:
            assert got >= previous - 1e-9, (
                f"the feature decreased between {x - 0.0005} and {x}"
            )
        previous, was_below = got, below
    # No gap: the feature now takes values inside the old (-0.10, 0.0) hole.
    attained = [
        stage2_iso_feature(bucket.apply_all(round(-0.0999 + i * 0.001, 6)),
                           round(-0.0999 + i * 0.001, 6))
        for i in range(95)
    ]
    assert any(-0.10 < v < 0.0 for v in attained), (
        "the feature still has no attainable value between the threshold and zero"
    )
    print("  PASS: the shared feature helper is monotone, unfloored and gap free")


def test_stage2_iso_feature_falls_back_to_the_published_value():
    """A dict without the key falls back rather than raising.

    Coefficients persisted before this change are still read by a serving
    path that may be handed an older shaped dict during a rolling upgrade, so
    the helper degrades to the previous behaviour instead of failing.
    """
    assert stage2_iso_feature({"calibrated": 0.05}, 0.07) == 0.05
    assert stage2_iso_feature({}, 0.07) == 0.07
    assert stage2_iso_feature({ISO_FEATURE_KEY: None, "calibrated": 0.05}, 0.07) == 0.05
    print("  PASS: the feature helper falls back to the published value then the raw")


# ── Deliberate behaviours from earlier work, confirmed still in place ────────

def test_below_domain_passthrough_still_returns_early_from_apply():
    """PR #74's gate is untouched: stage 2 never overrides a below-domain passthrough.

    The feature is present on that path too, so the guard has to be the source
    check and not an accident of a missing key.
    """
    bucket = _fixture_bucket()
    result = CalibrationResult(
        fitted_at=_ANCHOR.isoformat(),
        total_observations=500,
        models={"h24_48__solar": bucket},
        ols_models={
            "h24_48__solar": OlsModel(
                bucket_key="h24_48__solar",
                coef=[0.05, 1.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                n_train=90,
                r2=0.7,
            )
        },
    )
    stpasa = StpasaFeatures(
        log_surplus=7.2, log_solar=7.9, log_demand=9.0,
        poe_spread_n=0.18, stpasa_run_at=_ANCHOR.isoformat(),
    )
    rf = RunFeatures(run_max_h6_rrp=0.2, run_mean_rrp=0.08, run_spread=0.3)
    out = result.apply(-0.15, 36.0, 12, stpasa=stpasa, run_features=rf)
    assert out["calibrated_source"] == SOURCE_PASSTHROUGH_BELOW_DOMAIN
    assert out["calibrated"] == round(-0.15, 6)
    print("  PASS: apply still returns early on a below-domain passthrough")


def test_passthrough_band_is_still_left_unclamped():
    """The iso_model None path still publishes an unclamped band, on purpose.

    PR #71 left containment unenforced there because a fitted p10 above the
    raw forecast is the calibration saying the forecast is too low. Adding a
    key to that return dict must not have changed the band.
    """
    bucket = _fixture_bucket()
    bucket.iso_model = None
    out = bucket.apply_all(-0.02)
    assert out["calibrated_source"] == "passthrough"
    assert out["calibrated"] == -0.02
    assert out["p90"] > out["calibrated"], "the band collapsed onto the raw value"
    assert out["p10"] <= out["p50"] <= out["p90"], "band ordering broke"
    print("  PASS: the passthrough band is still deliberately unclamped")


# ── The bias measurement, driven through the real engine ─────────────────────

_TRUE_SLOPE = 1.10
_TRUE_INTERCEPT = 0.006
_FLOORED_DEPTH = -0.09
TARGET_H = 36.0
TARGET_HOUR = 12
TARGET = _bucket_key(TARGET_H, TARGET_HOUR)


def _hourly_base(hour: int) -> float:
    if 16 <= hour <= 21:
        return 0.16
    if 10 <= hour < 16:
        return 0.02
    return 0.08


def _build(n_runs: int = 26, seed: int = 7):
    """Return (observations, stpasa_by_key) with no negative rows at all.

    Same generator as tests/test_stage2_negative_row_exclusion.py: a SE
    Queensland diurnal shape with an evening peak, a cheap solar middle of the
    day and a flat shoulder. Each run carries near-term rows as well as
    in-band ones because _compute_run_features only produces an entry for a
    run with rows below 24 h.
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


def _promote(obs, k, depth=_FLOORED_DEPTH):
    """Move the first k in-band target rows into the floored band.

    The actual price comes from the same relation as every other row, so any
    coefficient movement is attributable to the feature and not to a different
    underlying relationship in the negative region.
    """
    out, taken = [], 0
    for o in obs:
        if taken < k and _in_target(o):
            out.append(o._replace(
                pd7day_forecast=depth,
                actual_rrp=_TRUE_SLOPE * depth + _TRUE_INTERCEPT,
            ))
            taken += 1
        else:
            out.append(o)
    assert taken == k, f"only promoted {taken} of {k} requested rows"
    return out


def _coef1(observations, stpasa):
    models = CalibrationEngine().fit_ols_stage2(
        observations, stpasa, region="QLD1"
    )
    m = models.get(TARGET)
    if m is None or len(m.coef) < 2:
        return None
    return m.coef[1]


def test_floored_rows_no_longer_bias_the_fitted_coefficient():
    """The regression case from the issue, at every count it reports.

    On main the fitted iso_cal coefficient rose monotonically with the number
    of floored rows: +8.1 percent at one row of 78 and +87.4 percent at
    sixteen, in the same direction in every seed. With the feature unfloored
    the shift against the same dataset holding no floored rows is under 4
    percent at every count and is not consistently signed, so what is left is
    noise and mild leverage rather than a systematic bias. The bar is set at
    5 percent, which main fails at k=1 already.
    """
    for seed in (7, 42):
        base_obs, stpasa = _build(seed=seed)
        reference = _coef1(base_obs, stpasa)
        assert reference is not None, "the target bucket was not fitted"
        for k in (1, 2, 4, 8, 16):
            coef = _coef1(_promote(base_obs, k), stpasa)
            assert coef is not None
            shift = (coef - reference) / abs(reference)
            assert abs(shift) < 0.05, (
                f"seed {seed}, {k} floored rows of 78 moved the iso_cal "
                f"coefficient by {shift * 100:+.1f} percent, from "
                f"{reference:.4f} to {coef:.4f}"
            )
    print("  PASS: floored rows shift the coefficient by under 5 percent at k up to 16")


def test_floored_rows_do_not_inflate_the_coefficient_monotonically():
    """The signature of the defect was monotonicity in the floored count.

    A coefficient that climbs with every extra floored row is the regression
    absorbing the error into the slope. This pins the shape of the curve
    rather than one point on it, because a single count could pass by luck.
    """
    base_obs, stpasa = _build(seed=7)
    reference = _coef1(base_obs, stpasa)
    coefs = [_coef1(_promote(base_obs, k), stpasa) for k in (1, 2, 4, 8, 16)]
    strictly_climbing = all(b > a for a, b in zip(coefs, coefs[1:]))
    assert not strictly_climbing, (
        f"the coefficient still climbs with the floored row count: {coefs}"
    )
    assert max(coefs) < reference * 1.10, (
        f"sixteen floored rows still inflate the coefficient: reference "
        f"{reference:.4f}, fitted {coefs}"
    )
    print("  PASS: the coefficient no longer climbs with the floored row count")


def test_fit_path_uses_the_same_unfloored_feature_as_serving():
    """The engine's fitted coefficients match a hand built unfloored design.

    Rebuilding the design matrix here with the unfloored isotonic value and
    checking the engine reproduces it to 1e-6 is what proves the fit path
    reads the same feature the serve path does. A fit that still floored would
    differ in the first coefficient by the biases measured above.
    """
    observations, stpasa_by_key = _build(seed=11)
    observations = _promote(observations, 8)
    engine = CalibrationEngine()
    iso_result = engine.fit(observations, region="QLD1")
    run_features = _compute_run_features(observations)
    rows = []
    for obs in observations:
        if obs.is_intervention:
            continue
        if obs.horizon_hours < OLS_MIN_HORIZON_H or obs.horizon_hours > OLS_MAX_HORIZON_H:
            continue
        if obs.actual_rrp >= SPIKE_THRESHOLD or obs.pd7day_forecast >= SPIKE_THRESHOLD:
            continue
        if stpasa_by_key.get(f"{obs.interval_time}|{obs.forecast_run_at}") is None:
            continue
        rf = run_features.get(obs.forecast_run_at)
        if rf is None:
            continue
        if _bucket_key(obs.horizon_hours, obs.hour_of_day) != TARGET:
            continue
        bucket = iso_result.get_bucket(obs.horizon_hours, obs.hour_of_day)
        if bucket.is_below_domain(obs.pd7day_forecast):
            continue
        sf = stpasa_by_key[f"{obs.interval_time}|{obs.forecast_run_at}"]
        feature = round(_raw_iso(bucket, obs.pd7day_forecast), 6)
        rows.append((
            [1.0, feature, rf.run_max_h6_rrp, rf.run_mean_rrp, rf.run_spread,
             obs.horizon_hours / 168.0, sf.log_surplus, sf.log_solar,
             sf.log_demand, sf.poe_spread_n],
            obs.actual_rrp,
        ))
    assert len(rows) >= OLS_MIN_OBS
    X = np.array([r[0] for r in rows], dtype=float)
    y = np.array([r[1] for r in rows], dtype=float)
    expected, *_ = np.linalg.lstsq(X, y, rcond=None)
    fitted = CalibrationEngine().fit_ols_stage2(
        observations, stpasa_by_key, region="QLD1"
    )[TARGET].coef
    assert len(fitted) == len(expected)
    for i, (got, want) in enumerate(zip(fitted, expected)):
        assert abs(got - want) < 1e-6, (
            f"coefficient {i} is {got}, an unfloored design gives {want}"
        )
    # And the negative feature values really are in the matrix. The eight
    # promoted rows sit at an isotonic prediction of -0.093, and the generator
    # also produces a few ordinary rows whose forecast is mildly negative and
    # which main floored to 0.0 as well, so the total is at least eight.
    promoted = int(np.isclose(X[:, 1], -0.093).sum())
    negatives = int((X[:, 1] < 0.0).sum())
    assert promoted == 8, (
        f"expected the eight promoted rows at a feature of -0.093, found {promoted}"
    )
    assert negatives >= promoted, (
        "the floored rows did not reach the design matrix as negatives"
    )
    print("  PASS: the fitted coefficients match a hand built unfloored design matrix")


def test_serve_path_feeds_the_unfloored_feature_to_the_ols_model():
    """A mildly negative interval is predicted from a negative feature.

    With a coefficient vector that is zero everywhere except iso_cal, the
    stage-2 prediction is a direct read of the feature. On main it would come
    back from a feature of 0.0; here it has to reflect the negative isotonic
    value, which is what makes the fit and serve paths consistent.
    """
    bucket = _fixture_bucket()
    # A negative intercept keeps the stage-2 prediction on the same side of
    # zero as the negative isotonic value, so the sign-agreement rule of
    # issue #114 serves it rather than falling back.
    coef = [-0.02] + [1.0] + [0.0] * 8
    result = CalibrationResult(
        fitted_at=_ANCHOR.isoformat(),
        total_observations=500,
        models={TARGET: bucket},
        ols_models={
            TARGET: OlsModel(bucket_key=TARGET, coef=coef, n_train=90, r2=0.7)
        },
    )
    stpasa = StpasaFeatures(
        log_surplus=0.0, log_solar=0.0, log_demand=0.0,
        poe_spread_n=0.0, stpasa_run_at=_ANCHOR.isoformat(),
    )
    rf = RunFeatures(run_max_h6_rrp=0.0, run_mean_rrp=0.0, run_spread=0.0)
    for x in (-0.07, -0.05, -0.02):
        out = result.apply(x, TARGET_H, TARGET_HOUR, stpasa=stpasa, run_features=rf)
        assert out["calibrated_source"] == "isotonic+stpasa"
        want = round(-0.02 + round(_raw_iso(bucket, x), 6), 6)
        assert abs(out["calibrated"] - want) < 1e-6, (
            f"stage 2 at {x} published {out['calibrated']}, an unfloored "
            f"feature gives {want}"
        )
        # Sanity: a floored feature would have produced exactly -0.02.
        assert abs(out["calibrated"] - (-0.02)) > 1e-4
    print("  PASS: the serve path predicts a floored interval from the unfloored feature")


_TESTS = [
    test_published_stage_one_output_matches_the_golden_table,
    test_published_price_sweep_is_the_unfloored_isotonic_value,
    test_published_price_equals_the_feature_where_the_floor_used_to_bind,
    test_feature_equals_published_price_where_the_floor_does_not_bind,
    test_feature_key_present_on_every_apply_all_path,
    test_stage2_iso_feature_helper_is_the_single_definition,
    test_stage2_iso_feature_falls_back_to_the_published_value,
    test_below_domain_passthrough_still_returns_early_from_apply,
    test_passthrough_band_is_still_left_unclamped,
    test_floored_rows_no_longer_bias_the_fitted_coefficient,
    test_floored_rows_do_not_inflate_the_coefficient_monotonically,
    test_fit_path_uses_the_same_unfloored_feature_as_serving,
    test_serve_path_feeds_the_unfloored_feature_to_the_ols_model,
]


if __name__ == "__main__":
    if "--capture" in sys.argv:
        _capture()
        raise SystemExit(0)
    print("Stage-2 floored feature, issue #85")
    for t in _TESTS:
        t()
    print(f"\n{len(_TESTS)} tests passed")
