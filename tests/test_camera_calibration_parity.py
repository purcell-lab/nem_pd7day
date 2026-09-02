"""
Parity between the forecast chart camera and the price forecast sensor, issue #80.

The chart and the sensor describe the same interval of the same PD7DAY run from
the same raw price, and they disagreed. ``camera.py`` called
``CalibrationStore.apply_to_price`` directly with the gas and QNI covariates
only, no ``stpasa_features`` and no ``run_features``. The stage 2 gate in
``CalibrationResult.apply`` needs both, so the camera could only ever take the
isotonic only branch and never rendered ``isotonic+stpasa``, while the sensor
did so routinely. The camera then wrote its own ``calibrated``, ``p10``,
``p50``, ``p90`` and ``calibrated_source`` into the chart data, so it drew a
different line, shaded a different band and labelled a different source for an
interval the sensor had already published.

This is the same defect as issue #66 in a third call site. The fix is the one
PR #77 established: route through ``calibration_inputs.calibrate_interval``,
the single shared entry point, rather than adding a fourth private copy of the
feature assembly.

These tests run a real fitted calibration, isotonic plus a real stage 2 OLS
fit, behind a real ``CalibrationStore``, and compare the camera's chart data
against the sensor's forecast attribute interval by interval. The fixture is
adapted from tests/test_tariff_calibration_parity.py, which pinned the same
property for the tariff sensors.

Run with:  python -m pytest tests/test_camera_calibration_parity.py -v
or simply: python tests/test_camera_calibration_parity.py
"""
from __future__ import annotations

import concurrent.futures
import enum as _enum
import importlib.util
import math
import os
import random
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Repo root on sys.path so the file also runs standalone, per repo convention.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from custom_components.nem_pd7day.calibration_engine import (
    CalibrationEngine,
    Observation,
    RunFeatures,
    StpasaFeatures,
)
from custom_components.nem_pd7day.calibration_store import CalibrationStore
from custom_components.nem_pd7day.const import DOMAIN
from custom_components.nem_pd7day.nem_time import interval_start, parse_iso, to_nem_iso
from custom_components.nem_pd7day.sensor import PD7DayForecastSensor
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult


def _load_camera_module():
    """Import camera.py, stubbing the Home Assistant camera platform if needed.

    Several test modules in this suite install MagicMock stand ins for the
    ``homeassistant`` package at import time, and whichever runs first wins for
    the whole session. Once ``homeassistant.components`` is a MagicMock rather
    than a package, ``homeassistant.components.camera`` cannot be imported at
    all, so a plain top level import of camera.py passes standalone and fails
    in a full suite run. The stubs below mirror tests/test_camera_setup.py and
    are installed only when the real import has already been made impossible.
    Nothing under test touches the ``Camera`` base class: the tests drive
    ``_build_forecast_data`` on an instance built with ``__new__``.
    """
    try:
        from custom_components.nem_pd7day.camera import (
            NemPd7dayForecastChartCamera as _cls,
        )
        return sys.modules[_cls.__module__]
    except (ImportError, AttributeError, TypeError):
        pass

    class _CameraEntityFeature(_enum.IntFlag):
        NONE = 0

    class _FakeCamera:
        def __init__(self) -> None:
            self._removals: list = []

        def async_on_remove(self, func) -> None:
            self._removals.append(func)

    class _FakeCoordinatorEntity:
        def __init__(self, coordinator=None, **kwargs):
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls

    camera_stub = MagicMock()
    camera_stub.Camera = _FakeCamera
    camera_stub.CameraEntityFeature = _CameraEntityFeature
    sys.modules["homeassistant.components.camera"] = camera_stub

    uc_stub = MagicMock()
    uc_stub.CoordinatorEntity = _FakeCoordinatorEntity
    uc_stub.DataUpdateCoordinator = MagicMock()
    uc_stub.UpdateFailed = Exception
    sys.modules["homeassistant.helpers.update_coordinator"] = uc_stub

    name = "custom_components.nem_pd7day.camera"
    path = os.path.join(_ROOT, "custom_components", "nem_pd7day", "camera.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The module object the camera class actually lives in. Patching has to target
# this one: loading an integration module a second time through importlib means
# the package attribute and sys.modules can point at different objects, and
# patching the wrong one silently does nothing.
_camera_mod = _load_camera_module()
NemPd7dayForecastChartCamera = _camera_mod.NemPd7dayForecastChartCamera

NEM_TZ = timezone(timedelta(hours=10))

# Observations must stay inside the engine's 90 day training window, so the
# fixture is anchored to now rather than to a fixed calendar date.
_ANCHOR = datetime.now(NEM_TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)

RUN_AT = to_nem_iso(_ANCHOR.replace(hour=4))
STPASA_RUN_AT = to_nem_iso(_ANCHOR.replace(hour=3))

# The calibration fields both entities publish for one interval. Parity is
# asserted on all of them, not only on the point estimate, because the camera
# publishes the band and the source label too and those were wrong as well.
CAL_KEYS = (
    "calibrated",
    "p10",
    "p50",
    "p90",
    "ols_mae",
    "calibrated_source",
    "n_obs",
)


def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


@dataclass
class FakePeriod:
    """A PricePeriod: ``time`` is the interval START, ``nemtime`` the END."""

    time: str
    nemtime: str
    value: float


def make_period(start_dt: datetime, value: float) -> FakePeriod:
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


class FakeCoordinator:
    """Coordinator stand-in exposing what the calibration inputs actually read.

    ``stpasa_index`` builds the index exactly as ``PD7DayCoordinator`` does, so
    the keys under test are real interval START strings. ``_store`` is set to
    the same object the sensor is handed, which is what the live wiring does:
    ``__init__.py`` passes one ``CalibrationStore`` to the coordinator and puts
    the same instance on ``entry.runtime_data.store``.
    """

    def __init__(self, region: str, periods: list[FakePeriod], stpasa_intervals, store):
        price_data = types.SimpleNamespace(
            forecast=list(periods),
            forecast_generated_at=RUN_AT,
        )
        self.data = types.SimpleNamespace(
            prices={region: price_data},
            interconnectors={},
            market_summary=None,
        )
        self.last_update_success = True
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


def fitted_store(region: str = "QLD1") -> CalibrationStore:
    """A real CalibrationStore holding a real isotonic plus stage 2 OLS fit.

    Nothing here stubs the calibration itself, so the numbers compared below
    come out of ``CalibrationResult.apply`` and the test is sensitive to which
    arguments each caller supplies and to nothing else.
    """
    rng = random.Random(7)
    engine = CalibrationEngine()
    observations: list[Observation] = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}

    train_run_at = nem_iso(_ANCHOR.replace(hour=3, minute=30) - timedelta(days=25))
    for j in range(6):
        near_dt = (_ANCHOR - timedelta(days=25)).replace(hour=4 + j, minute=0)
        observations.append(
            Observation(
                interval_time=nem_iso(near_dt),
                horizon_hours=2.0 + j,
                pd7day_forecast=rng.uniform(0.05, 0.25),
                actual_rrp=rng.uniform(0.05, 0.30),
                forecast_run_at=train_run_at,
                hour_of_day=4 + j,
                day_of_week=near_dt.weekday(),
                month=near_dt.month,
                gas_forecast_tj=75.0,
                qni_mwflow=-150.0,
                qni_violation_degree=0.0,
                is_intervention=False,
            )
        )

    # Two buckets in the OLS band: h24_48 peak and h48_96 peak. OLS_MIN_OBS is
    # 50 per bucket, so 70 each leaves margin.
    for horizon_hours in (30.0, 60.0):
        for i in range(70):
            interval_dt = (_ANCHOR - timedelta(days=i % 20)).replace(
                hour=17, minute=(i % 2) * 30
            )
            if horizon_hours > 48:
                interval_dt = interval_dt - timedelta(days=20)
            forecast = rng.uniform(0.04, 0.26)
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
                    forecast_run_at=train_run_at,
                    hour_of_day=17,
                    day_of_week=interval_dt.weekday(),
                    month=interval_dt.month,
                    gas_forecast_tj=75.0,
                    qni_mwflow=-150.0,
                    qni_violation_degree=0.0,
                    is_intervention=False,
                )
            )
            stpasa_by_key[f"{nem_iso(interval_dt)}|{train_run_at}"] = StpasaFeatures(
                log_surplus=math.log1p(surplus),
                log_solar=math.log1p(solar),
                log_demand=math.log(max(demand50, 1.0)),
                poe_spread_n=(demand50 * 1.1 - demand50 * 0.9) / demand50,
                stpasa_run_at=STPASA_RUN_AT,
            )

    result = engine.fit(observations)
    result.ols_models = engine.fit_ols_stage2(observations, stpasa_by_key, region=region)

    store = CalibrationStore(MagicMock(), region)
    store._calibration = result
    store._fit_generation = 1
    return store


def make_pair(periods: list[FakePeriod], stpasa_intervals, region: str = "QLD1"):
    """Build a forecast sensor and a forecast chart camera on one coordinator
    and one calibration store, as a live install has.
    """
    store = fitted_store(region)
    coordinator = FakeCoordinator(region, periods, stpasa_intervals, store)

    entry = MagicMock()
    entry.entry_id = "entry_camera_parity"
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
    camera._attr_unique_id = f"entry_camera_parity_{region}_forecast_chart"
    camera.hass = MagicMock()

    return sensor, camera, coordinator, store


def sensor_entries_by_time(sensor) -> dict[str, dict]:
    d = sensor._price_data
    return {e["time"]: e for e in sensor._calibrated_forecast(d)}


def camera_entries_by_time(camera) -> dict[str, dict]:
    return {e["time"]: e for e in camera._build_forecast_data()}


def in_band_peak_periods() -> tuple[list[FakePeriod], list[StpasaInterval]]:
    """Intervals at 17:00 NEM on the next two days.

    Horizon is 37 h and 61 h from a 04:00 run, so both land inside the OLS band
    and inside a peak bucket the fixture fitted. That is exactly the
    combination the issue reported disagreeing.
    """
    run_dt = parse_iso(RUN_AT)
    periods: list[FakePeriod] = []
    stpasa: list[StpasaInterval] = []
    for day, value, solar in ((1, 0.12093, 1200.0), (2, 0.52396, 3400.0)):
        start_dt = (run_dt + timedelta(days=day)).replace(hour=17, minute=0)
        periods.append(make_period(start_dt, value))
        stpasa.append(make_stpasa_interval(start_dt, solar=solar))
    return periods, stpasa


def full_run_periods() -> tuple[list[FakePeriod], list[StpasaInterval]]:
    """A whole seven day run at half hourly resolution, with a coverage gap."""
    run_dt = parse_iso(RUN_AT)
    rng = random.Random(11)
    periods: list[FakePeriod] = []
    stpasa: list[StpasaInterval] = []
    for i in range(336):
        start_dt = run_dt + timedelta(minutes=30 * (i + 1))
        # A spread including negatives, which take the passthrough_negative
        # branch, and a spike well above SPIKE_THRESHOLD.
        value = rng.choice([-0.05, -0.00757, 0.0, 0.03, 0.12093, 0.52396, 3.4])
        periods.append(make_period(start_dt, value))
        # Leave a gap in the middle of the band so some in band intervals have
        # no STPASA row and must degrade to isotonic on both paths alike.
        if not 100 <= i < 120:
            stpasa.append(make_stpasa_interval(start_dt, solar=rng.uniform(0.0, 4000.0)))
    return periods, stpasa


# Tests


def test_fixture_actually_reaches_the_stpasa_branch():
    """The sensor must reach isotonic+stpasa, or parity proves nothing."""
    periods, stpasa = in_band_peak_periods()
    sensor, _camera, _coord, _store = make_pair(periods, stpasa)
    sources = {e["time"]: e["calibrated_source"] for e in sensor_entries_by_time(sensor).values()}
    assert sources, "no calibrated entries built"
    assert "isotonic+stpasa" in sources.values(), (
        "fixture did not reach the stage 2 branch, so a parity assertion over "
        f"it would be vacuous: sources={sources}"
    )
    print("  PASS: fixture reaches the isotonic+stpasa branch on the sensor")


def test_camera_can_render_the_stpasa_branch():
    """The chart must be able to show isotonic+stpasa at all.

    This is the bare claim of issue #80 and it fails on main, where the camera
    passes neither feature group and the stage 2 gate therefore always returns
    the isotonic only result.
    """
    periods, stpasa = in_band_peak_periods()
    _sensor, camera, _coord, _store = make_pair(periods, stpasa)
    sources = [e.get("calibrated_source") for e in camera._build_forecast_data()]
    assert "isotonic+stpasa" in sources, (
        f"camera never reached the stage 2 branch: sources={sources}"
    )
    print("  PASS: camera renders isotonic+stpasa")


def test_camera_matches_forecast_sensor_for_the_same_interval():
    """Every calibration field the camera publishes equals the sensor's.

    The camera does not merely draw a line: it writes calibrated, p10, p50,
    p90, ols_mae, calibrated_source and n_obs into the chart data, so all of
    them are compared. On main the camera reports the isotonic only number, a
    band clamped around it and the source label "isotonic".
    """
    periods, stpasa = in_band_peak_periods()
    sensor, camera, _coord, _store = make_pair(periods, stpasa)
    sf = sensor_entries_by_time(sensor)
    cf = camera_entries_by_time(camera)

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
    print(f"  PASS: camera matches the sensor on {len(cf)} intervals")


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
    print(f"  PASS: parity across 336 intervals, sources={sources}")


def test_camera_band_is_reclamped_around_the_stage_two_value():
    """The band the chart shades contains the line the chart draws.

    PR #71 re-derives p10, p50 and p90 around the stage 2 point estimate in
    ``CalibrationResult.apply`` step 7. The camera reads p10 and p90 straight
    out of that dict and hands them to ``fill_between``, so once stage 2 can
    apply the camera inherits the re-clamp. Before this fix the question did
    not arise, because the camera could not reach step 7.

    The band is self consistent, not a stage 2 interval: the quantile fits know
    nothing about the STPASA features, so where the prediction lands outside
    them the nearer bound collapses onto the point estimate. That is the
    documented behaviour of step 7 and it is asserted as a non strict bound
    here rather than a strict one.
    """
    periods, stpasa = full_run_periods()
    _sensor, camera, _coord, _store = make_pair(periods, stpasa)
    entries = camera._build_forecast_data()
    stage2 = [e for e in entries if e.get("calibrated_source") == "isotonic+stpasa"]
    assert len(stage2) >= 20, f"not enough stage 2 intervals to test: {len(stage2)}"

    collapsed = 0
    for e in stage2:
        cal, p10, p90 = e["calibrated"], e["p10"], e["p90"]
        assert p10 is not None and p90 is not None, f"missing band at {e['time']}"
        assert p10 <= cal <= p90, (
            f"chart would shade a band that excludes its own line at "
            f"{e['time']}: p10={p10} calibrated={cal} p90={p90}"
        )
        if p10 == cal or p90 == cal:
            collapsed += 1
    print(
        f"  PASS: band contains the drawn value on {len(stage2)} stage 2 "
        f"intervals, {collapsed} with a bound collapsed onto it"
    )


def test_camera_goes_through_the_shared_entry_point():
    """The camera calls ``calibrate_interval``, not a private assembly.

    Issue #66 was three call sites drifting apart. A fix that copied the
    feature assembly into camera.py would satisfy every parity assertion above
    on the day it landed and then drift again, so the route itself is pinned:
    the camera must consult the shared helper once per interval.
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
    print(f"  PASS: camera routed {len(calls)} intervals through calibrate_interval")


def test_build_forecast_data_is_safe_off_the_event_loop():
    """The chart data builds identically from a worker thread.

    ``_render`` runs ``_build_forecast_data`` under
    ``hass.async_add_executor_job``, so the shared helper is reached from a
    worker thread rather than the event loop. It only reads, and the one cache
    it writes, the STPASA index inside ``coordinator.stpasa_index``, is already
    reached from both threads by the sensor's calibration warm. This pins that
    the answer does not depend on which thread asked.
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
    print(f"  PASS: identical chart data from a worker thread, {len(on_worker)} intervals")


def test_uncalibratable_interval_is_none_never_zero():
    """A missing raw price yields None, not a plausible looking 0.

    ``calibrate_interval`` returns None when there is no store or no raw price,
    and the camera must carry that through rather than publishing a calibrated
    zero, which is indistinguishable from a genuine zero price forecast.
    """
    run_dt = parse_iso(RUN_AT)
    start_dt = (run_dt + timedelta(days=1)).replace(hour=17, minute=0)
    periods = [make_period(start_dt, None)]
    stpasa = [make_stpasa_interval(start_dt)]
    _sensor, camera, _coord, _store = make_pair(periods, stpasa)

    entries = camera._build_forecast_data()
    assert len(entries) == 1
    e = entries[0]
    assert e.get("calibrated") is None, f"expected no calibrated value, got {e.get('calibrated')!r}"
    assert e["value"] is None, f"expected None, got {e['value']!r}"
    assert e["value"] != 0
    print("  PASS: uncalibratable interval carries None, not 0")


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
    print("  PASS: no calibration store degrades to the raw price")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("All camera calibration parity tests passed.")
