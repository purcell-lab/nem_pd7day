"""
The calibrated memo must never be published under a key that has already moved.

#58 warmed the memo off the loop. #61 added two things on top: a warm in
``async_added_to_hass`` so the platform's own first write is covered, and a
bounded re-warm when the currency check fails. What #61 did not change is where
the key comes from. ``_calibrated_forecast`` built the key itself, inside the
executor job, at the start of a pass that then took roughly 0.4 s, and stored
the result under that key unconditionally when the pass finished.

That leaves a time of check to time of use window on the key itself, which is
what the second half of #60 is about:

  * ``_calibrate_period`` reads the calibration store live, so a
    ``fit_generation`` bump part way through a pass produces a list built from
    two different models and labels it with the key of the first.
  * The memo has one slot per region, shared by PD7DayForecastSensor,
    SpotPriceForecastDays27Sensor and PD7DayDataSensor. A warm that started
    before a refit and landed after it overwrote the current entry a sibling
    entity had just published. The next reader of that slot then paid for a
    full rebuild on the event loop, which is the 6.766 s write recorded at the
    18:00+10:00 boundary in #60.

The invariant these tests pin is not "the write was fast", which can pass on
timing luck. It is: every value published into the memo is published under the
key that is live at the moment of publication, so the key the warm used and the
key the write asks for are the same key. That is checked directly by recording
both at every publication.

The fix that makes it true is structural: take the key once, on the loop,
before the executor hop, compute the values with no memo access at all, then
publish back on the loop only if the key is still current.

Reuses the fixtures from test_calibration_warm_write and its gaps companion,
which reuse the Home Assistant stub preamble installed by test_sensor.
"""

from __future__ import annotations

import itertools

if __name__ == "__main__":  # pragma: no cover - standalone entry point only
    # conftest installs the parent package bootstrap that lets the integration
    # modules resolve their relative imports. Under pytest it is applied for us.
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import conftest  # noqa: F401

import pytest
import test_calibration_warm_write as twc
import test_calibration_warm_write_gaps as tcg
import test_sensor as ts  # noqa: F401 - installs the HA stubs

import custom_components.nem_pd7day.sensor as sensor_module
from custom_components.nem_pd7day.coordinator import PD7DayCoordinator
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult

INTERVALS = tcg.INTERVALS
REGION = "QLD1"

# Resolve through the module object, not a top-level import: several test
# modules reload this module under the same name via importlib.
SHARING_CLASSES = (
    sensor_module.PD7DayForecastSensor,
    sensor_module.SpotPriceForecastDays27Sensor,
    sensor_module.PD7DayDataSensor,
)


# ── Instrumentation ───────────────────────────────────────────────────────────


class _PublicationLog(dict):
    """A memo dict that records the key in force at each publication.

    ``_calibrated_forecast`` and the warm both write ``cache[region] = (key,
    value)``. This records, for every such write, the key the writer used and
    the key that was actually live at that instant. The two must match. If they
    ever differ, the memo is holding a list that its own key does not describe.
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


class _HookedCounter:
    """The shared counting calibration stub, with a hook after each interval.

    The hook is how the world moves underneath a warm in these tests. It runs
    inside the calibration pass, which is exactly where a refit or an STPASA
    refetch lands in production: not before the warm and not after it, but
    during. Delegates ``calls`` and ``threads`` so the shared helpers in
    test_calibration_warm_write still see a counter.
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

    ``at_call`` is a position within the pass, counted modulo the run length, so
    1 is the first interval and 0 is the last. A refit bumping the generation is
    the cheapest faithful way to move the memo key: it is one of the four places
    ``calibration_store`` increments it, and #60 names the refit triggered by the
    same new run as the one that lands inside the warm.
    """
    state = {"moves": 0}

    def _hook(call_no: int) -> None:
        if state["moves"] < times and call_no % INTERVALS == at_call:
            state["moves"] += 1
            store.fit_generation += 1

    sensor._calibrate_period = _HookedCounter(counter, _hook)
    return state


def _setup_with_log(cls, region: str = REGION):
    """A sharing sensor whose memo records every publication."""
    sensor, hass, counter, store, d = tcg._setup(cls, region=region)
    log = _PublicationLog(lambda: sensor._calibrated_forecast_key(sensor._price_data))
    sensor.coordinator._calibrated_forecast_cache = log
    return sensor, hass, counter, store, d, log


# ── The headline invariant ────────────────────────────────────────────────────


def test_the_warm_never_publishes_under_a_key_that_has_already_moved():
    """A refit landing inside the warm must not produce a mislabelled entry.

    Before the fix the key was taken inside the executor job, so the pass that
    began at generation 1 and ended at generation 2 stored its half and half
    result under the generation 1 key, and did so unconditionally.
    """
    sensor, _hass, counter, store, _d, log = _setup_with_log(
        sensor_module.PD7DayForecastSensor
    )
    _install_key_move(sensor, store, counter, at_call=INTERVALS // 2, times=1)

    twc._run(sensor._async_warm_until_current)

    assert log.publications, "the warm published nothing at all"
    assert log.stale_publications == [], (
        "the memo was published under a key that had already moved: "
        f"{log.stale_publications}"
    )


def test_a_late_warm_does_not_clobber_a_fresher_memo_entry():
    """One slot per region, three entities. The late arrival must not win.

    Models the sequence in #60 exactly: this entity's warm starts, a refit
    lands, a sibling entity of the same region completes its own warm and
    publishes the current entry, and only then does this entity's pass finish.
    The result it is holding was computed under the superseded key, so it must
    be discarded rather than written over the sibling's.
    """
    sensor, _hass, counter, store, d, log = _setup_with_log(
        sensor_module.PD7DayForecastSensor
    )
    fresh_value = [{"time": "published by the sibling entity"}]
    state = {"done": False}

    def _sibling_publishes(_call_no: int) -> None:
        if state["done"]:
            return
        state["done"] = True
        store.fit_generation += 1
        log[REGION] = (sensor._calibrated_forecast_key(d), fresh_value)

    sensor._calibrate_period = _HookedCounter(counter, _sibling_publishes)

    twc._run(sensor._async_warm_calibrated_forecast)

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
    cheap, rather than asserting on elapsed time.
    """
    sensor, _hass, counter, store, _d, log = _setup_with_log(cls)
    _install_key_move(sensor, store, counter, at_call=1, times=1)

    seen = {}
    inner_write = sensor.async_write_ha_state

    def _write():
        seen["write_key"] = sensor._calibrated_forecast_key(sensor._price_data)
        inner_write()

    sensor.async_write_ha_state = _write

    twc._run(sensor._async_warm_then_write)

    assert sensor.writes == 1
    assert log.publications, "nothing was ever published"
    warm_key = log.publications[-1][0]
    assert warm_key == seen["write_key"], (
        f"the warm published under {warm_key} but the write asked for "
        f"{seen['write_key']}"
    )
    assert sensor.calibrations_during_write == 0, (
        f"{sensor.calibrations_during_write} intervals were calibrated on the "
        "event loop inside the state write"
    )
    assert log.stale_publications == []


# ── Sweep ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cls,at_call,times",
    [
        (cls, at_call, times)
        for cls, at_call, times in itertools.product(
            SHARING_CLASSES,
            # First interval of the pass, the middle, and the last one, which is
            # the tightest case: the key moves after the final calibration and
            # before the publish.
            (1, INTERVALS // 2, 0),
            (1, 2),
        )
    ],
    ids=lambda p: getattr(p, "__name__", str(p)),
)
def test_key_stability_sweep(cls, at_call, times):
    """The invariant must hold wherever in the pass the key moves, and however
    often, for every class sharing the slot.

    ``times`` of 2 exercises the re-warm: the key moves again during the retry,
    so the third attempt is the one that settles.
    """
    sensor, _hass, counter, store, _d, log = _setup_with_log(cls)
    state = _install_key_move(sensor, store, counter, at_call=at_call, times=times)

    twc._run(sensor._async_warm_until_current)

    assert state["moves"] == times, (
        f"the key only moved {state['moves']} times, expected {times}, so this "
        "case is not testing what it says it is"
    )
    assert log.stale_publications == [], (
        f"stale publications with the key moving at {at_call}: "
        f"{log.stale_publications}"
    )
    # And the memo must have settled on the live key, so the write is free.
    live_key = sensor._calibrated_forecast_key(sensor._price_data)
    assert sensor._cached_calibrated_forecast(live_key) is not None, (
        "the memo did not settle on the live key within the attempt budget"
    )
    assert sensor.calibrations_during_write is None
    assert twc._count_during_write(sensor) == 0, (
        "the state write would still have to calibrate on the event loop"
    )


# ── Structure the invariant depends on ────────────────────────────────────────


def test_the_executor_half_never_touches_the_memo():
    """``_calibrated_forecast_values`` must be a pure pass over the run.

    It is the half that runs off the loop. If it read or wrote the memo it
    would be making a publish decision from a worker thread, using state only
    the loop can read consistently, which is the bug.
    """
    sensor, _hass, _counter, _store, d, log = _setup_with_log(
        sensor_module.PD7DayForecastSensor
    )

    values = sensor._calibrated_forecast_values(d)

    assert len(values) == INTERVALS
    assert log.publications == [], "the executor half wrote to the memo"
    assert REGION not in log


def test_the_shared_implementations_are_shared_not_reimplemented():
    """All three classes must use one key builder and one values builder.

    A second copy of either would drift, and the currency check would then be
    comparing keys that were never meant to be equal.
    """
    for name in ("_calibrated_forecast_key", "_calibrated_forecast_values",
                 "_calibrated_forecast"):
        base = getattr(sensor_module.PD7DayForecastSensor, name)
        for cls in SHARING_CLASSES:
            assert getattr(cls, name) is base, (
                f"{cls.__name__} has its own {name}, which will drift"
            )


def test_a_warm_hit_costs_no_executor_work():
    """A second warm for an unchanged key must not recalibrate anything.

    The five minute dispatch listener routes every write through the warm, so
    the hit path has to be free. Taking the key before the executor hop rather
    than inside it is what makes this possible.
    """
    sensor, hass, counter, _store, _d, _log = _setup_with_log(
        sensor_module.PD7DayForecastSensor
    )

    twc._run(sensor._async_warm_until_current)
    calls_after_first = counter.calls
    executor_after_first = hass.executor_calls
    assert calls_after_first == INTERVALS
    assert executor_after_first >= 1

    twc._run(sensor._async_warm_until_current)

    assert counter.calls == calls_after_first, "the warm recalibrated a live memo"
    assert hass.executor_calls == executor_after_first, (
        "the warm paid for an executor round trip on a memo hit"
    )


# ── The other half of #60, kept as a guard ────────────────────────────────────


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_the_platform_first_write_is_still_covered(cls):
    """The first claim in #60, which #61 already fixed. This is a guard only.

    ``add_to_platform_finish`` awaits ``async_added_to_hass`` and then writes
    state directly, bypassing ``_handle_coordinator_update``. This passes with
    and without the change in this branch, and it is here so that restructuring
    the warm cannot quietly undo it.
    """

    async def scenario():
        sensor, _hass, _counter, _store, _d, _log = _setup_with_log(cls)
        await sensor.async_added_to_hass()
        sensor.async_write_ha_state()
        assert sensor.writes == 1
        assert sensor.calibrations_during_write == 0

    twc._run(scenario)


# ── The STPASA index half of the same TOCTOU ──────────────────────────────────


class _OrderRecordingCoordinator:
    """Just enough of PD7DayCoordinator to run the real ``stpasa_index``.

    Records the order in which the three index attributes are assigned. The run
    key is the freshness token the memo key folds in, so it has to be published
    after the data it names, or a reader on the other thread can pair the new
    key with the old index and memoise a forecast that nothing will ever
    recompute.
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


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    def _run_all() -> None:
        failures = 0
        cases: list[tuple[str, callable]] = []
        for cls in SHARING_CLASSES:
            cases.append(
                (
                    f"warm key and write key match for {cls.__name__}",
                    lambda c=cls: test_the_warm_key_and_the_write_key_are_the_same_key(c),
                )
            )
            cases.append(
                (
                    f"platform first write is covered for {cls.__name__}",
                    lambda c=cls: test_the_platform_first_write_is_still_covered(c),
                )
            )
        cases.insert(
            0,
            (
                "the warm never publishes under a moved key",
                test_the_warm_never_publishes_under_a_key_that_has_already_moved,
            ),
        )
        cases.insert(
            1,
            (
                "a late warm does not clobber a fresher entry",
                test_a_late_warm_does_not_clobber_a_fresher_memo_entry,
            ),
        )
        for cls, at_call, times in itertools.product(
            SHARING_CLASSES, (1, INTERVALS // 2, 0), (1, 2)
        ):
            cases.append(
                (
                    f"sweep {cls.__name__} move at {at_call} x{times}",
                    lambda c=cls, a=at_call, t=times: test_key_stability_sweep(c, a, t),
                )
            )
        cases += [
            ("the executor half never touches the memo",
             test_the_executor_half_never_touches_the_memo),
            ("the shared implementations are shared",
             test_the_shared_implementations_are_shared_not_reimplemented),
            ("a warm hit costs no executor work", test_a_warm_hit_costs_no_executor_work),
            ("stpasa index publishes the run key last",
             test_stpasa_index_publishes_the_run_key_last),
        ]
        for description, fn in cases:
            try:
                fn()
            except AssertionError as err:
                failures += 1
                print(f"  FAIL: {description}\n        {err}")
            else:
                print(f"  PASS: {description}")
        if failures:
            raise SystemExit(1)

    _run_all()
