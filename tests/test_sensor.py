"""
Tests for sensor.py: entity registration, the forecast sensor's calibration
contract, dispatch fallback, forecast-mode trimming and the diagnostic data
sensors. A short section of tariff_sensor.py tests that arrived with the
forecast-mode work sits at the end, marked for test_tariff_sensor.py.

History, condensed from the files merged here:
  * test_sensor.py: _calibrate_period used period.nemtime (interval END) for
    the horizon while async_record_actual used period.time (START), so bucket
    lookups near the 6 h boundary were misrouted (v1.8.0). Interconnector
    entity determinism: #46 (AEMO publishes Heywood as "V-SA", the map carried
    "SA1-VIC1", so VIC1 had no Heywood sensor), #47 (N-Q-MNSP1 present only on
    the QLD1 side of a live install), #48 (the entity set depended on what one
    fetch happened to contain). #148: SA1 published min_24h_value -0.864 from
    a Saturday row four days out because min/max ran over the whole window.
  * test_dispatch_and_modes.py: the 5-minute dispatch price wins over PD7DAY
    when present; days_2_7 mode registers the additive Day 2-7 sensors; an
    entry without forecast_mode defaults to days_2_7; #159 tariff names come
    from the installed library, not const.py's fallbacks.
  * test_data_sensors.py: PD7DayDataSensor and StpasaDataSensor expose the full
    dataset as an unrecorded attribute and report STATE_UNAVAILABLE without data.

Run with:  python -m pytest tests/test_sensor.py -v
"""
from __future__ import annotations

import contextlib
import importlib
import io
import types
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import MagicMock, patch

import pytest

import support
from support import NEM_TZ, install_ha_stubs, load_chain, make_price_period, nem_iso, run_async

install_ha_stubs()

(
    _nem_time,
    _engine_mod,
    _client_mod,
    _const_mod,
    _store_mod,
    _dispatch_mod,
    _coord_mod,
    _stpasa_client_mod,
    _tariff_mod,
    _sensor_mod,
) = load_chain(
    "nem_time",
    "calibration_engine",
    "pd7day_client",
    "const",
    "calibration_store",
    "dispatch_client",
    "coordinator",
    "stpasa_client",
    "tariff_sensor",
    "sensor",
)

# Bound to this file's module objects, not the last file's (see support.py).
make_real_price_period = partial(support.make_real_price_period, _client_mod)
make_pd7day_data = partial(support.make_pd7day_data, _client_mod)
expected_import_price = partial(support.expected_import_price, _tariff_mod)

CONF_ACTIVE_TARIFF = _const_mod.CONF_ACTIVE_TARIFF
CONF_FORECAST_MODE = _const_mod.CONF_FORECAST_MODE
CONF_REGION = _const_mod.CONF_REGION
DOMAIN = _const_mod.DOMAIN
FORECAST_MODE_DAYS_2_7 = _const_mod.FORECAST_MODE_DAYS_2_7
FORECAST_MODE_FULL = _const_mod.FORECAST_MODE_FULL
NSW1_INTERCONNECTORS = _const_mod.NSW1_INTERCONNECTORS
REGION_INTERCONNECTORS = _const_mod.REGION_INTERCONNECTORS
SPIKE_COVARIATE_CAP = _const_mod.SPIKE_COVARIATE_CAP
VIC1_INTERCONNECTORS = _const_mod.VIC1_INTERCONNECTORS
STATE_UNAVAILABLE = _sensor_mod.STATE_UNAVAILABLE

_amber_express_cutoff = _nem_time._amber_express_cutoff
_bucket_key = _engine_mod._bucket_key
_horizon_hours = _sensor_mod._horizon_hours
parse_iso = _nem_time.parse_iso

DispatchPrice = _dispatch_mod.DispatchPrice
PD7DayCalibrationSensor = _sensor_mod.PD7DayCalibrationSensor
PD7DayDataSensor = _sensor_mod.PD7DayDataSensor
PD7DayForecastSensor = _sensor_mod.PD7DayForecastSensor
PD7DayInterconnectorSensor = _sensor_mod.PD7DayInterconnectorSensor
PD7DayRegionDataUpdatedDatetimeSensor = _sensor_mod.PD7DayRegionDataUpdatedDatetimeSensor
PD7DayRegionSourceFileDatetimeSensor = _sensor_mod.PD7DayRegionSourceFileDatetimeSensor
PD7DayTodSensor = _sensor_mod.PD7DayTodSensor
SpotPriceForecastDays27Sensor = _sensor_mod.SpotPriceForecastDays27Sensor
StpasaDataSensor = _sensor_mod.StpasaDataSensor
NemPd7dayTariffSensor = _tariff_mod.NemPd7dayTariffSensor
get_tariff_name = _tariff_mod.get_tariff_name
sensor_async_setup_entry = _sensor_mod.async_setup_entry

BASE_SENSOR_NAME = "NEM Spot Price Forecast"
DAY27_SENSOR_NAME = "Day 2-7 NEM Spot Price Forecast"


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_entry(entry_id="entry_test", region="QLD1", options=None, coordinator=None, store=None):
    """A config-entry mock with runtime_data; dispatch is None (fast path skipped)."""
    if coordinator is None:
        coordinator = MagicMock()
        coordinator.data = None
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {CONF_REGION: region}
    entry.options = {} if options is None else options
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=store, dispatch=None
    )
    return entry


def run_setup(region="QLD1", options=None, live_ic_ids=None) -> list:
    """Run sensor.async_setup_entry for one region and return the entities created.

    ``live_ic_ids`` stands in for what the coordinator holds after a fetch;
    ``None`` models a coordinator with no data at all.
    """
    coordinator = MagicMock()
    if live_ic_ids is None:
        coordinator.data = None
    else:
        coordinator.data = MagicMock()
        coordinator.data.interconnectors = {ic: MagicMock() for ic in live_ic_ids}
    entry = make_entry(f"entry_{region.lower()}", region, options, coordinator, store=MagicMock())
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    created: list = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(sensor_async_setup_entry(hass, entry, _add_entities))
    return created


def interconnector_ids(entities) -> list[str]:
    return sorted(e._ic_id for e in entities if isinstance(e, PD7DayInterconnectorSensor))


def make_sensor(store=None, *, cls=PD7DayForecastSensor, region="QLD1", options=None):
    """Construct a forecast sensor (base or Day 2-7) bypassing CoordinatorEntity init."""
    coordinator = MagicMock()
    coordinator.data = None
    sensor = cls.__new__(cls)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._store = store
    sensor._attr_unique_id = f"nem_pd7day_{region.lower()}_forecast"
    sensor._attr_name = BASE_SENSOR_NAME
    sensor._entry = make_entry(region=region, options=options, coordinator=coordinator, store=store)
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    return sensor


def set_price_data(sensor, periods, run_at_dt: datetime):
    """Give the sensor's coordinator a PD7DayData-like mock for its region."""
    price_data = MagicMock()
    price_data.forecast = periods
    price_data.forecast_generated_at = nem_iso(run_at_dt)
    price_data.region = sensor._region
    price_data.interval_minutes = 30
    price_data.source_file = "test.xml"
    sensor.coordinator.data = MagicMock()
    sensor.coordinator.data.prices = {sensor._region: price_data}
    return price_data


def ascending_periods(run_at_dt: datetime, n: int, start=0.05, step=0.001) -> list:
    """``n`` consecutive 30-min periods from ``run_at_dt`` with rising values."""
    return [
        make_price_period(run_at_dt + timedelta(minutes=30 * (i + 1)), value=start + i * step)
        for i in range(n)
    ]


def set_current_interval(sensor, value: float):
    """A single period covering the real current NEM time, run an hour earlier."""
    now = datetime.now(NEM_TZ)
    interval_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    period = make_price_period(interval_start + timedelta(minutes=30), value=value)
    set_price_data(sensor, [period], now - timedelta(hours=1))
    return period


def make_calibrating_store(**result):
    """A store mock whose apply_to_price returns a fixed calibration result."""
    store = MagicMock()
    store.calibration = MagicMock()  # not None -> calibration active
    store.apply_to_price.return_value = {
        "calibrated": 0.085, "p10": None, "p50": None, "p90": None,
        "ols_mae": None, "calibrated_source": "passthrough", "n_obs": 0,
        **result,
    }
    return store


# ── Entity registration ───────────────────────────────────────────────────────

def test_setup_entry_creates_one_of_each_region_entity():
    """One forecast, calibration, data and datetime sensor each, for the configured region."""
    created = run_setup("NSW1")

    forecast = [e for e in created if isinstance(e, PD7DayForecastSensor)]
    calibration = [e for e in created if isinstance(e, PD7DayCalibrationSensor)]
    assert [e._region for e in forecast] == ["NSW1"]
    assert [e._region for e in calibration] == ["NSW1"]
    assert calibration[0]._attr_unique_id == "nem_pd7day_nsw1_calibration"
    for cls in (
        PD7DayRegionSourceFileDatetimeSensor,
        PD7DayRegionDataUpdatedDatetimeSensor,
        PD7DayDataSensor,
        StpasaDataSensor,
    ):
        assert sum(isinstance(e, cls) for e in created) == 1, cls.__name__


@pytest.mark.parametrize(
    "options, expected_active",
    [
        pytest.param(
            {CONF_FORECAST_MODE: FORECAST_MODE_DAYS_2_7, CONF_ACTIVE_TARIFF: "energex/6900"},
            ("energex", "6900"),
            id="days_2_7 with active tariff",
        ),
        pytest.param({}, None, id="no forecast_mode option defaults to days_2_7"),
    ],
)
def test_setup_entry_registers_day27_sensors_in_days_2_7_mode(options, expected_active):
    """days_2_7 mode adds a Day 2-7 spot sensor and one Day 2-7 tariff sensor.

    An entry from before forecast_mode existed carries no option and must be
    treated as days_2_7 (migration default).
    """
    created = run_setup("QLD1", options)

    base_spot = [e for e in created if getattr(e, "_attr_name", "") == BASE_SENSOR_NAME]
    day27_spot = [e for e in created if getattr(e, "_attr_name", "") == DAY27_SENSOR_NAME]
    day27_tariff = [
        e for e in created
        if "Day 2-7" in getattr(e, "_attr_name", "") and "Tariff" in getattr(e, "_attr_name", "")
    ]
    assert len(base_spot) == 1, "Base spot sensor must always be registered"
    assert len(day27_spot) == 1
    assert len(day27_tariff) == 1, "Only one Day 2-7 tariff sensor, for the active tariff"
    if expected_active is not None:
        distributor, code = expected_active
        assert day27_tariff[0]._distributor == distributor
        assert day27_tariff[0]._tariff_code == code
        assert day27_tariff[0]._attr_unique_id == f"nem_pd7day_QLD1_{distributor}_{code}_days27"


def test_setup_entry_registers_no_day27_sensors_in_days_1_7_mode():
    created = run_setup("QLD1", {CONF_FORECAST_MODE: FORECAST_MODE_FULL})
    day27 = [e for e in created if "Day 2-7" in getattr(e, "_attr_name", "")]
    assert day27 == []


# ── Interconnector entity determinism (issues #46, #47, #48) ─────────────────

def test_vic1_interconnector_map_uses_the_published_heywood_id():
    """AEMO publishes Heywood as "V-SA"; "SA1-VIC1" never appears in the file (#46)."""
    assert "V-SA" in VIC1_INTERCONNECTORS
    assert "SA1-VIC1" not in VIC1_INTERCONNECTORS


def test_every_interconnector_is_mapped_to_both_of_its_regions():
    """A one sided entry leaves one end of the link without a sensor (#46)."""
    counts = Counter(ic for ics in REGION_INTERCONNECTORS.values() for ic in ics)
    one_sided = sorted(ic for ic, n in counts.items() if n != 2)
    assert one_sided == []


def test_nsw1_creates_the_terranora_interconnector_sensor():
    """N-Q-MNSP1 is mapped to NSW1 as well as QLD1 (#47); the NSW1 set is pinned."""
    assert interconnector_ids(run_setup("NSW1")) == ["N-Q-MNSP1", "NSW1-QLD1", "VIC1-NSW1"]


@pytest.mark.parametrize(
    "live_ic_ids",
    [
        pytest.param(None, id="no data"),
        pytest.param(["NSW1-QLD1", "VIC1-NSW1", "N-Q-MNSP1"], id="full fetch"),
        pytest.param(["NSW1-QLD1"], id="partial fetch"),
    ],
)
def test_interconnector_entities_do_not_depend_on_live_fetch_contents(live_ic_ids):
    """The entity set is a function of configuration, not of one fetch (#48).

    An interconnector missing from a file must still get its entity so it
    reports unavailable rather than vanishing from dashboards and history.
    """
    assert interconnector_ids(run_setup("NSW1", live_ic_ids=live_ic_ids)) == sorted(NSW1_INTERCONNECTORS)


@pytest.mark.parametrize("region", sorted(REGION_INTERCONNECTORS))
def test_interconnector_entities_match_the_map_for_every_region(region):
    """Guards the total, so a future map edit cannot silently drop an end."""
    assert interconnector_ids(run_setup(region, live_ic_ids=[])) == sorted(REGION_INTERCONNECTORS[region])


# ── _horizon_hours ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "run_at, interval, expected",
    [
        pytest.param("2026-04-15T07:30:00+10:00", "2026-04-15T13:30:00+10:00", 6.0, id="6h"),
        pytest.param("2026-04-15T07:30:00+10:00", "2026-04-16T07:30:00+10:00", 24.0, id="24h tz-aware"),
        pytest.param(None, "2026-04-15T08:00:00+10:00", 0.0, id="run_at None"),
        pytest.param("", "2026-04-15T08:00:00+10:00", 0.0, id="run_at empty"),
        pytest.param("2026-04-15T10:00:00+10:00", "2026-04-15T09:00:00+10:00", 0.0, id="negative clamped"),
    ],
)
def test_horizon_hours(run_at, interval, expected):
    """horizon = interval - run_at in hours, 0.0 for a missing run_at or a negative gap."""
    assert abs(_horizon_hours(run_at, interval) - expected) < 0.001


@pytest.mark.parametrize(
    "offset",
    [
        pytest.param(timedelta(hours=5, minutes=54), id="just under the 6h boundary"),
        pytest.param(timedelta(hours=12), id="exactly the 12h boundary"),
    ],
)
def test_bucket_routing_consistent_between_sensor_and_store(offset):
    """The sensor's horizon from period.time must route to the bucket the store trained.

    Using period.nemtime (30 min later) would move an interval 6 minutes short
    of the 6 h boundary into h06_12.
    """
    run_at_dt = datetime(2026, 4, 15, 8, 0, tzinfo=NEM_TZ)
    interval_start_dt = run_at_dt + offset
    period = make_price_period(interval_start_dt + timedelta(minutes=30))

    sensor_horizon = _horizon_hours(nem_iso(run_at_dt), period.time)
    store_horizon = (interval_start_dt - run_at_dt).total_seconds() / 3600

    assert abs(sensor_horizon - store_horizon) < 0.001
    assert _bucket_key(sensor_horizon, interval_start_dt.hour) == _bucket_key(store_horizon, interval_start_dt.hour)


# ── _calibrate_period ─────────────────────────────────────────────────────────

def test_calibrate_period_passthrough_contract_without_store():
    """Without a store the entry carries the raw value through under every expected key."""
    sensor = make_sensor(store=None)
    interval_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period = make_price_period(interval_end_dt, value=0.085)

    result = sensor._calibrate_period(period, "2026-04-15T07:30:00+10:00")

    assert {"nemtime", "time", "raw_value", "horizon_hours", "value"} <= set(result)
    assert abs(result["raw_value"] - 0.085) < 1e-9
    assert abs(result["value"] - 0.085) < 1e-9
    assert result["nemtime"] == nem_iso(interval_end_dt), "nemtime must be the interval END"
    assert result["time"] == nem_iso(interval_end_dt - timedelta(minutes=30)), "time must be the interval START"
    assert result["horizon_hours"] == 6.0


def test_calibrate_period_horizon_uses_interval_start():
    """v1.8.0: run_at 08:30 with interval 14:00-14:30 is 5.5 h (h00_06), not 6.0 h (h06_12).

    Both the published horizon_hours and the horizon handed to the store must
    come from period.time, matching what async_record_actual trained on.
    """
    store = make_calibrating_store()
    sensor = make_sensor(store=store)
    period = make_price_period(datetime(2026, 4, 15, 14, 30, tzinfo=NEM_TZ), value=0.085)

    result = sensor._calibrate_period(period, "2026-04-15T08:30:00+10:00")

    assert abs(result["horizon_hours"] - 5.5) < 0.1
    h_passed = store.apply_to_price.call_args[0][1]  # apply_to_price(raw, h, hour, ...)
    assert abs(h_passed - 5.5) < 0.1, f"store looked up horizon {h_passed:.2f}h; nemtime is being used"


def test_calibrate_period_with_active_calibration():
    """With a store the entry carries the calibration keys and value == calibrated."""
    store = make_calibrating_store(
        calibrated=0.072, p10=0.055, p50=0.070, p90=0.095, ols_mae=0.012,
        calibrated_source="ols", n_obs=42,
    )
    sensor = make_sensor(store=store)
    period = make_price_period(datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ), value=0.085)

    result = sensor._calibrate_period(period, "2026-04-15T07:30:00+10:00")

    assert {"calibrated", "p10", "p50", "p90", "ols_mae", "calibrated_source", "n_obs"} <= set(result)
    assert abs(result["value"] - 0.072) < 1e-9
    assert result["calibrated_source"] == "ols"


# ── native_value: dispatch first, then the current PD7DAY interval ───────────

def test_native_value_is_none_without_coordinator_data():
    sensor = make_sensor(store=None)
    assert sensor.native_value is None


def test_native_value_prefers_dispatch_price():
    sensor = make_sensor(store=None)
    dispatch = MagicMock()
    dispatch.prices = {"QLD1": DispatchPrice("QLD1", "2026/05/21 09:30:00", 0.042)}
    sensor._entry.runtime_data.dispatch = dispatch

    assert abs(sensor.native_value - 0.042) < 1e-9


@pytest.mark.parametrize(
    "dispatch",
    [
        pytest.param(None, id="no dispatch coordinator"),
        pytest.param(MagicMock(prices={}), id="dispatch without this region"),
    ],
)
def test_native_value_falls_back_to_current_pd7day_interval(dispatch):
    """Without a dispatch price the state is the raw value of the current interval."""
    sensor = make_sensor(store=None)
    sensor._entry.runtime_data.dispatch = dispatch
    set_current_interval(sensor, 0.085)

    assert abs(sensor.native_value - 0.085) < 1e-9


def test_native_value_passes_covariates_to_the_store():
    """The covariate gate can only fire if gas and QNI reach apply_to_price (#66)."""
    def _apply(
        raw, h, hour, *,
        gas_forecast_tj=None, qni_mwflow=None,
        stpasa_features=None, run_features=None,
    ):
        capped = gas_forecast_tj is not None and qni_mwflow is not None
        value = SPIKE_COVARIATE_CAP if capped else raw
        return {
            "calibrated": value, "p10": value, "p50": value, "p90": value,
            "ols_mae": None, "n_obs": 0,
            "calibrated_source": "covariate_capped" if capped else "passthrough_high",
        }

    store = MagicMock()
    store.calibration = MagicMock()
    store.apply_to_price = _apply
    sensor = make_sensor(store=store)
    period = set_current_interval(sensor, 5.0)  # high spike
    price_data = sensor.coordinator.data.prices["QLD1"]
    price_data.forecast_generated_at = nem_iso(datetime.now(NEM_TZ) - timedelta(hours=24))

    qni_data = MagicMock()
    qni_data.forecast = [MagicMock(time=period.time, mwflow=-200.0)]
    market_summary = MagicMock()
    market_summary.forecast = [MagicMock(nemtime=period.time, value_tj=100.0)]
    sensor.coordinator.data.interconnectors = {"NSW1-QLD1": qni_data}
    sensor.coordinator.data.market_summary = market_summary

    assert sensor.native_value == SPIKE_COVARIATE_CAP


def test_spot_sensor_subscribes_to_dispatch_coordinator():
    sensor = make_sensor(store=None)
    dispatch = MagicMock()
    dispatch.async_add_listener = MagicMock(return_value=lambda: None)
    sensor._entry.runtime_data.dispatch = dispatch
    sensor.async_on_remove = MagicMock()
    sensor.async_write_ha_state = MagicMock()

    run_async(sensor.async_added_to_hass())

    dispatch.async_add_listener.assert_called_once()


def test_tod_sensor_device_info_includes_region():
    sensor = PD7DayTodSensor.__new__(PD7DayTodSensor)
    sensor.coordinator = MagicMock()
    sensor._region = "NSW1"
    sensor._entry = MagicMock(entry_id="entry_region_test")

    assert (DOMAIN, "entry_region_test_NSW1") in sensor.device_info["identifiers"]


# ── Amber Express cutoff (nem_time helper the Day 2-7 sensors trim on) ────────

@pytest.mark.parametrize(
    "now, expected",
    [
        # 3:30am-12:30pm NEM: cutoff is tomorrow 3:30am NEM.
        pytest.param(datetime(2026, 5, 19, 3, 30, tzinfo=NEM_TZ), datetime(2026, 5, 20, 3, 30, tzinfo=NEM_TZ), id="3:30am boundary inside"),
        pytest.param(datetime(2026, 5, 19, 4, 0, tzinfo=NEM_TZ), datetime(2026, 5, 20, 3, 30, tzinfo=NEM_TZ), id="4:00am"),
        pytest.param(datetime(2026, 5, 19, 8, 0, tzinfo=NEM_TZ), datetime(2026, 5, 20, 3, 30, tzinfo=NEM_TZ), id="8:00am"),
        pytest.param(datetime(2026, 5, 19, 12, 29, tzinfo=NEM_TZ), datetime(2026, 5, 20, 3, 30, tzinfo=NEM_TZ), id="12:29pm"),
        # Otherwise: rolling now + 24h.
        pytest.param(datetime(2026, 5, 19, 12, 30, tzinfo=NEM_TZ), datetime(2026, 5, 20, 12, 30, tzinfo=NEM_TZ), id="12:30pm boundary outside"),
        pytest.param(datetime(2026, 5, 19, 18, 0, tzinfo=NEM_TZ), datetime(2026, 5, 20, 18, 0, tzinfo=NEM_TZ), id="6:00pm"),
        pytest.param(datetime(2026, 5, 19, 2, 0, tzinfo=NEM_TZ), datetime(2026, 5, 20, 2, 0, tzinfo=NEM_TZ), id="2:00am"),
    ],
)
def test_amber_express_cutoff(now, expected):
    assert _amber_express_cutoff(now=now) == expected


# ── Forecast attributes: trim, next_value, min/max (#148), cheapest window ────

@pytest.mark.parametrize(
    "options",
    [
        pytest.param({}, id="no mode option"),
        pytest.param({CONF_FORECAST_MODE: FORECAST_MODE_FULL}, id="days_1_7"),
        pytest.param({CONF_FORECAST_MODE: FORECAST_MODE_DAYS_2_7}, id="days_2_7"),
    ],
)
def test_base_sensor_forecast_is_untrimmed_in_every_mode(options):
    """The day 1-7 sensor publishes every interval; next_value is the first of them."""
    sensor = make_sensor(store=None, options=options)
    run_at_dt = datetime(2026, 5, 19, 6, 0, tzinfo=NEM_TZ)
    periods = ascending_periods(run_at_dt, 200)  # ~100 h, well past any cutoff
    set_price_data(sensor, periods, run_at_dt)

    attrs = sensor.extra_state_attributes

    assert len(attrs["forecast"]) == 200
    assert attrs["next_value"] == attrs["forecast"][0]["value"] == periods[0].value


def test_day27_sensor_forecast_only_contains_post_cutoff_intervals():
    """The Day 2-7 sensor drops every interval starting at or before the cutoff."""
    sensor = make_sensor(store=None, cls=SpotPriceForecastDays27Sensor)
    run_at_dt = datetime(2026, 5, 19, 6, 0, tzinfo=NEM_TZ)
    cutoff = run_at_dt + timedelta(hours=12)  # fixed, so the test is deterministic
    periods = ascending_periods(run_at_dt, 200)
    set_price_data(sensor, periods, run_at_dt)

    with patch.object(_sensor_mod, "_amber_express_cutoff", return_value=cutoff):
        attrs = sensor.extra_state_attributes
    forecast = attrs["forecast"]

    for p in forecast:
        assert parse_iso(p["time"]) > cutoff, f"interval at {p['time']} is <= cutoff {cutoff}"
    # Intervals 0..24 start at or before run_at + 12 h; 25..199 survive.
    assert len(forecast) == 175
    assert attrs["next_value"] == forecast[0]["value"] == periods[25].value


def test_min_max_computed_over_first_24h_of_day17_window():
    """min_24h_value / max_24h_value cover the first 24 hours of the run (#148).

    The forecast attribute still carries every interval; only the two summary
    attributes are sized to the 24 hours their names claim.
    """
    sensor = make_sensor(store=None)
    run_at_dt = datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ)
    periods = []
    for i in range(96):
        # Beyond 24 h: deeper and higher than anything in the first 24 h.
        val = 0.10 + i * 0.001 if i < 48 else (0.001 if i % 2 == 0 else 9.99)
        periods.append(make_price_period(run_at_dt + timedelta(minutes=30 * (i + 1)), value=val))
    set_price_data(sensor, periods, run_at_dt)

    attrs = sensor.extra_state_attributes

    assert attrs["min_24h_value"] == 0.10
    assert attrs["max_24h_value"] == round(0.10 + 47 * 0.001, 6)
    assert len(attrs["forecast"]) == 96


def test_day27_min_max_cover_first_24h_after_cutoff_not_whole_window():
    """Day 2-7: min_24h_value is the first 24 h after the cutoff (#148).

    SA1 published min_24h_value -0.864 from a Saturday row four days out. Here
    the deepest and highest values sit beyond the first 24 post-cutoff hours
    and must not be reported, while cheapest_2h_window still searches the whole
    trimmed window by design.
    """
    sensor = make_sensor(store=None, cls=SpotPriceForecastDays27Sensor, region="SA1")
    run_at_dt = datetime(2026, 9, 8, 7, 30, tzinfo=NEM_TZ)
    # The trim keeps intervals starting strictly after the cutoff, so a cutoff
    # one minute short of 24 h makes interval 48 the first post-cutoff one.
    cutoff = run_at_dt + timedelta(hours=23, minutes=59)
    periods = []
    for i in range(6 * 48):
        post_cutoff_index = i - 48  # 0 is the first interval after the cutoff
        if 0 <= post_cutoff_index < 48:
            val = 0.05 + post_cutoff_index * 0.001
        elif post_cutoff_index == 200:
            val = -0.864  # the Saturday row
        elif post_cutoff_index == 210:
            val = 9.99
        else:
            val = 0.20
        periods.append(make_price_period(run_at_dt + timedelta(minutes=30 * (i + 1)), value=val))
    set_price_data(sensor, periods, run_at_dt)

    with patch.object(_sensor_mod, "_amber_express_cutoff", return_value=cutoff):
        attrs = sensor.extra_state_attributes

    assert attrs["min_24h_value"] == 0.05
    assert attrs["max_24h_value"] == round(0.05 + 47 * 0.001, 6)
    assert attrs["cheapest_2h_window"]["avg_value"] < 0.0  # whole-window search finds the Saturday row
    assert len(attrs["forecast"]) == 6 * 48 - 48


def test_cheapest_2h_window_computed_over_full_forecast():
    """With ascending values the cheapest 4-interval window is the first one."""
    sensor = make_sensor(store=None)
    run_at_dt = datetime(2026, 5, 19, 14, 0, tzinfo=NEM_TZ)
    set_price_data(sensor, ascending_periods(run_at_dt, 96, start=0.10), run_at_dt)

    attrs = sensor.extra_state_attributes
    cheapest = attrs["cheapest_2h_window"]

    assert cheapest is not None
    assert cheapest["points"] == 4
    assert cheapest["start"] == attrs["forecast"][0]["time"]
    assert cheapest["avg_value"] == round((0.10 + 0.101 + 0.102 + 0.103) / 4, 6)


# ── Diagnostic data sensors: PD7DayDataSensor and StpasaDataSensor ───────────

REGION = "QLD1"
RUN_DT = "2026-06-12T10:00:00+10:00"


def make_pd7day_data_sensor(coordinator_data=None, store=None) -> PD7DayDataSensor:
    coordinator = MagicMock()
    coordinator.data = coordinator_data
    coordinator.interconnectors = {}
    sensor = PD7DayDataSensor.__new__(PD7DayDataSensor)
    sensor.coordinator = coordinator
    sensor._region = REGION
    sensor._store = store
    sensor._entry = MagicMock(entry_id="entry_test")
    return sensor


def make_stpasa_result(run_dt: str = RUN_DT):
    return _stpasa_client_mod.StpasaResult(
        region=REGION,
        run_datetime=run_dt,
        intervals=[
            _stpasa_client_mod.StpasaInterval(
                interval_datetime="2026-06-12T10:30:00+10:00",
                run_datetime=run_dt,
                demand10=5000.0,
                demand50=5500.0,
                demand90=6000.0,
                surpluscapacity=1200.0,
                ss_solar_uigf=300.0,
                ss_wind_uigf=400.0,
            )
        ],
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def make_stpasa_data_sensor(stpasa_result=None) -> StpasaDataSensor:
    sensor = StpasaDataSensor.__new__(StpasaDataSensor)
    sensor.coordinator = MagicMock()
    sensor._region = REGION
    sensor._entry = MagicMock(entry_id="entry_test")
    store = MagicMock()
    store.latest.return_value = stpasa_result
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {"stpasa_stores": {REGION: store}}}
    return sensor


def test_pd7day_data_sensor_state_and_unrecorded_forecast():
    period = make_real_price_period(datetime(2026, 6, 12, 10, 30, tzinfo=NEM_TZ), value=0.10)
    coordinator_data = MagicMock()
    coordinator_data.prices = {REGION: make_pd7day_data(RUN_DT, [period])}
    sensor = make_pd7day_data_sensor(coordinator_data=coordinator_data)

    assert sensor.native_value == RUN_DT
    attrs = sensor.extra_state_attributes
    assert attrs["run_datetime"] == RUN_DT
    assert attrs["region"] == REGION
    assert attrs["interval_count"] == 1
    entry = attrs["forecast"][0]
    assert entry["raw_rrp"] == 0.10
    assert set(entry) == {
        "time", "nemtime", "raw_rrp", "calibrated",
        "p10", "p90", "calibrated_source", "band_source", "horizon_hours",
    }
    assert "forecast" in PD7DayDataSensor._unrecorded_attributes


def test_pd7day_data_sensor_unavailable_without_data():
    sensor = make_pd7day_data_sensor(coordinator_data=None)
    assert sensor.native_value == STATE_UNAVAILABLE
    assert sensor.extra_state_attributes == {}


def test_stpasa_data_sensor_state_and_unrecorded_intervals():
    sensor = make_stpasa_data_sensor(stpasa_result=make_stpasa_result())

    assert sensor.native_value == RUN_DT
    attrs = sensor.extra_state_attributes
    assert attrs["run_datetime"] == RUN_DT
    assert attrs["region"] == REGION
    assert attrs["interval_count"] == 1
    interval = attrs["intervals"][0]
    assert interval["demand50"] == 5500.0
    assert interval["surpluscapacity"] == 1200.0
    assert set(interval) == {
        "interval_datetime", "demand10", "demand50", "demand90",
        "surpluscapacity", "ss_solar_uigf", "ss_wind_uigf",
    }
    assert "intervals" in StpasaDataSensor._unrecorded_attributes


def test_stpasa_data_sensor_unavailable_without_data():
    sensor = make_stpasa_data_sensor(stpasa_result=None)
    assert sensor.native_value == STATE_UNAVAILABLE
    assert sensor.extra_state_attributes == {}


# ── tariff_sensor.py: forecast-mode behaviour and dispatch selection ──────────
# These exercise NemPd7dayTariffSensor and belong in test_tariff_sensor.py;
# they are kept here because that file is owned elsewhere. Lift them as a block.

def make_tariff_sensor(
    region="QLD1",
    distributor="energex",
    tariff_code="8400",
    price_periods=None,
    mode=FORECAST_MODE_DAYS_2_7,
    active_tariff="",
) -> NemPd7dayTariffSensor:
    """Construct a NemPd7dayTariffSensor with mode-aware options.

    ``mode=None`` models an entry from before forecast_mode existed (no options).
    """
    coordinator = MagicMock()
    if price_periods is not None:
        price_data = MagicMock()
        price_data.forecast = price_periods
        coordinator.data = MagicMock()
        coordinator.data.prices = {region: price_data}
    else:
        coordinator.data = None
    coordinator.last_update_success = True

    options = {} if mode is None else {CONF_FORECAST_MODE: mode, CONF_ACTIVE_TARIFF: active_tariff}
    entry = make_entry("entry_1", region, options, coordinator)

    sensor = NemPd7dayTariffSensor.__new__(NemPd7dayTariffSensor)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._tariff_code = tariff_code
    sensor._entry = entry
    sensor._store = None
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{tariff_code}_tariff"
    sensor._attr_name = f"{distributor.title()} {get_tariff_name(distributor, tariff_code)} Tariff ({tariff_code})"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    sensor.hass.states.get.return_value = None
    return sensor


def current_interval_period(value=0.10):
    now = datetime.now(NEM_TZ)
    current_end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
    return make_price_period(current_end, value=value)


@pytest.mark.parametrize(
    "tariff_code, mode, active_tariff, expected",
    [
        pytest.param("6900", FORECAST_MODE_DAYS_2_7, "energex/6900", True, id="days_2_7 active tariff"),
        pytest.param("8900", FORECAST_MODE_DAYS_2_7, "energex/6900", True, id="days_2_7 other default tariff"),
        pytest.param("8400", FORECAST_MODE_DAYS_2_7, "energex/6900", False, id="days_2_7 non-default tariff"),
        pytest.param("6900", FORECAST_MODE_DAYS_2_7, "", True, id="days_2_7 no active tariff"),
        pytest.param("6900", FORECAST_MODE_FULL, "energex/8900", True, id="days_1_7 default tariff"),
        pytest.param("8400", FORECAST_MODE_FULL, "", False, id="days_1_7 non-default tariff"),
        pytest.param("6900", None, "", True, id="no forecast_mode option"),
    ],
)
def test_tariff_visibility_uses_default_enabled_tariffs_regardless_of_mode(tariff_code, mode, active_tariff, expected):
    """Base tariff sensors are enabled by DEFAULT_ENABLED_TARIFFS alone; mode and active_tariff do not change it."""
    sensor = make_tariff_sensor(
        distributor="energex", tariff_code=tariff_code, mode=mode, active_tariff=active_tariff,
    )
    assert sensor.entity_registry_enabled_default is expected


@pytest.mark.parametrize("mode", [FORECAST_MODE_FULL, FORECAST_MODE_DAYS_2_7])
def test_tariff_forecast_is_untrimmed_in_every_mode(mode):
    """The base tariff sensor publishes every interval; only the Day 2-7 tariff sensor trims."""
    base = datetime.now(tz=NEM_TZ).replace(minute=0, second=0, microsecond=0)
    periods = [make_price_period(base + timedelta(minutes=30 * (i + 1)), value=0.05) for i in range(100)]
    sensor = make_tariff_sensor(price_periods=periods, mode=mode)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        attrs = sensor.extra_state_attributes

    assert len(attrs["forecast"]) == 100


@pytest.mark.parametrize(
    "dispatch_prices, lib_c_kwh, rrp_mwh",
    [
        # Dispatch price 0.050 $/kWh is 50 $/MWh into the split.
        pytest.param({"QLD1": DispatchPrice("QLD1", "2026/05/21 09:30:00", 0.050)}, 12.5, 50.0, id="dispatch price"),
        # Fallback uses the PD7DAY forecast (0.10 $/kWh = 100 $/MWh).
        pytest.param({}, 15.5, 100.0, id="pd7day fallback"),
    ],
)
def test_tariff_native_value_prefers_dispatch_then_pd7day(dispatch_prices, lib_c_kwh, rrp_mwh):
    sensor = make_tariff_sensor(price_periods=[current_interval_period(0.10)])
    dispatch = MagicMock()
    dispatch.prices = dispatch_prices
    sensor._entry.runtime_data.dispatch = dispatch

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=lib_c_kwh):
        val = sensor.native_value

    assert val is not None
    expected = expected_import_price(lib_c_kwh, rrp_mwh)
    assert abs(val - expected) < 1e-6, f"Expected {expected}, got {val}"


def test_tariff_sensor_subscribes_to_dispatch_coordinator():
    sensor = make_tariff_sensor(distributor="energex", tariff_code="6900")
    dispatch = MagicMock()
    dispatch.async_add_listener = MagicMock(return_value=lambda: None)
    sensor._entry.runtime_data.dispatch = dispatch
    sensor.async_on_remove = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    sensor._schedule_next_boundary = lambda: None  # avoid the dt_util mock in full-suite runs

    run_async(sensor.async_added_to_hass())

    dispatch.async_add_listener.assert_called_once()


def test_get_tariff_name_from_library():
    """get_tariff_name() returns the library's name when it is installed (#159).

    The literals this test used to pin were the const.py fallbacks, which is
    what the lookup silently returned once the library replaced its
    ``module.tariffs`` attribute. The expectation now comes from the library
    itself, and from the constants only when it is absent.
    """
    def expected(distributor, code):
        try:
            mod = importlib.import_module(
                "aemo_to_tariff." + {"sapn": "sapower"}.get(distributor, distributor)
            )
        except ImportError:
            return _const_mod.TARIFF_NAMES[distributor][code]
        with contextlib.redirect_stdout(io.StringIO()):
            table = mod.get_tariffs() if hasattr(mod, "get_tariffs") else mod.tariffs
        return table[code]["name"]

    assert get_tariff_name("energex", "6900") == expected("energex", "6900")
    assert get_tariff_name("ergon", "ERTOUET1") == expected("ergon", "ERTOUET1")
    assert get_tariff_name("sapn", "RTOU") == expected("sapn", "RTOU")  # sapn is sapower in the library
    assert get_tariff_name("energex", "ZZZZZ") == "ZZZZZ"  # unknown code falls back to the code
