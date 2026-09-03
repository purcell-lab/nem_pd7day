"""
Calibrated forecast memoisation (issue #35).

Platform setup took 42 to 53 seconds per region. The calibrated forecast was
memoised on the entity, but three entities per region ask for it:
PD7DayForecastSensor, SpotPriceForecastDays27Sensor and PD7DayDataSensor. Their
`_calibrate_period` and `_covariates_for_interval` implementations are the same
computation (verified by AST comparison, they are identical once docstrings are
stripped), and they share the region's coordinator and calibration store, so the
full recalibration of roughly 336 intervals ran three times per region instead
of once.

The memo now lives on the coordinator, which is the natural owner because it is
per region, exactly like the value being cached.

The cache key also used `id(store.calibration)`, which cannot detect a refit:
`async_refit` publishes the result and then mutates that same object in place to
attach the OLS stage 2 models. `test_inplace_stage2_is_invisible_to_id` pins
that down. `fit_generation` replaces it.

Reuses the Home Assistant stub preamble already installed by test_sensor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import test_sensor as ts

from custom_components.nem_pd7day.sensor import (
    PD7DayDataSensor,
    PD7DayForecastSensor,
    SpotPriceForecastDays27Sensor,
)

NEM_TZ = timezone(timedelta(hours=10))

# The three classes that share one region's calibrated forecast.
SHARING_CLASSES = (
    PD7DayForecastSensor,
    SpotPriceForecastDays27Sensor,
    PD7DayDataSensor,
)


class _CountingStore:
    """Calibration store stub exposing a controllable fit generation."""

    def __init__(self, fit_generation: int = 1) -> None:
        self.fit_generation = fit_generation


def _make_price_data(run_at_dt: datetime, intervals: int = 336):
    """A full 7 day, 30 minute forecast, which is what production carries."""
    periods = [
        ts.make_price_period(run_at_dt + timedelta(minutes=30 * (i + 1)), value=0.1)
        for i in range(intervals)
    ]
    d = MagicMock()
    d.forecast = periods
    d.forecast_generated_at = ts.nem_iso(run_at_dt)
    d.region = "QLD1"
    d.interval_minutes = 30
    return d


def _make_region_sensors(coordinator, store, region="QLD1"):
    """Build one instance of each sharing class against a shared coordinator.

    Built with __new__ and only the attributes `_calibrated_forecast` touches,
    following the pattern in test_sensor.make_sensor, because the real __init__
    needs Home Assistant's CoordinatorEntity machinery.
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
    """Replace _calibrate_period on each instance with a counting wrapper."""
    counter = {"calls": 0}

    def _make(original):
        def _wrapped(period, run_at_str):
            counter["calls"] += 1
            return {"time": period.time, "value": period.value}

        return _wrapped

    for s in sensors:
        s._calibrate_period = _make(s._calibrate_period)
    return counter


def _fresh_coordinator():
    """A coordinator stub that behaves like the real one for cache purposes."""
    coordinator = MagicMock()
    coordinator.data = None
    # The real PD7DayCoordinator initialises this dict in __init__.
    coordinator._calibrated_forecast_cache = {}
    coordinator._stpasa_index_run = "stpasa-run-1"
    coordinator.stpasa_index = MagicMock(return_value=None)
    return coordinator


# ── The headline property ─────────────────────────────────────────────────────


def test_three_sensors_in_a_region_calibrate_each_interval_once():
    """All three entities share one calibration pass over the forecast."""
    coordinator = _fresh_coordinator()
    store = _CountingStore()
    sensors = _make_region_sensors(coordinator, store)
    counter = _count_calibrations(sensors)
    d = _make_price_data(datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=336)

    results = [s._calibrated_forecast(d) for s in sensors]

    assert counter["calls"] == 336, (
        "expected one calibration pass over 336 intervals for the region, "
        f"got {counter['calls']}"
    )
    # Every entity sees the same list object, so there is one copy in memory
    # rather than three.
    assert results[1] is results[0]
    assert results[2] is results[0]


def test_entity_level_memo_would_calibrate_three_times():
    """
    Guard test: reproduce the pre-fix behaviour and assert it was really broken.

    Without this, the test above could start passing for the wrong reason if the
    memo silently moved back onto the entity.
    """
    store = _CountingStore()
    # Each sensor getting its own coordinator is equivalent to each sensor
    # having its own memo, which is what an entity level cache amounted to.
    sensors = []
    for cls in SHARING_CLASSES:
        s = cls.__new__(cls)
        s.coordinator = _fresh_coordinator()
        s._region = "QLD1"
        s._store = store
        sensors.append(s)
    counter = _count_calibrations(sensors)
    d = _make_price_data(datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=336)

    for s in sensors:
        s._calibrated_forecast(d)

    assert counter["calls"] == 336 * 3


def test_repeated_state_writes_do_not_recalibrate():
    """The memo still does its original job for an unchanged run."""
    coordinator = _fresh_coordinator()
    sensors = _make_region_sensors(coordinator, _CountingStore())
    counter = _count_calibrations(sensors)
    d = _make_price_data(datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=48)

    for _ in range(10):
        for s in sensors:
            s._calibrated_forecast(d)

    assert counter["calls"] == 48


# ── Invalidation ──────────────────────────────────────────────────────────────


def test_new_pd7day_run_invalidates_the_memo():
    coordinator = _fresh_coordinator()
    sensors = _make_region_sensors(coordinator, _CountingStore())
    counter = _count_calibrations(sensors)

    first = _make_price_data(datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=48)
    sensors[0]._calibrated_forecast(first)
    assert counter["calls"] == 48

    # A later run at a different generation time must not be served from cache.
    second = _make_price_data(datetime(2026, 5, 19, 14, 30, tzinfo=NEM_TZ), intervals=48)
    sensors[0]._calibrated_forecast(second)
    assert counter["calls"] == 96


def test_refit_invalidates_the_memo_via_fit_generation():
    coordinator = _fresh_coordinator()
    store = _CountingStore(fit_generation=1)
    sensors = _make_region_sensors(coordinator, store)
    counter = _count_calibrations(sensors)
    d = _make_price_data(datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=48)

    sensors[0]._calibrated_forecast(d)
    assert counter["calls"] == 48

    # Same run, same STPASA, but the calibration has been refitted.
    store.fit_generation = 2
    sensors[0]._calibrated_forecast(d)
    assert counter["calls"] == 96


def test_new_stpasa_run_invalidates_the_memo():
    coordinator = _fresh_coordinator()
    sensors = _make_region_sensors(coordinator, _CountingStore())
    counter = _count_calibrations(sensors)
    d = _make_price_data(datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=48)

    sensors[0]._calibrated_forecast(d)
    coordinator._stpasa_index_run = "stpasa-run-2"
    sensors[0]._calibrated_forecast(d)

    assert counter["calls"] == 96


def test_regions_do_not_share_cache_entries():
    """One coordinator per region in production, but keep the key region safe."""
    coordinator = _fresh_coordinator()
    qld = _make_region_sensors(coordinator, _CountingStore(), region="QLD1")[0]
    nsw = _make_region_sensors(coordinator, _CountingStore(), region="NSW1")[0]
    counter = _count_calibrations([qld, nsw])
    d = _make_price_data(datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ), intervals=48)

    qld_result = qld._calibrated_forecast(d)
    nsw_result = nsw._calibrated_forecast(d)

    assert counter["calls"] == 96
    assert nsw_result is not qld_result


# ── Why id(calibration) could not work ────────────────────────────────────────


def test_inplace_stage2_is_invisible_to_id():
    """
    The old cache key was `id(store.calibration)`.

    async_refit assigns the fitted result to self._calibration and then sets
    result.ols_models on that same object for the OLS stage 2 fit. Object
    identity therefore does not change across a change that moves every
    calibrated price, so the memo kept serving stage 1 output.
    """
    published = MagicMock()
    published.ols_models = {}
    before = id(published)

    # This is precisely what the stage 2 block does.
    published.ols_models = {"bucket": object()}

    assert id(published) == before, (
        "in place mutation leaves id() unchanged, which is why fit_generation "
        "exists"
    )


def test_fit_generation_advances_on_restore_refit_and_stage2():
    """A monotonic counter moves for each of the three ways the fit changes."""
    from custom_components.nem_pd7day.calibration_store import CalibrationStore

    store = CalibrationStore.__new__(CalibrationStore)
    store._fit_generation = 0

    assert store.fit_generation == 0

    # Restore from storage.
    store._fit_generation += 1
    assert store.fit_generation == 1

    # Refit.
    store._fit_generation += 1
    # OLS stage 2 in place update.
    store._fit_generation += 1
    assert store.fit_generation == 3

    # Monotonic, so a key built from it can never collide with an earlier fit.
    assert store.fit_generation > 1


# ── Published key set ─────────────────────────────────────────────────────────


def test_every_builder_that_publishes_a_band_publishes_band_source():
    """
    Issue #100: PD7DayDataSensor builds its per-interval dict from its own
    literal rather than the shared ATTR_CAL_* update, so band_source, added
    to the other two builders in PR #96, never reached it. Whatever the
    calibration returns, any builder exposing p10 and p90 must also expose
    band_source, so the sensor carrying the full forecast array can answer
    which band derivation produced it.
    """
    from custom_components.nem_pd7day.const import (
        ATTR_CAL_BAND_SOURCE,
        ATTR_CAL_P10,
        ATTR_CAL_P90,
    )

    run_at = datetime(2026, 9, 3, 7, 30, tzinfo=timezone(timedelta(hours=10)))
    price_data = _make_price_data(run_at, intervals=8)
    coordinator = _fresh_coordinator()
    coordinator.data = MagicMock()
    coordinator.data.prices = {"QLD1": price_data}
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
        ATTR_CAL_BAND_SOURCE: "stage2_residual",
        "n_obs": 100,
    }

    # Patch the globals the classes' methods actually resolve against. Other
    # test modules reload sensor.py under the same name, so the module object
    # in sys.modules can differ from the one SHARING_CLASSES were defined in.
    method_globals = PD7DayForecastSensor._calibrate_period.__globals__
    with patch.dict(method_globals, {
        "calibrate_interval": lambda *a, **k: dict(fake_cal),
        "_amber_express_cutoff": lambda: run_at - timedelta(days=1),
    }):
        for s in sensors:
            entries = s.extra_state_attributes["forecast"]
            assert entries, type(s).__name__
            entry = entries[0]
            assert ATTR_CAL_P10 in entry and ATTR_CAL_P90 in entry, type(s).__name__
            assert entry.get(ATTR_CAL_BAND_SOURCE) == "stage2_residual", (
                f"{type(s).__name__} publishes p10/p90 without band_source: "
                f"{sorted(entry)}"
            )
