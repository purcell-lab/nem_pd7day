"""
Parity between the tariff sensors and the price forecast sensor, issue #66.

The two sensors describe the same interval of the same PD7DAY run from the same
raw price, and they used to publish different calibrated spot prices: the
forecast path passed STPASA features, run features and the gas/QNI covariates
to ``CalibrationStore.apply_to_price`` while the tariff path passed only raw
price, horizon and hour, so it silently took the isotonic only branch. On a
live five region install that disagreed on 183 of 183 intervals that had STPASA
features, by up to 0.633470 $/kWh.

These tests run a real fitted calibration, isotonic plus a real stage 2 OLS
fit, behind a real ``CalibrationStore``, and compare the two sensors interval by
interval. The central assertion is direct equality of the calibrated spot: the
``spot`` key a tariff sensor publishes must equal the ``value`` the price
forecast sensor publishes for the same interval. ``test_tariff_matches_forecast``
and the sweep both fail against main, where the tariff number is the isotonic
only one.

Run with:  python -m pytest tests/test_tariff_calibration_parity.py -v
or simply: python tests/test_tariff_calibration_parity.py
"""
from __future__ import annotations

import math
import os
import random
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Repo root on sys.path so the file also runs standalone, per repo convention.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
from custom_components.nem_pd7day.tariff_sensor import (
    NemPd7dayExportTariffSensor,
    NemPd7dayTariffSensor,
)

# Patch the module the tariff classes actually live in. Other test modules in
# this suite load integration modules a second time through importlib, so the
# package attribute and sys.modules can point at different module objects and
# patching the wrong one silently does nothing.
_tariff_mod = sys.modules[NemPd7dayTariffSensor.__module__]

NEM_TZ = timezone(timedelta(hours=10))

# Observations must stay inside the engine's 90 day training window, so the
# fixture is anchored to now rather than to a fixed calendar date.
_ANCHOR = datetime.now(NEM_TZ).replace(minute=0, second=0, microsecond=0) - timedelta(days=2)

# The run is anchored to the same clock. The forecast intervals below sit at
# horizons that fall inside the OLS band (22 h to 120 h), which is the only
# range where the two paths could ever have disagreed.
RUN_AT = to_nem_iso(_ANCHOR.replace(hour=4))
STPASA_RUN_AT = to_nem_iso(_ANCHOR.replace(hour=3))


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

    # One training run, as a real PD7DAY run is. The stage 2 fit needs run
    # features for the run each in band row belongs to, and those are derived
    # from the run's own near term rows, so seed a few below h6.
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
            ) + timedelta(seconds=0)
            # Distinct interval keys within the run: vary the day, and offset
            # the second bucket so the two do not collide on one key.
            if horizon_hours > 48:
                interval_dt = interval_dt - timedelta(days=20)
            run_at = train_run_at
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


def make_sensors(periods: list[FakePeriod], stpasa_intervals, region: str = "QLD1"):
    """Build a forecast sensor, an import tariff sensor and an export tariff
    sensor on one coordinator and one calibration store, as a live install has.
    """
    coordinator = FakeCoordinator(region, periods, stpasa_intervals)
    store = fitted_store(region)

    entry = MagicMock()
    entry.entry_id = "entry_parity"
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=store, dispatch=None
    )

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


# ── Tests ────────────────────────────────────────────────────────────────────


def test_fixture_actually_reaches_the_stpasa_branch():
    """The fixture must exercise isotonic+stpasa, or parity proves nothing."""
    periods, stpasa = in_band_peak_periods()
    forecast, _tariff, _export, _coord, _store = make_sensors(periods, stpasa)
    sources = {
        e["time"]: e["calibrated_source"]
        for e in forecast_entries_by_time(forecast).values()
    }
    assert sources, "no calibrated entries built"
    assert "isotonic+stpasa" in sources.values(), (
        "fixture did not reach the stage 2 branch, so a parity assertion over "
        f"it would be vacuous: sources={sources}"
    )
    print("  PASS: fixture reaches the isotonic+stpasa branch")


def test_tariff_matches_forecast():
    """Tariff ``spot`` equals forecast ``value`` for the same interval.

    This is the assertion the issue asks for and it fails on main, where the
    tariff path omits the STPASA features and publishes the isotonic only
    number instead.
    """
    periods, stpasa = in_band_peak_periods()
    forecast, tariff, _export, _coord, _store = make_sensors(periods, stpasa)
    ff = forecast_entries_by_time(forecast)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5):
        entries = tariff.extra_state_attributes["forecast"]

    assert entries, "no tariff forecast entries built"
    for entry in entries:
        g = ff[entry["time"]]
        assert entry["spot_raw"] == round(g["raw_value"], 6), (
            "the two sensors must start from the same raw price"
        )
        assert entry["spot"] == round(g["value"], 6), (
            f"calibrated spot disagrees at {entry['time']}: tariff "
            f"{entry['spot']} vs forecast {round(g['value'], 6)}, source "
            f"{g['calibrated_source']}"
        )
    print(f"  PASS: tariff spot equals forecast value on {len(entries)} intervals")


def test_export_tariff_matches_forecast():
    """The export tariff class carried its own copy and must agree too."""
    periods, stpasa = in_band_peak_periods()
    forecast, _tariff, export, _coord, _store = make_sensors(periods, stpasa)
    ff = forecast_entries_by_time(forecast)

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=8.0):
        entries = export.extra_state_attributes["forecast"]

    assert entries, "no export forecast entries built"
    for entry in entries:
        g = ff[entry["time"]]
        assert entry["spot"] == round(g["value"], 6), (
            f"export tariff spot disagrees at {entry['time']}: "
            f"{entry['spot']} vs {round(g['value'], 6)}"
        )
    print("  PASS: export tariff spot equals forecast value")


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
        # A spread of prices including negatives, which take the
        # passthrough_negative branch, and a spike well above SPIKE_THRESHOLD.
        value = rng.choice([-0.05, -0.00757, 0.0, 0.03, 0.12093, 0.52396, 3.4])
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
                f"{entry['time']} src={src} tariff={entry['spot']} "
                f"forecast={round(g['value'], 6)}"
            )
    assert not mismatches, "calibrated spot disagrees on:\n" + "\n".join(mismatches[:10])
    assert sources.get("isotonic+stpasa", 0) >= 20, (
        f"sweep did not cover enough stage 2 intervals: {sources}"
    )
    non_stage2 = sum(v for k, v in sources.items() if k != "isotonic+stpasa")
    assert non_stage2 >= 20, (
        f"sweep covered only the stage 2 branch, so it does not show the "
        f"degrade paths agree as well: {sources}"
    )
    print(f"  PASS: parity across 336 intervals, sources={sources}")


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
        assert entry["value"] == round((15.5 / 100 + fee) * 1.1, 6), (
            "network plus retail assembly moved"
        )
    passed_rrp = [call[0][3] for call in stt.call_args_list]
    expected_rrp = [
        round(ff[e["time"]]["value"] * 1000, 10) for e in entries
    ]
    assert [round(v, 10) for v in passed_rrp] == expected_rrp, (
        f"library must receive the shared calibrated spot in $/MWh: "
        f"{passed_rrp} vs {expected_rrp}"
    )
    print("  PASS: network and retail components applied to the shared spot")


def test_uncalibratable_interval_degrades_to_none_not_zero():
    """No calibrated spot means None on both keys, never 0 and never the raw price."""
    periods, stpasa = in_band_peak_periods()
    forecast, tariff, _export, _coord, store = make_sensors(periods, stpasa)

    class _NoAnswerStore:
        """A store that has a calibration but cannot produce a number."""

        fit_generation = 1

        def apply_to_price(self, raw_price, horizon_hours, hour_of_day, **kwargs):
            return {
                "calibrated": None,
                "p10": None,
                "p50": None,
                "p90": None,
                "ols_mae": None,
                "calibrated_source": "passthrough",
                "n_obs": 0,
            }

    tariff._store = _NoAnswerStore()
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5):
        entries = tariff.extra_state_attributes["forecast"]

    for entry in entries:
        assert entry["spot"] is None, f"expected None spot, got {entry['spot']!r}"
        assert entry["value"] is None, f"expected None value, got {entry['value']!r}"
        assert entry["spot"] != 0 and entry["value"] != 0
    print("  PASS: uncalibratable interval degrades to None, not 0")


def test_isotonic_only_call_is_what_used_to_disagree():
    """Regression case from the issue: the old argument list disagrees.

    Calling the store the way the tariff path used to call it, raw price,
    horizon and hour only, gives a different number for these intervals. That
    is the defect, stated as a property of the store rather than of the sensor,
    so it stays true if the sensors are refactored again.
    """
    periods, stpasa = in_band_peak_periods()
    forecast, tariff, _export, coordinator, store = make_sensors(periods, stpasa)
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
    print(f"  PASS: old argument list disagrees on {differences} intervals")


if __name__ == "__main__":
    test_fixture_actually_reaches_the_stpasa_branch()
    test_tariff_matches_forecast()
    test_export_tariff_matches_forecast()
    test_parity_sweep_over_a_full_run()
    test_tariff_value_is_the_shared_spot_with_network_applied()
    test_uncalibratable_interval_degrades_to_none_not_zero()
    test_isotonic_only_call_is_what_used_to_disagree()
    print("All tariff calibration parity tests passed.")
