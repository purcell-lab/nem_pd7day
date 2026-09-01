"""
Calibration must not run inside a state write.

The memo from issue #35 is correct but lazy: whichever of the three sharing
entities wrote state first after an invalidation paid for the whole
recalibration, and it paid for it on the event loop inside
``async_write_ha_state()``. Home Assistant reported it on the live five region
install on every PD7DAY run:

    Updating state for sensor.nem_pd7day_nsw1_nem_nsw1_pd7day_data
    (PD7DayDataSensor) took 0.493 seconds.

Worst observed single write was 2.181 s just after a restart. All five regions
fired together because a new run invalidates every region's memo at once.

``CalibratedWriteMixin`` warms the memo in the executor and only then writes
state. These tests assert the property rather than the mechanism: no
calibration may happen while the state write is in progress.

Reuses the Home Assistant stub preamble installed by test_sensor.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import test_calibration_memo as tcm
import test_sensor as ts  # noqa: F401 - installs the HA stubs

from custom_components.nem_pd7day.sensor import (
    CalibratedWriteMixin,
    PD7DayDataSensor,
    PD7DayForecastSensor,
    SpotPriceForecastDays27Sensor,
)

NEM_TZ = timezone(timedelta(hours=10))

SHARING_CLASSES = (
    PD7DayForecastSensor,
    SpotPriceForecastDays27Sensor,
    PD7DayDataSensor,
)


class _FakeHass:
    """Runs executor jobs on a real worker thread, like Home Assistant does."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []
        self.executor_calls = 0

    async def async_add_executor_job(self, func, *args):
        self.executor_calls += 1
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def async_create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


def _make_sensor(cls, coordinator, store, hass, region="QLD1"):
    """Build a bare instance carrying only what the write path touches."""
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
    import threading

    def _wrapped(period, run_at_str):
        _wrapped.calls += 1
        _wrapped.threads.add(threading.current_thread().name)
        return {"time": period.time, "value": period.value}

    _wrapped.calls = 0
    _wrapped.threads = set()
    sensor._calibrate_period = _wrapped
    return _wrapped


def _setup(cls):
    coordinator = tcm._fresh_coordinator()
    store = tcm._CountingStore()
    hass = _FakeHass()
    d = tcm._make_price_data(
        datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=367
    )
    coordinator.data = MagicMock()
    coordinator.data.prices = {"QLD1": d}
    sensor = _make_sensor(cls, coordinator, store, hass)
    counter = _instrument(sensor)
    return sensor, hass, counter, d


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


# ── The headline property ─────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_no_calibration_happens_inside_the_state_write(cls):
    """The write must find a warm memo, whatever entity triggers it."""

    async def scenario():
        sensor, hass, counter, _ = _setup(cls)

        sensor._handle_coordinator_update()
        assert sensor.writes == 0, "state written before the memo was warmed"
        await asyncio.gather(*hass.tasks)

        assert sensor.writes == 1
        assert counter.calls == 367, (
            f"expected one warming pass over 367 intervals, got {counter.calls}"
        )
        assert sensor.calibrations_during_write == 0, (
            "the state write still recalibrated "
            f"{sensor.calibrations_during_write} intervals on the event loop"
        )

    _run(scenario)


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_warming_runs_off_the_event_loop(cls):
    """Calibration must execute on a worker thread, not the loop thread."""

    async def scenario():
        sensor, hass, counter, _ = _setup(cls)
        loop_thread = __import__("threading").current_thread().name

        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)

        assert hass.executor_calls == 1
        assert counter.threads, "no calibration ran at all"
        assert loop_thread not in counter.threads, (
            f"calibration ran on the event loop thread {loop_thread}"
        )

    _run(scenario)


def test_a_warm_memo_costs_no_calibration_on_later_writes():
    """The 30 minute tick and 5 minute dispatch writes stay free."""

    async def scenario():
        sensor, hass, counter, _ = _setup(PD7DayDataSensor)

        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)
        assert counter.calls == 367

        hass.tasks.clear()
        for _ in range(5):
            sensor._schedule_warm_state_write()
        await asyncio.gather(*hass.tasks)

        assert counter.calls == 367, (
            "a warm memo recalibrated on a subsequent write"
        )
        assert sensor.writes == 6

    _run(scenario)


def test_a_new_run_is_warmed_rather_than_paid_for_in_the_write():
    """A fresh PD7DAY run invalidates the memo; warming absorbs the rebuild."""

    async def scenario():
        sensor, hass, counter, _ = _setup(PD7DayDataSensor)

        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)
        assert counter.calls == 367

        # New run, same as production 30 minutes later.
        newer = tcm._make_price_data(
            datetime(2026, 5, 19, 14, 30, tzinfo=NEM_TZ), intervals=367
        )
        sensor.coordinator.data.prices = {"QLD1": newer}

        hass.tasks.clear()
        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)

        assert counter.calls == 734, "the new run was not warmed"
        assert sensor.calibrations_during_write == 0, (
            "the new run's rebuild landed inside the state write"
        )

    _run(scenario)


def test_warm_failure_still_writes_state_and_keeps_the_lazy_fallback():
    """A broken warm must degrade to the old behaviour, not drop the state."""

    async def scenario():
        sensor, hass, counter, _ = _setup(PD7DayDataSensor)

        async def _broken(func, *args):
            raise RuntimeError("executor unavailable")

        hass.async_add_executor_job = _broken

        sensor._handle_coordinator_update()
        await asyncio.gather(*hass.tasks)

        assert sensor.writes == 1, "state was not written after a failed warm"
        # The lazy path inside the attribute build still produced the forecast,
        # which is the correctness fallback the mixin relies on.
        assert sensor.calibrations_during_write == 367

    _run(scenario)


def test_every_calibrated_sensor_uses_the_warm_write_path():
    """A new calibration-backed sensor cannot quietly skip the mixin."""
    import inspect

    from custom_components.nem_pd7day import sensor as sensor_module

    # Resolve the mixin from the same module object being inspected. Several
    # test modules reload custom_components.nem_pd7day.sensor through importlib
    # under the same name, so a class imported at collection time is not
    # necessarily identical to the one in the module present at run time.
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
