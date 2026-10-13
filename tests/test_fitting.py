"""Contract tests for fitting.py, the calibration fit moved out of the engine (spec 006).

The fit fixture (tests/test_fit_io.py) pins every output. These pin the shape
the spec gives the move:

  * invariant 3: a stage 2 fit runs exactly one stage 1 fit, as before, and
    Stage2Fitter itself never fits stage 1;
  * invariant 4: Stage2Fitter is a function of its arguments, and a
    hand-built stage 1 result decides the below-domain exclusion;
  * invariant 6: fitting.py imports only the engine, const and serving from
    the package, and the engine does not import fitting at module level.

Run with:  python -m pytest tests/test_fitting.py -v
"""
from __future__ import annotations

import ast
import dataclasses
import json
import math
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pytest

from support import PKG_DIR, load_chain

_const, _nem_time, _serving, ce, fitting = load_chain(
    "const", "nem_time", "serving", "calibration_engine", "fitting",
)

NEM = timezone(timedelta(hours=10))
NOW = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)


def _stage2_inputs(rows: int = 80) -> tuple[list, dict]:
    """``rows`` peak-hour rows at a 30 h horizon with STPASA features, each run
    with a 2 h row so run features exist."""
    obs: list = []
    stpasa: dict = {}
    base = NOW.astimezone(NEM).replace(hour=18, minute=0) - timedelta(days=2)
    for k in range(rows):
        t = base - timedelta(days=k % 40, minutes=5 * (k // 40))
        x = 0.05 + 0.002 * k
        y = 0.8 * x + 0.01 * ((k * 7) % 5 - 2)
        run = t - timedelta(hours=30)
        near = t - timedelta(hours=28)
        for when, horizon in ((t, 30.0), (near, 2.0)):
            obs.append(ce.Observation(
                interval_time=when.isoformat(), horizon_hours=horizon, pd7day_forecast=x,
                actual_rrp=y, forecast_run_at=run.isoformat(), hour_of_day=when.hour,
                day_of_week=when.weekday(), month=when.month, gas_forecast_tj=None,
                qni_mwflow=None, qni_violation_degree=None, is_intervention=False,
            ))
        stpasa[f"{t.isoformat()}|{run.isoformat()}"] = ce.StpasaFeatures(
            log_surplus=7.0 + 0.01 * (k % 13), log_solar=0.5 + 0.02 * (k % 7),
            log_demand=8.5 + 0.01 * (k % 11), poe_spread_n=0.1 + 0.001 * (k % 17),
            stpasa_run_at=run.isoformat(),
        )
    return obs, stpasa


def test_a_full_bucket_keeps_its_most_recent_rows_whatever_the_input_order():
    """Until #209 the first MAX_OBS rows in input order were kept, and the
    input is mostly oldest first, so a full bucket froze on its oldest rows."""
    night = NOW.astimezone(NEM).replace(hour=2, minute=0)
    days = [9, 1, 7, 3, 5, 2]                 # out of order on purpose
    windowed = []
    for d in days:
        t = night - timedelta(days=d)
        windowed.append((ce.Observation(
            interval_time=t.isoformat(), horizon_hours=130.0, pd7day_forecast=0.1,
            actual_rrp=d / 100, forecast_run_at=(t - timedelta(hours=130)).isoformat(),
            hour_of_day=t.hour, day_of_week=t.weekday(), month=t.month, gas_forecast_tj=None,
            qni_mwflow=None, qni_violation_degree=None, is_intervention=False,
        ), t))

    with patch.object(fitting, "MAX_OBS", 3):
        buckets, weights = fitting.partition_stage1(windowed, "QLD1", NOW.astimezone(NEM))

    (key,) = [k for k, v in buckets.items() if v]
    assert [round(y * 100) for _, y in buckets[key]] == [1, 3, 2]   # newest three, input order
    assert min(weights[key]) > max(
        math.exp(-ce.DECAY_LAMBDA * d) for d in (5, 7, 9)
    )


def test_stage1_classifies_each_interval_once_and_keys_it_as_before():
    """The solar label depends only on the interval and the region, so it is
    computed once per interval, not once per observation (#212)."""
    start = NOW.astimezone(NEM).replace(hour=0, minute=0) - timedelta(days=1)
    intervals = [start + timedelta(minutes=30 * i) for i in range(48)]
    windowed = [
        (ce.Observation(
            interval_time=t.isoformat(), horizon_hours=h, pd7day_forecast=0.1, actual_rrp=0.1,
            forecast_run_at=(t - timedelta(hours=h)).isoformat(), hour_of_day=t.hour,
            day_of_week=t.weekday(), month=t.month, gas_forecast_tj=None, qni_mwflow=None,
            qni_violation_degree=None, is_intervention=False,
        ), t)
        for t in intervals
        for h in (2.0, 30.0, 100.0)   # each interval seen by three runs
    ]

    real = fitting._tod_label_solar
    with patch.object(fitting, "_tod_label_solar", side_effect=real) as label:
        buckets, _ = fitting.partition_stage1(windowed, "QLD1", NOW.astimezone(NEM))

    assert label.call_count == len(intervals)
    expected: dict[str, int] = {}
    for o, obs_dt in windowed:
        key = ce._bucket_key_solar(o.horizon_hours, obs_dt.astimezone(NEM), "QLD1")
        expected[key] = expected.get(key, 0) + 1
    assert {k: len(v) for k, v in buckets.items() if v} == expected


def _storage(models: dict) -> str:
    result = ce.CalibrationResult(fitted_at="", total_observations=0, observations_in_window=0, models={})
    result.ols_models = models
    return json.dumps(ce.CalibrationEngine().to_storage(result))


# ── Invariant 3: one stage 1 fit inside a stage 2 fit ─────────────────────────

def test_a_stage2_fit_runs_exactly_one_stage1_fit_on_the_wall_clock():
    obs, stpasa = _stage2_inputs()
    real = fitting.Stage1Fitter.fit
    with patch.object(fitting.Stage1Fitter, "fit", autospec=True, side_effect=real) as stage1:
        ce.CalibrationEngine().fit_ols_stage2(obs, stpasa, "QLD1")
    assert stage1.call_count == 1
    (_self, observations, region, now), _kwargs = stage1.call_args
    assert (observations, region, now) == (obs, "QLD1", None)   # only when not given one


def test_a_stage2_fit_given_the_published_stage1_does_not_fit_it_again():
    """async_refit passes the result it just published (#210, #213)."""
    obs, stpasa = _stage2_inputs()
    stage1 = fitting.Stage1Fitter().fit(obs, "QLD1", NOW)
    with patch.object(fitting.Stage1Fitter, "fit", side_effect=AssertionError("stage 1 refit")):
        models = ce.CalibrationEngine().fit_ols_stage2(obs, stpasa, "QLD1", stage1)
    assert len(models["h24_48__peak"].coef) == 10


def _shifted(obs: list, stpasa: dict, days: int) -> tuple[list, dict]:
    def back(iso: str) -> str:
        return (datetime.fromisoformat(iso) - timedelta(days=days)).isoformat()

    moved = [o._replace(interval_time=back(o.interval_time), forecast_run_at=back(o.forecast_run_at)) for o in obs]
    feats = {}
    for key, f in stpasa.items():
        interval, run = key.split("|")
        feats[f"{back(interval)}|{back(run)}"] = dataclasses.replace(f, stpasa_run_at=back(f.stpasa_run_at))
    return moved, feats


def test_stage2_trains_only_inside_the_stage1_window():
    """Rows older than OBSERVATION_WINDOW_DAYS before the stage 1 fit were
    stage 2 training rows until #210, though stage 1 no longer saw them."""
    obs, stpasa = _stage2_inputs()
    old_obs, old_stpasa = _shifted(obs, stpasa, ce.OBSERVATION_WINDOW_DAYS + 30)
    both, both_stpasa = obs + old_obs, {**stpasa, **old_stpasa}
    engine = ce.CalibrationEngine()

    stage1 = engine.fit(both, "QLD1", now=NOW)
    with_old = engine.fit_ols_stage2(both, both_stpasa, "QLD1", stage1)
    without = engine.fit_ols_stage2(obs, stpasa, "QLD1", engine.fit(obs, "QLD1", now=NOW))

    assert _storage(with_old) == _storage(without)
    # Fitted as of the old rows' own time instead, they are the ones trained on.
    then = engine.fit(both, "QLD1", now=NOW - timedelta(days=ce.OBSERVATION_WINDOW_DAYS + 30))
    assert _storage(engine.fit_ols_stage2(both, both_stpasa, "QLD1", then)) != _storage(without)


def test_the_stage2_fitter_never_fits_stage1():
    obs, stpasa = _stage2_inputs()
    stage1 = fitting.Stage1Fitter().fit(obs, "QLD1", NOW)
    run_features = ce._compute_run_features(obs)
    with patch.object(fitting.Stage1Fitter, "fit", side_effect=AssertionError("stage 1 refit")):
        models = fitting.Stage2Fitter().fit(obs, stpasa, stage1, run_features)
    assert len(models["h24_48__peak"].coef) == 10


def test_the_engine_fit_is_the_stage1_fitter():
    obs, _ = _stage2_inputs()
    engine = ce.CalibrationEngine()
    assert json.dumps(engine.to_storage(engine.fit(obs, "QLD1", now=NOW))) == json.dumps(
        engine.to_storage(fitting.Stage1Fitter().fit(obs, "QLD1", NOW))
    )


# ── Invariant 4: the stage 2 fitter is a function of its arguments ────────────

def test_the_stage2_fitter_gives_equal_models_for_equal_arguments():
    obs, stpasa = _stage2_inputs()
    stage1 = fitting.Stage1Fitter().fit(obs, "QLD1", NOW)
    run_features = ce._compute_run_features(obs)
    first = fitting.Stage2Fitter().fit(obs, stpasa, stage1, run_features)
    second = fitting.Stage2Fitter().fit(obs, stpasa, stage1, run_features)
    assert _storage(first) == _storage(second)


def test_a_hand_built_stage1_result_decides_the_below_domain_exclusion():
    obs, stpasa = _stage2_inputs()
    run_features = ce._compute_run_features(obs)
    stage1 = fitting.Stage1Fitter().fit(obs, "QLD1", NOW)
    # Replace the peak bucket's isotonic model with one fitted only above
    # x = 0.15, so every row below it is outside the domain.
    edge = 0.15
    iso = ce.IsotonicRegression().fit([edge, 0.3], [0.1, 0.2])
    stage1.models["h24_48__peak"].iso_model = iso
    rows = fitting.stage2_rows(obs, stpasa, run_features, stage1)
    below = sum(1 for o in obs if o.horizon_hours == 30.0 and o.pd7day_forecast < edge)
    assert below > 0
    assert rows.excluded == {"h24_48__peak": below}
    assert len(rows.rows["h24_48__peak"]) == 80 - below


def test_a_screened_count_is_reported_even_when_the_bucket_falls_back():
    obs, stpasa = _stage2_inputs(rows=ce.OLS_MIN_OBS + 1)
    keys = [k for k in stpasa]
    for i, field in enumerate(("log_surplus", "log_demand", "poe_spread_n")):
        sf = stpasa[keys[i]]
        stpasa[keys[i]] = ce.StpasaFeatures(**{**sf.__dict__, field: 60.0 - 20.0 * i})
    stage1 = fitting.Stage1Fitter().fit(obs, "QLD1", NOW)
    rows = fitting.stage2_rows(obs, stpasa, ce._compute_run_features(obs), stage1)
    model, screened = fitting.fit_stage2_bucket("h24_48__peak", rows.rows["h24_48__peak"])
    assert screened >= 2 and model.coef == []


# ── Invariant 6: layering ─────────────────────────────────────────────────────

def _relative_imports(name: str, *, module_level_only: bool) -> set[str]:
    with open(os.path.join(PKG_DIR, f"{name}.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found: set[str] = set()
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if module_level_only and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            found.add(node.module or "")
        stack.extend(ast.iter_child_nodes(node))
    return found


def test_fitting_imports_only_the_engine_const_and_serving():
    assert _relative_imports("fitting", module_level_only=False) == {"calibration_engine", "const", "serving"}
    with open(os.path.join(PKG_DIR, "fitting.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    absolute = {
        (n.module or "").split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) and not getattr(n, "level", 0)
        for a in (n.names if isinstance(n, ast.Import) else [None])
    }
    assert "homeassistant" not in absolute


def test_the_engine_reaches_fitting_only_inside_its_two_fit_methods():
    assert "fitting" not in _relative_imports("calibration_engine", module_level_only=True)
    assert "fitting" in _relative_imports("calibration_engine", module_level_only=False)


@pytest.mark.parametrize("logger_name", [fitting._LOGGER.name])
def test_fit_log_lines_keep_the_engine_logger(logger_name):
    assert logger_name == ce._LOGGER.name == "custom_components.nem_pd7day.calibration_engine"


# ── Isotonic end steps in the summary (#247) ──────────────────────────────────
# A step's level is served flat across its whole x range, and the top step on
# to x_max and beyond. On 13 Oct 2026 QLD1's h24_48__solar top step started at
# a raw 8.9 c and served 17.85 c to every higher forecast, against a realised
# 1.5 to 4.9 c for those slots; the summary had the step count and x range but
# not the steps. Each bucket now reports iso_top and iso_bottom. No fitted or
# served value changes.

def _solar_bucket(seed: int = 7):
    """A day-ahead solar bucket shaped like QLD1 h24_48__solar on 13 Oct.

    ~3,000 rows over 60 days of decay weights, raw forecasts mostly below
    5 c and outcomes a little above them, plus 12 rows from the last day with
    raw forecasts of 9 to 30 c and outcomes of 28 to 45 c (13 Oct 10:00-13:00).
    """
    rng = np.random.default_rng(seed)
    n = 3000
    days = rng.uniform(1, 60, n)
    x = rng.normal(-0.02, 0.035, n)
    y = 0.02 + 0.5 * np.clip(x, -0.05, None) + rng.normal(0, 0.02, n)
    w = np.exp(-0.033 * days)
    hx = rng.uniform(0.09, 0.30, 12)
    hy = rng.uniform(0.28, 0.45, 12)
    hw = np.full(12, np.exp(-0.033 * 0.2))
    return np.concatenate([x, hx]), np.concatenate([y, hy]), np.concatenate([w, hw])


def _summary_bucket(x, y, w):
    pairs = list(zip(x.tolist(), y.tolist()))
    model = fitting.fit_stage1_bucket("h24_48__solar", pairs, w.tolist())
    result = ce.CalibrationResult(
        fitted_at="2026-10-13T13:00:00+10:00", total_observations=len(pairs),
        models={"h24_48__solar": model},
    )
    return model, result.summary()["buckets"]["h24_48__solar"]


def test_a_thin_top_step_shows_in_the_summary():
    """The #247 shape: the top step is a few fresh rows, served at their mean."""
    x, y, w = _solar_bucket()
    model, bucket = _summary_bucket(x, y, w)
    top = bucket["iso_top"]

    assert top["n"] == 7 and top["n_eff"] == pytest.approx(7.0)
    assert top["x_end"] == pytest.approx(float(x.max()), abs=1e-4)
    served = model.iso_model.predict(np.array([top["x_start"], top["x_end"], 1.0]))
    assert served == pytest.approx([top["level"]] * 3, abs=1e-4)   # flat to x_max, and beyond
    assert top["level"] > 0.3


def test_the_end_steps_describe_the_fitted_map():
    x, y, w = _solar_bucket(seed=11)
    model, bucket = _summary_bucket(x, y, w)
    iso = model.iso_model
    xt, yt = iso._x_thresholds, iso._y_thresholds
    order = np.argsort(x, kind="stable")
    ws = w[order]
    for name, rows in (("iso_top", yt == yt[-1]), ("iso_bottom", yt == yt[0])):
        step = bucket[name]
        assert step["n"] == int(rows.sum()), name
        assert step["x_start"] == pytest.approx(float(xt[rows].min()), abs=1e-4)
        assert step["x_end"] == pytest.approx(float(xt[rows].max()), abs=1e-4)
        assert step["level"] == pytest.approx(float(yt[rows][0]), abs=1e-4)
        expected = ws[rows].sum() ** 2 / (ws[rows] ** 2).sum()
        assert step["n_eff"] == pytest.approx(expected, abs=0.05)


def test_no_fitted_value_changes():
    x, y, w = _solar_bucket(seed=3)
    order = np.argsort(x, kind="stable")
    iso = ce.IsotonicRegression().fit(x, y, sample_weight=w)
    assert np.array_equal(iso._y_thresholds, ce._pav(y[order], w[order]))


def test_equal_weights_give_an_effective_size_equal_to_the_count():
    iso = ce.IsotonicRegression().fit([0.0, 0.1, 0.2, 0.3], [0.05, 0.04, 0.2, 0.3])
    assert (iso.bottom_step.n, iso.bottom_step.n_eff) == (2, pytest.approx(2.0))
    assert iso.bottom_step.level == pytest.approx(0.045)
    assert (iso.top_step.x_start, iso.top_step.n) == (0.3, 1)


def test_a_bucket_without_an_isotonic_model_reports_none():
    model = fitting.fit_stage1_bucket("h96plus__peak", [(0.1, 0.1)] * (_const.MIN_OBS - 1), [1.0] * (_const.MIN_OBS - 1))
    result = ce.CalibrationResult(fitted_at="x", total_observations=1, models={"h96plus__peak": model})
    bucket = result.summary()["buckets"]["h96plus__peak"]
    assert bucket["iso_top"] is None and bucket["iso_bottom"] is None
