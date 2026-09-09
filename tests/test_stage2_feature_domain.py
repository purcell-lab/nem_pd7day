"""
Stage 2 is served only inside the feature range it was fitted on.

Issue #147: SA1, run 07:30 on 8 September 2026, Saturday 12 September 12:30
(horizon 101 h, bucket h96plus__solar). STPASA had demand50 at 24 MW, giving
log_demand 3.18 against a training range of about 5.7 to 7.3, and the stage-2
regression extrapolated to publish -$0.864/kWh for a raw -$0.10 that stage 1
put at -$0.012. The sign and floor gates both passed. The neighbouring rows
had negative demand50, a degenerate transform (log_demand 0, poe_spread_n 2
to 6), and were refused only because their predictions happened to land
below the floor.

Fix under test:
  * OlsModel carries each feature's training min and max. Stage 2 is refused
    when the extrapolation cost, sum of |coef_i| times the distance each
    feature lies outside its range, exceeds half the bucket's residual
    spread (#147 range gate, weighed per #153).
  * stpasa_feature_values returns None for demand50 below the transform's
    floor, on both the training and the serving side.
  * The ranges persist with the coefficients; a legacy store without them
    serves as before until the next fit.
  * The calibration summary publishes per-bucket stage-2 diagnostics.

Run with:  python -m pytest tests/test_stage2_feature_domain.py -v
or simply: python tests/test_stage2_feature_domain.py
"""
from __future__ import annotations

import importlib.util
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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
ResidualQuantiles = _ce.ResidualQuantiles
OLS_MIN_OBS = _ce.OLS_MIN_OBS
RunFeatures = _ce.RunFeatures
StpasaFeatures = _ce.StpasaFeatures
STAGE2_FEATURE_NAMES = _ce.STAGE2_FEATURE_NAMES
STPASA_DEMAND_FLOOR_MW = _ce.STPASA_DEMAND_FLOOR_MW
stpasa_feature_values = _ce.stpasa_feature_values
_bucket_key = _ce._bucket_key
_compute_run_features = _ce._compute_run_features
# stpasa_client imports aiohttp, which this HA-free test does not have, and
# from_interval reads only attributes, so a namespace with the same fields
# stands in for StpasaInterval.
StpasaInterval = SimpleNamespace

# The live case: horizon 101 h at 12:30 lands in h96plus__solar.
_HORIZON = 101.0
_HOUR = 12
_KEY = _bucket_key(_HORIZON, _HOUR)
_RUN_AT = (_ANCHOR - timedelta(days=1)).replace(
    hour=7, minute=30, second=0, microsecond=0
).isoformat()

# Training demand in the hundreds to over a thousand MW, as on the live
# install; the serve-time cases sit inside and far below that range.
_TRAIN_DEMAND_MW = (300.0, 1500.0)
_LIVE_DEMAND_MW = 24.0
_IN_RANGE_DEMAND_MW = 500.0


def _stpasa(demand50: float, surplus: float = 1800.0, solar: float = 900.0) -> StpasaFeatures:
    """Features from the shared transform at a chosen demand50."""
    values = stpasa_feature_values(surplus, solar, demand50, demand50 * 1.1, demand50 * 0.9)
    assert values is not None
    log_surplus, log_solar, log_demand, poe_spread_n = values
    return StpasaFeatures(
        log_surplus=log_surplus,
        log_solar=log_solar,
        log_demand=log_demand,
        poe_spread_n=poe_spread_n,
        stpasa_run_at=_RUN_AT,
    )


def _fitted_result(seed: int = 147):
    """A stage-1 and stage-2 fit on midday rows with demand 300 to 1500 MW.

    Returns (result, run_features) where run_features are those computed
    from the training run itself, so a serve-time vector built from them
    differs from the training rows only in what the test varies.
    """
    rng = random.Random(seed)
    obs: list = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}

    # Near-term rows so _compute_run_features has a run to summarise.
    for j in range(8):
        near = (_ANCHOR - timedelta(days=1)).replace(
            hour=8 + j, minute=0, second=0, microsecond=0
        )
        obs.append(
            Observation(
                interval_time=near.isoformat(),
                horizon_hours=0.5 + j,
                pd7day_forecast=rng.uniform(0.05, 0.25),
                actual_rrp=rng.uniform(0.05, 0.30),
                forecast_run_at=_RUN_AT,
                hour_of_day=near.hour,
                day_of_week=near.weekday(),
                month=near.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )

    # In-band rows. The actual tracks the raw forecast with a modest demand
    # term, so the fitted stage 2 is well behaved inside its range and the
    # in-range case below is served by it.
    for i in range(300):
        interval = (_ANCHOR - timedelta(days=i % 60)).replace(
            hour=_HOUR, minute=(i % 2) * 30, second=(i % 50), microsecond=0
        )
        fc = rng.uniform(-0.24, 0.30)
        surplus = rng.uniform(800.0, 3000.0)
        solar = rng.uniform(300.0, 1500.0)
        demand50 = rng.uniform(*_TRAIN_DEMAND_MW)
        actual = 0.9 * fc + 0.00003 * (demand50 - 900.0) + rng.gauss(0, 0.01)
        obs.append(
            Observation(
                interval_time=interval.isoformat(),
                horizon_hours=_HORIZON,
                pd7day_forecast=fc,
                actual_rrp=actual,
                forecast_run_at=_RUN_AT,
                hour_of_day=_HOUR,
                day_of_week=interval.weekday(),
                month=interval.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )
        stpasa_by_key[f"{interval.isoformat()}|{_RUN_AT}"] = _stpasa(
            demand50, surplus=surplus, solar=solar
        )

    engine = CalibrationEngine()
    result = engine.fit(obs)
    result.ols_models = engine.fit_ols_stage2(obs, stpasa_by_key)
    model = result.ols_models.get(_KEY)
    assert model is not None and len(model.coef) >= 2, "stage 2 did not fit"
    assert len(model.feature_min) == len(STAGE2_FEATURE_NAMES)
    assert len(model.feature_max) == len(STAGE2_FEATURE_NAMES)
    return engine, result, _compute_run_features(obs)[_RUN_AT]


def _feature_vec(result, raw, rf, sf):
    return [
        float(_ce.stage2_iso_feature(result.get_bucket(_HORIZON, _HOUR).apply_all(raw), raw)),
        rf.run_max_h6_rrp,
        rf.run_mean_rrp,
        rf.run_spread,
        _HORIZON / 168.0,
        sf.log_surplus,
        sf.log_solar,
        sf.log_demand,
        sf.poe_spread_n,
    ]


def test_demand_far_below_training_range_falls_back_to_stage1():
    """The live case: 24 MW demand against a 300 to 1500 MW training range."""
    _engine, result, rf = _fitted_result()
    raw = 0.12
    model = result.ols_models[_KEY]

    # The fixture must exercise the hazard: log_demand is the feature outside
    # its range, and nothing else is.
    vec = _feature_vec(result, raw, rf, _stpasa(_LIVE_DEMAND_MW))
    outside = model.out_of_domain_features(vec)
    assert [STAGE2_FEATURE_NAMES[i] for i in outside] == ["log_demand"], outside
    assert not model.in_feature_domain(vec)

    out = result.apply(
        raw, horizon_hours=_HORIZON, hour_of_day=_HOUR,
        stpasa=_stpasa(_LIVE_DEMAND_MW), run_features=rf,
    )
    stage1 = result.get_bucket(_HORIZON, _HOUR).apply_all(raw)
    assert out["calibrated_source"] == "isotonic", out["calibrated_source"]
    assert out["calibrated"] == stage1["calibrated"]
    assert out["p10"] == stage1["p10"] and out["p90"] == stage1["p90"]
    assert "stpasa_run_at" not in out
    print(
        "  PASS: 24 MW demand outside training range publishes stage 1 "
        f"(calibrated={out['calibrated']:.4f}, blocked={model.predict(vec):+.4f})"
    )


def test_demand_inside_training_range_is_still_served_by_stage2():
    """The same interval at 500 MW must still get the stage-2 correction."""
    _engine, result, rf = _fitted_result()
    raw = 0.12
    model = result.ols_models[_KEY]
    vec = _feature_vec(result, raw, rf, _stpasa(_IN_RANGE_DEMAND_MW))
    assert model.in_feature_domain(vec), model.out_of_domain_features(vec)

    out = result.apply(
        raw, horizon_hours=_HORIZON, hour_of_day=_HOUR,
        stpasa=_stpasa(_IN_RANGE_DEMAND_MW), run_features=rf,
    )
    assert out["calibrated_source"] == "isotonic+stpasa", out["calibrated_source"]
    assert out["stpasa_run_at"] == _RUN_AT
    print(f"  PASS: 500 MW demand inside range served by stage 2 (calibrated={out['calibrated']:.4f})")


def test_negative_raw_inside_domain_at_low_demand_falls_back():
    """The published shape of #147: raw -0.10 inside the stage-1 domain.

    Before the gate this row reached stage 2; now it is answered by stage 1
    whatever the extrapolated prediction would have been.
    """
    _engine, result, rf = _fitted_result()
    raw = -0.10
    bucket = result.get_bucket(_HORIZON, _HOUR)
    assert not bucket.is_below_domain(raw), "fixture: -0.10 must be inside the domain"
    out = result.apply(
        raw, horizon_hours=_HORIZON, hour_of_day=_HOUR,
        stpasa=_stpasa(_LIVE_DEMAND_MW), run_features=rf,
    )
    assert out["calibrated_source"] == "isotonic"
    assert out["calibrated"] == bucket.apply_all(raw)["calibrated"]
    print(f"  PASS: raw -0.10 at 24 MW demand publishes stage 1 ({out['calibrated']:.4f})")


def test_gate_weighs_each_excursion_by_its_coefficient():
    """An excursion is refused when |coef| * excess exceeds the allowance.

    Issue #153: the range test alone refused hairline excursions on features
    the model barely weights. Now each feature's excursion costs |coef_i|
    times the distance outside its range, summed, against half the bucket's
    residual spread. So for every feature: an excursion costing twice the
    allowance is refused, one costing half of it is served, and the bounds
    themselves are served.
    """
    _engine, result, rf = _fitted_result()
    model = result.ols_models[_KEY]
    base = _feature_vec(result, 0.12, rf, _stpasa(_IN_RANGE_DEMAND_MW))
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
    print(f"  PASS: {checked} features gate by coefficient-weighted excursion (allowance {allowance:.4f})")


def test_hairline_excursion_on_a_light_feature_is_served():
    """The #153 shape: poe_spread_n a hundredth below a narrow range.

    On the live install this refused 19 SA1 rows whose extrapolation cost
    was 0.0003 $/kWh against a 0.037 allowance. It must serve now.
    """
    _engine, result, rf = _fitted_result()
    model = result.ols_models[_KEY]
    i = STAGE2_FEATURE_NAMES.index("poe_spread_n")
    sf = _stpasa(_IN_RANGE_DEMAND_MW)
    sf.poe_spread_n = model.feature_min[i] - 0.01
    vec = _feature_vec(result, 0.12, rf, sf)
    assert model.out_of_domain_features(vec) == [i]
    cost = model.extrapolation_cost(vec)
    assert cost is not None and cost < model.extrapolation_allowance, (cost, model.extrapolation_allowance)
    out = result.apply(
        0.12, horizon_hours=_HORIZON, hour_of_day=_HOUR, stpasa=sf, run_features=rf,
    )
    assert out["calibrated_source"] == "isotonic+stpasa", out["calibrated_source"]
    print(f"  PASS: poe_spread_n 0.01 below range served (cost {cost:.5f} < {model.extrapolation_allowance:.4f})")


def test_original_147_case_costs_more_than_the_band():
    """The 24 MW row is refused because its extrapolation cost exceeds the allowance."""
    _engine, result, rf = _fitted_result()
    model = result.ols_models[_KEY]
    vec = _feature_vec(result, 0.12, rf, _stpasa(_LIVE_DEMAND_MW))
    cost = model.extrapolation_cost(vec)
    assert cost is not None and cost > model.extrapolation_allowance, (cost, model.extrapolation_allowance)
    assert not model.serves(vec)
    print(f"  PASS: 24 MW demand costs {cost:.4f} against allowance {model.extrapolation_allowance:.4f}")


def test_no_residual_quantiles_falls_back_to_the_strict_range_test():
    """With ranges but no usable band there is nothing to size an allowance from."""
    m = OlsModel(
        bucket_key="x", coef=[0.0, 1.0, 0.001], feature_min=[0.0, 0.0], feature_max=[1.0, 1.0],
    )
    assert m.extrapolation_allowance is None
    assert m.serves([0.5, 0.5])
    assert not m.serves([0.5, 1.001]), "a light feature's hairline excursion is refused without a band"
    m.resid = ResidualQuantiles(bucket_key="x", q10=-0.02, q50=0.0, q90=0.02, n=OLS_MIN_OBS)
    assert m.extrapolation_allowance == 0.02
    assert m.serves([0.5, 1.001])       # cost 0.001 * 0.001
    assert m.serves([1.019, 0.5])       # cost 0.019
    assert not m.serves([1.021, 0.5])   # cost 0.021
    print("  PASS: strict range test without a band, allowance with one")


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
    print("  PASS: tolerance and length checks")


def test_legacy_model_without_ranges_serves_as_before():
    """A store written before #147 has no ranges; the gate must pass."""
    _engine, result, rf = _fitted_result()
    result.ols_models[_KEY] = OlsModel(
        bucket_key=_KEY,
        coef=[0.02, 1.10, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        n_train=120,
        r2=0.8,
    )
    assert result.ols_models[_KEY].serves([0.0] * 9)
    out = result.apply(
        0.12, horizon_hours=_HORIZON, hour_of_day=_HOUR,
        stpasa=_stpasa(_LIVE_DEMAND_MW), run_features=rf,
    )
    assert out["calibrated_source"] == "isotonic+stpasa"
    print("  PASS: legacy model without ranges is served")


def test_feature_ranges_survive_storage_round_trip():
    engine, result, rf = _fitted_result()
    stored = engine.to_storage(result)
    md = stored["ols_models"][_KEY]
    assert md["feature_min"] == result.ols_models[_KEY].feature_min
    assert md["feature_max"] == result.ols_models[_KEY].feature_max
    loaded = engine.from_storage(stored)
    m = loaded.ols_models[_KEY]
    assert m.feature_min == result.ols_models[_KEY].feature_min
    assert m.feature_max == result.ols_models[_KEY].feature_max
    assert m.coef == result.ols_models[_KEY].coef

    # A payload from before #147 loads with empty ranges.
    del md["feature_min"], md["feature_max"]
    legacy = engine.from_storage(stored).ols_models[_KEY]
    assert legacy.feature_min == [] and legacy.feature_max == []
    assert legacy.serves([0.0] * 9)
    print("  PASS: ranges round-trip through storage; legacy payload loads empty")


def test_ranges_come_from_rows_that_survived_the_leverage_screen():
    """The training range must be that of the rows that fitted the coefficients."""
    engine, result, rf = _fitted_result()
    m = result.ols_models[_KEY]
    lo, hi = m.feature_min[7], m.feature_max[7]  # log_demand
    assert math.log(_TRAIN_DEMAND_MW[0]) - 1e-6 <= lo < hi <= math.log(_TRAIN_DEMAND_MW[1]) + 1e-6
    assert lo > math.log(_LIVE_DEMAND_MW) + 1.0
    print(f"  PASS: log_demand training range {lo:.3f} to {hi:.3f}")


def test_summary_publishes_stage2_diagnostics():
    _engine, result, rf = _fitted_result()
    s = result.summary()
    assert "stage2" in s
    entry = s["stage2"][_KEY]
    m = result.ols_models[_KEY]
    assert entry["n_train"] == m.n_train
    assert entry["r2"] == m.r2
    assert entry["coef"] == m.coef and len(entry["coef"]) == 1 + len(STAGE2_FEATURE_NAMES)
    assert entry["resid_q10"] == m.resid.q10
    assert entry["resid_q90"] == m.resid.q90
    assert set(entry["feature_min"]) == set(STAGE2_FEATURE_NAMES)
    assert entry["feature_min"]["log_demand"] == m.feature_min[7]
    assert entry["feature_max"]["log_demand"] == m.feature_max[7]
    # Buckets without a fitted model are not listed.
    for key, model in result.ols_models.items():
        if len(model.coef) < 2:
            assert key not in s["stage2"]
    print(f"  PASS: summary.stage2[{_KEY}] carries n_train, r2, residuals and ranges")


def test_demand_below_floor_yields_no_features():
    """A degenerate transform yields None, on the shared helper and from_interval."""
    for demand50 in (-27.0, -13.0, 0.0, 0.5, STPASA_DEMAND_FLOOR_MW - 1e-9):
        assert stpasa_feature_values(1200.0, 800.0, demand50, demand50 + 5.0, demand50 - 5.0) is None, demand50
        interval = StpasaInterval(
            interval_datetime="2026-09-12T13:00:00+10:00",
            run_datetime="2026-09-08T07:30:00+10:00",
            demand10=demand50 + 5.0,
            demand50=demand50,
            demand90=demand50 - 5.0,
            surpluscapacity=1200.0,
            ss_solar_uigf=800.0,
            ss_wind_uigf=400.0,
        )
        assert StpasaFeatures.from_interval(interval) is None, demand50

    # At the floor and above, features are the plain transform: no clamping
    # of the divisor, so poe_spread_n is the ratio it claims to be.
    values = stpasa_feature_values(1200.0, 800.0, 24.0, 29.0, 19.0)
    assert values is not None
    log_surplus, log_solar, log_demand, poe_spread_n = values
    assert abs(log_demand - math.log(24.0)) < 1e-12
    assert abs(poe_spread_n - 10.0 / 24.0) < 1e-12
    assert abs(log_surplus - math.log1p(1200.0)) < 1e-12
    assert abs(log_solar - math.log1p(800.0)) < 1e-12
    assert stpasa_feature_values(None, 800.0, 24.0, 29.0, 19.0) is None
    print("  PASS: demand below the floor yields no features; above it, the plain transform")


if __name__ == "__main__":
    test_demand_far_below_training_range_falls_back_to_stage1()
    test_demand_inside_training_range_is_still_served_by_stage2()
    test_negative_raw_inside_domain_at_low_demand_falls_back()
    test_gate_weighs_each_excursion_by_its_coefficient()
    test_hairline_excursion_on_a_light_feature_is_served()
    test_original_147_case_costs_more_than_the_band()
    test_no_residual_quantiles_falls_back_to_the_strict_range_test()
    test_in_feature_domain_tolerates_float_jitter_and_rejects_bad_ranges()
    test_legacy_model_without_ranges_serves_as_before()
    test_feature_ranges_survive_storage_round_trip()
    test_ranges_come_from_rows_that_survived_the_leverage_screen()
    test_summary_publishes_stage2_diagnostics()
    test_demand_below_floor_yields_no_features()
    print("\nAll stage-2 feature-domain tests passed.")
