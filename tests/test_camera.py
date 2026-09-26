"""Tests for camera.py: platform setup, and the forecast chart camera's data.

Three concerns, each once a file of its own:

* Setup must not wait for the first chart render. ``async_added_to_hass`` runs
  inside ``async_add_entities``, so awaiting the first matplotlib render there
  held up the whole camera platform; on a five region install that is 15
  camera entities rendering through a contended executor pool at startup,
  which reliably logged "Setup of camera platform nem_pd7day is taking over
  10 seconds". The tests assert the property, not the mechanism: adding a
  camera returns promptly even when the render is slow, and the image still
  arrives once the render completes.

* Parity with the price forecast sensor, issue #80. The chart and the sensor
  describe the same interval of the same PD7DAY run from the same raw price,
  and they disagreed: ``camera.py`` called ``CalibrationStore.apply_to_price``
  directly with the gas and network covariates only, no ``stpasa_features`` and no
  ``run_features``, so the stage 2 gate always took the isotonic only branch
  and the camera never rendered ``isotonic+stpasa`` while the sensor did so
  routinely. It then wrote its own ``calibrated``, ``p10``, ``p50``, ``p90``
  and ``calibrated_source`` into the chart data. Same defect as issue #66 in a
  third call site; the fix is PR #77's: route through
  ``calibration_inputs.calibrate_interval``. The parity tests run a real
  isotonic plus stage 2 OLS fit behind a real ``CalibrationStore`` and compare
  the camera's chart data against the sensor's forecast attribute interval by
  interval (fixture adapted from tests/test_tariff_calibration_parity.py).

* The spike callout wiring, issue #84. ``camera.py`` never wrote
  ``spike_credible`` into the entries it hands the chart renderer, so the set
  ``_save_spike_intervals`` accumulated was always empty, ``spike_first_run``
  was always True, and the renderer skips any interval whose
  ``spike_credible`` is not True: no spike callout had ever been drawn. The
  tri-state matters: ``apply_to_price`` sets True when both covariates support
  the spike, None when either is missing, and no key at all below
  SPIKE_THRESHOLD. None and absent are an unanswered and an unasked question
  and neither may be recorded as False. The chart side of #84 lives in
  tests/test_forecast_chart.py.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import math
import random
import time
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, nem_iso, run_async

install_ha_stubs()

(
    _const_mod,
    _nem_time,
    _engine_mod,
    _client_mod,
    _store_mod,
    _dispatch_mod,
    _coord_mod,
    _stpasa_client_mod,
    _inputs_mod,
    _tariff_mod,
    _sensor_mod,
    _camera_mod,
) = load_chain(
    "const",
    "nem_time",
    "calibration_engine",
    "pd7day_client",
    "calibration_store",
    "dispatch_client",
    "coordinator",
    "stpasa_client",
    "calibration_inputs",
    "tariff_sensor",
    "sensor",
    "camera",
)

DOMAIN = _const_mod.DOMAIN
SPIKE_GAS_THRESHOLD_TJ = _const_mod.SPIKE_GAS_THRESHOLD_TJ
SPIKE_CAPABILITY_DEPRESSION = _const_mod.SPIKE_CAPABILITY_DEPRESSION
REGION_INTERCONNECTOR_SIGN = _const_mod.REGION_INTERCONNECTOR_SIGN
SPIKE_THRESHOLD = _engine_mod.SPIKE_THRESHOLD
CalibrationEngine = _engine_mod.CalibrationEngine
Observation = _engine_mod.Observation
RunFeatures = _engine_mod.RunFeatures
StpasaFeatures = _engine_mod.StpasaFeatures
CalibrationStore = _store_mod.CalibrationStore
StpasaInterval = _stpasa_client_mod.StpasaInterval
StpasaResult = _stpasa_client_mod.StpasaResult
PD7DayForecastSensor = _sensor_mod.PD7DayForecastSensor
NemPd7dayForecastChartCamera = _camera_mod.NemPd7dayForecastChartCamera
interval_start, parse_iso, to_nem_iso = (
    _nem_time.interval_start, _nem_time.parse_iso, _nem_time.to_nem_iso,
)

# Observations must stay inside the engine's 90 day training window, so the
# fixture is anchored to now rather than to a fixed calendar date.
_ANCHOR = datetime.now(NEM_TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
RUN_DT = _ANCHOR.replace(hour=4)
RUN_AT = to_nem_iso(RUN_DT)
STPASA_RUN_AT = to_nem_iso(_ANCHOR.replace(hour=3))

# The calibration fields both entities publish for one interval. Parity is
# asserted on all of them, not only on the point estimate, because the camera
# publishes the band and the source label too and those were wrong as well.
CAL_KEYS = ("calibrated", "p10", "p50", "p90", "ols_mae", "calibrated_source", "n_obs")


# ── Fixture builders ──────────────────────────────────────────────────────────

@dataclass
class FakePeriod:
    """A PricePeriod: ``time`` is the interval START, ``nemtime`` the END."""

    time: str
    nemtime: str
    value: float | None


@dataclass
class FakeFlow:
    time: str
    mwflow: float
    exportlimit: float
    importlimit: float


@dataclass
class FakeGas:
    nemtime: str
    value_tj: float


def make_period(start_dt: datetime, value: float | None) -> FakePeriod:
    return FakePeriod(
        time=nem_iso(start_dt),
        nemtime=nem_iso(start_dt + timedelta(minutes=30)),
        value=value,
    )


def make_stpasa_interval(start_dt: datetime, solar: float = 1500.0) -> StpasaInterval:
    """An STPASA interval keyed on the interval END, per AEMO convention."""
    return StpasaInterval(
        interval_datetime=nem_iso(start_dt + timedelta(minutes=30)),
        run_datetime=STPASA_RUN_AT,
        demand10=7400.0,
        demand50=7000.0,
        demand90=6600.0,
        surpluscapacity=4200.0,
        ss_solar_uigf=solar,
        ss_wind_uigf=900.0,
    )


# A region imports across each of its links in the link's nominal direction,
# so the ceiling on its imports is exportlimit on each. A run of background
# intervals at full capability sets the median each priced interval's
# capability is judged against: without them a uniformly depressed series
# would be its own reference and would score as normal (#176).
FULL_CAPABILITY_MW = 1000.0
DEPRESSED_CAPABILITY_MW = 50.0
BACKGROUND_INTERVALS = 20


def region_interconnectors(region, periods, tight):
    """The region's own interconnector forecasts for the spike covariate gate.

    ``tight`` True depresses capability on the priced intervals while the
    region imports, which is the condition the gate looks for; False leaves
    capability at the run median; None publishes no forecast at all, which
    must read as unknown rather than as a confirmed negative.
    """
    links = REGION_INTERCONNECTOR_SIGN[region]
    if tight is None:
        return {ic_id: types.SimpleNamespace(forecast=[]) for ic_id in links}
    capability = DEPRESSED_CAPABILITY_MW if tight else FULL_CAPABILITY_MW
    points = [
        FakeFlow(time=p.time, mwflow=400.0, exportlimit=capability,
                 importlimit=-FULL_CAPABILITY_MW)
        for p in periods
    ]
    last = datetime.fromisoformat(periods[-1].time) if periods else RUN_DT
    for i in range(BACKGROUND_INTERVALS):
        points.append(
            FakeFlow(time=nem_iso(last + timedelta(minutes=30 * (i + 1))), mwflow=400.0,
                     exportlimit=FULL_CAPABILITY_MW, importlimit=-FULL_CAPABILITY_MW)
        )
    return {ic_id: types.SimpleNamespace(forecast=list(points)) for ic_id in links}


class StubCoordinator:
    """Coordinator stand-in exposing what the calibration inputs actually read.

    ``stpasa_index`` builds the index exactly as ``PD7DayCoordinator`` does, so
    the keys under test are real interval START strings. ``_store`` is set to
    the same object the sensor is handed, which is what the live wiring does:
    ``__init__.py`` passes one ``CalibrationStore`` to the coordinator and puts
    the same instance on ``entry.runtime_data.store``. ``gas_tj`` and
    ``tight`` feed the spike covariate gate; None leaves that covariate out of
    the coordinator data, as a missing market summary or interconnector does.
    """

    def __init__(self, region, periods, stpasa_intervals=(), store=None,
                 *, gas_tj=None, tight=None):
        price_data = types.SimpleNamespace(
            forecast=list(periods), forecast_generated_at=RUN_AT,
        )
        gas = (
            types.SimpleNamespace(
                forecast=[FakeGas(nemtime=p.nemtime, value_tj=gas_tj) for p in periods]
            )
            if gas_tj is not None else None
        )
        self.data = types.SimpleNamespace(
            prices={region: price_data},
            interconnectors=region_interconnectors(region, periods, tight),
            market_summary=gas,
        )
        self.last_update_success = True
        self._regions = [region]
        self._store = store
        self._calibrated_forecast_cache = {}
        self._stpasa_index_run = f"{STPASA_RUN_AT}|fetched"
        self._result = StpasaResult(
            region=region,
            run_datetime=STPASA_RUN_AT,
            intervals=list(stpasa_intervals),
            fetched_at=STPASA_RUN_AT,
        )
        self._map = {}
        self._sorted = []
        for si in stpasa_intervals:
            start_iso = interval_start(si.interval_datetime)
            self._map[start_iso] = si
            self._sorted.append((parse_iso(start_iso).timestamp(), si))
        self._sorted.sort(key=lambda t: t[0])

    def stpasa_index(self):
        return self._result, self._map, self._sorted

    @property
    def current_run_features(self):
        # The same object both the camera and the sensor would read.
        return RunFeatures(run_max_h6_rrp=0.24, run_mean_rrp=0.11, run_spread=0.06)


# (base, width) of the near-term raw prices of each training run in
# fitted_store: run_max_h6 runs 0.04 to 0.59, run_mean 0.04 to 0.55 and
# run_spread 0.03 to 0.40 across them, bracketing what StubCoordinator serves.
_RUN_SHAPES = (
    (0.02, 0.04), (0.05, 0.10), (0.10, 0.16), (0.16, 0.24), (0.22, 0.36), (0.30, 0.50),
)


def fitted_store(region: str = "QLD1") -> CalibrationStore:
    """A real CalibrationStore holding a real isotonic plus stage 2 OLS fit.

    Nothing here stubs the calibration itself, so the numbers compared below
    come out of ``CalibrationResult.apply`` and the parity tests are sensitive
    to which arguments each caller supplies and to nothing else. The spike
    tests need only that a calibration exists at all, since a store with none
    returns early from ``apply_to_price`` and never reaches the spike
    annotation.
    """
    rng = random.Random(7)
    engine = CalibrationEngine()
    observations: list[Observation] = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}

    # Several training runs rather than one: the stage-2 serving gate (#147)
    # refuses a feature outside its training range, and the three run
    # features are constant within a run, so a single-run fixture has
    # zero-width ranges there and would refuse the run StubCoordinator
    # serves. The near-term prices are laid out per run so the run features
    # bracket current_run_features.
    run_ats = []
    for k, (base, width) in enumerate(_RUN_SHAPES):
        run_dt = (_ANCHOR - timedelta(days=25 + k)).replace(hour=3, minute=30)
        run_at = nem_iso(run_dt)
        run_ats.append(run_at)
        for j in range(8):
            near_dt = run_dt + timedelta(hours=1 + j)
            observations.append(
                Observation(
                    interval_time=nem_iso(near_dt),
                    horizon_hours=1.0 + j,
                    pd7day_forecast=base + width * j / 7.0,
                    actual_rrp=rng.uniform(0.05, 0.30),
                    forecast_run_at=run_at,
                    hour_of_day=near_dt.hour,
                    day_of_week=near_dt.weekday(),
                    month=near_dt.month,
                    gas_forecast_tj=75.0,
                    qni_mwflow=-150.0,
                    qni_violation_degree=0.0,
                    is_intervention=False,
                )
            )

    # Two buckets in the OLS band: h24_48 peak and h48_96 peak. OLS_MIN_OBS is
    # 50 per bucket, so 70 each leaves margin. Horizons vary across each
    # bucket so the horizons the sweeps serve sit inside the fitted range.
    for horizon_lo, horizon_hi in ((25.0, 47.0), (49.0, 95.0)):
        for i in range(70):
            interval_dt = (_ANCHOR - timedelta(days=i % 20)).replace(
                hour=17, minute=(i % 2) * 30
            )
            if horizon_lo > 48:
                interval_dt = interval_dt - timedelta(days=20)
            run_at = run_ats[i % len(run_ats)]
            horizon_hours = rng.uniform(horizon_lo, horizon_hi)
            # Spans the mild negatives the sweep below serves through stage 2,
            # so they sit inside the fitted domain (#117).
            forecast = rng.uniform(-0.08, 0.26)
            surplus = rng.uniform(500.0, 5000.0)
            solar = rng.uniform(0.0, 4000.0)
            demand50 = rng.uniform(5000.0, 9000.0)
            actual = max(
                0.0,
                1.4 * forecast + 0.02 - solar * 2e-5 + rng.gauss(0, 0.004),
            )
            observations.append(
                Observation(
                    interval_time=nem_iso(interval_dt),
                    horizon_hours=horizon_hours,
                    pd7day_forecast=forecast,
                    actual_rrp=actual,
                    forecast_run_at=run_at,
                    hour_of_day=17,
                    day_of_week=interval_dt.weekday(),
                    month=interval_dt.month,
                    gas_forecast_tj=75.0,
                    qni_mwflow=-150.0,
                    qni_violation_degree=0.0,
                    is_intervention=False,
                )
            )
            stpasa_by_key[f"{nem_iso(interval_dt)}|{run_at}"] = StpasaFeatures(
                log_surplus=math.log1p(surplus),
                log_solar=math.log1p(solar),
                log_demand=math.log(max(demand50, 1.0)),
                # Spans the 0.114 make_stpasa_interval serves (#147 gate).
                poe_spread_n=rng.uniform(0.08, 0.30),
                stpasa_run_at=STPASA_RUN_AT,
            )

    result = engine.fit(observations)
    result.ols_models = engine.fit_ols_stage2(observations, stpasa_by_key, region=region)

    store = CalibrationStore(MagicMock(), region)
    store._calibration = result
    store._fit_generation = 1
    return store


def make_pair(periods, stpasa_intervals=(), region="QLD1", **coordinator_kw):
    """A forecast sensor and a forecast chart camera on one coordinator and
    one calibration store, as a live install has.
    """
    store = fitted_store(region)
    coordinator = StubCoordinator(region, periods, stpasa_intervals, store, **coordinator_kw)

    entry = MagicMock()
    entry.entry_id = "entry_camera"
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=store, dispatch=None
    )

    sensor = PD7DayForecastSensor.__new__(PD7DayForecastSensor)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._store = store
    sensor._entry = entry
    sensor._attr_unique_id = f"nem_pd7day_{region.lower()}_forecast"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}

    camera = NemPd7dayForecastChartCamera.__new__(NemPd7dayForecastChartCamera)
    camera.coordinator = coordinator
    camera._region = region
    camera._entry = entry
    camera._image_bytes = b""
    camera._attr_unique_id = f"entry_camera_{region}_forecast_chart"
    camera.hass = MagicMock()

    return sensor, camera, coordinator, store


def make_camera(values, gas_tj=200.0, tight=True):
    """A forecast chart camera over a run whose half hourly prices are ``values``."""
    periods = [
        make_period(RUN_DT + timedelta(minutes=30 * (i + 1)), value)
        for i, value in enumerate(values)
    ]
    _sensor, camera, _coord, _store = make_pair(periods, gas_tj=gas_tj, tight=tight)
    return camera


def sensor_entries_by_time(sensor) -> dict[str, dict]:
    return {e["time"]: e for e in sensor._calibrated_forecast(sensor._price_data)}


def camera_entries_by_time(camera) -> dict[str, dict]:
    return {e["time"]: e for e in camera._build_forecast_data()}


def in_band_peak_periods() -> tuple[list[FakePeriod], list[StpasaInterval]]:
    """Intervals at 17:00 NEM on the next two days.

    Horizon is 37 h and 61 h from a 04:00 run, so both land inside the OLS band
    and inside a peak bucket the fixture fitted. That is exactly the
    combination issue #80 reported disagreeing.
    """
    periods: list[FakePeriod] = []
    stpasa: list[StpasaInterval] = []
    for day, value, solar in ((1, 0.12093, 1200.0), (2, 0.52396, 3400.0)):
        start_dt = (RUN_DT + timedelta(days=day)).replace(hour=17, minute=0)
        periods.append(make_period(start_dt, value))
        stpasa.append(make_stpasa_interval(start_dt, solar=solar))
    return periods, stpasa


def full_run_periods() -> tuple[list[FakePeriod], list[StpasaInterval]]:
    """A whole seven day run at half hourly resolution, with a coverage gap."""
    rng = random.Random(11)
    periods: list[FakePeriod] = []
    stpasa: list[StpasaInterval] = []
    for i in range(336):
        start_dt = RUN_DT + timedelta(minutes=30 * (i + 1))
        # A spread including mild negatives served by stage 2, a deep negative
        # below the fitted domain, which takes the isotonic_below_domain
        # branch, and a spike well above SPIKE_THRESHOLD.
        value = rng.choice([-0.30, -0.05, -0.00757, 0.0, 0.03, 0.12093, 0.52396, 3.4])
        periods.append(make_period(start_dt, value))
        # Leave a gap in the middle of the band so some in band intervals have
        # no STPASA row and must degrade to isotonic on both paths alike.
        if not 100 <= i < 120:
            stpasa.append(make_stpasa_interval(start_dt, solar=rng.uniform(0.0, 4000.0)))
    return periods, stpasa


# ── Platform setup does not wait for the first render ─────────────────────────

# How long the fake render blocks for. Comfortably longer than any plausible
# setup path, so an awaited render cannot pass by being fast.
RENDER_SECONDS = 0.5


class _FakeHass:
    """Runs executor jobs on a real thread and background tasks on the loop."""

    def __init__(self) -> None:
        self.background_tasks: list[asyncio.Task] = []

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def async_create_background_task(self, coro, name=None, eager_start=False):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.background_tasks.append(task)
        return task

    def async_create_task(self, coro, name=None):
        return self.async_create_background_task(coro, name=name)


class _SlowRenderCamera(_camera_mod._InitialRenderMixin):
    """Exercises the mixin in isolation with a deliberately slow render."""

    def __init__(self, hass: _FakeHass) -> None:
        self.hass = hass
        self.entity_id = "camera.nem_pd7day_qld1_price_tod_chart"
        self.render_started = asyncio.Event()
        self.image_bytes: bytes | None = None
        self._removals: list = []

    def async_on_remove(self, func) -> None:
        self._removals.append(func)

    def _render(self) -> bytes:
        time.sleep(RENDER_SECONDS)
        return b"PNG-BYTES"

    async def _async_refresh_image(self) -> None:
        self.render_started.set()
        self.image_bytes = await self.hass.async_add_executor_job(self._render)

    async def async_added_to_hass(self) -> None:
        self._schedule_initial_render()


def test_added_to_hass_returns_before_the_render_finishes():
    """Setup must not block on the render, which is what tripped the warning."""

    async def scenario():
        hass = _FakeHass()
        cam = _SlowRenderCamera(hass)

        started = time.monotonic()
        await cam.async_added_to_hass()
        elapsed = time.monotonic() - started

        assert elapsed < RENDER_SECONDS / 4, (
            f"async_added_to_hass took {elapsed:.2f}s; it is waiting for the "
            "render instead of scheduling it"
        )
        assert cam.image_bytes is None, "render should still be in flight"

        # The render was genuinely scheduled, not dropped.
        await asyncio.wait_for(cam.render_started.wait(), timeout=1)
        await asyncio.gather(*hass.background_tasks)
        assert cam.image_bytes == b"PNG-BYTES"

    run_async(scenario())


def test_fifteen_cameras_all_set_up_well_inside_the_warning_threshold():
    """Five regions of three cameras each is the live configuration."""

    async def scenario():
        hass = _FakeHass()
        cams = [_SlowRenderCamera(hass) for _ in range(15)]

        started = time.monotonic()
        for cam in cams:
            await cam.async_added_to_hass()
        elapsed = time.monotonic() - started

        assert elapsed < 10, (
            f"setting up 15 cameras took {elapsed:.2f}s, at or over Home "
            "Assistant's platform warning threshold"
        )

        for task in hass.background_tasks:
            task.cancel()
        await asyncio.gather(*hass.background_tasks, return_exceptions=True)

    run_async(scenario())


def test_initial_render_is_cancelled_on_entity_removal():
    """An in-flight render must not outlive the entity."""

    async def scenario():
        hass = _FakeHass()
        cam = _SlowRenderCamera(hass)
        await cam.async_added_to_hass()

        assert cam._removals, "no removal callback registered for the render"
        for cancel in cam._removals:
            cancel()

        results = await asyncio.gather(*hass.background_tasks, return_exceptions=True)
        assert any(
            isinstance(r, asyncio.CancelledError) for r in results
        ) or all(r is None for r in results)

    run_async(scenario())


@pytest.mark.parametrize(
    "class_name",
    [
        "NemPd7dayTodCamera",
        "NemPd7dayBiasChartCamera",
        "NemPd7dayIsoChartCamera",
        "NemPd7dayForecastChartCamera",
    ],
)
def test_every_camera_class_schedules_rather_than_awaits(class_name):
    """No camera class may await its first render in async_added_to_hass."""
    cls = getattr(_camera_mod, class_name)
    assert issubclass(cls, _camera_mod._InitialRenderMixin)

    source = inspect.getsource(cls.async_added_to_hass)
    assert "_schedule_initial_render" in source
    assert "await self._async_refresh_image" not in source


# ── Parity with the forecast sensor, issue #80 ────────────────────────────────

def test_camera_matches_forecast_sensor_for_the_same_interval():
    """Every calibration field the camera publishes equals the sensor's.

    The camera does not merely draw a line: it writes calibrated, p10, p50,
    p90, ols_mae, calibrated_source and n_obs into the chart data, so all of
    them are compared. The sensor must reach isotonic+stpasa on this fixture,
    or parity would prove nothing; on main the camera could not reach that
    branch at all and reported the isotonic only number, a band clamped around
    it and the source label "isotonic".
    """
    periods, stpasa = in_band_peak_periods()
    sensor, camera, _coord, _store = make_pair(periods, stpasa)
    sf = sensor_entries_by_time(sensor)
    cf = camera_entries_by_time(camera)

    sources = {k: e["calibrated_source"] for k, e in sf.items()}
    assert sources, "no calibrated entries built"
    assert "isotonic+stpasa" in sources.values(), (
        "fixture did not reach the stage 2 branch, so a parity assertion over "
        f"it would be vacuous: sources={sources}"
    )
    assert set(sf) == set(cf), "camera and sensor covered different intervals"
    for key, cam in cf.items():
        sen = sf[key]
        assert cam["raw_value"] == sen["raw_value"], (
            "the two must start from the same raw price"
        )
        for field in CAL_KEYS:
            assert cam.get(field) == sen.get(field), (
                f"{field} disagrees at {key}: camera {cam.get(field)!r} vs "
                f"sensor {sen.get(field)!r}, source {sen.get('calibrated_source')!r}"
            )


def test_parity_sweep_over_a_full_run():
    """Sweep every interval of a seven day run, not only the hand picked ones.

    The run covers h0 to h168 at every half hour and every hour of day, so the
    sweep crosses both edges of the OLS band, buckets that fitted and buckets
    that did not, negative prices that pass through untouched and in band
    intervals with no STPASA row. Parity must hold on all of them, and the
    counts are asserted so the sweep cannot pass by covering nothing.
    """
    periods, stpasa = full_run_periods()
    sensor, camera, _coord, _store = make_pair(periods, stpasa)
    sf = sensor_entries_by_time(sensor)
    cf = camera_entries_by_time(camera)

    assert len(cf) == 336, f"expected 336 intervals, got {len(cf)}"
    sources: dict[str, int] = {}
    mismatches: list[str] = []
    for key, cam in cf.items():
        sen = sf[key]
        src = sen.get("calibrated_source")
        sources[src] = sources.get(src, 0) + 1
        for field in CAL_KEYS:
            if cam.get(field) != sen.get(field):
                mismatches.append(
                    f"{key} src={src} {field}: camera {cam.get(field)!r} vs "
                    f"sensor {sen.get(field)!r}"
                )
    assert not mismatches, "camera disagrees with the sensor on:\n" + "\n".join(
        mismatches[:10]
    )
    assert sources.get("isotonic+stpasa", 0) >= 20, (
        f"sweep did not cover enough stage 2 intervals: {sources}"
    )
    non_stage2 = sum(v for k, v in sources.items() if k != "isotonic+stpasa")
    assert non_stage2 >= 20, (
        f"sweep covered only the stage 2 branch, so it does not show the "
        f"degrade paths agree as well: {sources}"
    )


def test_camera_band_is_reclamped_around_the_stage_two_value():
    """The band the chart shades contains the line the chart draws.

    PR #71 re-derives p10, p50 and p90 around the stage 2 point estimate in
    ``CalibrationResult.apply`` step 7. The camera reads p10 and p90 straight
    out of that dict and hands them to ``fill_between``, so once stage 2 can
    apply the camera inherits the re-clamp. The band is self consistent, not a
    stage 2 interval: where the prediction lands outside the quantile fits the
    nearer bound collapses onto the point estimate, so the bound is non strict.
    """
    periods, stpasa = full_run_periods()
    _sensor, camera, _coord, _store = make_pair(periods, stpasa)
    entries = camera._build_forecast_data()
    stage2 = [e for e in entries if e.get("calibrated_source") == "isotonic+stpasa"]
    assert len(stage2) >= 20, f"not enough stage 2 intervals to test: {len(stage2)}"

    for e in stage2:
        cal, p10, p90 = e["calibrated"], e["p10"], e["p90"]
        assert p10 is not None and p90 is not None, f"missing band at {e['time']}"
        assert p10 <= cal <= p90, (
            f"chart would shade a band that excludes its own line at "
            f"{e['time']}: p10={p10} calibrated={cal} p90={p90}"
        )


def test_camera_goes_through_the_shared_entry_point():
    """The camera calls ``calibrate_interval``, not a private assembly.

    Issue #66 was three call sites drifting apart. A fix that copied the
    feature assembly into camera.py would satisfy every parity assertion above
    on the day it landed and then drift again, so the route itself is pinned:
    the camera must consult the shared helper once per interval, threading
    ``run_at_iso`` through for the per run stage 2 band floor of issue #68.
    """
    periods, stpasa = in_band_peak_periods()
    _sensor, camera, _coord, _store = make_pair(periods, stpasa)
    real = _camera_mod.calibrate_interval
    calls: list[tuple] = []

    def spy(store, coordinator, value, interval_key, h, hour, **kwargs):
        calls.append((interval_key, h, hour, kwargs.get("run_at_iso")))
        return real(store, coordinator, value, interval_key, h, hour, **kwargs)

    with patch.object(_camera_mod, "calibrate_interval", spy):
        camera._build_forecast_data()

    assert len(calls) == len(periods), (
        f"expected one shared call per interval, got {len(calls)} for "
        f"{len(periods)} intervals"
    )
    assert all(c[3] == RUN_AT for c in calls), (
        f"run_at_iso must be threaded through for the per run stage 2 band "
        f"floor of issue #68: {calls}"
    )


def test_build_forecast_data_is_safe_off_the_event_loop():
    """The chart data builds identically from a worker thread.

    ``_render`` runs ``_build_forecast_data`` under
    ``hass.async_add_executor_job``, so the shared helper is reached from a
    worker thread rather than the event loop. It only reads, and the one cache
    it writes, the STPASA index inside ``coordinator.stpasa_index``, is already
    reached from both threads by the sensor's calibration warm.
    """
    periods, stpasa = full_run_periods()
    _sensor, camera, _coord, _store = make_pair(periods, stpasa)
    on_this_thread = camera._build_forecast_data()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        on_worker = pool.submit(camera._build_forecast_data).result()

    assert len(on_worker) == len(on_this_thread)
    for a, b in zip(on_this_thread, on_worker):
        assert a["time"] == b["time"]
        for field in CAL_KEYS:
            assert a.get(field) == b.get(field), (
                f"{field} differs by calling thread at {a['time']}: "
                f"{a.get(field)!r} vs {b.get(field)!r}"
            )


def test_uncalibratable_interval_is_none_never_zero():
    """A missing raw price yields None, not a plausible looking 0.

    ``calibrate_interval`` returns None when there is no store or no raw price,
    and the camera must carry that through rather than publishing a calibrated
    zero, which is indistinguishable from a genuine zero price forecast.
    """
    start_dt = (RUN_DT + timedelta(days=1)).replace(hour=17, minute=0)
    periods = [make_period(start_dt, None)]
    stpasa = [make_stpasa_interval(start_dt)]
    _sensor, camera, _coord, _store = make_pair(periods, stpasa)

    entries = camera._build_forecast_data()
    assert len(entries) == 1
    e = entries[0]
    assert e.get("calibrated") is None, f"expected no calibrated value, got {e.get('calibrated')!r}"
    assert e["value"] is None, f"expected None, got {e['value']!r}"
    assert e["value"] != 0


def test_no_store_still_renders_raw():
    """With no calibration store the chart falls back to the raw price."""
    periods, stpasa = in_band_peak_periods()
    _sensor, camera, coordinator, _store = make_pair(periods, stpasa)
    coordinator._store = None

    entries = camera._build_forecast_data()
    assert len(entries) == len(periods)
    for e, p in zip(entries, periods):
        assert e["value"] == p.value
        assert "calibrated" not in e


# ── spike_credible travels into the chart entries, issue #84 ──────────────────

@pytest.mark.parametrize(
    "gas, tight, expected",
    [
        (200.0, True, True),
        (100.0, True, False),
        (200.0, False, False),
        (100.0, False, False),
        (None, True, None),
        (200.0, None, None),
        (None, None, None),
    ],
)
def test_spike_credible_is_the_covariate_gate_and_keeps_its_tri_state(gas, tight, expected):
    """The bare claim of issue #84, plus the values the key may take.

    On main the key is absent from every entry, so the chart can never draw a
    callout. The gate is gas above 150 TJ and the region's own network tight,
    both together (#176 replaced a single QNI flow threshold applied to every
    region with each region's own links).
    A missing covariate carries through as None, never False: reading it as
    False would say the market data ruled the spike out when in fact it was
    never consulted. Only True belongs in the prior spike set.
    """
    assert SPIKE_GAS_THRESHOLD_TJ == 150.0
    assert SPIKE_CAPABILITY_DEPRESSION == 0.25
    camera = make_camera([0.10, 12.0, 0.08], gas_tj=gas, tight=tight)
    entries = camera._build_forecast_data()
    spike = [e for e in entries if e["raw_value"] >= SPIKE_THRESHOLD]
    assert len(spike) == 1, "fixture produced no spike interval"
    assert "spike_credible" in spike[0], (
        "camera dropped spike_credible, so the chart can never draw a callout: "
        f"keys={sorted(spike[0])}"
    )
    assert spike[0]["spike_credible"] is expected, (
        f"gas={gas} tight={tight} gave {spike[0]['spike_credible']!r}, expected {expected!r}"
    )
    saved = camera._prior_spike_intervals()
    assert saved == ({spike[0]["time"]} if expected is True else set())


def test_key_is_absent_below_the_spike_threshold():
    """Below SPIKE_THRESHOLD the question is never asked, so no key is written.

    Absent, None and True are three different facts and the camera keeps them
    apart. A default of False here would be the missing-data-as-zero mistake in
    another dress.
    """
    camera = make_camera([0.10, 2.99, 3.00])
    entries = camera._build_forecast_data()
    assert "spike_credible" not in entries[0]
    assert "spike_credible" not in entries[1]
    assert entries[2]["spike_credible"] is True


def test_the_spike_interval_set_is_no_longer_always_empty():
    """``_save_spike_intervals`` accumulated nothing before this change."""
    camera = make_camera([0.10, 12.0, 0.08, 9.0])
    camera._build_forecast_data()
    saved = camera._prior_spike_intervals()
    assert len(saved) == 2, f"expected both spike intervals to be saved, got {saved}"


def test_spike_first_run_now_distinguishes_a_repeat_from_a_new_spike():
    """The persistence scoring the field exists for had never distinguished anything.

    With the prior set always empty, ``spike_first_run`` was True for every
    interval of every run, so ``_is_spike_callout_eligible`` could only ever
    have returned "candidate". A confirmed spike was unreachable twice over.
    """
    camera = make_camera([0.10, 12.0, 0.08])
    first = camera._build_forecast_data()
    assert [e["spike_first_run"] for e in first] == [True, True, True]
    second = camera._build_forecast_data()
    flags = {e["time"]: e["spike_first_run"] for e in second}
    spike_time = first[1]["time"]
    assert flags[spike_time] is False, (
        "a spike seen in the previous run is still being reported as first run"
    )
    assert flags[first[0]["time"]] is True
