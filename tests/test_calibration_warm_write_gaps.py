"""
The two holes that #58's warm-before-write left open, measured on v3.3.1.

#58 moved the calibration off the event loop for coordinator driven writes and
the tests in test_calibration_warm_write.py cover that. On the live five region
install it still logged slow writes, 16 of them in the 13 minutes after a
restart against 11 for the version it replaced, with a worst case an order of
magnitude worse:

    17:56:11  0.456s  PD7DayDataSensor      sensor.nem_pd7day_vic1_nem_vic1_pd7day_data
    17:56:12  0.500s  PD7DayDataSensor      sensor.nem_pd7day_nsw1_nem_nsw1_pd7day_data
    18:00:18  6.766s  PD7DayForecastSensor  sensor.nem_pd7day_tas1_price_forecast

Two distinct causes, one test class each.

**The first write of an entity's life never reached the mixin.** Entities are
added with ``update_before_add=True`` and Home Assistant's
``Entity.add_to_platform_finish`` calls ``async_write_ha_state()`` directly
after awaiting ``async_added_to_hass()``. That write does not go through
``_handle_coordinator_update``, so it always ran the lazy path. That is the
17:56 cluster, and it is the exact condition #55 was reported from.

**The memo key could move while the warm was in flight.** The key folds in the
STPASA index key and ``CalibrationStore.fit_generation``. A refit triggered by
the same new run that prompted the write bumps the generation, and a warm takes
long enough for that to land, so the write missed the memo it had just
populated and recalibrated on the loop anyway. That is the 18:00 cluster.

Reuses the fixtures from test_calibration_warm_write, which reuses the Home
Assistant stub preamble installed by test_sensor.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import test_calibration_memo as tcm
import test_calibration_warm_write as twc
import test_sensor as ts  # noqa: F401 - installs the HA stubs

import custom_components.nem_pd7day.sensor as sensor_module

NEM_TZ = timezone(timedelta(hours=10))
INTERVALS = 357  # the run length carried by the install these were measured on

# Resolve through the module object, not a top-level import. Several test
# modules reload this module under the same name via importlib, so a class
# captured at collection time is not necessarily the one the module holds when
# the test body runs.
SHARING_CLASSES = (
    sensor_module.PD7DayForecastSensor,
    sensor_module.SpotPriceForecastDays27Sensor,
    sensor_module.PD7DayDataSensor,
)
MAX_ATTEMPTS = sensor_module._MAX_CALIBRATION_WARM_ATTEMPTS


def _setup(cls, region="QLD1"):
    """A sharing sensor with a cold memo and a 357 interval run, as measured."""
    coordinator = tcm._fresh_coordinator()
    store = tcm._CountingStore()
    hass = twc._FakeHass()
    d = tcm._make_price_data(
        datetime(2026, 9, 1, 18, 0, tzinfo=NEM_TZ), intervals=INTERVALS
    )
    coordinator.data = MagicMock()
    coordinator.data.prices = {region: d}
    sensor = twc._make_sensor(cls, coordinator, store, hass, region=region)
    # The real async_added_to_hass on two of the three classes subscribes to a
    # dispatch coordinator when the entry carries one. Leave it absent so these
    # tests exercise the warm and nothing else.
    sensor._entry = SimpleNamespace(runtime_data=None)
    sensor.async_on_remove = lambda _cb: None
    counter = twc._instrument(sensor)
    return sensor, hass, counter, store, d


class _BumpingCounter:
    """Moves the memo key the way a refit landing mid-warm does.

    Wraps the shared counting stub and bumps ``fit_generation`` as each
    calibration pass finishes, which happens inside the executor. That is when
    a refit triggered by the same new run actually lands. Delegates ``calls``
    and ``threads`` so the shared helpers still see a counter.
    """

    def __init__(self, inner, store, times):
        self._inner = inner
        self._store = store
        self._times = times
        self.passes = 0

    @property
    def calls(self):
        return self._inner.calls

    @property
    def threads(self):
        return self._inner.threads

    def __call__(self, period, run_at_str):
        result = self._inner(period, run_at_str)
        if self._inner.calls % INTERVALS == 0 and self.passes < self._times:
            self.passes += 1
            self._store.fit_generation += 1
        return result


def _bump_generation_after_each_pass(sensor, store, counter, times):
    bumping = _BumpingCounter(counter, store, times)
    sensor._calibrate_period = bumping
    return bumping


# ── Cause 1: the platform's own first write ──────────────────────────────────


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_the_first_write_would_be_slow_without_the_hook(cls):
    """Establishes that the guard below is measuring something real.

    A freshly added entity with a cold memo pays for every interval. If this
    ever reads 0 the test underneath it has stopped proving anything.
    """
    sensor, _hass, counter, _store, _d = _setup(cls)

    assert counter.calls == 0
    cost = twc._count_during_write(sensor)

    assert cost == INTERVALS, (
        "a cold memo should cost one calibration per interval at write time, "
        f"got {cost}"
    )


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_added_to_hass_warms_before_the_platform_writes_state(cls):
    """Reproduces add_to_platform_finish: await the hook, then write.

    Home Assistant does exactly this and nothing in between:

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

    twc._run(scenario)


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_the_first_write_warm_runs_off_the_event_loop(cls):
    """Warming during setup must not simply move the stall into setup."""

    async def scenario():
        sensor, _hass, counter, _store, _d = _setup(cls)

        await sensor.async_added_to_hass()

        assert counter.calls == INTERVALS
        assert counter.threads and "MainThread" not in counter.threads, (
            f"calibration ran on {counter.threads}, expected a worker thread"
        )

    twc._run(scenario)


def test_the_hook_is_reached_through_every_subclass_override():
    """Two of the three classes override async_added_to_hass themselves.

    They reach the warm only through ``await super().async_added_to_hass()``.
    Reordering the bases, or dropping that call while adding a subscription,
    would silently stop the warming and nothing else would fail.
    """
    mixin = sensor_module.CalibratedWriteMixin
    for cls in SHARING_CLASSES:
        mro = cls.__mro__
        assert mixin in mro, f"{cls.__name__} lost CalibratedWriteMixin"
        own = cls.__dict__.get("async_added_to_hass")
        if own is None:
            # Inherits the mixin's implementation directly.
            assert cls.async_added_to_hass is mixin.async_added_to_hass
            continue
        # Overridden, so the mixin must sit between this class and
        # CoordinatorEntity for the super() call to land on it.
        assert mro.index(mixin) == mro.index(cls) + 1, (
            f"{cls.__name__} does not delegate to CalibratedWriteMixin next; "
            f"MRO is {[c.__name__ for c in mro[:4]]}"
        )


# ── Cause 2: the key moving while the warm is in flight ──────────────────────


def test_a_refit_landing_during_the_warm_is_re_warmed():
    """The 6.766 s write. One bump, so the second warm should settle it."""

    async def scenario():
        sensor, hass, counter, store, _d = _setup(
            sensor_module.PD7DayForecastSensor
        )
        _bump_generation_after_each_pass(sensor, store, counter, times=1)

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

    twc._run(scenario)


def test_re_warming_is_bounded_and_state_is_still_written():
    """If the inputs never settle, give up and write rather than spin."""

    async def scenario():
        sensor, hass, counter, store, _d = _setup(
            sensor_module.PD7DayForecastSensor
        )
        # Bump forever: the key is never current when checked.
        _bump_generation_after_each_pass(sensor, store, counter, times=10**6)

        await sensor._async_warm_then_write()

        assert hass.executor_calls == MAX_ATTEMPTS, (
            f"expected at most {MAX_ATTEMPTS} warm attempts, "
            f"got {hass.executor_calls}"
        )
        assert sensor.writes == 1, "state must still be written after giving up"

    twc._run(scenario)


def test_a_settled_key_costs_exactly_one_warm():
    """The ordinary case must not pay for the retry machinery."""

    async def scenario():
        sensor, hass, _counter, _store, _d = _setup(
            sensor_module.PD7DayForecastSensor
        )

        await sensor._async_warm_then_write()

        assert hass.executor_calls == 1, (
            f"expected a single warm, got {hass.executor_calls}"
        )
        assert sensor.calibrations_during_write == 0

    twc._run(scenario)


# ── The currency check itself ────────────────────────────────────────────────


def test_currency_tracks_the_fit_generation():
    sensor, _hass, _counter, store, d = _setup(sensor_module.PD7DayForecastSensor)

    assert sensor._calibrated_cache_is_current() is False, "cold memo is not current"

    sensor._calibrated_forecast(d)
    assert sensor._calibrated_cache_is_current() is True

    store.fit_generation += 1
    assert sensor._calibrated_cache_is_current() is False, (
        "a refit must invalidate the currency check"
    )


def test_currency_tracks_the_stpasa_index_run():
    sensor, _hass, _counter, _store, d = _setup(sensor_module.PD7DayForecastSensor)
    sensor._calibrated_forecast(d)
    assert sensor._calibrated_cache_is_current() is True

    # Any STPASA refetch moves this, including a same-run refetch, because the
    # coordinator key is "run_datetime|fetched_at".
    sensor.coordinator._stpasa_index_run = "stpasa-run-1|refetched"
    assert sensor._calibrated_cache_is_current() is False


def test_currency_is_true_when_there_is_nothing_to_calibrate():
    """No price data means the write cannot pay for a rebuild, so do not spin."""
    sensor, hass, _counter, _store, _d = _setup(sensor_module.PD7DayForecastSensor)
    sensor.coordinator.data.prices = {}
    # The shared write stub measures what the attribute build would cost, which
    # needs price data. There is none here, so just count the write.
    sensor.async_write_ha_state = lambda: setattr(
        sensor, "writes", sensor.writes + 1
    )

    assert sensor._calibrated_cache_is_current() is True

    async def scenario():
        await sensor._async_warm_then_write()
        assert hass.executor_calls == 0, "nothing to warm, so no executor work"
        assert sensor.writes == 1

    twc._run(scenario)


def test_the_check_and_the_memo_agree_on_the_key():
    """One key builder, used by both sides.

    If the currency check derived the key differently from the memo, it would
    either never settle or approve a stale entry. Assert the memo stores exactly
    what the check will ask for, for every sharing class.
    """
    for cls in SHARING_CLASSES:
        sensor, _hass, _counter, _store, d = _setup(cls)
        expected = sensor._calibrated_forecast_key(d)
        sensor._calibrated_forecast(d)
        stored_key, _value = sensor.coordinator._calibrated_forecast_cache["QLD1"]
        assert stored_key == expected, (
            f"{cls.__name__} stores {stored_key} but the check asks for {expected}"
        )


def test_the_key_builder_is_shared_not_reimplemented():
    base = sensor_module.PD7DayForecastSensor._calibrated_forecast_key
    for cls in SHARING_CLASSES:
        assert cls._calibrated_forecast_key is base, (
            f"{cls.__name__} has its own key builder, which will drift"
        )
