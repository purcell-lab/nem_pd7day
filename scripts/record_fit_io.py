#!/usr/bin/env python3
"""Record every output of the calibration fit over fixed observation sets.

Spec 006, invariant 1: ``CalibrationEngine.fit`` and ``fit_ols_stage2`` move
into ``fitting.py``, and every fitted number, stored field and log line must
stay the same to the bit. The golden master reaches only some branches of the
fit; this fixture reaches all of them that the code can reach.

The observation sets are generated here from a seeded ``random.Random`` and
written into the fixture, so the fixture does not depend on the generator
staying stable. ``CASES`` builds:

* ``main``: 100 days of two-hourly intervals in QLD1, each seen by a sample of
  the day's three runs over the previous week, across the 90-day cutoff, with
  naive, aware and unparseable timestamps, rows on the cutoff, interventions,
  spikes in the forecast, the actual and both, STPASA features for most
  stage 2 rows, runs with no near-horizon rows (so no run features), morning
  forecasts below the night-time domain (issue #208) and one isolated
  high-leverage row;
* ``region_*``: a smaller set of the same shape for the other regions and one
  unknown region, whose time-of-day falls back to clock hours;
* ``thin``: stage 1 buckets of 1, ``MIN_OBS - 1``, ``MIN_OBS`` and 11 rows;
* ``degenerate``: constant forecasts, a falling relationship and quantile
  lines whose slopes arrive out of order;
* ``max_obs``: one bucket over ``MAX_OBS``;
* ``stage2_*``: a stage 2 bucket of exactly ``OLS_MIN_OBS`` rows, one the
  leverage screen takes below it, one with constant actuals and one with a
  non-finite feature.

Each case runs with the wall clock frozen at ``INSTANT``: every loaded package
module whose ``datetime`` binding is the real class gets a subclass whose
``now`` returns the instant, so the freeze follows the clock read wherever it
lives. Per case it records, through ``json.dumps`` with key order kept:

* ``CalibrationEngine.to_storage`` of ``fit(obs, region, now=NOW)``;
* the same for ``fit(obs, region)`` on the frozen clock, with the stage 2
  models from ``fit_ols_stage2`` attached as ``async_refit`` attaches them;
* every bucket's isotonic breakpoints as ``float.hex`` (``to_storage`` does
  not carry them);
* every log record on ``custom_components.nem_pd7day.calibration_engine`` at
  DEBUG and above: logger name, level, format string and ``repr`` of the
  arguments.

Branches are counted by replaying the fit's conditions here, independently of
the code under test, and printed; ``REQUIRED_BRANCHES`` must each be reached,
and any other listed branch the code cannot reach is reported, not forced.

``tests/test_fit_io.py`` runs the same ``record()`` and compares it with
``tests/fixtures/fit_io.json.gz``, which this script wrote on the base commit
of spec 006 before any code moved.

Usage:

    python scripts/record_fit_io.py          # write the fixture
    python scripts/record_fit_io.py --check  # compare as the test does, exit 1 on a difference
"""
from __future__ import annotations

import os

# Same floating point environment as tests/conftest.py, before numpy loads.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_CORETYPE", "Haswell")
os.environ.setdefault("NPY_DISABLE_CPU_FEATURES", "X86_V4")

import collections  # noqa: E402
import contextlib  # noqa: E402
import datetime as _dt  # noqa: E402
import gzip  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import math  # noqa: E402
import pathlib  # noqa: E402
import random  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from typing import Any, Iterator  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fit_io.json.gz"
PKG = "custom_components.nem_pd7day"
LOGGER = PKG + ".calibration_engine"

if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import support  # noqa: E402

NEM = _dt.timezone(_dt.timedelta(hours=10))
INSTANT = _dt.datetime(2026, 9, 29, 4, 0, tzinfo=_dt.timezone.utc)   # 14:00 NEM
NOW = _dt.datetime(2026, 9, 29, 3, 30, tzinfo=_dt.timezone.utc)      # an explicit now, 30 min earlier
RUN_HOURS = (4, 12, 20)                                               # NEM hours of the daily runs

# Every branch the replay counts. REQUIRED_BRANCHES must each be reached.
BRANCHES = (
    # stage 1 rows
    "ts_aware", "ts_naive", "ts_unparseable", "window_out", "window_edge",
    "stage1_intervention", "spike_forecast", "spike_actual", "spike_both",
    "max_obs_dropped",
    # stage 1 buckets
    "bucket_empty", "bucket_thin", "bucket_at_min", "bucket_fitted",
    "ols_fallback", "ols_slope_clamped", "quantile_reordered", "quantile_clamped",
    "tod_morning_ramp", "tod_unknown_region",
    # stage 2 rows
    "row_intervention", "row_horizon_out", "row_spike", "row_no_stpasa",
    "row_no_run", "row_below_domain", "row_kept",
    # stage 2 buckets
    "ols_thin", "ols_at_min", "ols_lstsq_error", "ols_screened",
    "ols_screened_thin", "ols_r2_zero", "ols_fitted", "resid_missing",
)
# _residual_quantiles is only ever given at least OLS_MIN_OBS rows, and the
# LOO residuals of a fitted design are finite, so its unfitted result has no
# known input; it is counted, reported, and not required.
REQUIRED_BRANCHES = tuple(b for b in BRANCHES if b != "resid_missing")


# ── Isolation and the frozen clock ───────────────────────────────────────────

_STUB_ROOTS = ("homeassistant", "aiohttp", "voluptuous")


def _owned(key: str) -> bool:
    return key.startswith(PKG + ".") or key.split(".")[0] in _STUB_ROOTS


@contextlib.contextmanager
def _isolated() -> Iterator[types.SimpleNamespace]:
    """Fresh integration modules; sys.modules restored on exit."""
    support._ensure_packages()
    package = sys.modules[PKG]
    saved_modules = {key: sys.modules.pop(key) for key in list(sys.modules) if _owned(key)}
    saved_attrs = dict(vars(package))
    try:
        mods = types.SimpleNamespace()
        for name in ("const", "nem_time", "serving", "calibration_engine"):
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


class _FrozenDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz: _dt.tzinfo | None = None) -> _dt.datetime:  # type: ignore[override]
        if tz is None:
            return INSTANT.astimezone(NEM).replace(tzinfo=None)
        return INSTANT.astimezone(tz)

    @classmethod
    def utcnow(cls) -> _dt.datetime:  # type: ignore[override]
        return INSTANT.replace(tzinfo=None)


@contextlib.contextmanager
def _frozen_clock() -> Iterator[None]:
    """Rebind ``datetime`` in every loaded package module that holds the real class.

    Called inside each fit, so a module the fit imports lazily is loaded by
    then; the rebinding is repeated just before every call for that reason.
    """
    saved: list[tuple[types.ModuleType, Any]] = []
    for key, module in list(sys.modules.items()):
        if key.startswith(PKG + ".") and getattr(module, "datetime", None) is _dt.datetime:
            saved.append((module, module.datetime))
            module.datetime = _FrozenDatetime
    try:
        yield
    finally:
        for module, value in saved:
            module.datetime = value


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[list[str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append([record.name, record.levelname, str(record.msg), repr(record.args)])


@contextlib.contextmanager
def _captured() -> Iterator[_Capture]:
    logger = logging.getLogger(LOGGER)
    handler = _Capture()
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


# ── Inputs ───────────────────────────────────────────────────────────────────

# One observation as stored in the fixture, in Observation field order.
OBS_FIELDS = (
    "interval_time", "horizon_hours", "pd7day_forecast", "actual_rrp",
    "forecast_run_at", "hour_of_day", "day_of_week", "month",
    "gas_forecast_tj", "qni_mwflow", "qni_violation_degree", "is_intervention",
)
STPASA_FIELDS = ("log_surplus", "log_solar", "log_demand", "poe_spread_n", "stpasa_run_at")


def _r(x: float, places: int = 4) -> float:
    return round(x, places)


def _level(hour: int, rng: random.Random) -> float:
    """A forecast level by NEM hour: morning forecasts sit below the night's (#208)."""
    if 16 <= hour < 21:
        return rng.gauss(0.15, 0.08)
    if 10 <= hour < 16:
        return rng.gauss(0.03, 0.05)
    if 5 <= hour < 10:
        return rng.gauss(-0.02, 0.03)
    return rng.gauss(0.09, 0.02)


def _runs_before(t: _dt.datetime, days: int = 7) -> list[_dt.datetime]:
    """The daily runs (RUN_HOURS NEM) in the ``days`` before interval ``t``."""
    t_nem = t.astimezone(NEM)
    out = []
    day = t_nem.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(days + 1):
        for h in RUN_HOURS:
            run = day - _dt.timedelta(days=d) + _dt.timedelta(hours=h)
            if _dt.timedelta(minutes=30) <= t_nem - run <= _dt.timedelta(days=days):
                out.append(run)
    return out


def _stpasa(rng: random.Random, hour: int, run: str) -> list[Any]:
    solar = rng.uniform(4.0, 8.0) if 8 <= hour < 17 else rng.uniform(0.0, 1.0)
    return [_r(rng.uniform(6.0, 9.0)), _r(solar), _r(rng.uniform(8.0, 9.0)), _r(rng.uniform(0.0, 0.2)), run]


def _history(
    rng: random.Random, days: int, per_interval: int, step_h: int, *,
    specials: bool,
) -> tuple[list[list[Any]], dict[str, list[Any]]]:
    """Two-hourly (``step_h``) intervals over ``days``, each seen by ``per_interval`` runs."""
    obs: list[list[Any]] = []
    stpasa: dict[str, list[Any]] = {}
    end = INSTANT - _dt.timedelta(days=1)
    t = INSTANT - _dt.timedelta(days=days)
    i = 0
    while t <= end:
        t_nem = t.astimezone(NEM)
        base = _level(t_nem.hour, rng)
        runs = _runs_before(t)
        for run in rng.sample(runs, min(per_interval, len(runs))):
            i += 1
            horizon = (t_nem - run).total_seconds() / 3600.0
            forecast = _r(base + rng.gauss(0.0, 0.004 * (1 + horizon / 24)))
            actual = _r(0.8 * base + 0.02 + rng.gauss(0.0, 0.02))
            interval = t_nem.isoformat()
            if specials and i % 29 == 0:
                interval = t_nem.replace(tzinfo=None).isoformat()        # legacy naive
            run_at = run.isoformat()
            intervention = bool(specials and i % 53 == 0)
            if specials and i % 211 == 0:
                forecast = 3.5                                           # spike, forecast only
            elif specials and i % 223 == 0:
                actual = 4.25                                            # spike, actual only
            elif specials and i % 227 == 0:
                forecast, actual = 3.0, 3.0                              # spike, both, at the threshold
            obs.append([interval, _r(horizon, 6), forecast, actual, run_at,
                        t_nem.hour, t_nem.weekday(), t_nem.month, None, None, None, intervention])
            if 22.0 <= horizon <= 120.0 and not (specials and i % 11 == 0):
                stpasa[f"{interval}|{run_at}"] = _stpasa(rng, t_nem.hour, run_at)
        t += _dt.timedelta(hours=step_h)
    return obs, stpasa


def _main_case(rng: random.Random) -> dict[str, Any]:
    obs, stpasa = _history(rng, days=100, per_interval=8, step_h=2, specials=True)
    cutoff = (NOW - _dt.timedelta(days=90)).astimezone(NEM)
    edge = [cutoff.isoformat(), 30.0, 0.08, 0.07, (cutoff - _dt.timedelta(hours=30)).isoformat(),
            cutoff.hour, cutoff.weekday(), cutoff.month, None, None, None, False]
    obs.append(edge)                                                     # on the cutoff of fit(now=NOW)
    frozen_cutoff = (INSTANT - _dt.timedelta(days=90)).astimezone(NEM)
    obs.append([frozen_cutoff.isoformat()] + edge[1:4] + [
        (frozen_cutoff - _dt.timedelta(hours=30)).isoformat(), frozen_cutoff.hour,
        frozen_cutoff.weekday(), frozen_cutoff.month, None, None, None, False])
    for k in range(3):                                                   # unparseable timestamps
        obs.append([f"not-a-time-{k}", 40.0 + k, 0.1, 0.09, "2026-09-20T04:00:00+10:00",
                    18, 0, 9, None, None, None, False])
    # One isolated high-leverage stage 2 row among many (#79): a surplus far
    # outside every other row's, in a peak bucket that has plenty of rows.
    for row in obs:
        key = f"{row[0]}|{row[4]}"
        if key in stpasa and row[5] == 18 and 50.0 < row[1] < 90.0:
            stpasa[key] = [60.0] + stpasa[key][1:]
            break
    return {"region": "QLD1", "obs": obs, "stpasa": stpasa}


def _region_case(rng: random.Random, region: str) -> dict[str, Any]:
    obs, stpasa = _history(rng, days=30, per_interval=4, step_h=2, specials=False)
    return {"region": region, "obs": obs, "stpasa": stpasa}


def _row(t_nem: _dt.datetime, horizon: float, forecast: float, actual: float) -> list[Any]:
    run = t_nem - _dt.timedelta(hours=horizon)
    return [t_nem.isoformat(), horizon, forecast, actual, run.isoformat(),
            t_nem.hour, t_nem.weekday(), t_nem.month, None, None, None, False]


def _thin_case(rng: random.Random) -> dict[str, Any]:
    """Peak-hour buckets of 1, MIN_OBS - 1, MIN_OBS and 11 rows, one per horizon label."""
    obs = []
    base = INSTANT.astimezone(NEM).replace(hour=18, minute=0) - _dt.timedelta(days=3)
    for horizon, count in ((1.0, 1), (8.0, 9), (15.0, 10), (30.0, 11)):
        for k in range(count):
            x = _r(0.05 + 0.01 * k + rng.uniform(0, 0.005))
            obs.append(_row(base - _dt.timedelta(days=k), horizon, x, _r(0.9 * x + 0.01)))
    return {"region": "QLD1", "obs": obs, "stpasa": {}}


def _degenerate_case(rng: random.Random) -> dict[str, Any]:
    """Constant forecasts (the _ols fallback), a falling line, quantile slopes out of order."""
    obs = []
    base = INSTANT.astimezone(NEM).replace(hour=18, minute=0) - _dt.timedelta(days=2)
    for k in range(40):                                  # h00_06__peak: constant x
        obs.append(_row(base - _dt.timedelta(days=k), 2.0, 0.12, _r(0.1 + rng.uniform(-0.02, 0.02))))
    for k in range(40):                                  # h06_12__peak: y falls as x rises
        x = _r(0.02 + 0.005 * k)
        obs.append(_row(base - _dt.timedelta(days=k), 9.0, x, _r(0.3 - 2.0 * x + rng.uniform(-0.01, 0.01))))
    for k in range(60):                                  # h12_24__peak: spread shrinks as x rises
        x = _r(0.02 + 0.004 * k)
        spread = 0.25 - x
        obs.append(_row(base - _dt.timedelta(days=k % 60, hours=0), 18.0, x,
                        _r(0.5 * x + rng.uniform(-spread, spread))))
    return {"region": "QLD1", "obs": obs, "stpasa": {}}


def _max_obs_case(rng: random.Random, max_obs: int) -> dict[str, Any]:
    """One bucket (h96plus, night) over MAX_OBS, rows 5 minutes apart."""
    obs = []
    base = INSTANT.astimezone(NEM).replace(hour=2, minute=0) - _dt.timedelta(days=1)
    for k in range(max_obs + 300):
        t = base - _dt.timedelta(days=k % 80, minutes=5 * (k // 80) % 60)
        x = _r(rng.uniform(0.02, 0.2))
        obs.append(_row(t, 130.0, x, _r(0.8 * x + rng.gauss(0, 0.01))))
    return {"region": "QLD1", "obs": obs, "stpasa": {}}


def _stage2_case(rng: random.Random, rows: int, kind: str) -> dict[str, Any]:
    """One stage 2 bucket (h24_48__peak) of ``rows`` rows with STPASA and run features.

    Each row's run also has a near-horizon row (horizon 2), so run features
    exist; those rows sit in h00_06 and are below the stage 2 horizon band.
    """
    obs: list[list[Any]] = []
    stpasa: dict[str, list[Any]] = {}
    base = INSTANT.astimezone(NEM).replace(hour=18, minute=0) - _dt.timedelta(days=2)
    for k in range(rows):
        t = base - _dt.timedelta(days=k % 60, minutes=5 * (k // 60))
        x = _r(0.05 + rng.uniform(0.0, 0.2))
        y = 0.05 if kind == "constant" else _r(0.8 * x + rng.gauss(0, 0.02))
        row = _row(t, 30.0, x, y)
        obs.append(row)
        near = t - _dt.timedelta(hours=28)
        obs.append([near.isoformat(), 2.0, x, y, row[4], near.hour, near.weekday(), near.month,
                    None, None, None, False])
        feats = _stpasa(rng, 18, row[4])
        if kind == "screened" and k < 3:
            # Three rows each isolated along its own feature axis, so each
            # has hat leverage near 1 and the screen drops all three.
            feats[(0, 2, 3)[k]] = (60.0, 30.0, 5.0)[k]
        if kind == "nonfinite" and k == 7:
            feats[2] = float("inf")
        stpasa[f"{row[0]}|{row[4]}"] = feats
    return {"region": "QLD1", "obs": obs, "stpasa": stpasa}


def cases(const: types.ModuleType) -> dict[str, dict[str, Any]]:
    rng = random.Random(6006)
    out = {"main": _main_case(rng)}
    for region in ("NSW1", "VIC1", "SA1", "TAS1", "XX1"):
        out[f"region_{region}"] = _region_case(rng, region)
    out["thin"] = _thin_case(rng)
    out["degenerate"] = _degenerate_case(rng)
    out["max_obs"] = _max_obs_case(rng, const.MAX_OBS)
    out["stage2_at_min"] = _stage2_case(rng, const.OLS_MIN_OBS, "plain")
    out["stage2_screened_thin"] = _stage2_case(rng, const.OLS_MIN_OBS + 1, "screened")
    out["stage2_constant"] = _stage2_case(rng, 60, "constant")
    out["stage2_nonfinite"] = _stage2_case(rng, 60, "nonfinite")
    return out


# ── Branch replay ────────────────────────────────────────────────────────────

def _parse(ts: str) -> tuple[str, _dt.datetime | None]:
    try:
        dt = _dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return "ts_unparseable", None
    if dt.tzinfo is None:
        return "ts_naive", dt.replace(tzinfo=NEM)
    return "ts_aware", dt


def _replay(e: types.ModuleType, const: types.ModuleType, case: dict[str, Any],
            observations: list[Any], now: _dt.datetime, stage1: Any) -> collections.Counter:
    """Count the fit's branches from its inputs, independently of the fit's code."""
    import numpy as np

    c: collections.Counter = collections.Counter()
    region = case["region"]
    cutoff = now - _dt.timedelta(days=e.OBSERVATION_WINDOW_DAYS)
    now_nem = now.astimezone(NEM)
    pairs: dict[str, list[tuple[float, float]]] = {k: [] for k in e.all_bucket_keys()}
    weights: dict[str, list[float]] = {k: [] for k in e.all_bucket_keys()}
    spike = e.SPIKE_THRESHOLD
    for obs in observations:
        label, dt = _parse(obs.interval_time)
        c[label] += 1
        if dt is None:
            dt = now_nem
        elif dt < cutoff:
            c["window_out"] += 1
            continue
        elif dt == cutoff:
            c["window_edge"] += 1
        if obs.is_intervention:
            c["stage1_intervention"] += 1
            continue
        f_spike, a_spike = obs.pd7day_forecast >= spike, obs.actual_rrp >= spike
        if f_spike or a_spike:
            c["spike_both" if f_spike and a_spike else "spike_forecast" if f_spike else "spike_actual"] += 1
            continue
        obs_nem = dt.astimezone(NEM)
        key = e._bucket_key_solar(obs.horizon_hours, obs_nem, region)
        if key.endswith("__morning_ramp"):
            c["tod_morning_ramp"] += 1
        if region not in e.REGION_COORDS:
            c["tod_unknown_region"] += 1
        if len(pairs[key]) >= const.MAX_OBS:
            c["max_obs_dropped"] += 1
            continue
        pairs[key].append((obs.pd7day_forecast, obs.actual_rrp))
        days_ago = (now_nem - obs_nem).total_seconds() / 86400.0
        weights[key].append(math.exp(-e.DECAY_LAMBDA * max(days_ago, 0.0)))
    for key, p in pairs.items():
        n = len(p)
        c["bucket_empty" if n == 0 else "bucket_thin" if n < const.MIN_OBS else "bucket_fitted"] += 1
        if n == const.MIN_OBS:
            c["bucket_at_min"] += 1
        if n < const.MIN_OBS:
            continue
        a, b = e._ols(p, weights=weights[key] or None)
        if (a, b) == (1.0, 0.0):
            c["ols_fallback"] += 1
        if a < 0.0:
            c["ols_slope_clamped"] += 1
        slopes = [e._quantile_regression(p, q, weights=weights[key] or None)[0] for q in const.QUANTILES]
        if slopes != sorted(slopes):
            c["quantile_reordered"] += 1
        if min(slopes) < 0.0:
            c["quantile_clamped"] += 1

    if stage1 is None:
        return c
    run_features = e._compute_run_features(observations)
    rows: dict[str, list[tuple[list[float], float]]] = {}
    for obs in observations:
        if obs.is_intervention:
            c["row_intervention"] += 1
            continue
        if obs.horizon_hours < e.OLS_MIN_HORIZON_H or obs.horizon_hours > e.OLS_MAX_HORIZON_H:
            c["row_horizon_out"] += 1
            continue
        if obs.actual_rrp >= spike or obs.pd7day_forecast >= spike:
            c["row_spike"] += 1
            continue
        sf = case["stpasa_objects"].get(f"{obs.interval_time}|{obs.forecast_run_at}")
        if sf is None:
            c["row_no_stpasa"] += 1
            continue
        rf = run_features.get(obs.forecast_run_at)
        if rf is None:
            c["row_no_run"] += 1
            continue
        bucket = stage1.get_bucket(obs.horizon_hours, obs.hour_of_day)
        if bucket.is_below_domain(obs.pd7day_forecast):
            c["row_below_domain"] += 1
            continue
        c["row_kept"] += 1
        iso = e.stage2_iso_feature(bucket.apply_all(obs.pd7day_forecast), obs.pd7day_forecast)
        vec = [float(iso), rf.run_max_h6_rrp, rf.run_mean_rrp, rf.run_spread, obs.horizon_hours / 168.0,
               sf.log_surplus, sf.log_solar, sf.log_demand, sf.poe_spread_n]
        rows.setdefault(e._bucket_key(obs.horizon_hours, obs.hour_of_day), []).append((vec, obs.actual_rrp))
    for key, r in rows.items():
        if len(r) < const.OLS_MIN_OBS:
            c["ols_thin"] += 1
            continue
        if len(r) == const.OLS_MIN_OBS:
            c["ols_at_min"] += 1
        X = np.array([[1.0] + v for v, _ in r], dtype=float)
        y = np.array([a for _, a in r], dtype=float)
        try:
            coef = np.linalg.lstsq(X, y, rcond=None)[0]
        except np.linalg.LinAlgError:
            c["ols_lstsq_error"] += 1
            continue
        high = e._hat_leverage(X) > const.STAGE2_LEVERAGE_MULTIPLE * X.shape[1] / X.shape[0]
        if high.any():
            c["ols_screened"] += 1
            X, y = X[~high], y[~high]
            if X.shape[0] < const.OLS_MIN_OBS:
                c["ols_screened_thin"] += 1
                continue
            try:
                coef = np.linalg.lstsq(X, y, rcond=None)[0]
            except np.linalg.LinAlgError:
                c["ols_lstsq_error"] += 1
                continue
        if float(np.sum((y - np.mean(y)) ** 2)) <= 1e-12:
            c["ols_r2_zero"] += 1
        c["ols_fitted"] += 1
        resid = e._residual_quantiles(key, X, y, coef)
        if not resid.is_fitted:
            c["resid_missing"] += 1
    return c


# ── Recording ────────────────────────────────────────────────────────────────

def _iso_hex(result: Any) -> dict[str, list[list[str]] | None]:
    out: dict[str, list[list[str]] | None] = {}
    for key, model in result.models.items():
        iso = model.iso_model
        if iso is None:
            out[key] = None
        else:
            out[key] = [[float(v).hex() for v in iso._x_thresholds], [float(v).hex() for v in iso._y_thresholds]]
    return out


def _record_case(mods: types.SimpleNamespace, name: str, case: dict[str, Any]) -> dict[str, Any]:
    e = mods.calibration_engine
    observations = [e.Observation(*row) for row in case["obs"]]
    case["stpasa_objects"] = {
        k: e.StpasaFeatures(**dict(zip(STPASA_FIELDS, v))) for k, v in case["stpasa"].items()
    }
    engine = e.CalibrationEngine()
    out: dict[str, Any] = {}

    with _captured() as log, _frozen_clock():
        result = engine.fit(observations, case["region"], now=NOW)
    out["fit_now"] = {
        "storage": json.dumps(engine.to_storage(result)),
        "iso": _iso_hex(result),
        "log": log.records,
    }

    with _captured() as log, _frozen_clock():
        clock_result = engine.fit(observations, case["region"])
    fit_log = log.records
    models = None
    stage2_log: list[list[str]] = []
    if case["stpasa_objects"]:
        with _captured() as log, _frozen_clock():
            models = engine.fit_ols_stage2(
                observations, case["stpasa_objects"], case["region"], clock_result
            )
        stage2_log = log.records
        clock_result.ols_models = models
    out["fit_clock"] = {
        "storage": json.dumps(engine.to_storage(clock_result)),
        "iso": _iso_hex(clock_result),
        "log": fit_log,
        "stage2_log": stage2_log,
    }

    with _frozen_clock():
        counts = _replay(e, mods.const, case, observations, NOW, None)
        counts += _replay(e, mods.const, case, observations, INSTANT,
                          clock_result if models is not None else None)
    out["branches"] = dict(sorted(counts.items()))
    del case["stpasa_objects"]
    return out


def record() -> dict[str, Any]:
    with _isolated() as mods:
        inputs = cases(mods.const)
        outputs = {name: _record_case(mods, name, case) for name, case in inputs.items()}
    return {
        "obs_fields": list(OBS_FIELDS),
        "stpasa_fields": list(STPASA_FIELDS),
        "instant": INSTANT.isoformat(),
        "now": NOW.isoformat(),
        "inputs": inputs,
        "outputs": outputs,
    }


def branch_counts(recording: dict[str, Any]) -> dict[str, int]:
    total: collections.Counter = collections.Counter()
    for out in recording["outputs"].values():
        total.update(out["branches"])
    return {b: total.get(b, 0) for b in BRANCHES}


def _dumps(recording: dict[str, Any]) -> bytes:
    return json.dumps(recording, separators=(",", ":")).encode()


def load_fixture() -> dict[str, Any]:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def main(argv: list[str]) -> int:
    recording = record()
    counts = branch_counts(recording)
    missing = [b for b in REQUIRED_BRANCHES if not counts[b]]
    for branch in BRANCHES:
        print(f"{branch:22} {counts[branch]}")
    unreached = [b for b in BRANCHES if not counts[b]]
    if unreached:
        print("not reached:", ", ".join(unreached))
    if "--check" in argv:
        same = json.loads(_dumps(recording)) == load_fixture()
        print("fixture:", "identical" if same else "DIFFERS")
        return 0 if same and not missing else 1
    if missing:
        print("required branches not reached:", ", ".join(missing))
        return 1
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(gzip.compress(_dumps(recording), mtime=0))
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({FIXTURE.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
