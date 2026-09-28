#!/usr/bin/env python3
"""Record every output of the serving path over a fixed grid of inputs.

Spec 005, invariant 1: every calibrated price the integration publishes passes
through ``CalibrationResult.apply``, so the split of that method into a gate
pipeline must leave every output identical to the bit, in every branch,
including the dict key order and whether the returned object is the stage 1
dict itself. The golden master reaches only some branches; this fixture
reaches all of them.

The script builds these ``CalibrationResult``s from hand-built models (see
``RESULTS``), with every coefficient, threshold and feature an exact binary
fraction so the arithmetic does not drift between Python versions:

* ``full``: every routed bucket with an isotonic model and quantile lines,
  and a stage 2 model with residual quantiles and feature ranges;
* ``no_resid``: the same models, stage 2 without residual quantiles, so the
  fallback band and the strict range test are reached;
* ``no_iso``: buckets with quantile lines but no isotonic model, and buckets
  missing from the result altogether;
* ``partial``: stage 2 models for some buckets only, one with a single
  coefficient, one legacy model with no ranges, one with ranges of the wrong
  length and two whose residual quantiles are unusable;
* no calibration at all, for ``CalibrationStore.apply_to_price``'s passthrough.

For each result the grid crosses forecasts (below every domain to above
``SPIKE_THRESHOLD``, with negatives, zero and the market floor), horizons
either side of both OLS bounds, the STPASA and run feature scenarios in
``SCENARIOS`` (absent, inside, at the edge of and far outside the training
ranges) and one hour per time-of-day label, rotating so every hour is used;
the gas and network flags rotate through every combination.

Per input it records the inputs, ``json.dumps`` of the ``apply`` output and of
the ``apply_to_price`` output (insertion order kept, no sort_keys), whether
each returned object ``is`` the dict ``BucketModel.apply_all`` returned (seen
by wrapping ``apply_all`` here, never in the code under test), and a branch
label: the stage 1 path, the serving outcome (one of six gate refusals or one
of two stage 2 bands) and the spike annotation. The serving outcome is found
by replaying the gate conditions here, and the replay is checked against the
output (source label and identity), so a label cannot drift from what the
code did.

``tests/test_serving_io.py`` runs the same ``record()`` and compares it with
``tests/fixtures/serving_io.json.gz``, which this script wrote on the base
commit of spec 005 before any code moved.

Usage:

    python scripts/record_serving_io.py          # write the fixture
    python scripts/record_serving_io.py --check  # compare as the test does, exit 1 on a difference
"""
from __future__ import annotations

import os

# Same floating point environment as tests/conftest.py, before numpy loads.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_CORETYPE", "Haswell")
os.environ.setdefault("NPY_DISABLE_CPU_FEATURES", "X86_V4")

import collections  # noqa: E402
import contextlib  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from typing import Any, Callable, Iterator  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "serving_io.json.gz"
PKG = "custom_components.nem_pd7day"

if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import support  # noqa: E402

# Field order of one recorded input.
FIELDS = (
    "result", "forecast", "horizon_hours", "hour_of_day", "scenario",
    "gas_forecast_tj", "network_tight",
    "stage1", "serving", "spike",
    "apply_json", "apply_is_stage1", "store_json", "store_is_stage1",
)

STAGE1_LABELS = {
    "passthrough": "stage1_passthrough",
    "isotonic_below_domain": "stage1_below_domain",
    "isotonic": "stage1_isotonic",
}
SERVING_LABELS = (
    "refuse_below_domain", "refuse_inputs", "refuse_model",
    "refuse_feature_domain", "refuse_sign", "refuse_floor",
    "stage2_residual", "stage2_fallback", "store_passthrough",
)
SPIKE_LABELS = ("spike_true", "spike_false", "spike_none", "no_annotation")


def _f(n: int, d: int = 64) -> float:
    """``n / d`` for a power-of-two ``d``: an exact binary fraction."""
    return n / d


# ── Isolation ────────────────────────────────────────────────────────────────

_STUB_ROOTS = ("homeassistant", "aiohttp", "voluptuous")


def _owned(key: str) -> bool:
    return key.startswith(PKG + ".") or key.split(".")[0] in _STUB_ROOTS


@contextlib.contextmanager
def _isolated() -> Iterator[types.SimpleNamespace]:
    """Fresh stubs and fresh integration modules; sys.modules restored on exit."""
    support._ensure_packages()
    package = sys.modules[PKG]
    saved_modules = {key: sys.modules.pop(key) for key in list(sys.modules) if _owned(key)}
    saved_attrs = dict(vars(package))
    try:
        support.install_ha_stubs()
        mods = types.SimpleNamespace()
        for name in ("const", "nem_time", "calibration_engine", "calibration_store"):
            setattr(mods, name, support.load(name))
        yield mods
    finally:
        for key in [k for k in sys.modules if _owned(k)]:
            del sys.modules[key]
        sys.modules.update(saved_modules)
        for name in [n for n in vars(package) if n not in saved_attrs]:
            delattr(package, name)
        for name, value in saved_attrs.items():
            setattr(package, name, value)


# ── Inputs ───────────────────────────────────────────────────────────────────

FORECASTS = (
    -2.0, -1.0, -0.75, -0.5, -0.3, -0.25, -0.125, -0.1, -0.0625, -0.03125,
    -0.01, 0.0, 0.01, 0.03125, 0.0625, 0.1, 0.125, 0.1875, 0.25, 0.3, 0.5,
    1.0, 2.0, 3.0, 3.5, 12.0,
)
# Either side of OLS_MIN_HORIZON_H (22) and OLS_MAX_HORIZON_H (120), and one
# in every horizon label. Inside the band, horizon / 168 is an exact binary
# fraction except at the two bounds themselves.
HORIZONS = (
    0.5, 5.75, 11.5, 21.0, 21.875, 22.0, 26.25, 42.0, 47.25, 63.0, 84.0,
    105.0, 115.5, 120.0, 120.125, 168.0,
)
SHOULDER_HOURS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23)
SOLAR_HOURS = (10, 11, 12, 13, 14, 15)
PEAK_HOURS = (16, 17, 18, 19, 20)
GAS = (None, 150.0, 150.5)          # SPIKE_GAS_THRESHOLD_TJ is 150.0, strict
NETWORK = (None, True, False)

# Training ranges of every stage 2 model with ranges, in feature order.
FEATURE_MIN = [-1.0, 0.0, 0.0, 0.0, _f(8), 6.0, 0.0, 8.0, _f(-16)]
FEATURE_MAX = [1.0, 1.0, _f(32), _f(32), _f(48), 8.0, 4.0, 9.0, 0.0]

# name: (run features, stpasa features), each a tuple or None.
#   run:    run_max_h6_rrp, run_mean_rrp, run_spread
#   stpasa: log_surplus, log_solar, log_demand, poe_spread_n
SCENARIOS: dict[str, tuple[tuple[float, ...] | None, tuple[float, ...] | None]] = {
    "absent": (None, None),
    "no_stpasa": ((_f(16), _f(8), 0.0), None),
    "no_run": (None, (7.0, 2.0, 8.5, _f(-8))),
    "inside": ((_f(16), _f(8), 0.0), (7.0, 2.0, 8.5, _f(-8))),
    # log_solar a sixteenth above its maximum: an excursion worth 1/64 $/kWh,
    # inside the residual allowance of 1/32 (issue #153).
    "edge": ((_f(16), _f(8), 0.0), (7.0, 4.0 + _f(4), 8.5, _f(-8))),
    # log_demand far below its minimum, the issue #147 case.
    "far": ((_f(16), _f(8), 0.0), (7.0, 2.0, 3.25, _f(-8))),
    # run_spread at its maximum, where its coefficient pulls the prediction
    # down by two dollars: the sign and market floor gates.
    "deep": ((_f(16), _f(8), _f(32)), (7.0, 2.0, 8.5, _f(-8))),
}
STPASA_RUN_AT = "2026-09-26T07:25:07+10:00"


# ── Models ───────────────────────────────────────────────────────────────────

def _routed_keys(e: Any) -> list[str]:
    """Every bucket key CalibrationResult.apply can route to, in a fixed order."""
    keys: list[str] = []
    for h in HORIZONS:
        for hour in (0, 12, 18):
            key = e._bucket_key(h, hour)
            if key not in keys:
                keys.append(key)
    return keys


def _iso(e: Any, k: int) -> Any:
    lo = (-0.5, -0.25, -0.125, -0.0625, 0.0, 0.03125)[k % 6]
    xs = [lo] + [v for v in (-0.25, -0.125, -0.0625, 0.0, 0.0625, 0.125, 0.25, 0.5, 1.0, 2.0) if v > lo]
    ys = [round(v * 48) / 64 + (k % 4 - 1) / 64 for v in xs]
    if k == 5:
        # A step dragged below MARKET_PRICE_FLOOR by a corrupt batch (#144).
        ys[0] = -2.0
    iso = e.IsotonicRegression()
    return iso.fit(xs, ys)


def _quantiles(e: Any, bucket: Any, k: int) -> None:
    variant = k % 4
    if variant == 0:
        lines = ((_f(32), _f(-2)), (_f(48), 0.0), (1.0, _f(2)))
        ns = (40, 40, 40)
    elif variant == 1:
        # Slopes differ and intercepts agree, so the lines invert for x < 0.
        lines = ((_f(24), 0.0), (_f(32), 0.0), (_f(48), 0.0))
        ns = (40, 40, 40)
    elif variant == 2:
        lines = ((_f(40), _f(-1)), (_f(56), _f(1)), (_f(72), _f(3)))
        ns = (40, 5, 40)
    else:
        return
    for q, (a, b), n in zip((bucket.q10, bucket.q50, bucket.q90), lines, ns):
        q.a, q.b, q.n = a, b, n
    bucket.ols = e.LinearCoeff(a=1.0, b=0.0, n=40 + k, mae=k / 512 if variant != 2 else None)


def _bucket(e: Any, key: str, k: int, *, iso: bool) -> Any:
    bucket = e.BucketModel(bucket_key=key)
    _quantiles(e, bucket, k)
    if iso:
        bucket.iso_model = _iso(e, k)
    return bucket


def _coef(k: int) -> list[float]:
    # With the SCENARIOS features, every term but the stage 1 one is a
    # multiple of 1/64, and so is the intercept: a prediction is then a six
    # decimal stage 1 value plus a sixty-fourth, never close to a tie at the
    # sixth decimal, so round(prediction, 6) is the same whether sum() adds
    # naively (3.11) or with compensation (3.12 and later). The horizon term
    # is inexact only at the two OLS bounds.
    return [
        _f(-112 + (k % 8)), 1.0, _f(16), _f(8), -4.0, _f(32), _f(1), _f(16), _f(8), _f(8),
    ]


def _resid(e: Any, key: str) -> Any:
    return e.ResidualQuantiles(bucket_key=key, q10=_f(-2), q50=_f(1, 256), q90=_f(3), n=64)


def _ols(e: Any, key: str, k: int, *, resid: bool = True, ranges: bool = True) -> Any:
    return e.OlsModel(
        bucket_key=key,
        coef=_coef(k),
        n_train=64,
        r2=0.5,
        resid=_resid(e, key) if resid else None,
        feature_min=list(FEATURE_MIN) if ranges else [],
        feature_max=list(FEATURE_MAX) if ranges else [],
    )


def _result(e: Any, name: str) -> Any:
    keys = _routed_keys(e)
    result = e.CalibrationResult(fitted_at="2026-09-26T08:00:00+10:00", total_observations=900)
    for k, key in enumerate(keys):
        if name == "no_iso":
            if k % 3 != 0:
                result.models[key] = _bucket(e, key, k, iso=False)
        else:
            result.models[key] = _bucket(e, key, k, iso=True)
        if name == "partial":
            ols = _partial_ols(e, key, k)
            if ols is not None:
                result.ols_models[key] = ols
        else:
            result.ols_models[key] = _ols(e, key, k, resid=name != "no_resid")
    return result


def _partial_ols(e: Any, key: str, k: int) -> Any:
    variant = k % 7
    if variant in (0, 1):
        return None
    if variant == 2:
        return e.OlsModel(bucket_key=key, coef=[_f(8)], n_train=64)
    if variant == 3:
        return _ols(e, key, k, ranges=False)
    if variant == 4:
        ols = _ols(e, key, k)
        ols.feature_min, ols.feature_max = FEATURE_MIN[:8], FEATURE_MAX[:8]
        return ols
    if variant == 5:
        ols = _ols(e, key, k)
        ols.resid = e.ResidualQuantiles(bucket_key=key, q10=_f(-2), q50=0.0, q90=_f(2), n=10)
        return ols
    ols = _ols(e, key, k)
    ols.resid = e.ResidualQuantiles(bucket_key=key, q10=_f(1), q50=_f(2), q90=_f(3), n=64)
    return ols


RESULTS = ("full", "no_resid", "no_iso", "partial")


def _features(e: Any, scenario: str) -> tuple[Any, Any]:
    run, stpasa = SCENARIOS[scenario]
    run_features = e.RunFeatures(*run) if run is not None else None
    stpasa_features = (
        e.StpasaFeatures(*stpasa, stpasa_run_at=STPASA_RUN_AT) if stpasa is not None else None
    )
    return stpasa_features, run_features


def _inputs() -> Iterator[tuple[str, float, float, int, str, float | None, bool | None]]:
    i = 0
    for result in RESULTS:
        for forecast in FORECASTS:
            for horizon in HORIZONS:
                for scenario in SCENARIOS:
                    for hours in (SHOULDER_HOURS, SOLAR_HOURS, PEAK_HOURS):
                        hour = hours[i % len(hours)]
                        gas, network = GAS[i % 3], NETWORK[(i // 3) % 3]
                        yield result, forecast, horizon, hour, scenario, gas, network
                        i += 1
    for forecast in FORECASTS:
        for horizon in (5.75, 42.0, 120.0, 168.0):
            for gas in GAS:
                for network in NETWORK:
                    hour = (SHOULDER_HOURS + SOLAR_HOURS + PEAK_HOURS)[i % 24]
                    yield "none", forecast, horizon, hour, "inside", gas, network
                    i += 1


# ── Replay of the gate conditions ────────────────────────────────────────────

def _replay(e: Any, result: Any, stage1: dict, forecast: float, horizon: float, hour: int,
            stpasa: Any, run: Any) -> str:
    """The serving outcome by the pinned gate conditions (spec 005)."""
    if stage1.get("calibrated_source") == e.SOURCE_ISOTONIC_BELOW_DOMAIN:
        return "refuse_below_domain"
    if stpasa is None or run is None or horizon < e.OLS_MIN_HORIZON_H or horizon > e.OLS_MAX_HORIZON_H:
        return "refuse_inputs"
    ols = result.ols_models.get(e._bucket_key(horizon, hour))
    if ols is None or len(ols.coef) < 2:
        return "refuse_model"
    vec = [
        float(e.stage2_iso_feature(stage1, forecast)),
        run.run_max_h6_rrp, run.run_mean_rrp, run.run_spread,
        horizon / 168.0,
        stpasa.log_surplus, stpasa.log_solar, stpasa.log_demand, stpasa.poe_spread_n,
    ]
    if not ols.serves(vec):
        return "refuse_feature_domain"
    prediction = ols.predict(vec)
    if (prediction < 0.0) != (float(stage1["calibrated"]) < 0.0):
        return "refuse_sign"
    if prediction < e.MARKET_PRICE_FLOOR:
        return "refuse_floor"
    return "stage2_residual" if ols.residual_band(prediction) is not None else "stage2_fallback"


def _check(e: Any, serving: str, out: dict, stage1: dict, is_stage1: bool) -> None:
    """The replayed outcome agrees with the output: identity and labels."""
    if serving.startswith("refuse_"):
        assert is_stage1, f"{serving}: refusal did not return the stage 1 dict"
        return
    assert not is_stage1, f"{serving}: stage 2 returned the stage 1 dict"
    assert out["calibrated_source"] == "isotonic+stpasa", serving
    band = e.BAND_SOURCE_STAGE2 if serving == "stage2_residual" else e.BAND_SOURCE_STAGE2_FALLBACK
    assert out[e.BAND_SOURCE_KEY] == band, serving
    assert list(out)[: len(stage1)] == list(stage1), serving


def _spike(e: Any, forecast: float, out: dict) -> str:
    if forecast < e.SPIKE_THRESHOLD or "spike_credible" not in out:
        assert "spike_credible" not in out
        return "no_annotation"
    value = out["spike_credible"]
    return {True: "spike_true", False: "spike_false", None: "spike_none"}[value]


# ── Recording ────────────────────────────────────────────────────────────────

class _Stage1Capture:
    """Wraps ``BucketModel.apply_all`` on the class this recording loaded."""

    def __init__(self, bucket_cls: type) -> None:
        self.seen: list[dict] = []
        original: Callable[[Any, float], dict] = bucket_cls.apply_all
        seen = self.seen

        def apply_all(self_: Any, x: float) -> dict:
            out = original(self_, x)
            seen.append(out)
            return out

        bucket_cls.apply_all = apply_all  # type: ignore[method-assign]

    def take(self) -> dict:
        assert len(self.seen) == 1, f"apply_all called {len(self.seen)} times"
        return self.seen.pop()


def _record(mods: types.SimpleNamespace) -> list[list[Any]]:
    e = mods.calibration_engine
    capture = _Stage1Capture(e.BucketModel)
    results = {name: _result(e, name) for name in RESULTS}
    results["none"] = None
    rows: list[list[Any]] = []
    for name, forecast, horizon, hour, scenario, gas, network in _inputs():
        result = results[name]
        stpasa, run = _features(e, scenario)
        store = mods.calibration_store.CalibrationStore.__new__(mods.calibration_store.CalibrationStore)
        store._calibration = result
        kwargs = dict(gas_forecast_tj=gas, network_tight=network,
                      stpasa_features=stpasa, run_features=run)
        if result is None:
            stored = store.apply_to_price(forecast, horizon, hour, **kwargs)
            assert not capture.seen
            rows.append([name, forecast, horizon, hour, scenario, gas, network,
                         None, "store_passthrough", _spike(e, 0.0, stored),
                         None, None, json.dumps(stored), None])
            continue
        out = result.apply(forecast, horizon, hour, stpasa=stpasa, run_features=run)
        stage1 = capture.take()
        is_stage1 = out is stage1
        serving = _replay(e, result, stage1, forecast, horizon, hour, stpasa, run)
        _check(e, serving, out, stage1, is_stage1)
        stage1_label = STAGE1_LABELS[stage1["calibrated_source"]]
        applied = json.dumps(out)
        stored = store.apply_to_price(forecast, horizon, hour, **kwargs)
        store_stage1 = capture.take()
        spike = _spike(e, forecast, stored)
        without = {k: v for k, v in stored.items() if k != "spike_credible"}
        assert json.dumps(without) == applied, "apply_to_price changed the apply output"
        rows.append([name, forecast, horizon, hour, scenario, gas, network,
                     stage1_label, serving, spike,
                     applied, is_stage1, json.dumps(stored), stored is store_stage1])
    return rows


def record() -> dict[str, Any]:
    """Run the grid against the code on disk and return the recording."""
    with _isolated() as mods:
        rows = _record(mods)
    return {
        "about": (
            "Outputs of CalibrationResult.apply and CalibrationStore.apply_to_price over a "
            "fixed grid (spec 005, invariant 1). Written by scripts/record_serving_io.py on "
            "the base commit; compared by tests/test_serving_io.py."
        ),
        "fields": list(FIELDS),
        "scenarios": {name: [run, stpasa] for name, (run, stpasa) in SCENARIOS.items()},
        "stpasa_run_at": STPASA_RUN_AT,
        "inputs": rows,
    }


def branch_counts(recording: dict[str, Any]) -> dict[str, int]:
    """How many inputs reached each stage 1 path, serving outcome and spike label."""
    fields = recording["fields"]
    counts: collections.Counter[str] = collections.Counter()
    for row in recording["inputs"]:
        for column in ("stage1", "serving", "spike"):
            label = row[fields.index(column)]
            if label is not None:
                counts[label] += 1
    return dict(counts)


def _dumps(recording: dict[str, Any]) -> bytes:
    return (json.dumps(recording, separators=(",", ":")) + "\n").encode("utf-8")


def load_fixture() -> dict[str, Any]:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()).decode("utf-8"))


def main(argv: list[str]) -> int:
    recording = json.loads(_dumps(record()))
    if "--check" in argv:
        if load_fixture() == recording:
            print("record_serving_io: identical")
            return 0
        print("record_serving_io: differs from the fixture")
        return 1
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(gzip.compress(_dumps(recording), compresslevel=9, mtime=0))
    counts = branch_counts(recording)
    print(f"wrote {FIXTURE.relative_to(ROOT)}: {len(recording['inputs'])} inputs")
    for label in list(STAGE1_LABELS.values()) + list(SERVING_LABELS) + list(SPIKE_LABELS):
        print(f"  {label:24s} {counts.get(label, 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
