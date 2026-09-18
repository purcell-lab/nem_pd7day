"""
Parity between the tariff sensors and the price forecast sensor, issue #66.

The two sensors describe the same interval of the same PD7DAY run from the same
raw price, and they used to publish different calibrated spot prices: the
forecast path passed STPASA features, run features and the gas/QNI covariates
to ``CalibrationStore.apply_to_price`` while the tariff path passed only raw
price, horizon and hour, so it silently took the isotonic only branch. On a
live five region install that disagreed on 183 of 183 intervals that had STPASA
features, by up to 0.633470 $/kWh. Issue #68 then made the stage 2 band floor a
property of the run's STPASA coverage; the shared entry point threads the run
timestamp through so the tariff path gets the same edge.

These tests run a real fitted calibration, isotonic plus a real stage 2 OLS
fit, behind a real ``CalibrationStore``, and compare the sensors interval by
interval. The central assertion is direct equality of the calibrated spot: the
``spot`` key a tariff sensor publishes must equal the ``value`` the price
forecast sensor publishes for the same interval.

``RUN_AT``, ``_tariff_mod``, ``make_period``, ``make_stpasa_interval`` and
``make_sensors`` are imported by test_tariff_spot_memo.py and
test_tariff_write_latency.py.

Run with:  python -m pytest tests/test_tariff_calibration_parity.py -v
"""
from __future__ import annotations

import math
import random
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from unittest.mock import MagicMock, patch

import pytest

import support
from support import NEM_TZ, install_ha_stubs, load_chain, nem_iso

install_ha_stubs()

(
    _const_mod, _nem_time, _client_mod, _engine_mod, _store_mod, _inputs_mod,
    _stpasa_mod, _coord_mod, _tariff_mod, _sensor_mod,
) = load_chain(
    "const", "nem_time", "pd7day_client", "calibration_engine", "calibration_store",
    "calibration_inputs", "stpasa_client", "coordinator", "tariff_sensor", "sensor",
)

CalibrationEngine = _engine_mod.CalibrationEngine
Observation = _engine_mod.Observation
RunFeatures = _engine_mod.RunFeatures
StpasaFeatures = _engine_mod.StpasaFeatures
CalibrationStore = _store_mod.CalibrationStore
StpasaInterval = _stpasa_mod.StpasaInterval
StpasaResult = _stpasa_mod.StpasaResult
PD7DayForecastSensor = _sensor_mod.PD7DayForecastSensor
NemPd7dayTariffSensor = _tariff_mod.NemPd7dayTariffSensor
NemPd7dayExportTariffSensor = _tariff_mod.NemPd7dayExportTariffSensor
DOMAIN = _const_mod.DOMAIN
interval_start = _nem_time.interval_start
parse_iso = _nem_time.parse_iso
to_nem_iso = _nem_time.to_nem_iso

expected_import_price = partial(support.expected_import_price, _tariff_mod)

# Observations must stay inside the engine's 90 day training window, so the
# fixture is anchored to now rather than to a fixed calendar date.
_ANCHOR = datetime.now(NEM_TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)

# The run is anchored to the same clock. The forecast intervals below sit at
# horizons that fall inside the OLS band (22 h to 120 h), which is the only
# range where the two paths could ever have disagreed.
RUN_AT = to_nem_iso(_ANCHOR.replace(hour=4))
STPASA_RUN_AT = to_nem_iso(_ANCHOR.replace(hour=3))

# (base, width) of the near-term raw prices of each training run in
# fitted_store: run_max_h6 runs 0.04 to 0.59, run_mean 0.04 to 0.55 and
# run_spread 0.03 to 0.40 across them, bracketing what FakeCoordinator serves.
_RUN_SHAPES = (
    (0.02, 0.04), (0.05, 0.10), (0.10, 0.16), (0.16, 0.24), (0.22, 0.36), (0.30, 0.50),
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

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
    the keys under test are real interval START strings.
    """

    def __init__(self, region: str, periods: list[FakePeriod], stpasa_intervals):
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
        # The same object both sensors would read from the live coordinator.
        return RunFeatures(run_max_h6_rrp=0.24, run_mean_rrp=0.11, run_spread=0.06)


def fitted_store(region: str = "QLD1") -> CalibrationStore:
    """A real CalibrationStore holding a real isotonic plus stage 2 OLS fit.

    Nothing here is a stub of the calibration itself: the numbers the two
    sensors compare come out of ``CalibrationResult.apply``, so the test is
    sensitive to which arguments each sensor supplies and to nothing else.
    """
    rng = random.Random(7)
    engine = CalibrationEngine()
    observations: list[Observation] = []
    stpasa_by_key: dict[str, StpasaFeatures] = {}

    # Several training runs. The stage 2 fit needs run features for the run
    # each in band row belongs to, derived from the run's own near term rows,
    # so each run seeds a few below h6. Several rather than one because the
    # stage-2 serving gate (#147) refuses a feature outside its training
    # range and the three run features are constant within a run: a
    # single-run fixture has zero-width ranges there and would refuse the
    # run FakeCoordinator serves. The near-term prices are laid out per run
    # so the run features bracket current_run_features.
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
    # bucket so the horizons the sweep serves (37 h, 61 h, 85 h) sit inside
    # the fitted range (#147).
    for horizon_lo, horizon_hi in ((25.0, 47.0), (49.0, 95.0)):
        for i in range(70):
            interval_dt = (_ANCHOR - timedelta(days=i % 20)).replace(hour=17, minute=(i % 2) * 30)
            # Distinct interval keys within the run: vary the day, and offset
            # the second bucket so the two do not collide on one key.
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
            actual = max(0.0, 1.4 * forecast + 0.02 - solar * 2e-5 + rng.gauss(0, 0.004))
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


def make_sensors(periods: list[FakePeriod], stpasa_intervals, region: str = "QLD1"):
    """A forecast sensor, an import tariff sensor and an export tariff sensor
    on one coordinator and one calibration store, as a live install has.
    """
    coordinator = FakeCoordinator(region, periods, stpasa_intervals)
    store = fitted_store(region)

    entry = MagicMock()
    entry.entry_id = "entry_parity"
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator, store=store, dispatch=None)

    forecast = PD7DayForecastSensor.__new__(PD7DayForecastSensor)
    forecast.coordinator = coordinator
    forecast._region = region
    forecast._store = store
    forecast._entry = entry
    forecast._attr_unique_id = f"nem_pd7day_{region.lower()}_forecast"
    forecast.hass = MagicMock()
    forecast.hass.data = {DOMAIN: {}}

    tariff = NemPd7dayTariffSensor.__new__(NemPd7dayTariffSensor)
    tariff.coordinator = coordinator
    tariff._region = region
    tariff._distributor = "energex"
    tariff._tariff_code = "8400"
    tariff._entry = entry
    tariff._store = store
    tariff._attr_unique_id = f"entry_parity_{region}_energex_8400_tariff"
    tariff.hass = MagicMock()
    tariff.hass.data = {DOMAIN: {}}
    tariff.hass.states.get.return_value = None

    export = NemPd7dayExportTariffSensor.__new__(NemPd7dayExportTariffSensor)
    export.coordinator = coordinator
    export._region = region
    export._distributor = "energex"
    export._import_code = "8400"
    export._export_code = "8400"
    export._entry = entry
    export._store = store
    export._attr_unique_id = f"entry_parity_{region}_energex_export"
    export.hass = MagicMock()
    export.hass.data = {DOMAIN: {}}
    export.hass.states.get.return_value = None

    return forecast, tariff, export, coordinator, store


def in_band_peak_periods() -> tuple[list[FakePeriod], list[StpasaInterval]]:
    """Intervals at 17:00 to 17:30 NEM on the next two days.

    Horizon is 37 h and 61 h from a 04:00 run, so both land inside the OLS band
    and inside a peak bucket that the fixture fitted. That is exactly the
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


def forecast_entries_by_time(forecast_sensor) -> dict[str, dict]:
    d = forecast_sensor._price_data
    return {e["time"]: e for e in forecast_sensor._calibrated_forecast(d)}


class _TripwireIndexMap(dict):
    """An index map that refuses to be queried.

    Same trick as tests/test_stpasa_band_floor.py, which uses it to show the
    per-run floor short-circuits ahead of the lookup.
    """

    def get(self, *args, **kwargs):  # noqa: D102
        raise AssertionError("STPASA index consulted for an interval below coverage")


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind, library", [
    ("import", "spot_to_tariff"),
    ("export", "spot_to_feed_in_tariff"),
])
def test_tariff_spot_matches_forecast_value(kind, library):
    """Tariff ``spot`` equals forecast ``value`` for the same interval.

    Fails on main, where the tariff path omits the STPASA features and
    publishes the isotonic only number instead. The export class carried its
    own copy and must agree too. The fixture is asserted to reach the stage 2
    branch, or the equality would be vacuous.
    """
    periods, stpasa = in_band_peak_periods()
    forecast, tariff, export, _coord, _store = make_sensors(periods, stpasa)
    ff = forecast_entries_by_time(forecast)
    assert ff, "no calibrated entries built"
    assert "isotonic+stpasa" in {e["calibrated_source"] for e in ff.values()}, (
        "fixture did not reach the stage 2 branch, so a parity assertion over it would be vacuous"
    )

    sensor = tariff if kind == "import" else export
    with patch.object(_tariff_mod, library, return_value=15.5):
        entries = sensor.extra_state_attributes["forecast"]

    assert entries, "no tariff forecast entries built"
    for entry in entries:
        g = ff[entry["time"]]
        assert entry["spot_raw"] == round(g["raw_value"], 6), "the two sensors must start from the same raw price"
        assert entry["spot"] == round(g["value"], 6), (
            f"{kind} calibrated spot disagrees at {entry['time']}: tariff "
            f"{entry['spot']} vs forecast {round(g['value'], 6)}, source {g['calibrated_source']}"
        )


def test_parity_sweep_over_a_full_run():
    """Sweep every interval of a seven day run, not only the hand picked ones.

    The forecast covers h0 to h168 at every half hour and every hour of day, so
    the sweep crosses both edges of the OLS band, buckets that fitted and
    buckets that did not, negative prices that pass through untouched and
    intervals with no STPASA row at all. Parity must hold on all of them, and
    the counts are asserted so the sweep cannot pass by covering nothing.
    """
    run_dt = parse_iso(RUN_AT)
    rng = random.Random(11)
    periods: list[FakePeriod] = []
    stpasa: list[StpasaInterval] = []
    for i in range(336):
        start_dt = run_dt + timedelta(minutes=30 * (i + 1))
        # A spread of prices including mild negatives served by stage 2, a
        # deep negative below the fitted domain, which takes the
        # isotonic_below_domain branch, and a spike well above SPIKE_THRESHOLD.
        value = rng.choice([-0.30, -0.05, -0.00757, 0.0, 0.03, 0.12093, 0.52396, 3.4])
        periods.append(make_period(start_dt, value))
        # Leave a gap in the middle of the band so some in band intervals have
        # no STPASA row and must degrade to isotonic on both paths alike.
        if not 100 <= i < 120:
            stpasa.append(make_stpasa_interval(start_dt, solar=rng.uniform(0.0, 4000.0)))

    forecast, tariff, _export, _coord, _store = make_sensors(periods, stpasa)
    ff = forecast_entries_by_time(forecast)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5):
        entries = tariff.extra_state_attributes["forecast"]

    assert len(entries) == 336, f"expected 336 intervals, got {len(entries)}"
    sources: dict[str, int] = {}
    mismatches: list[str] = []
    for entry in entries:
        g = ff[entry["time"]]
        src = g["calibrated_source"]
        sources[src] = sources.get(src, 0) + 1
        if entry["spot"] != round(g["value"], 6):
            mismatches.append(
                f"{entry['time']} src={src} tariff={entry['spot']} forecast={round(g['value'], 6)}"
            )
    assert not mismatches, "calibrated spot disagrees on:\n" + "\n".join(mismatches[:10])
    assert sources.get("isotonic+stpasa", 0) >= 20, f"sweep did not cover enough stage 2 intervals: {sources}"
    non_stage2 = sum(v for k, v in sources.items() if k != "isotonic+stpasa")
    assert non_stage2 >= 20, (
        f"sweep covered only the stage 2 branch, so it does not show the degrade paths agree as well: {sources}"
    )


def test_tariff_value_is_the_shared_spot_with_network_applied():
    """The network and retail components are applied to the shared spot.

    Changing the spot input must not change how the components are applied, and
    everything stays in $/kWh: the only 1000 is the $/MWh conversion the
    aemo_to_tariff library expects, which was already there.
    """
    periods, stpasa = in_band_peak_periods()
    forecast, tariff, _export, _coord, _store = make_sensors(periods, stpasa)
    ff = forecast_entries_by_time(forecast)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as stt:
        entries = tariff.extra_state_attributes["forecast"]

    fee = tariff._get_additional_fee()
    for entry in entries:
        rrp_mwh = round(ff[entry["time"]]["value"] * 1000, 10)
        assert entry["value"] == expected_import_price(15.5, rrp_mwh, fee=fee), "network plus retail assembly moved"
    passed_rrp = [round(call[0][3], 10) for call in stt.call_args_list]
    expected_rrp = [round(ff[e["time"]]["value"] * 1000, 10) for e in entries]
    assert passed_rrp == expected_rrp, (
        f"library must receive the shared calibrated spot in $/MWh: {passed_rrp} vs {expected_rrp}"
    )


def test_tariff_path_gets_the_per_run_band_floor():
    """The tariff path resolves the stage 2 band floor from run coverage too.

    Issue #68 made the floor a property of the run's STPASA coverage rather
    than the flat 22h constant, but it only threaded the run timestamp into the
    forecast sensor's feature lookup. The shared entry point of issue #66 now
    carries run_at_iso, so the tariff path applies the same edge.

    The interval here sits at h30, inside the static band and below the run's
    coverage, which starts at h37. With the run timestamp threaded through, the
    floor gates it and the index is never consulted. Without it the static 22h
    floor applies and the tripwire fires, which is what makes this test
    non vacuous.

    After issue #67 the bounded nearest match already declined these
    intervals, so the published tariff number is the same either way. What the
    tariff path gains is the same band edge semantics and the same short
    circuit, so the two paths cannot drift apart again when that edge moves.
    """
    run_dt = parse_iso(RUN_AT)
    below = (run_dt + timedelta(hours=30)).replace(minute=0)
    covered = (run_dt + timedelta(days=1)).replace(hour=17, minute=0)
    periods = [make_period(below, 0.12093)]
    stpasa = [make_stpasa_interval(covered, solar=1200.0)]

    forecast, tariff, _export, coordinator, store = make_sensors(periods, stpasa)
    coordinator._map = _TripwireIndexMap(coordinator._map)

    h = (parse_iso(periods[0].time) - run_dt).total_seconds() / 3600.0
    assert 22.0 < h < 36.0, f"probe must be inside the static band, got h{h}"

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5):
        entries = tariff.extra_state_attributes["forecast"]
    assert len(entries) == 1

    ff = forecast_entries_by_time(forecast)
    assert entries[0]["spot"] == round(ff[entries[0]["time"]]["value"], 6)

    # Non vacuity: the same call without the run timestamp falls back to the
    # static floor and does reach the index.
    with pytest.raises(AssertionError, match="STPASA index consulted"):
        _inputs_mod.calibrate_interval(
            store, coordinator, periods[0].value, entries[0]["time"], h, below.hour,
        )


def test_isotonic_only_call_is_what_used_to_disagree():
    """Regression case from the issue: the old argument list disagrees.

    Calling the store the way the tariff path used to call it, raw price,
    horizon and hour only, gives a different number for these intervals. That
    is the defect, stated as a property of the store rather than of the sensor,
    so it stays true if the sensors are refactored again; it is also what keeps
    the parity assertions above from being vacuous.
    """
    periods, stpasa = in_band_peak_periods()
    forecast, _tariff, _export, _coordinator, store = make_sensors(periods, stpasa)
    ff = forecast_entries_by_time(forecast)

    differences = 0
    for period in periods:
        key = to_nem_iso(parse_iso(period.time))
        h = (parse_iso(period.time) - parse_iso(RUN_AT)).total_seconds() / 3600.0
        hour = parse_iso(period.time).hour
        old = store.apply_to_price(period.value, h, hour)["calibrated"]
        shared = ff[key]["value"]
        if round(old, 6) != round(shared, 6):
            differences += 1
    assert differences == len(periods), (
        "the fixture should reproduce the reported disagreement on every "
        f"in band interval, got {differences} of {len(periods)}"
    )
