"""
Keeping calibration off the state-write path.

sensor.py memoises the calibrated forecast per region on the coordinator,
warms it in the executor before writing state, publishes it only under the key
that is live at publication, and the tariff sensors read the same slot. Each
section below keeps the history of the file it came from.

Calibrated forecast memo (issue #35)
  Platform setup took 42 to 53 s per region. Three entities per region
  (PD7DayForecastSensor, SpotPriceForecastDays27Sensor, PD7DayDataSensor)
  each memoised the calibrated forecast on themselves; their _calibrate_period
  and _covariates_for_interval are the same computation (identical by AST once
  docstrings are stripped) over the same coordinator and store, so ~336
  intervals were calibrated three times. The memo moved to the coordinator,
  per region. Its key uses store.fit_generation rather than
  id(store.calibration): async_refit publishes the result and then mutates
  that same object in place to attach the OLS stage 2 models, which object
  identity cannot see. test_calibration_store pins that the counter moves.

Warm before write (#58)
  The memo was lazy: whichever sharing entity wrote first after an
  invalidation recalibrated the run on the event loop inside
  async_write_ha_state(). Home Assistant logged it on the live five region
  install on every PD7DAY run ("Updating state for
  sensor.nem_pd7day_nsw1_nem_nsw1_pd7day_data (PD7DayDataSensor) took 0.493
  seconds"; worst single write 2.181 s just after a restart, all five regions
  together because a new run invalidates every region's memo at once).
  CalibratedWriteMixin warms the memo in the executor and only then writes.
  The tests assert the property, not the mechanism: no calibration while the
  state write is in progress.

The two holes #58 left (#60, #61), measured on v3.3.1
  16 slow writes in the 13 minutes after a restart against 11 before, with a
  worst case an order of magnitude worse:
    17:56:11  0.456s  PD7DayDataSensor      sensor.nem_pd7day_vic1_nem_vic1_pd7day_data
    17:56:12  0.500s  PD7DayDataSensor      sensor.nem_pd7day_nsw1_nem_nsw1_pd7day_data
    18:00:18  6.766s  PD7DayForecastSensor  sensor.nem_pd7day_tas1_price_forecast
  Cause 1: entities are added with update_before_add=True and
  Entity.add_to_platform_finish calls async_write_ha_state() straight after
  awaiting async_added_to_hass(), bypassing _handle_coordinator_update, so
  the first write of an entity's life always ran the lazy path (the 17:56
  cluster, the condition #55 was reported from). Cause 2: the key folds in the
  STPASA index key and CalibrationStore.fit_generation, and a refit triggered
  by the same new run landed while the warm was in flight, so the write
  missed the memo it had just filled (the 18:00 cluster). #61 warms in
  async_added_to_hass and re-warms, bounded, while the currency check fails.

The key TOCTOU (second half of #60, PR #76)
  _calibrated_forecast built its key inside the executor job at the start of
  a ~0.4 s pass and stored the result under it unconditionally. A
  fit_generation bump part way through produced a list built from two models
  labelled with the first key, and a warm that started before a refit and
  landed after it overwrote the entry a sibling entity had just published, so
  the next reader rebuilt on the loop: the 6.766 s write. The invariant pinned
  here is not "the write was fast", which can pass on timing luck, but that
  every value published into the memo is published under the key live at that
  moment. Structurally: take the key once on the loop before the executor hop,
  compute with no memo access, publish back on the loop only if the key is
  still current.

Tariff spot memo (#62 item 4)
  The tariff sensors rebuilt the whole calibrated forecast on every state
  write, and so did every other tariff entity of the region (22 tariff
  entities over 5 regions on the install), while the region's forecast sensor
  had already memoised the same numbers. A per region slot the tariff path
  reads is filled from the forecast memo or from one shared build. Sharing is
  only safe because PR #77 unified both call sites on
  calibration_inputs.calibrate_interval with run_at_iso threaded through, so
  both use the same model and the same per run stage-2 band floor; the
  equality tests police that over a seven day sweep whose branch coverage
  (passthrough, isotonic_below_domain, isotonic, isotonic+stpasa) is itself
  asserted. The call counting tests and the key signature test fail without
  the production change; the equality tests are pins that pass by
  construction, because a change that moved a published tariff price would be
  the defect rather than the fix.

Per run feature cache (#135)
  Tariff state writes of 0.4 to 0.9 s on the loop. Profiling a cold write on a
  336 interval run put 92 per cent of the time in
  PD7DayCoordinator.current_run_features, called once per interval by
  calibrate_interval and parsing every interval's timestamp each time:
  115,795 parse_iso calls to calibrate one run. The coordinator caches the
  features per run, so a cold write computes them once.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import random
import threading
import types
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, make_price_period, nem_iso

install_ha_stubs()
_const_mod, _nem_time, _engine_mod, _client_mod, _store_mod, _coord_mod, sensor_module = load_chain(
    "const", "nem_time", "calibration_engine", "pd7day_client",
    "calibration_store", "coordinator", "sensor",
)

# The tariff fixtures (a real isotonic plus stage 2 fit behind a real
# CalibrationStore, and the three sensors sharing it) live with the parity
# tests they were written for. That module imports the integration through
# the package, so it binds to the modules loaded above.
from test_tariff_calibration_parity import (
    RUN_AT,
    _tariff_mod,
    make_period,
    make_sensors,
    make_stpasa_interval,
)

from custom_components.nem_pd7day import calibration_inputs
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult

parse_iso = _nem_time.parse_iso
to_nem_iso = _nem_time.to_nem_iso
PD7DayCoordinator = _coord_mod.PD7DayCoordinator

# The three classes that share one region's calibrated forecast. Resolved
# through the module object loaded above: other test files reload sensor.py
# under the same name, so classes and module must come from the same load.
SHARING_CLASSES = (
    sensor_module.PD7DayForecastSensor,
    sensor_module.SpotPriceForecastDays27Sensor,
    sensor_module.PD7DayDataSensor,
)
REGION = "QLD1"
INTERVALS = 357  # the run length carried by the install the #60 figures were measured on
RUN_DT = datetime(2026, 9, 1, 18, 0, tzinfo=NEM_TZ)
MAX_ATTEMPTS = sensor_module._MAX_CALIBRATION_WARM_ATTEMPTS


# ── Fixtures ─────────────────────────────────────────────────────────────────


class _CountingStore:
    """Calibration store stub exposing a controllable fit generation."""

    def __init__(self, fit_generation: int = 1) -> None:
        self.fit_generation = fit_generation


def _make_price_data(run_at_dt: datetime, intervals: int = 336):
    """A 30 minute forecast of ``intervals`` periods, as production carries."""
    d = MagicMock()
    d.forecast = [
        make_price_period(run_at_dt + timedelta(minutes=30 * (i + 1)), value=0.1)
        for i in range(intervals)
    ]
    d.forecast_generated_at = nem_iso(run_at_dt)
    d.region = REGION
    d.interval_minutes = 30
    return d


def _fresh_coordinator():
    """A coordinator stub that behaves like the real one for cache purposes."""
    coordinator = MagicMock()
    coordinator.data = None
    # The real PD7DayCoordinator initialises this dict in __init__.
    coordinator._calibrated_forecast_cache = {}
    coordinator._stpasa_index_run = "stpasa-run-1"
    coordinator.stpasa_index = MagicMock(return_value=None)
    return coordinator


def _make_region_sensors(coordinator, store, region=REGION):
    """One bare instance of each sharing class against a shared coordinator.

    Built with __new__ and only the attributes _calibrated_forecast touches,
    because the real __init__ needs Home Assistant's CoordinatorEntity.
    """
    sensors = []
    for cls in SHARING_CLASSES:
        s = cls.__new__(cls)
        s.coordinator = coordinator
        s._region = region
        s._store = store
        sensors.append(s)
    return sensors


def _count_calibrations(sensors):
    """Replace _calibrate_period on each instance with a counting stub."""
    counter = {"calls": 0}

    def _wrapped(period, run_at_str):
        counter["calls"] += 1
        return {"time": period.time, "value": period.value}

    for s in sensors:
        s._calibrate_period = _wrapped
    return counter


class _FakeHass:
    """Runs executor jobs on a real worker thread, like Home Assistant does."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []
        self.executor_calls = 0

    async def async_add_executor_job(self, func, *args):
        self.executor_calls += 1
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def async_create_background_task(self, coro, name=None, eager_start=False):
        return self.async_create_task(coro, name=name)

    def async_create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


def _make_sensor(cls, coordinator, store, hass, region=REGION):
    """A bare instance carrying only what the write path touches."""
    s = cls.__new__(cls)
    s.coordinator = coordinator
    s._region = region
    s._store = store
    s.hass = hass
    s.entity_id = f"sensor.nem_pd7day_{region.lower()}_probe"
    s.writes = 0
    s.calibrations_during_write = None

    def _write_ha_state():
        s.writes += 1
        # This is the moment that used to be slow. Record what calibration the
        # attribute build would still have to do at this point.
        s.calibrations_during_write = _count_during_write(s)

    s.async_write_ha_state = _write_ha_state
    return s


def _count_during_write(sensor):
    """How many calibrations building the attributes would cost right now."""
    before = sensor._calibrate_period.calls
    sensor._calibrated_forecast(sensor._price_data)
    return sensor._calibrate_period.calls - before


def _instrument(sensor):
    """Replace _calibrate_period with a counting stub that records its thread."""

    def _wrapped(period, run_at_str):
        _wrapped.calls += 1
        _wrapped.threads.add(threading.current_thread().name)
        return {"time": period.time, "value": period.value}

    _wrapped.calls = 0
    _wrapped.threads = set()
    sensor._calibrate_period = _wrapped
    return _wrapped


def _setup(cls, region=REGION):
    """A sharing sensor with a cold memo and an INTERVALS long run."""
    coordinator = _fresh_coordinator()
    store = _CountingStore()
    hass = _FakeHass()
    d = _make_price_data(RUN_DT, intervals=INTERVALS)
    coordinator.data = MagicMock()
    coordinator.data.prices = {region: d}
    sensor = _make_sensor(cls, coordinator, store, hass, region=region)
    # The real async_added_to_hass on two of the three classes subscribes to a
    # dispatch coordinator when the entry carries one. Leave it absent so the
    # tests exercise the warm and nothing else.
    sensor._entry = SimpleNamespace(runtime_data=None)
    sensor.async_on_remove = lambda _cb: None
    counter = _instrument(sensor)
    return sensor, hass, counter, store, d


def _run(coro):
    """run_async that closes its loop, so executor worker threads are released."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _HookedCounter:
    """The counting calibration stub, with a hook after each interval.

    The hook is how the world moves underneath a warm: it runs inside the
    calibration pass, which is exactly where a refit or an STPASA refetch
    lands in production, during the warm rather than before or after it.
    Delegates ``calls`` and ``threads`` so the helpers still see a counter.
    """

    def __init__(self, inner, hook) -> None:
        self._inner = inner
        self._hook = hook

    @property
    def calls(self) -> int:
        return self._inner.calls

    @property
    def threads(self) -> set:
        return self._inner.threads

    def __call__(self, period, run_at_str):
        result = self._inner(period, run_at_str)
        self._hook(self._inner.calls)
        return result


def _install_key_move(sensor, store, counter, at_call: int, times: int = 1):
    """Bump ``fit_generation`` at a chosen point inside each calibration pass.

    ``at_call`` is a position within the pass, counted modulo the run length:
    1 is the first interval and 0 the last. A refit bumping the generation is
    the cheapest faithful way to move the memo key; it is one of the places
    calibration_store increments it, and #60 names the refit triggered by the
    same new run as the one that lands inside the warm.
    """
    state = {"moves": 0}

    def _hook(call_no: int) -> None:
        if state["moves"] < times and call_no % INTERVALS == at_call:
            state["moves"] += 1
            store.fit_generation += 1

    sensor._calibrate_period = _HookedCounter(counter, _hook)
    return state


# ── Calibrated forecast memo (#35) ───────────────────────────────────────────


def test_three_sensors_in_a_region_calibrate_each_interval_once():
    """All three entities share one calibration pass over the forecast."""
    coordinator = _fresh_coordinator()
    sensors = _make_region_sensors(coordinator, _CountingStore())
    counter = _count_calibrations(sensors)
    d = _make_price_data(RUN_DT, intervals=336)

    results = [s._calibrated_forecast(d) for s in sensors]

    assert counter["calls"] == 336, (
        f"expected one calibration pass over 336 intervals for the region, got {counter['calls']}"
    )
    # Every entity sees the same list object: one copy in memory, not three.
    assert results[1] is results[0]
    assert results[2] is results[0]


def test_entity_level_memo_would_calibrate_three_times():
    """Guard: the pre-fix behaviour really was broken.

    Without this the test above could pass for the wrong reason if the memo
    silently moved back onto the entity. Each sensor getting its own
    coordinator is what an entity level cache amounted to.
    """
    store = _CountingStore()
    sensors = []
    for cls in SHARING_CLASSES:
        s = cls.__new__(cls)
        s.coordinator = _fresh_coordinator()
        s._region = REGION
        s._store = store
        sensors.append(s)
    counter = _count_calibrations(sensors)
    d = _make_price_data(RUN_DT, intervals=336)

    for s in sensors:
        s._calibrated_forecast(d)

    assert counter["calls"] == 336 * 3


def test_repeated_state_writes_do_not_recalibrate():
    """The memo still does its original job for an unchanged run."""
    coordinator = _fresh_coordinator()
    sensors = _make_region_sensors(coordinator, _CountingStore())
    counter = _count_calibrations(sensors)
    d = _make_price_data(RUN_DT, intervals=48)

    for _ in range(10):
        for s in sensors:
            s._calibrated_forecast(d)

    assert counter["calls"] == 48


def _new_run(coordinator, store, d):
    return _make_price_data(RUN_DT + timedelta(minutes=30), intervals=48)


def _refit(coordinator, store, d):
    store.fit_generation += 1
    return d


def _new_stpasa_run(coordinator, store, d):
    coordinator._stpasa_index_run = "stpasa-run-2"
    return d


@pytest.mark.parametrize(
    "move", [_new_run, _refit, _new_stpasa_run], ids=["new_run", "refit", "new_stpasa_run"]
)
def test_memo_invalidates_when_a_key_input_moves(move):
    """A later run, a refit or a new STPASA run must not be served from cache."""
    coordinator = _fresh_coordinator()
    store = _CountingStore(fit_generation=1)
    sensors = _make_region_sensors(coordinator, store)
    counter = _count_calibrations(sensors)
    d = _make_price_data(RUN_DT, intervals=48)

    sensors[0]._calibrated_forecast(d)
    assert counter["calls"] == 48

    sensors[0]._calibrated_forecast(move(coordinator, store, d))
    assert counter["calls"] == 96


def test_regions_do_not_share_cache_entries():
    """One coordinator per region in production, but keep the key region safe."""
    coordinator = _fresh_coordinator()
    qld = _make_region_sensors(coordinator, _CountingStore(), region="QLD1")[0]
    nsw = _make_region_sensors(coordinator, _CountingStore(), region="NSW1")[0]
    counter = _count_calibrations([qld, nsw])
    d = _make_price_data(RUN_DT, intervals=48)

    qld_result = qld._calibrated_forecast(d)
    nsw_result = nsw._calibrated_forecast(d)

    assert counter["calls"] == 96
    assert nsw_result is not qld_result


def test_every_builder_that_publishes_a_band_publishes_band_source():
    """Issue #100: PD7DayDataSensor builds its per-interval dict from its own
    literal rather than the shared ATTR_CAL_* update, so band_source, added
    to the other two builders in PR #96, never reached it. Any builder
    exposing p10 and p90 must also expose band_source."""
    run_at = datetime(2026, 9, 3, 7, 30, tzinfo=NEM_TZ)
    price_data = _make_price_data(run_at, intervals=8)
    coordinator = _fresh_coordinator()
    coordinator.data = MagicMock()
    coordinator.data.prices = {REGION: price_data}
    store = MagicMock()
    store.fit_generation = 1
    store.observation_count = 100
    store.active_bucket_count = 5
    sensors = _make_region_sensors(coordinator, store)

    fake_cal = {
        "calibrated": 0.12,
        "p10": 0.08,
        "p50": 0.12,
        "p90": 0.20,
        "ols_mae": 0.01,
        "calibrated_source": "isotonic+stpasa",
        _const_mod.ATTR_CAL_BAND_SOURCE: "stage2_residual",
        "n_obs": 100,
    }

    # Patch the globals the methods resolve against, which are those of the
    # module object the classes were defined in.
    method_globals = sensor_module.PD7DayForecastSensor._calibrate_period.__globals__
    with patch.dict(method_globals, {
        "calibrate_interval": lambda *a, **k: dict(fake_cal),
        "_amber_express_cutoff": lambda: run_at - timedelta(days=1),
    }):
        for s in sensors:
            entries = s.extra_state_attributes["forecast"]
            assert entries, type(s).__name__
            entry = entries[0]
            assert _const_mod.ATTR_CAL_P10 in entry and _const_mod.ATTR_CAL_P90 in entry, type(s).__name__
            assert entry.get(_const_mod.ATTR_CAL_BAND_SOURCE) == "stage2_residual", (
                f"{type(s).__name__} publishes p10/p90 without band_source: {sorted(entry)}"
            )


# ── Warm before write (#58) ──────────────────────────────────────────────────


async def _via_coordinator_update(sensor, hass):
    sensor._handle_coordinator_update()
    await asyncio.gather(*hass.tasks)


async def _via_added_to_hass(sensor, hass):
    """add_to_platform_finish: await the hook, then the platform writes."""
    await sensor.async_added_to_hass()


_WARM_ENTRY_POINTS = [
    pytest.param(_via_coordinator_update, id="coordinator_update"),
    pytest.param(_via_added_to_hass, id="added_to_hass"),
]


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_no_calibration_happens_inside_the_state_write(cls):
    """The write must find a warm memo, whatever entity triggers it."""

    async def scenario():
        sensor, hass, counter, _store, _d = _setup(cls)

        sensor._handle_coordinator_update()
        assert sensor.writes == 0, "state written before the memo was warmed"
        await asyncio.gather(*hass.tasks)

        assert sensor.writes == 1
        assert counter.calls == INTERVALS, (
            f"expected one warming pass over {INTERVALS} intervals, got {counter.calls}"
        )
        assert sensor.calibrations_during_write == 0, (
            "the state write still recalibrated "
            f"{sensor.calibrations_during_write} intervals on the event loop"
        )

    _run(scenario())


@pytest.mark.parametrize("enter", _WARM_ENTRY_POINTS)
@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_warming_runs_off_the_event_loop(cls, enter):
    """Calibration executes on a worker thread, not the loop thread, whether
    the coordinator update or the platform's first write triggers the warm.
    Warming during setup must not simply move the stall into setup."""

    async def scenario():
        sensor, hass, counter, _store, _d = _setup(cls)
        loop_thread = threading.current_thread().name

        await enter(sensor, hass)

        assert hass.executor_calls == 1
        assert counter.calls == INTERVALS
        assert counter.threads, "no calibration ran at all"
        assert loop_thread not in counter.threads, (
            f"calibration ran on the event loop thread {loop_thread}"
        )

    _run(scenario())


def test_a_warm_memo_costs_no_calibration_on_later_writes():
    """The 30 minute tick and 5 minute dispatch writes stay free."""

    async def scenario():
        sensor, hass, counter, _store, _d = _setup(sensor_module.PD7DayDataSensor)

        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)
        assert counter.calls == INTERVALS

        hass.tasks.clear()
        for _ in range(5):
            sensor._schedule_warm_state_write()
        await asyncio.gather(*hass.tasks)

        assert counter.calls == INTERVALS, "a warm memo recalibrated on a subsequent write"
        assert sensor.writes == 6

    _run(scenario())


def test_a_new_run_is_warmed_rather_than_paid_for_in_the_write():
    """A fresh PD7DAY run invalidates the memo; warming absorbs the rebuild."""

    async def scenario():
        sensor, hass, counter, _store, _d = _setup(sensor_module.PD7DayDataSensor)

        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)
        assert counter.calls == INTERVALS

        # New run, as in production 30 minutes later.
        sensor.coordinator.data.prices = {
            REGION: _make_price_data(RUN_DT + timedelta(minutes=30), intervals=INTERVALS)
        }
        hass.tasks.clear()
        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)

        assert counter.calls == 2 * INTERVALS, "the new run was not warmed"
        assert sensor.calibrations_during_write == 0, (
            "the new run's rebuild landed inside the state write"
        )

    _run(scenario())


def test_warm_failure_still_writes_state_and_keeps_the_lazy_fallback():
    """A broken warm must degrade to the old behaviour, not drop the state.

    The cold cost it falls back to is one calibration per interval, which is
    also what establishes that calibrations_during_write measures something.
    """

    async def scenario():
        sensor, hass, _counter, _store, _d = _setup(sensor_module.PD7DayDataSensor)

        async def _broken(func, *args):
            raise RuntimeError("executor unavailable")

        hass.async_add_executor_job = _broken

        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)

        assert sensor.writes == 1, "state was not written after a failed warm"
        # The lazy path inside the attribute build still produced the forecast,
        # which is the correctness fallback the mixin relies on.
        assert sensor.calibrations_during_write == INTERVALS

    _run(scenario())


def test_every_calibrated_sensor_uses_the_warm_write_path():
    """A new calibration-backed sensor cannot quietly skip the mixin."""
    mixin = sensor_module.CalibratedWriteMixin
    users = [
        obj
        for _, obj in inspect.getmembers(sensor_module, inspect.isclass)
        if getattr(obj, "__module__", "") == sensor_module.__name__
        and hasattr(obj, "_calibrated_forecast")
    ]
    assert users, "no calibration-backed sensors found"
    for cls in users:
        assert issubclass(cls, mixin), (
            f"{cls.__name__} uses _calibrated_forecast but writes state without "
            "warming it, which puts the calibration back on the event loop"
        )


# ── The platform's own first write (#60 cause 1, #61) ────────────────────────


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_added_to_hass_warms_before_the_platform_writes_state(cls):
    """Reproduces add_to_platform_finish, which does exactly this and nothing
    in between:

        await self.async_internal_added_to_hass()
        await self.async_added_to_hass()
        self.async_write_ha_state()
    """

    async def scenario():
        sensor, hass, _counter, _store, _d = _setup(cls)

        await sensor.async_added_to_hass()
        sensor.async_write_ha_state()

        assert sensor.writes == 1
        assert sensor.calibrations_during_write == 0, (
            "the platform's first state write still had to calibrate "
            f"{sensor.calibrations_during_write} intervals on the event loop"
        )
        assert hass.executor_calls >= 1, "the warm did not use the executor"

    _run(scenario())


def test_the_hook_is_reached_through_every_subclass_override():
    """Two of the three classes override async_added_to_hass themselves and
    reach the warm only through ``await super().async_added_to_hass()``.
    Reordering the bases, or dropping that call while adding a subscription,
    would silently stop the warming and nothing else would fail."""
    mixin = sensor_module.CalibratedWriteMixin
    for cls in SHARING_CLASSES:
        mro = cls.__mro__
        assert mixin in mro, f"{cls.__name__} lost CalibratedWriteMixin"
        own = cls.__dict__.get("async_added_to_hass")
        if own is None:
            assert cls.async_added_to_hass is mixin.async_added_to_hass
            continue
        # Overridden, so the mixin must sit between this class and
        # CoordinatorEntity for the super() call to land on it.
        assert mro.index(mixin) == mro.index(cls) + 1, (
            f"{cls.__name__} does not delegate to CalibratedWriteMixin next; "
            f"MRO is {[c.__name__ for c in mro[:4]]}"
        )


# ── The key moving while the warm is in flight (#60 cause 2, #61) ────────────


def test_a_refit_landing_during_the_warm_is_re_warmed():
    """The 6.766 s write. One bump, so the second warm should settle it."""

    async def scenario():
        sensor, hass, counter, store, _d = _setup(sensor_module.PD7DayForecastSensor)
        _install_key_move(sensor, store, counter, at_call=0, times=1)

        await sensor._async_warm_then_write()

        assert sensor.writes == 1
        assert sensor.calibrations_during_write == 0, (
            "the write still paid for "
            f"{sensor.calibrations_during_write} calibrations on the loop "
            "after a refit moved the key mid-warm"
        )
        assert hass.executor_calls == 2, (
            f"expected one re-warm, got {hass.executor_calls} executor calls"
        )

    _run(scenario())


def test_re_warming_is_bounded_and_state_is_still_written():
    """If the inputs never settle, give up and write rather than spin."""

    async def scenario():
        sensor, hass, counter, store, _d = _setup(sensor_module.PD7DayForecastSensor)
        # Bump forever: the key is never current when checked.
        _install_key_move(sensor, store, counter, at_call=0, times=10**6)

        await sensor._async_warm_then_write()

        assert hass.executor_calls == MAX_ATTEMPTS, (
            f"expected at most {MAX_ATTEMPTS} warm attempts, got {hass.executor_calls}"
        )
        assert sensor.writes == 1, "state must still be written after giving up"

    _run(scenario())


def test_a_settled_key_costs_exactly_one_warm():
    """The ordinary case must not pay for the retry machinery."""

    async def scenario():
        sensor, hass, _counter, _store, _d = _setup(sensor_module.PD7DayForecastSensor)

        await sensor._async_warm_then_write()

        assert hass.executor_calls == 1, f"expected a single warm, got {hass.executor_calls}"
        assert sensor.calibrations_during_write == 0

    _run(scenario())


def _bump_fit_generation(sensor, store):
    store.fit_generation += 1


def _refetch_stpasa(sensor, store):
    # Any STPASA refetch moves this, including a same-run refetch, because the
    # coordinator key is "run_datetime|fetched_at".
    sensor.coordinator._stpasa_index_run = "stpasa-run-1|refetched"


@pytest.mark.parametrize(
    "move", [_bump_fit_generation, _refetch_stpasa], ids=["fit_generation", "stpasa_index"]
)
def test_currency_tracks_the_memo_key_inputs(move):
    sensor, _hass, _counter, store, d = _setup(sensor_module.PD7DayForecastSensor)

    assert sensor._calibrated_cache_is_current() is False, "cold memo is not current"

    sensor._calibrated_forecast(d)
    assert sensor._calibrated_cache_is_current() is True

    move(sensor, store)
    assert sensor._calibrated_cache_is_current() is False, (
        "a moved key input must invalidate the currency check"
    )


def test_currency_is_true_when_there_is_nothing_to_calibrate():
    """No price data means the write cannot pay for a rebuild, so do not spin."""
    sensor, hass, _counter, _store, _d = _setup(sensor_module.PD7DayForecastSensor)
    sensor.coordinator.data.prices = {}
    # The shared write stub measures what the attribute build would cost, which
    # needs price data. There is none here, so just count the write.
    sensor.async_write_ha_state = lambda: setattr(sensor, "writes", sensor.writes + 1)

    assert sensor._calibrated_cache_is_current() is True

    async def scenario():
        await sensor._async_warm_then_write()
        assert hass.executor_calls == 0, "nothing to warm, so no executor work"
        assert sensor.writes == 1

    _run(scenario())


# ── The key TOCTOU (#60, PR #76) ─────────────────────────────────────────────


class _PublicationLog(dict):
    """A memo dict that records the key in force at each publication.

    ``_calibrated_forecast`` and the warm both write ``cache[region] = (key,
    value)``. This records, for every such write, the key the writer used and
    the key that was actually live at that instant. The two must match, or the
    memo is holding a list that its own key does not describe.
    """

    def __init__(self, live_key) -> None:
        super().__init__()
        self._live_key = live_key
        self.publications: list[tuple] = []

    def __setitem__(self, region, entry) -> None:
        self.publications.append((entry[0], self._live_key()))
        super().__setitem__(region, entry)

    @property
    def stale_publications(self) -> list[tuple]:
        return [(used, live) for used, live in self.publications if used != live]


def _setup_with_log(cls, region: str = REGION):
    """A sharing sensor whose memo records every publication."""
    sensor, hass, counter, store, d = _setup(cls, region=region)
    log = _PublicationLog(lambda: sensor._calibrated_forecast_key(sensor._price_data))
    sensor.coordinator._calibrated_forecast_cache = log
    return sensor, hass, counter, store, d, log


def test_the_warm_never_publishes_under_a_key_that_has_already_moved():
    """A refit landing inside the warm must not produce a mislabelled entry.

    Before the fix the key was taken inside the executor job, so the pass that
    began at generation 1 and ended at generation 2 stored its half and half
    result under the generation 1 key, unconditionally.
    """
    sensor, _hass, counter, store, _d, log = _setup_with_log(sensor_module.PD7DayForecastSensor)
    _install_key_move(sensor, store, counter, at_call=INTERVALS // 2, times=1)

    _run(sensor._async_warm_until_current())

    assert log.publications, "the warm published nothing at all"
    assert log.stale_publications == [], (
        f"the memo was published under a key that had already moved: {log.stale_publications}"
    )


def test_a_late_warm_does_not_clobber_a_fresher_memo_entry():
    """One slot per region, three entities. The late arrival must not win.

    The sequence in #60 exactly: this entity's warm starts, a refit lands, a
    sibling entity of the same region completes its own warm and publishes
    the current entry, and only then does this entity's pass finish. The
    result it holds was computed under the superseded key, so it must be
    discarded rather than written over the sibling's.
    """
    sensor, _hass, counter, store, d, log = _setup_with_log(sensor_module.PD7DayForecastSensor)
    fresh_value = [{"time": "published by the sibling entity"}]
    state = {"done": False}

    def _sibling_publishes(_call_no: int) -> None:
        if state["done"]:
            return
        state["done"] = True
        store.fit_generation += 1
        log[REGION] = (sensor._calibrated_forecast_key(d), fresh_value)

    sensor._calibrate_period = _HookedCounter(counter, _sibling_publishes)

    _run(sensor._async_warm_calibrated_forecast())

    assert state["done"], "the sibling never got to publish, the test is vacuous"
    _key, value = log[REGION]
    assert value is fresh_value, (
        "a warm that started before the refit overwrote the entry the sibling "
        "entity published after it"
    )
    assert log.stale_publications == [], (
        f"stale publication into the shared slot: {log.stale_publications}"
    )


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_the_warm_key_and_the_write_key_are_the_same_key(cls):
    """Capture both ends across a simulated interval rollover and compare.

    The 30 minute boundary is where the new run and the refit it triggers land
    together, so it is where the key moves while a warm is in flight. This
    drives the whole path an entity takes at that boundary,
    ``_async_warm_then_write``, and asserts the identity that makes the write
    cheap rather than asserting on elapsed time.
    """
    sensor, _hass, counter, store, _d, log = _setup_with_log(cls)
    _install_key_move(sensor, store, counter, at_call=1, times=1)

    seen = {}
    inner_write = sensor.async_write_ha_state

    def _write():
        seen["write_key"] = sensor._calibrated_forecast_key(sensor._price_data)
        inner_write()

    sensor.async_write_ha_state = _write

    _run(sensor._async_warm_then_write())

    assert sensor.writes == 1
    assert log.publications, "nothing was ever published"
    warm_key = log.publications[-1][0]
    assert warm_key == seen["write_key"], (
        f"the warm published under {warm_key} but the write asked for {seen['write_key']}"
    )
    assert sensor.calibrations_during_write == 0, (
        f"{sensor.calibrations_during_write} intervals were calibrated on the "
        "event loop inside the state write"
    )
    assert log.stale_publications == []


@pytest.mark.parametrize(
    "cls,at_call,times",
    list(itertools.product(
        SHARING_CLASSES,
        # First interval of the pass, the middle, and the last one, which is
        # the tightest case: the key moves after the final calibration and
        # before the publish.
        (1, INTERVALS // 2, 0),
        (1, 2),
    )),
    ids=lambda p: getattr(p, "__name__", str(p)),
)
def test_key_stability_sweep(cls, at_call, times):
    """The invariant must hold wherever in the pass the key moves, and however
    often, for every class sharing the slot. ``times`` of 2 exercises the
    re-warm: the key moves again during the retry, so the third attempt is the
    one that settles."""
    sensor, _hass, counter, store, _d, log = _setup_with_log(cls)
    state = _install_key_move(sensor, store, counter, at_call=at_call, times=times)

    _run(sensor._async_warm_until_current())

    assert state["moves"] == times, (
        f"the key only moved {state['moves']} times, expected {times}, so this "
        "case is not testing what it says it is"
    )
    assert log.stale_publications == [], (
        f"stale publications with the key moving at {at_call}: {log.stale_publications}"
    )
    # And the memo must have settled on the live key, so the write is free.
    live_key = sensor._calibrated_forecast_key(sensor._price_data)
    assert sensor._cached_calibrated_forecast(live_key) is not None, (
        "the memo did not settle on the live key within the attempt budget"
    )
    assert sensor.calibrations_during_write is None
    assert _count_during_write(sensor) == 0, (
        "the state write would still have to calibrate on the event loop"
    )


def test_the_executor_half_never_touches_the_memo():
    """``_calibrated_forecast_values`` must be a pure pass over the run.

    It is the half that runs off the loop. If it read or wrote the memo it
    would be making a publish decision from a worker thread, using state only
    the loop can read consistently, which is the bug.
    """
    sensor, _hass, _counter, _store, d, log = _setup_with_log(sensor_module.PD7DayForecastSensor)

    values = sensor._calibrated_forecast_values(d)

    assert len(values) == INTERVALS
    assert log.publications == [], "the executor half wrote to the memo"
    assert REGION not in log


def test_the_shared_implementations_are_shared_not_reimplemented():
    """All three classes must use one key builder and one values builder.

    A second copy of either would drift, and the currency check would then be
    comparing keys that were never meant to be equal.
    """
    for name in ("_calibrated_forecast_key", "_calibrated_forecast_values", "_calibrated_forecast"):
        base = getattr(sensor_module.PD7DayForecastSensor, name)
        for cls in SHARING_CLASSES:
            assert getattr(cls, name) is base, f"{cls.__name__} has its own {name}, which will drift"


def test_a_warm_hit_costs_no_executor_work():
    """A second warm for an unchanged key must not recalibrate anything.

    The five minute dispatch listener routes every write through the warm, so
    the hit path has to be free. Taking the key before the executor hop rather
    than inside it is what makes this possible.
    """
    sensor, hass, counter, _store, _d, _log = _setup_with_log(sensor_module.PD7DayForecastSensor)

    _run(sensor._async_warm_until_current())
    calls_after_first = counter.calls
    executor_after_first = hass.executor_calls
    assert calls_after_first == INTERVALS
    assert executor_after_first >= 1

    _run(sensor._async_warm_until_current())

    assert counter.calls == calls_after_first, "the warm recalibrated a live memo"
    assert hass.executor_calls == executor_after_first, (
        "the warm paid for an executor round trip on a memo hit"
    )


class _OrderRecordingCoordinator:
    """Just enough of PD7DayCoordinator to run the real ``stpasa_index``.

    Records the order in which the three index attributes are assigned. The
    run key is the freshness token the memo key folds in, so it has to be
    published after the data it names, or a reader on the other thread can
    pair the new key with the old index and memoise a forecast that nothing
    will ever recompute.
    """

    stpasa_index = PD7DayCoordinator.stpasa_index

    def __init__(self, store) -> None:
        object.__setattr__(self, "assignments", [])
        self._stpasa_store = store
        self._stpasa_index_run = None
        self._stpasa_index_map = {}
        self._stpasa_index_sorted = []

    def __setattr__(self, name, value) -> None:
        if name.startswith("_stpasa_index"):
            self.assignments.append(name)
        object.__setattr__(self, name, value)


def test_stpasa_index_publishes_the_run_key_last():
    """The STPASA index half of the same TOCTOU."""
    interval = StpasaInterval(
        interval_datetime="2026-09-01T18:30:00+10:00",
        run_datetime="2026-09-01T18:00:00+10:00",
        demand10=7400.0,
        demand50=7000.0,
        demand90=6600.0,
        surpluscapacity=4941.0,
        ss_solar_uigf=120.0,
        ss_wind_uigf=900.0,
    )
    result = StpasaResult(
        region=REGION,
        run_datetime="2026-09-01T18:00:00+10:00",
        intervals=[interval],
        fetched_at="2026-09-01T08:00:30+00:00",
    )

    class _Store:
        def latest(self):
            return result

    coordinator = _OrderRecordingCoordinator(_Store())
    coordinator.assignments.clear()

    coordinator.stpasa_index()

    rebuild = [a for a in coordinator.assignments if a.startswith("_stpasa_index")]
    assert rebuild, "the index was never rebuilt, so nothing was measured"
    assert rebuild[-1] == "_stpasa_index_run", (
        "the run key was published before the index it names, leaving a window "
        f"for a torn read: {rebuild}"
    )
    assert coordinator._stpasa_index_map, "the index is empty"


# ── Tariff spot memo (#62 item 4) ────────────────────────────────────────────


def full_run(n_intervals: int = 336):
    """A seven day run spanning every branch of the calibration pipeline.

    The price choices include values below the fitted domain, which take
    isotonic_below_domain, and a spike above SPIKE_THRESHOLD. The STPASA gap
    leaves in band intervals with no features, which must degrade to isotonic
    only on the memoised and the unmemoised path alike.
    """
    run_dt = parse_iso(RUN_AT)
    rng = random.Random(11)
    periods = []
    stpasa = []
    for i in range(n_intervals):
        start_dt = run_dt + timedelta(minutes=30 * (i + 1))
        # -0.15 is below the fitted domain of every bucket and 3.4 is above
        # SPIKE_THRESHOLD, so the sweep reaches both ends of the pipeline.
        value = rng.choice([-0.15, -0.05, -0.00757, 0.0, 0.03, 0.12093, 0.52396, 3.4])
        periods.append(make_period(start_dt, value))
        # The STPASA gap sits over peak hours of an in band day so that some
        # fitted buckets get no features and take the isotonic only branch.
        # Moving it moves the branch coverage the sweep asserts.
        if not 118 <= i < 132:
            stpasa.append(make_stpasa_interval(start_dt, solar=rng.uniform(0.0, 4000.0)))
    return periods, stpasa


def prime(sensor) -> None:
    """Populate the caches __init__ populates, for a __new__ built sensor."""
    sensor._tariff_cache = None
    sensor._period_tariff_cache = None
    sensor._cached_tariff_periods = sensor._get_tariff_periods()
    if hasattr(sensor, "_get_daily_supply_charge"):
        sensor._cached_daily_supply_charge = sensor._get_daily_supply_charge()


def build(region: str = "QLD1"):
    periods, stpasa = full_run()
    forecast, tariff, export, coord, store = make_sensors(periods, stpasa, region)
    for s in (tariff, export):
        prime(s)
    return periods, forecast, tariff, export, coord, store


def days27_of(tariff):
    """A day 2 to 7 sensor sharing the import sensor's coordinator and store."""
    cls = _tariff_mod.TariffForecastDays27Sensor
    sensor = cls.__new__(cls)
    sensor.__dict__.update(tariff.__dict__)
    return sensor


def attrs_of(sensor, memoised: bool):
    """The forecast attribute list, with the memo either used or bypassed."""
    library = (
        "spot_to_feed_in_tariff"
        if isinstance(sensor, _tariff_mod.NemPd7dayExportTariffSensor)
        else "spot_to_tariff"
    )
    with patch.object(_tariff_mod, library, return_value=15.5):
        if memoised:
            return sensor.extra_state_attributes["forecast"]
        with patch.object(type(sensor), "_calibrated_spot_map", lambda self, d: None):
            return sensor.extra_state_attributes["forecast"]


def clear_memos(coord) -> None:
    coord._calibrated_forecast_cache = {}
    coord._calibrated_spot_cache = {}


@contextlib.contextmanager
def counting_apply(store):
    """Count CalibrationStore.apply_to_price calls without changing results."""
    real = store.apply_to_price
    calls = []

    def wrapper(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    store.apply_to_price = wrapper
    try:
        yield calls
    finally:
        store.apply_to_price = real


def sources_over_run(forecast_sensor) -> dict:
    counts: dict = {}
    for entry in forecast_sensor._calibrated_forecast(forecast_sensor._price_data):
        src = entry.get("calibrated_source")
        counts[src] = counts.get(src, 0) + 1
    return counts


def test_sweep_reaches_every_calibration_branch():
    """Without this the equality assertions below would prove very little."""
    _periods, forecast, _tariff, _export, _coord, _store = build()
    counts = sources_over_run(forecast)
    for branch in ("passthrough", "isotonic_below_domain", "isotonic", "isotonic+stpasa"):
        assert counts.get(branch, 0) > 0, (
            f"the sweep never reached {branch}, so pinning tariff output over "
            f"it would be vacuous: {counts}"
        )


def test_memo_does_not_change_any_published_tariff_price():
    """Every attribute of every interval is identical with and without the memo.

    Compared as whole dicts rather than only on ``spot``, so a memo that moved
    ``value``, ``network_rate`` or ``spot_raw`` would fail here too.
    """
    _periods, _forecast, tariff, export, coord, _store = build()
    days27 = days27_of(tariff)
    for sensor in (tariff, days27, export):
        clear_memos(coord)
        unmemoised = attrs_of(sensor, memoised=False)
        clear_memos(coord)
        memoised = attrs_of(sensor, memoised=True)
        assert len(unmemoised) == len(memoised) > 0
        assert unmemoised == memoised, (
            f"{type(sensor).__name__} published different attributes with the memo in place"
        )


def test_memo_hit_and_miss_agree_interval_by_interval():
    """A memo hit equals the direct calibration for the same interval."""
    periods, _forecast, tariff, _export, _coord, _store = build()
    spot_map = tariff._calibrated_spot_map(tariff._price_data)
    assert spot_map, "no memo was built"
    for period in periods:
        assert tariff._calibrated_value_memoised(period, spot_map) == tariff._calibrated_value(period), (
            f"memo disagrees with the direct call at {period.time}"
        )


def test_memo_filled_by_the_forecast_sensor_gives_the_same_prices():
    """Reading the forecast memo must publish what a tariff build would.

    This is the shared slot case: the price forecast sensor has already been
    warmed for this run, so the tariff path takes its numbers from the list
    sensor.py memoised rather than calibrating anything.
    """
    _periods, forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    own = attrs_of(tariff, memoised=True)

    clear_memos(coord)
    forecast._calibrated_forecast(forecast._price_data)
    with counting_apply(store) as calls:
        shared = attrs_of(tariff, memoised=True)

    assert shared == own, "the forecast memo and a tariff build disagree"
    assert not calls, (
        f"the tariff write calibrated {len(calls)} intervals despite the "
        "forecast memo holding the run"
    )


def test_tariff_spot_still_equals_forecast_value():
    """Issue #66 parity holds whichever sensor fills the slot first."""
    _periods, forecast, tariff, _export, coord, _store = build()
    for label, warm_forecast_first in (("tariff first", False), ("forecast first", True)):
        clear_memos(coord)
        if warm_forecast_first:
            forecast._calibrated_forecast(forecast._price_data)
        entries = attrs_of(tariff, memoised=True)
        ff = {e["time"]: e for e in forecast._calibrated_forecast(forecast._price_data)}
        for entry in entries:
            g = ff[entry["time"]]
            assert entry["spot"] == round(g["value"], 6), (
                f"{label}: tariff spot {entry['spot']} against forecast "
                f"{round(g['value'], 6)} at {entry['time']}"
            )


def test_second_write_of_a_run_calibrates_nothing():
    """The reported defect: every write rebuilt the whole forecast."""
    _periods, _forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    with counting_apply(store) as calls:
        attrs_of(tariff, memoised=True)
        first = len(calls)
        calls.clear()
        attrs_of(tariff, memoised=True)
        second = len(calls)
    assert first > 300, f"the first write should calibrate the run, saw {first}"
    assert second == 0, f"the second write recalibrated {second} intervals"


def test_other_entities_of_the_region_reuse_the_slot():
    """22 tariff entities on the install, 5 regions: one build per region.

    The export class and the day 2 to 7 class carry their own attribute
    loops, so each is checked rather than assumed.
    """
    _periods, _forecast, tariff, export, coord, store = build()
    days27 = days27_of(tariff)
    clear_memos(coord)
    with counting_apply(store) as calls:
        attrs_of(tariff, memoised=True)
        assert len(calls) > 300
        for sensor in (export, days27):
            calls.clear()
            entries = attrs_of(sensor, memoised=True)
            assert entries, f"{type(sensor).__name__} published nothing"
            assert not calls, (
                f"{type(sensor).__name__} calibrated {len(calls)} intervals "
                "that the region had already calibrated"
            )


def test_a_refit_invalidates_the_tariff_slot():
    """A calibration refit moves every price, so the slot must not survive it."""
    _periods, _forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    before = attrs_of(tariff, memoised=True)

    # A refit bumps fit_generation. Drop the isotonic model at the same time so
    # the recomputed prices are visibly different, which is what makes the
    # assertion below meaningful rather than a tautology.
    store._calibration.iso_models = {}
    store._calibration.ols_models = {}
    store._fit_generation = 2

    with counting_apply(store) as calls:
        after = attrs_of(tariff, memoised=True)

    assert len(calls) > 300, "the refit did not force a rebuild"
    assert after != before, (
        "the refit changed the model but the published prices did not move, "
        "so this test is not proving invalidation"
    )


def _tariff_new_stpasa_index(tariff, coord):
    # Any STPASA refetch moves fetched_at and can move a stage-2 price.
    coord._stpasa_index_run = "a-different-stpasa-run|refetched"


def _tariff_new_run(tariff, coord):
    tariff._price_data.forecast_generated_at = "2026-09-01T05:00:00+10:00"


@pytest.mark.parametrize(
    "move", [_tariff_new_stpasa_index, _tariff_new_run], ids=["stpasa_index", "new_run"]
)
def test_a_moved_key_input_invalidates_the_tariff_slot(move):
    """A new STPASA index or a new PD7DAY run must not be priced from the old slot."""
    _periods, _forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    attrs_of(tariff, memoised=True)
    move(tariff, coord)
    with counting_apply(store) as calls:
        attrs_of(tariff, memoised=True)
    assert len(calls) > 300, "a moved key input did not force a rebuild"


def test_an_interval_absent_from_the_memo_is_calibrated_not_nulled():
    """A memo miss must never be published as none.

    Returning none for an interval the memo simply does not carry would be the
    missing data rule applied to the wrong thing: there is an honest calibrated
    price available, the memo just does not hold it.
    """
    periods, _forecast, tariff, _export, _coord, _store = build()
    period = periods[0]
    direct = tariff._calibrated_value(period)
    assert direct is not None, "fixture interval does not calibrate, pick another"
    assert tariff._calibrated_value_memoised(period, {}) == direct
    assert tariff._calibrated_value_memoised(period, None) == direct


def test_no_store_means_no_memo_and_raw_passthrough():
    """With no calibration store there is nothing to memoise."""
    _periods, _forecast, tariff, _export, _coord, _store = build()
    tariff._store = None
    assert tariff._calibrated_spot_map(tariff._price_data) is None
    period = tariff._price_data.forecast[0]
    assert tariff._calibrated_value_memoised(period, None) == period.value


def test_memo_key_cannot_be_derived_inside_the_helper():
    """PR #76's rule: take the memo key on the loop and pass it down.

    Pinned as a signature requirement, because the defect PR #76 fixed was a
    key taken late and used to publish a result computed under a key that had
    already moved. A default here would let a caller stop passing one.
    """
    sig = inspect.signature(calibration_inputs.calibrated_spot_map)
    assert "key" in sig.parameters, "the memo helper lost its key argument"
    assert sig.parameters["key"].default is inspect.Parameter.empty, (
        "calibrated_spot_map must require the key from its caller"
    )


def test_one_key_implementation_is_shared_with_sensor_py():
    """sensor.py and the tariff path must agree on what makes the slot stale."""
    _periods, forecast, tariff, _export, coord, store = build()
    d = tariff._price_data
    assert forecast._calibrated_forecast_key(d) == calibration_inputs.calibrated_forecast_key(
        coord, store, tariff._region, d
    ), "the forecast sensor's key and the shared key builder disagree"
    assert sensor_module.PD7DayForecastSensor._calibrated_forecast_key.__doc__


# ── Per run feature cache (#135) ─────────────────────────────────────────────


@contextlib.contextmanager
def real_run_features(coord, periods, region):
    """Give the parity FakeCoordinator the real run-feature property, for the
    duration of one test: the patch is on the class, which every later
    ``make_sensors`` call shares."""
    coord._regions = [region]
    run_dt = parse_iso(RUN_AT)
    coord.data.interconnectors = {
        "NSW1-QLD1": types.SimpleNamespace(forecast=[
            types.SimpleNamespace(time=p.time, mwflow=-150.0) for p in periods
        ])
    }
    coord.data.market_summary = types.SimpleNamespace(forecast=[
        types.SimpleNamespace(nemtime=(run_dt + timedelta(days=d)).isoformat(), value_tj=70.0)
        for d in range(8)
    ])
    with patch.object(type(coord), "current_run_features", PD7DayCoordinator.current_run_features), \
         patch.object(type(coord), "_compute_run_features",
                      staticmethod(PD7DayCoordinator._compute_run_features), create=True):
        yield


def test_cold_tariff_write_computes_run_features_once():
    periods, _forecast, tariff, _export, coord, _store = build("QLD1")
    calls = []
    real = PD7DayCoordinator._compute_run_features

    def counting(price_data):
        calls.append(1)
        return real(price_data)

    with real_run_features(coord, periods, "QLD1"), \
         patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5), \
         patch.object(type(coord), "_compute_run_features", staticmethod(counting)):
        clear_memos(coord)
        attrs = tariff.extra_state_attributes
        assert attrs["forecast"], "the write must still publish a forecast"
        assert len(calls) == 1, (
            f"a cold write of {len(periods)} intervals computed run features "
            f"{len(calls)} times; the per-run cache should make it once"
        )
        # A warm write, and a second cold one for the same run, add nothing.
        tariff.extra_state_attributes
        clear_memos(coord)
        tariff.extra_state_attributes
        assert len(calls) == 1


def test_run_features_recompute_when_the_run_changes():
    periods, _forecast, _tariff, _export, coord, _store = build("QLD1")
    with real_run_features(coord, periods, "QLD1"):
        first = coord.current_run_features
        assert first is coord.current_run_features, "same run must return the cached object"
        price_data = coord.data.prices["QLD1"]
        # A new stamp near the fixture's own run, not a calendar date: the run
        # features need rows within 24 h of the stamp, and the fixture is
        # anchored to now, so a fixed date ages out and the property returns None.
        price_data.forecast_generated_at = to_nem_iso(parse_iso(RUN_AT) + timedelta(minutes=30))
        second = coord.current_run_features
        assert second is not first, "a new run stamp must recompute"
        # An interval count change on the same stamp (a refetched file) recomputes too.
        price_data.forecast = price_data.forecast[:-1]
        assert coord.current_run_features is not second
