"""Contract of the serving gate pipeline (spec 005, invariants 3, 4, 5 and 6).

* Invariant 3: ``SERVING_GATES`` holds exactly six gates, in the order
  below, each with the issues it guards. Reordering, adding or dropping a
  gate fails here, so it is a visible, reviewed change.
* Invariant 4: an interval any gate refuses is served the stage 1 dict
  object itself; stage 2 returns a new dict and leaves stage 1 untouched.
* Invariant 5: serving.py imports only the standard library, numpy and
  const; in particular nothing from Home Assistant, the store, the
  coordinator, the sensors or the engine (which imports it).
* Invariant 6: ``ols.serves`` and ``ols.predict`` run at most once per
  ``apply``, and neither runs after an earlier refusal.

The outputs themselves are pinned by tests/test_serving_io.py.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import support

_const, _nem_time, serving, engine = support.load_chain(
    "const", "nem_time", "serving", "calibration_engine"
)

HORIZON = 30.0      # inside the OLS band
HOUR = 12
KEY = engine._bucket_key(HORIZON, HOUR)
RANGE = 2.0         # every feature's training range is [-2, 2]


def test_the_gates_their_order_and_their_issues():
    assert [(type(g).__name__, g.name, g.issues) for g in serving.SERVING_GATES] == [
        ("BelowDomainGate", "below_domain", ("#73", "#114", "#117")),
        ("Stage2InputsGate", "stage2_inputs", ()),
        ("Stage2ModelGate", "stage2_model", ()),
        ("FeatureDomainGate", "feature_domain", ("#85", "#147", "#153")),
        ("SignAgreementGate", "sign_agreement", ("#73", "#114")),
        ("MarketFloorGate", "market_floor", ("#114",)),
    ]


def test_the_engine_serves_through_the_pipeline_it_imports():
    assert engine.SERVING_GATES is serving.SERVING_GATES
    assert engine.stage2_result is serving.stage2_result


class CountingOls(engine.OlsModel):
    """An OlsModel that records every serves, predict and residual_band call."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def serves(self, features):
        self.calls.append("serves")
        return super().serves(features)

    def predict(self, features):
        self.calls.append("predict")
        return super().predict(features)

    def residual_band(self, prediction):
        self.calls.append("residual_band")
        return super().residual_band(prediction)


def _result(intercept: float, *, with_ols: bool = True):
    """One bucket at KEY; stage 2 predicts ``intercept + iso + log_demand / 8``."""
    bucket = engine.BucketModel(bucket_key=KEY)
    bucket.iso_model = engine.IsotonicRegression().fit(
        [-0.5, 0.0, 0.25, 0.5, 1.0], [-0.25, 0.0, 0.125, 0.25, 0.5]
    )
    for q, b in ((bucket.q10, -0.0625), (bucket.q50, 0.0), (bucket.q90, 0.0625)):
        q.a, q.b, q.n = 1.0, b, 40
    stage1_seen: list[dict] = []
    original = bucket.apply_all

    def apply_all(x):
        out = original(x)
        stage1_seen.append(out)
        return out

    bucket.apply_all = apply_all
    result = engine.CalibrationResult(fitted_at="2026-09-27T08:00:00+10:00", total_observations=100)
    result.models[KEY] = bucket
    ols = CountingOls(
        bucket_key=KEY,
        coef=[intercept, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.125, 0.0],
        n_train=64,
        resid=engine.ResidualQuantiles(bucket_key=KEY, q10=-0.0625, q50=0.0, q90=0.0625, n=64),
        feature_min=[-RANGE] * 9,
        feature_max=[RANGE] * 9,
    )
    if with_ols:
        result.ols_models[KEY] = ols
    return result, ols, stage1_seen, original


def _stpasa(log_demand: float = 0.0):
    return engine.StpasaFeatures(0.5, 0.5, log_demand, 0.0, "2026-09-27T07:25:07+10:00")


RUN = engine.RunFeatures(0.125, 0.125, 0.125)

# name: (forecast, intercept, stpasa, with an OLS model, calls expected on it)
CASES = {
    "below_domain": (-1.0, 0.0625, _stpasa(), True, []),
    "stage2_inputs": (0.25, 0.0625, None, True, []),
    "stage2_model": (0.25, 0.0625, _stpasa(), False, []),
    "feature_domain": (0.25, 0.0625, _stpasa(log_demand=50.0), True, ["serves"]),
    "sign_agreement": (0.25, -1.0, _stpasa(), True, ["serves", "predict"]),
    "market_floor": (-0.25, -1.0, _stpasa(), True, ["serves", "predict"]),
    "stage2": (0.25, 0.0625, _stpasa(), True, ["serves", "predict", "residual_band"]),
}


def _serve(name: str):
    forecast, intercept, stpasa, with_ols, _calls = CASES[name]
    result, ols, seen, original = _result(intercept, with_ols=with_ols)
    out = result.apply(forecast, HORIZON, HOUR, stpasa=stpasa, run_features=RUN)
    assert len(seen) == 1
    return out, seen[0], ols, original(forecast)


@pytest.mark.parametrize("name", [g.name for g in serving.SERVING_GATES])
def test_a_refusal_serves_the_stage1_dict_itself(name):
    out, stage1, _ols, fresh = _serve(name)
    assert out is stage1
    assert out == fresh
    assert out["calibrated_source"] != "isotonic+stpasa"


def test_each_case_is_refused_by_the_gate_it_names():
    """The cases above reach the gate they are named after, and it refuses."""
    for name, (forecast, intercept, stpasa, with_ols, _calls) in CASES.items():
        result, _ols, seen, _original = _result(intercept, with_ols=with_ols)
        bucket = result.models[KEY]
        stage1 = bucket.apply_all(forecast)
        ctx = serving.Stage2Context(
            forecast, HORIZON, HOUR, stage1, bucket, stpasa, RUN, KEY
        )
        refused_by = None
        for gate in serving.SERVING_GATES:
            admitted = gate(ctx, result.ols_models)
            if admitted is None:
                refused_by = gate.name
                break
            assert admitted is ctx
        assert refused_by == (None if name == "stage2" else name)


def test_stage2_returns_a_new_dict_and_leaves_stage1_untouched():
    out, stage1, _ols, fresh = _serve("stage2")
    assert out is not stage1
    assert stage1 == fresh
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out[engine.BAND_SOURCE_KEY] == engine.BAND_SOURCE_STAGE2
    assert list(out)[: len(stage1)] == list(stage1)


@pytest.mark.parametrize("name", list(CASES))
def test_serves_and_predict_run_at_most_once_and_never_after_a_refusal(name):
    _out, _stage1, ols, _fresh = _serve(name)
    assert ols.calls == CASES[name][4]


def test_serving_imports_only_the_standard_library_numpy_and_const():
    source = pathlib.Path(support.PKG_DIR, "serving.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    assert imported == {"__future__", "dataclasses", "typing", "numpy", ".const"}
