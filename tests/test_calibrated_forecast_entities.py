"""
The entity side of the calibrated forecast provider (spec 003, invariants 4 and 7).

Invariant 4: no method is shared by assignment in sensor.py or
tariff_sensor.py, and ``_calibrate_period`` has one body. The three sensors
sharing a region's calibrated forecast used to borrow eight methods from
PD7DayForecastSensor by assignment, and SpotPriceForecastDays27Sensor carried
its own copy of ``_calibrate_period``; the export tariff sensor's borrowed
copy is how #66 happened. They now inherit one implementation each.

Invariant 7: the warm-then-write task keeps its name and is cancelled when the
entity leaves hass (#106), so a warm still in flight when the entry unloads
does not write state for an entity that is gone. Nothing pinned that before.

Run with:  python -m pytest tests/test_calibrated_forecast_entities.py -v
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import pathlib
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from support import NEM_TZ, PKG_DIR, install_ha_stubs, load_chain, make_price_period, nem_iso

install_ha_stubs()
(
    _const_mod, _nem_time, _client_mod, _engine_mod, _store_mod, _inputs_mod,
    _provider_mod, _coord_mod, _tariff_mod, _sensor_mod,
) = load_chain(
    "const", "nem_time", "pd7day_client", "calibration_engine", "calibration_store",
    "calibration_inputs", "calibrated_forecast", "coordinator", "tariff_sensor", "sensor",
)

SHARING_CLASSES = (
    _sensor_mod.PD7DayForecastSensor,
    _sensor_mod.SpotPriceForecastDays27Sensor,
    _sensor_mod.PD7DayDataSensor,
)
REGION = "QLD1"
RUN_DT = datetime(2026, 9, 1, 18, 0, tzinfo=NEM_TZ)


# ── Invariant 4: nothing shared by assignment, one _calibrate_period ─────────


def _own_function(value):
    """The function behind a class attribute, or None when it is not a method."""
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    if inspect.isfunction(value):
        return value
    return None


@pytest.mark.parametrize("module", [_sensor_mod, _tariff_mod], ids=["sensor", "tariff_sensor"])
def test_no_method_is_shared_by_assignment(module):
    """Every method a class carries was defined in that class's own body.

    A method taken by assignment keeps the qualified name of the class it was
    written in, so ``SpotPriceForecastDays27Sensor._calibrated_forecast =
    PD7DayForecastSensor._calibrated_forecast`` shows up here as a function
    whose ``__qualname__`` names the wrong class.
    """
    borrowed = []
    for _name, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        for attr, value in vars(cls).items():
            func = _own_function(value)
            if func is None:
                continue
            if func.__qualname__ != f"{cls.__qualname__}.{attr}":
                borrowed.append(f"{cls.__name__}.{attr} is {func.__qualname__}")
    assert borrowed == [], f"methods shared by assignment: {borrowed}"


@pytest.mark.parametrize("filename", ["sensor.py", "tariff_sensor.py"])
def test_no_class_body_assigns_another_class_attribute_to_a_method_name(filename):
    """The spec's acceptance grep, kept as a test so it cannot regress quietly."""
    source = pathlib.Path(PKG_DIR, filename).read_text(encoding="utf-8")
    hits = re.findall(r"(?m)^\s+_[a-z_]+ = [A-Z][A-Za-z0-9]+\._.*$", source)
    assert hits == []


def test_calibrate_period_has_one_body():
    """Days27 used to carry a copy; it now inherits the one implementation."""
    days27 = _sensor_mod.SpotPriceForecastDays27Sensor
    base = _sensor_mod.PD7DayForecastSensor
    assert days27._calibrate_period is base._calibrate_period
    assert _sensor_mod.PD7DayDataSensor._calibrate_period is base._calibrate_period
    for cls in SHARING_CLASSES:
        assert "_calibrate_period" not in vars(cls), f"{cls.__name__} has its own _calibrate_period"


def test_calibrate_period_resolves_against_sensor_py():
    """Its globals are sensor.py's, which a memo test patches calibrate_interval through."""
    globals_ = _sensor_mod.PD7DayForecastSensor._calibrate_period.__globals__
    assert globals_ is vars(_sensor_mod)


@pytest.mark.parametrize(
    "name",
    [
        "_calibrated_forecast_key",
        "_cached_calibrated_forecast",
        "_calibrated_forecast",
        "_calibrated_forecast_values",
        "_calibrate_period",
        "_calibrated_current",
    ],
)
def test_the_sharing_classes_inherit_one_implementation(name):
    first = getattr(SHARING_CLASSES[0], name)
    for cls in SHARING_CLASSES:
        assert getattr(cls, name) is first, f"{cls.__name__}.{name} is not the shared one"
        assert name not in vars(cls), f"{cls.__name__} defines its own {name}"


# ── Invariant 7: the warm write task is named and cancelled on removal ───────


class _BlockingHass:
    """A hass whose executor jobs wait on a gate, so a warm stays in flight."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.names: list[str | None] = []
        self.executor_started = 0

    async def async_add_executor_job(self, func, *args):
        self.executor_started += 1
        await self.gate.wait()
        return func(*args)

    def async_create_background_task(self, coro, name=None, eager_start=False):
        self.names.append(name)
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


def _price_data():
    d = SimpleNamespace()
    d.forecast = [
        make_price_period(RUN_DT + timedelta(minutes=30 * (i + 1)), value=0.1)
        for i in range(8)
    ]
    d.forecast_generated_at = nem_iso(RUN_DT)
    d.region = REGION
    d.interval_minutes = 30
    return d


def _coordinator():
    coordinator = SimpleNamespace()
    coordinator.data = SimpleNamespace(prices={REGION: _price_data()})
    coordinator._calibrated_forecast_cache = {}
    coordinator._stpasa_index_run = "stpasa-run-1"
    coordinator.stpasa_index = lambda: None
    return coordinator


def _entity(cls, hass):
    sensor = cls.__new__(cls)
    sensor.coordinator = _coordinator()
    sensor._region = REGION
    # No calibration store: every interval is a raw passthrough, which is all
    # the lifecycle needs.
    sensor._store = None
    sensor.hass = hass
    sensor.entity_id = f"sensor.nem_pd7day_{REGION.lower()}_{cls.__name__.lower()}"
    sensor.writes = 0

    def _write():
        sensor.writes += 1

    sensor.async_write_ha_state = _write
    return sensor


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_the_warm_write_task_is_named_and_cancelled_on_removal(cls, monkeypatch):
    """#106: schedule a write, remove the entity mid-warm, and nothing is written."""
    removed = []

    async def _base_will_remove(self):
        removed.append(self)

    # The Home Assistant stand-in entity has no removal hook of its own; give
    # it one so the mixin's super() call has somewhere to land.
    monkeypatch.setattr(
        _sensor_mod.CoordinatorEntity, "async_will_remove_from_hass", _base_will_remove,
        raising=False,
    )

    async def scenario():
        hass = _BlockingHass()
        sensor = _entity(cls, hass)

        sensor._schedule_warm_state_write()

        assert len(hass.tasks) == 1
        task = hass.tasks[0]
        expected = f"nem_pd7day warm state write {sensor.entity_id}"
        assert hass.names == [expected]
        assert task.get_name() == expected

        # Let the warm reach the executor and block there.
        for _ in range(5):
            await asyncio.sleep(0)
        assert hass.executor_started == 1, "the warm never reached the executor"
        assert not task.done()

        await sensor.async_will_remove_from_hass()
        assert removed == [sensor], "removal did not reach the entity base class"

        # Releasing the executor now must not bring the write back.
        hass.gate.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for _ in range(5):
            await asyncio.sleep(0)

        assert task.cancelled(), "the warm write task survived the entity's removal"
        assert sensor.writes == 0, "state was written for an entity that is gone"
        assert not sensor._pending_warm_writes

    _run(scenario())


@pytest.mark.parametrize("cls", SHARING_CLASSES, ids=lambda c: c.__name__)
def test_a_warm_write_that_is_not_removed_still_writes(cls):
    """Control for the test above: the same scenario without removal writes once."""

    async def scenario():
        hass = _BlockingHass()
        sensor = _entity(cls, hass)

        sensor._schedule_warm_state_write()
        task = hass.tasks[0]
        for _ in range(5):
            await asyncio.sleep(0)
        hass.gate.set()
        await task

        assert sensor.writes == 1
        assert not sensor._pending_warm_writes, "a finished task was left pending"

    _run(scenario())
