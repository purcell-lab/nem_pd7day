#!/usr/bin/env python3
"""Record every storage write and log line of a fixed CalibrationStore sequence.

Spec 004, invariant 2: the files CalibrationStore writes live in users' Home
Assistant ``.storage``, so the split of the store must leave them identical to
the byte. This script drives the real ``CalibrationStore``, constructed the
normal way, through a fixed sequence and records, per step:

* every ``async_save(data)`` and ``async_remove()`` on any Store, in call
  order, as ``{"key", "op", "json"}`` with ``json`` = ``json.dumps(data)``
  (insertion order kept, no sort_keys);
* the log records of the ``custom_components.nem_pd7day`` loggers, as
  ``{"level", "logger", "message"}`` (the logger name is informative only; the
  test compares level and message, since a log call may move to another module
  of the package);
* ``fit_generation`` after the step, and what the step returned.

``tests/test_calibration_store_io.py`` runs the same ``record()`` and compares
it with ``tests/fixtures/calibration_store_io.json``, which this script wrote
on the base commit before any code moved.

The sequence (see ``_steps``):

1. load from legacy payloads: the unscoped observation key, and the legacy
   coefficient and forecast history keys; the scoped keys are empty;
2. ingest two runs with STPASA, gas (market summary) and QNI, then the second
   run again (a duplicate); the first ingest prunes a stale legacy interval;
3. record actuals: a repeat reading of a loaded pair, first and repeat
   readings, a region mismatch, negative and beyond-168h horizons, an interval
   with no history, and enough rows to prune the oldest day twice
   (``MAX_TOTAL_OBS`` is patched to 36);
4. refit with stage 2 data, then refit with stage 2 raising;
5. restart: a fresh store loads the scoped keys, takes one repeat reading
   through the rebuilt accumulator, and ingests a run with no
   ``forecast_generated_at``;
6. a fresh store loads a corrupt coefficient payload and no forecast history.

What is pinned, so the output is deterministic:

* ``calibration_store._now_nem`` is replaced by a clock this script sets per
  step (the only clock read in the store);
* ``CalibrationEngine.fit`` is called with ``now`` set to that clock less
  ``FIT_LAG``, through an instance attribute on each store's engine, so
  ``fitted_at`` and the rolling window do not read the wall clock
  (``fit_ols_stage2`` calls ``self.fit`` and goes through the same pin), and
  every decay weight is exactly 1.0 (see ``FIT_LAG``);
* ``const.MAX_TOTAL_OBS`` is set to 36 before the store module is loaded, so
  whichever module binds the name sees the small cap;
* the BLAS and CPU feature environment of ``tests/conftest.py`` is set before
  numpy loads;
* the Store stand-in has no ``async_delay_save``, so the observation log
  writes its segments through ``async_save`` at once and every write is seen.

Nothing here is random. Usage:

    python scripts/record_calibration_store_io.py          # write the fixture
    python scripts/record_calibration_store_io.py --check  # compare as the test does, exit 1 on a difference
"""
from __future__ import annotations

import os

# Same floating point environment as tests/conftest.py, before numpy loads.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_CORETYPE", "Haswell")
os.environ.setdefault("NPY_DISABLE_CPU_FEATURES", "X86_V4")

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Any, Iterator  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "calibration_store_io.json"
PKG = "custom_components.nem_pd7day"
NEM_TZ = timezone(timedelta(hours=10))
REGION = "QLD1"
MAX_TOTAL_OBS = 36
# The engine's fit is measured from the step's clock less FIT_LAG, which puts
# every observation after the fit's "now": each decay weight is then
# exp(-0.0) == 1.0 exactly, and with the bucketed values multiples of 1/1024
# the stage 1 OLS sums are exact. Python 3.12 changed the built-in sum() of
# floats to compensated summation, so inexact sums differ in the last bit
# between 3.11 and 3.13, and this fixture has to hold on both.
FIT_LAG = timedelta(days=5)

if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import support  # noqa: E402


# ── Recording Home Assistant stand-ins ───────────────────────────────────────

class Recorder:
    """Backing data and the ordered call log shared by every RecordingStore."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.calls: list[dict[str, str]] = []


def _store_class(recorder: Recorder) -> type:
    class RecordingStore:
        """``homeassistant.helpers.storage.Store`` over ``recorder.data``.

        Loads return a JSON round trip, as the real store reads from disk.
        No ``async_delay_save``: the observation log then saves at once.
        """

        def __init__(self, hass: Any, version: int, key: str, *args: Any, **kwargs: Any) -> None:
            self.key = key

        async def async_load(self) -> Any:
            value = recorder.data.get(self.key)
            return None if value is None else json.loads(json.dumps(value))

        async def async_save(self, data: Any) -> None:
            text = json.dumps(data)
            recorder.calls.append({"key": self.key, "op": "save", "json": text})
            recorder.data[self.key] = json.loads(text)

        async def async_remove(self) -> None:
            recorder.calls.append({"key": self.key, "op": "remove", "json": ""})
            recorder.data.pop(self.key, None)

    return RecordingStore


class FakeHass:
    """The slice of HomeAssistant the store uses: an inline executor."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def async_add_executor_job(self, fn: Any, *args: Any) -> Any:
        return fn(*args)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        })


# ── Isolation ────────────────────────────────────────────────────────────────

_STUB_ROOTS = ("homeassistant", "aiohttp", "voluptuous")


def _owned(key: str) -> bool:
    return key.startswith(PKG + ".") or key.split(".")[0] in _STUB_ROOTS


@contextlib.contextmanager
def _isolated(recorder: Recorder) -> Iterator[types.SimpleNamespace]:
    """Fresh stubs and fresh integration modules; sys.modules restored on exit."""
    support._ensure_packages()
    package = sys.modules[PKG]
    saved_modules = {key: sys.modules.pop(key) for key in list(sys.modules) if _owned(key)}
    saved_attrs = dict(vars(package))
    try:
        support.install_ha_stubs()
        sys.modules["homeassistant.helpers.storage"].Store = _store_class(recorder)
        mods = types.SimpleNamespace()
        mods.const = support.load("const")
        mods.const.MAX_TOTAL_OBS = MAX_TOTAL_OBS
        for name in ("nem_time", "calibration_engine", "observation_log", "calibration_store"):
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

def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


R1 = "2026-09-19T13:00:00+10:00"
R2 = "2026-09-19T18:00:00+10:00"
P_NEG = "2026-09-19T12:00:00+10:00"      # before both runs: negative horizon
P1 = "2026-09-20T18:00:00+10:00"
P2 = "2026-09-20T18:30:00+10:00"
P3 = "2026-09-20T09:00:00+10:00"
P_FAR = "2026-09-27T14:00:00+10:00"      # beyond MAX_HORIZON_HOURS for both runs
LEGACY_INTERVAL = "2026-09-16T10:00:00+10:00"
LEGACY_RUN = "2026-09-15T13:00:00+10:00"
STALE_INTERVAL = "2026-09-01T10:00:00+10:00"
NO_HISTORY_INTERVAL = "2026-09-20T23:00:00+10:00"


def _end(start: str) -> str:
    return _iso(_t(start) + timedelta(minutes=30))


def _legacy_observation(interval: str, run_at: str, forecast: float, actual: float,
                        stpasa: tuple[float, float, float, float] | None = None) -> dict:
    it, rt = _t(interval), _t(run_at)
    obs: dict[str, Any] = {
        "interval_time": interval,
        "horizon_hours": round((it - rt).total_seconds() / 3600, 2),
        "pd7day_forecast": forecast,
        "actual_rrp": actual,
        "forecast_run_at": run_at,
        "hour_of_day": it.hour,
        "day_of_week": it.weekday(),
        "month": it.month,
        "gas_forecast_tj": 120.0,
        "qni_mwflow": -300.0,
        "qni_violation_degree": 0.0,
        "is_intervention": False,
        "actual_source": "amber",
    }
    if stpasa is not None:
        (obs["stpasa_log_surplus"], obs["stpasa_log_solar"],
         obs["stpasa_log_demand"], obs["stpasa_poe_spread_n"]) = stpasa
        obs["stpasa_run_at"] = "2026-09-16T07:25:07+10:00"
    return obs


def _legacy_payloads() -> dict[str, Any]:
    day_a = [
        _legacy_observation(LEGACY_INTERVAL, LEGACY_RUN, 0.101, 0.095),
        _legacy_observation("2026-09-16T10:30:00+10:00", LEGACY_RUN, 0.104, 0.099),
        _legacy_observation("2026-09-16T11:00:00+10:00", LEGACY_RUN, 0.108, 0.1025),
    ]
    day_b = []
    for i in range(30):
        interval = "2026-09-17T03:00:00+10:00" if i % 2 == 0 else "2026-09-17T03:30:00+10:00"
        run_at = _iso(_t(interval) - timedelta(hours=20, minutes=30 * i))
        # Multiples of 1/1024, so the fit's sums are exact (see FIT_LAG).
        forecast = (60 + 3 * i) / 1024
        actual = (56 + 3 * i + (4 if i % 3 == 0 else -2)) / 1024
        stpasa = (
            (round(7.0 + 0.01 * i, 6), round(0.5 * (i % 4), 6), round(8.6 + 0.005 * i, 6),
             round(-0.15 - 0.002 * i, 6))
            if i < 12 else None
        )
        day_b.append(_legacy_observation(interval, run_at, forecast, actual, stpasa))

    def _entry(run_at: str, price: float, region: str = REGION) -> dict:
        return {
            "run_at": run_at, "forecast_price": price, "gas_tj": 118.0,
            "qni_mwflow": -410.5, "qni_violation": 0.0, "is_intervention": False,
            "region": region,
        }

    history = {
        STALE_INTERVAL: [_entry("2026-08-31T13:00:00+10:00", 0.2)],
        LEGACY_INTERVAL: [
            _entry(LEGACY_RUN, 0.101),
            _entry("2026-09-15T18:00:00+10:00", 0.097, region="NSW1"),
            _entry("not a time", 0.099),
        ],
    }
    coefficients = {
        "fitted_at": "2026-09-15T08:00:00+10:00",
        "total_observations": 33,
        "observations_in_window": 33,
        "models": {
            "h12_24_shoulder": {
                "ols": {"a": 0.93, "b": 0.004, "n": 20, "mae": 0.006, "rmse": 0.008},
                "q10": {"a": 0.81, "b": -0.002, "n": 20, "pl": 0.001},
                "q50": {"a": 0.92, "b": 0.003, "n": 20, "pl": 0.002},
                "q90": {"a": 1.05, "b": 0.009, "n": 20, "pl": 0.001},
            },
        },
        "ols_models": {},
    }
    return {
        "nem_pd7day.observation_log": {"observations": day_a + day_b},
        "nem_pd7day.calibration_coefficients": coefficients,
        "nem_pd7day.forecast_history": {"forecast_history": history},
    }


def _price_data(run_at: str | None, prices: dict[str, float]) -> Any:
    return types.SimpleNamespace(
        forecast_generated_at=run_at,
        forecast=[
            types.SimpleNamespace(nemtime=_end(start), time=start, value=value)
            for start, value in prices.items()
        ],
    )


def _stpasa_interval(end: str, **mw: Any) -> Any:
    values: dict[str, Any] = dict(
        demand10=5600.0, demand50=6100.0, demand90=6650.0,
        surpluscapacity=1350.0, ss_solar_uigf=0.0, ss_wind_uigf=420.0,
    )
    values.update(mw)
    return types.SimpleNamespace(
        interval_datetime=end, run_datetime="2026-09-19T07:25:07+10:00", **values,
    )


def _run1() -> dict[str, Any]:
    prices = {P_NEG: 0.090, P1: 0.112, P2: 0.118, P3: 0.064, P_FAR: 0.150}
    qni = types.SimpleNamespace(forecast=[
        types.SimpleNamespace(time=P1, mwflow=-512.25, violationdegree=0.0),
        types.SimpleNamespace(time=P2, mwflow=-498.0, violationdegree=1.5),
    ])
    gas = types.SimpleNamespace(forecast=[
        types.SimpleNamespace(nemtime="2026-09-20T00:00:00+10:00", value_tj=131.4),
    ])
    stpasa = types.SimpleNamespace(intervals=[
        _stpasa_interval(_end(P1)),
        _stpasa_interval(_end(P2), demand10=-2.0, demand50=-5.0, demand90=-9.0),
        _stpasa_interval(_end(P3), ss_solar_uigf=None),
        _stpasa_interval("garbage"),
    ])
    return dict(
        region=REGION, price_data=_price_data(R1, prices),
        interconnectors={"NSW1-QLD1": qni, "V1-S1": types.SimpleNamespace(forecast=[])},
        case=types.SimpleNamespace(intervention=False), market_summary=gas, stpasa=stpasa,
    )


def _run2() -> dict[str, Any]:
    prices = {P_NEG: 0.091, P1: 0.109, P2: 0.121, P3: 0.066, P_FAR: 0.149}
    gas = types.SimpleNamespace(forecast=[
        types.SimpleNamespace(nemtime="2026-09-19T00:00:00+10:00", value_tj=127.0),
        types.SimpleNamespace(nemtime="2026-09-20T00:00:00+10:00", value_tj=133.9),
    ])
    return dict(
        region=REGION, price_data=_price_data(R2, prices), interconnectors={},
        case=None, market_summary=gas, stpasa=None,
    )


# ── The sequence ─────────────────────────────────────────────────────────────

class _Run:
    def __init__(self, mods: types.SimpleNamespace, recorder: Recorder) -> None:
        self.mods = mods
        self.recorder = recorder
        self.hass = FakeHass()
        self.now = _t("2026-09-19T19:00:00+10:00")
        mods.calibration_store._now_nem = lambda: self.now
        self.steps: list[dict[str, Any]] = []

    def new_store(self) -> Any:
        store = self.mods.calibration_store.CalibrationStore(self.hass, REGION)
        engine = store._engine
        fit = type(engine).fit

        def pinned_fit(observations: Any, region: str = "QLD1", now: Any = None) -> Any:
            fit_now = (self.now - FIT_LAG).astimezone(timezone.utc)
            return fit(engine, observations, region, now=fit_now)

        engine.fit = pinned_fit
        return store

    async def step(self, name: str, store: Any, at: str, action: Any) -> None:
        self.now = _t(at)
        capture = _Capture()
        logger = logging.getLogger(PKG)
        previous = logger.level
        logger.addHandler(capture)
        logger.setLevel(logging.DEBUG)
        start = len(self.recorder.calls)
        try:
            result = await action()
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous)
        self.steps.append({
            "step": name,
            "calls": self.recorder.calls[start:],
            "logs": capture.records,
            "fit_generation": store.fit_generation,
            "result": result,
        })


def _record_actual(store: Any, interval: str, rrp: float, **kw: Any) -> Any:
    async def run() -> Any:
        return await store.async_record_actual(interval, rrp, **kw)
    return run


def _summary(store: Any) -> str:
    return json.dumps(store.summary_attributes(), default=repr)


async def _steps(run: _Run) -> None:
    run.recorder.data.update(copy.deepcopy(_legacy_payloads()))
    store = run.new_store()

    async def load() -> Any:
        await store.async_load()
        return {"observations": len(store.observations), "history": len(store._forecast_history)}

    await run.step("load_legacy", store, "2026-09-19T19:00:00+10:00", load)

    async def ingest(kwargs: dict[str, Any]) -> Any:
        await store.ingest_forecast(**kwargs)
        return sorted(store._forecast_history)

    await run.step("ingest_run1", store, "2026-09-19T19:00:00+10:00", lambda: ingest(_run1()))
    await run.step("ingest_run2", store, "2026-09-19T19:05:00+10:00", lambda: ingest(_run2()))
    await run.step("ingest_run2_duplicate", store, "2026-09-19T19:10:00+10:00", lambda: ingest(_run2()))

    readings: list[tuple[str, str, float, dict[str, Any]]] = [
        ("actual_repeat_of_loaded_pair", LEGACY_INTERVAL, 0.21, {"calibration_region": REGION, "source": "amber"}),
        ("actual_first", P1, 0.11, {"source": "amber"}),
        ("actual_repeat", P1, 0.13, {}),
        ("actual_region_mismatch", P2, 0.09, {"calibration_region": "NSW1"}),
        ("actual_negative_horizon", P_NEG, 0.1, {}),
        ("actual_beyond_horizon", P_FAR, 0.1, {}),
        ("actual_no_history", NO_HISTORY_INTERVAL, 0.1, {}),
        ("actual_first_prunes_oldest_day", P2, 0.095, {"calibration_region": REGION, "source": "dispatch"}),
        ("actual_after_prune_recreates_pair", LEGACY_INTERVAL, 0.25, {}),
        ("actual_first_prunes_again", P3, 0.30, {}),
    ]
    for name, interval, rrp, kw in readings:
        await run.step(name, store, "2026-09-20T10:00:00+10:00", _record_actual(store, interval, rrp, **kw))

    async def refit() -> Any:
        result = await store.async_refit()
        return {
            "fitted_at": result.fitted_at,
            "ols_models": sorted(result.ols_models),
            "iso_history": json.dumps(store.iso_history),
            "summary_attributes": _summary(store),
        }

    await run.step("refit_with_stage2", store, "2026-09-20T12:00:00+10:00", refit)

    def stage2_fails(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("stage 2 unavailable")

    store._engine.fit_ols_stage2 = stage2_fails
    await run.step("refit_stage2_fails", store, "2026-09-20T13:00:00+10:00", refit)

    restarted = run.new_store()

    async def reload() -> Any:
        await restarted.async_load()
        return {
            "observations": len(restarted.observations),
            "history": len(restarted._forecast_history),
            "summary_attributes": _summary(restarted),
        }

    await run.step("restart_load_scoped", restarted, "2026-09-20T14:00:00+10:00", reload)
    await run.step("restart_repeat_reading", restarted, "2026-09-20T14:00:00+10:00",
                   _record_actual(restarted, P1, 0.12))

    async def ingest_unstamped() -> Any:
        prices = {"2026-09-21T02:00:00+10:00": 0.105, P3: 0.07}
        await restarted.ingest_forecast(REGION, _price_data(None, prices), {}, None)
        return sorted(restarted._forecast_history)

    await run.step("restart_ingest_without_run_time", restarted, "2026-09-20T14:30:00+10:00", ingest_unstamped)

    run.recorder.data["nem_pd7day.qld1.calibration_coefficients"] = {"models": {"h00_06_peak": {}}}
    run.recorder.data.pop("nem_pd7day.qld1.forecast_history", None)
    run.recorder.data.pop("nem_pd7day.forecast_history", None)
    corrupt = run.new_store()

    async def load_corrupt() -> Any:
        await corrupt.async_load()
        return {
            "calibration": corrupt.calibration is not None,
            "history": len(corrupt._forecast_history),
            "summary_attributes": _summary(corrupt),
        }

    await run.step("load_corrupt_coefficients", corrupt, "2026-09-20T15:00:00+10:00", load_corrupt)


def record() -> dict[str, Any]:
    """Run the sequence against the code on disk and return the recording."""
    recorder = Recorder()
    with _isolated(recorder) as mods:
        run = _Run(mods, recorder)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_steps(run))
        finally:
            loop.close()
    return {
        "about": (
            "Storage writes, log lines and fit generations of a fixed CalibrationStore "
            "sequence (spec 004, invariant 2). Written by scripts/record_calibration_store_io.py "
            "on the base commit; compared by tests/test_calibration_store_io.py."
        ),
        "max_total_obs": MAX_TOTAL_OBS,
        "steps": run.steps,
    }


def comparable(recording: dict[str, Any]) -> dict[str, Any]:
    """The recording without logger names, which may change when a log call
    moves to another module of the package; everything else must match."""
    out = json.loads(json.dumps(recording))
    for step in out["steps"]:
        for log in step["logs"]:
            log.pop("logger", None)
    return out


def main(argv: list[str]) -> int:
    recording = record()
    text = json.dumps(recording, indent=1) + "\n"
    if "--check" in argv:
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if comparable(expected) == comparable(recording):
            print("record_calibration_store_io: identical apart from logger names")
            return 0
        print("record_calibration_store_io: differs from the fixture")
        return 1
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(text, encoding="utf-8")
    steps = recording["steps"]
    saves = sum(c["op"] == "save" for s in steps for c in s["calls"])
    removes = sum(c["op"] == "remove" for s in steps for c in s["calls"])
    logs = sum(len(s["logs"]) for s in steps)
    print(f"wrote {FIXTURE.relative_to(ROOT)}: {len(steps)} steps, {saves} saves, "
          f"{removes} removes, {logs} log records")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
