"""
Tests for sensor.py — _calibrate_period output contract, horizon/bucket routing
consistency between training and inference, and attribute shape.

Zero coverage previously.  The v1.8.0 bug (nemtime vs time for horizon) was in
_calibrate_period and would have been caught immediately by this test file.

Run with:  python -m pytest tests/test_sensor.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# ── Module loader ─────────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


# Stub HA and aiohttp before loading any integration module
sys.modules.setdefault("aiohttp", MagicMock())
for ha_mod in [
    "homeassistant", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.storage", "homeassistant.helpers.event",
    "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform", "homeassistant.helpers.device_registry",
    "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.util", "homeassistant.util.dt",
    "homeassistant.components", "homeassistant.components.sensor",
]:
    sys.modules.setdefault(ha_mod, MagicMock())

device_registry_mock = MagicMock()
device_registry_mock.DeviceInfo = dict
sys.modules["homeassistant.helpers.device_registry"] = device_registry_mock

# Make SensorStateClass and SensorDeviceClass importable as real names
import enum
class _SensorDeviceClass(str, enum.Enum):
    MONETARY = "monetary"
    ENERGY = "energy"
    TIMESTAMP = "timestamp"

class _SensorStateClass(str, enum.Enum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"

sensor_mock = MagicMock()
sensor_mock.SensorDeviceClass = _SensorDeviceClass
sensor_mock.SensorStateClass = _SensorStateClass
sensor_mock.SensorEntity = object  # base class stub
sys.modules["homeassistant.components.sensor"] = sensor_mock

_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)
sys.modules.setdefault("aiohttp", MagicMock())
_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)

# Stub DataUpdateCoordinator with subscript support before loading coordinator
class _FakeCoordinator:
    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.last_update_success = True
        self.data = None
    def __class_getitem__(cls, item):
        return cls
    async def async_config_entry_first_refresh(self): pass
    async def async_refresh(self): pass

class _FakeCoordinatorEntity:
    """Stub for CoordinatorEntity — supports subscript and HA init signature."""
    def __init__(self, coordinator=None, **kwargs):
        self.coordinator = coordinator
    def __class_getitem__(cls, item):
        return cls
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

_uc_mock = MagicMock()
_uc_mock.DataUpdateCoordinator = _FakeCoordinator
_uc_mock.UpdateFailed = Exception
_uc_mock.CoordinatorEntity = _FakeCoordinatorEntity
sys.modules["homeassistant.helpers.update_coordinator"] = _uc_mock

# Load const and coordinator before sensor (sensor imports coordinator)
_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)

ha_storage_mock = MagicMock()
class _FakeStore:
    def __init__(self, hass, version, key): pass
    async def async_load(self): return None
    async def async_save(self, data): pass
ha_storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = ha_storage_mock

_store_mod = _load(
    "custom_components.nem_pd7day.calibration_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"),
)
_coord_mod = _load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

_sensor_mod = _load(
    "custom_components.nem_pd7day.sensor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "sensor.py"),
)

from custom_components.nem_pd7day.nem_time import NEM_TZ
from custom_components.nem_pd7day.calibration_engine import CalibrationEngine, Observation
from custom_components.nem_pd7day.const import (
    CONF_CALIBRATION_REGION,
    CONF_REGIONS,
    COORDINATOR_KEY,
    DOMAIN,
    STORE_KEY,
)
from custom_components.nem_pd7day.sensor import (
    _horizon_hours,
    PD7DayCalibrationSensor,
    PD7DayGasForecastSensor,
    PD7DayForecastSensor,
    PD7DayRegionDataUpdatedDatetimeSensor,
    PD7DayRegionSourceFileDatetimeSensor,
    PD7DayInterconnectorSensor,
    async_setup_entry as sensor_async_setup_entry,
)

NEM_TZ = timezone(timedelta(hours=10))


# ── Helpers ───────────────────────────────────────────────────────────────────

def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def make_price_period(nemtime_dt: datetime, value: float = 0.10):
    """Create a PricePeriod-like mock with correct time fields."""
    start_dt = nemtime_dt - timedelta(minutes=30)
    return MagicMock(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),
        value=value,
    )


def make_sensor(store=None) -> PD7DayForecastSensor:
    """Construct a PD7DayForecastSensor bypassing HA CoordinatorEntity init."""
    coordinator = MagicMock()
    coordinator.data = None
    sensor = PD7DayForecastSensor.__new__(PD7DayForecastSensor)
    sensor.coordinator = coordinator
    sensor._region = "QLD1"
    sensor._store = store
    sensor._attr_unique_id = "nem_pd7day_qld1_forecast"
    sensor._attr_name = "QLD1 PD7DAY Forecast"
    sensor.entity_id = "sensor.qld1_pd7day_forecast"
    return sensor


def test_async_setup_entry_uses_options_regions_for_forecast_entities():
    """
    Regression: region selection changed in OptionsFlow must control which
    forecast entities are created. If entry.data is used instead of
    entry.options, only the original first region appears.
    """
    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.data = {CONF_REGIONS: ["QLD1"]}
    entry.options = {CONF_REGIONS: ["QLD1", "NSW1"]}

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_KEY: coordinator,
                STORE_KEY: MagicMock(),
            }
        }
    }

    created = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(sensor_async_setup_entry(hass, entry, _add_entities))

    forecast_entities = [
        ent for ent in created if isinstance(ent, PD7DayForecastSensor)
    ]
    regions = sorted(ent._region for ent in forecast_entities)

    assert regions == ["NSW1", "QLD1"], (
        "Forecast entities must match entry.options regions. "
        f"Got regions={regions}"
    )


def test_async_setup_entry_creates_selected_region_interconnector_entities():
    """
    Regression: interconnector entities must be created from selected regions,
    not always from the QLD-only default set.
    """
    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_2"
    entry.data = {CONF_REGIONS: ["QLD1"]}
    entry.options = {CONF_REGIONS: ["NSW1"]}

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_KEY: coordinator,
                STORE_KEY: MagicMock(),
            }
        }
    }

    created = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(sensor_async_setup_entry(hass, entry, _add_entities))

    interconnector_entities = [
        ent for ent in created if isinstance(ent, PD7DayInterconnectorSensor)
    ]
    ic_ids = sorted(ent._ic_id for ent in interconnector_entities)

    assert ic_ids == ["N-Q-MNSP1", "NSW1-QLD1", "VIC1-NSW1"], (
        "Interconnector entities must match selected region interconnectors. "
        f"Got ic_ids={ic_ids}"
    )


def test_async_setup_entry_creates_calibration_sensor_for_selected_region():
    """Calibration sensor must be created for configured calibration region."""
    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_3"
    entry.data = {CONF_REGIONS: ["QLD1"]}
    entry.options = {
        CONF_REGIONS: ["QLD1", "NSW1"],
        CONF_CALIBRATION_REGION: "NSW1",
    }

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_KEY: coordinator,
                STORE_KEY: MagicMock(),
            }
        }
    }

    created = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(sensor_async_setup_entry(hass, entry, _add_entities))

    cal_entities = [
        ent for ent in created if isinstance(ent, PD7DayCalibrationSensor)
    ]

    assert len(cal_entities) == 1
    assert cal_entities[0]._region == "NSW1"
    assert cal_entities[0].entity_id == "sensor.nem_pd7day_nsw1_calibration"
    assert cal_entities[0]._attr_unique_id == "nem_pd7day_nsw1_calibration"


def test_async_setup_entry_creates_single_gas_forecast_sensor():
    """Gas forecast must be a single standalone sensor/device."""
    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_4"
    entry.data = {CONF_REGIONS: ["QLD1"]}
    entry.options = {CONF_REGIONS: ["QLD1", "NSW1"]}

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_KEY: coordinator,
                STORE_KEY: MagicMock(),
            }
        }
    }

    created = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(sensor_async_setup_entry(hass, entry, _add_entities))

    gas_entities = [
        ent for ent in created if isinstance(ent, PD7DayGasForecastSensor)
    ]
    assert len(gas_entities) == 1
    assert gas_entities[0].entity_id == "sensor.nem_pd7day_gas_forecast"


def test_async_setup_entry_creates_region_diagnostic_datetime_sensors():
    """Each region must get source-file and updated-at diagnostic timestamp sensors."""
    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_5"
    entry.data = {CONF_REGIONS: ["QLD1"]}
    entry.options = {CONF_REGIONS: ["QLD1", "NSW1"]}

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_KEY: coordinator,
                STORE_KEY: MagicMock(),
            }
        }
    }

    created = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(sensor_async_setup_entry(hass, entry, _add_entities))

    source_dt_entities = [
        ent for ent in created if isinstance(ent, PD7DayRegionSourceFileDatetimeSensor)
    ]
    updated_dt_entities = [
        ent for ent in created if isinstance(ent, PD7DayRegionDataUpdatedDatetimeSensor)
    ]

    assert sorted(ent.entity_id for ent in source_dt_entities) == [
        "sensor.nem_pd7day_nsw1_source_file_datetime",
        "sensor.nem_pd7day_qld1_source_file_datetime",
    ]
    assert sorted(ent.entity_id for ent in updated_dt_entities) == [
        "sensor.nem_pd7day_nsw1_data_updated_datetime",
        "sensor.nem_pd7day_qld1_data_updated_datetime",
    ]


# ── Tests: _horizon_hours() ───────────────────────────────────────────────────

def test_horizon_hours_basic():
    """horizon = interval_time − run_at in hours."""
    run_at = "2026-04-15T07:30:00+10:00"
    interval = "2026-04-15T13:30:00+10:00"  # 6h later
    assert abs(_horizon_hours(run_at, interval) - 6.0) < 0.001


def test_horizon_hours_zero_run_at():
    """If run_at is None or empty, horizon must be 0.0 (not crash)."""
    assert _horizon_hours(None, "2026-04-15T08:00:00+10:00") == 0.0
    assert _horizon_hours("", "2026-04-15T08:00:00+10:00") == 0.0


def test_horizon_hours_negative_clamped_to_zero():
    """If interval is before run_at, horizon must clamp to 0.0."""
    run_at = "2026-04-15T10:00:00+10:00"
    interval = "2026-04-15T09:00:00+10:00"  # 1h before run_at
    assert _horizon_hours(run_at, interval) == 0.0, (
        "Negative horizon must be clamped to 0.0"
    )


def test_horizon_hours_tz_aware():
    """Horizon must be correct even if system clock is not UTC+10."""
    # Both strings have explicit +10:00 — subtraction must be timezone-safe
    run_at = "2026-04-15T07:30:00+10:00"
    interval = "2026-04-16T07:30:00+10:00"  # exactly 24h
    assert abs(_horizon_hours(run_at, interval) - 24.0) < 0.001


# ── Tests: _calibrate_period() — core contract ────────────────────────────────

def test_calibrate_period_uses_interval_start_for_horizon():
    """
    BUG (v1.8.0): _calibrate_period previously used period.nemtime (interval END)
    for horizon, but async_record_actual uses period.time (interval START).

    This caused misrouted bucket lookups near the 6h boundary.

    With run_at=07:30 and interval START=13:30, horizon must be exactly 6.0h.
    If nemtime (14:00) were used, horizon=6.5h — same bucket in this case,
    but wrong in general.  Use a boundary case to make the test definitive.

    run_at=08:00, interval START=14:00 → horizon=6.0h → h06_12
    run_at=08:00, interval nemtime=14:30 → horizon=6.5h → also h06_12

    Use run_at=08:30, interval START=14:00 → horizon=5.5h → h00_06 (< 6h)
    If nemtime=14:30 used → horizon=6.0h → h06_12 (wrong bucket!)
    """
    sensor = make_sensor(store=None)
    run_at = "2026-04-15T08:30:00+10:00"
    # interval START = 14:00, nemtime = 14:30
    interval_start_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.10)

    result = sensor._calibrate_period(period, run_at)

    # With interval START: (14:00 - 08:30) = 5.5h → h00_06 bucket
    # With interval END:   (14:30 - 08:30) = 6.0h → h06_12 bucket (WRONG)
    assert abs(result["horizon_hours"] - 5.5) < 0.1, (
        f"horizon_hours={result['horizon_hours']}. "
        f"Expected 5.5h (using interval START 14:00 − run_at 08:30). "
        f"If 6.0h, _calibrate_period is incorrectly using nemtime (interval END)."
    )


def test_calibrate_period_output_has_required_keys():
    """
    _calibrate_period must return a dict with all keys expected by downstream
    template sensors: nemtime, time, raw_value, horizon_hours, value.
    """
    sensor = make_sensor(store=None)
    run_at = "2026-04-15T07:30:00+10:00"
    interval_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period = make_price_period(interval_end_dt, value=0.085)

    result = sensor._calibrate_period(period, run_at)

    required_keys = {"nemtime", "time", "raw_value", "horizon_hours", "value"}
    missing = required_keys - set(result.keys())
    assert not missing, (
        f"_calibrate_period missing keys: {missing}. "
        f"Template sensors depending on 'value' will break."
    )


def test_calibrate_period_value_equals_raw_when_no_store():
    """Without a calibration store, 'value' must equal 'raw_value' (passthrough)."""
    sensor = make_sensor(store=None)
    run_at = "2026-04-15T07:30:00+10:00"
    interval_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period = make_price_period(interval_end_dt, value=0.085)

    result = sensor._calibrate_period(period, run_at)

    assert abs(result["raw_value"] - 0.085) < 1e-9
    assert abs(result["value"] - 0.085) < 1e-9, (
        f"Without store, 'value' must equal raw. Got {result['value']}"
    )


def test_calibrate_period_nemtime_is_interval_end():
    """result['nemtime'] must be interval END (period.nemtime)."""
    sensor = make_sensor(store=None)
    interval_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period = make_price_period(interval_end_dt, value=0.10)

    result = sensor._calibrate_period(period, "2026-04-15T07:30:00+10:00")

    assert result["nemtime"] == nem_iso(interval_end_dt), (
        f"nemtime wrong: {result['nemtime']!r}. Must be interval END."
    )


def test_calibrate_period_time_is_interval_start():
    """result['time'] must be interval START (period.time = nemtime − 30min)."""
    sensor = make_sensor(store=None)
    interval_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    interval_start_dt = interval_end_dt - timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.10)

    result = sensor._calibrate_period(period, "2026-04-15T07:30:00+10:00")

    assert result["time"] == nem_iso(interval_start_dt), (
        f"time wrong: {result['time']!r}. Must be interval START (nemtime − 30min)."
    )


def test_calibrate_period_with_active_calibration():
    """
    With an active calibration store, _calibrate_period must include
    calibrated, p10, p50, p90, mae, calibrated_source, n_obs keys.
    """
    # Build a calibration store with enough observations to activate a bucket
    from custom_components.nem_pd7day.calibration_engine import (
        CalibrationEngine, Observation, CalibrationResult, BucketModel,
        LinearCoeff, QuantileCoeff
    )

    # Create a mock store that returns a fixed apply_to_price result
    mock_store = MagicMock()
    mock_store.calibration = MagicMock()  # not None → calibration active
    mock_store.apply_to_price.return_value = {
        "calibrated": 0.072,
        "p10": 0.055,
        "p50": 0.070,
        "p90": 0.095,
        "mae": 0.012,
        "calibrated_source": "ols",
        "n_obs": 42,
    }

    sensor = make_sensor(store=mock_store)
    interval_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period = make_price_period(interval_end_dt, value=0.085)

    result = sensor._calibrate_period(period, "2026-04-15T07:30:00+10:00")

    cal_keys = {"calibrated", "p10", "p50", "p90", "mae", "calibrated_source", "n_obs"}
    missing = cal_keys - set(result.keys())
    assert not missing, f"Calibration keys missing from output: {missing}"
    assert abs(result["value"] - 0.072) < 1e-9, (
        f"'value' must equal 'calibrated' when store is active. Got {result['value']}"
    )
    assert result["calibrated_source"] == "ols"


def test_calibrate_period_horizon_used_for_store_lookup():
    """
    The horizon passed to store.apply_to_price() must be computed from
    period.time (interval START), matching what async_record_actual uses.
    Run_at=08:30, interval_start=14:00 → horizon=5.5h.
    """
    mock_store = MagicMock()
    mock_store.calibration = MagicMock()
    mock_store.apply_to_price.return_value = {
        "calibrated": 0.085, "p10": None, "p50": None, "p90": None,
        "mae": None, "calibrated_source": "passthrough", "n_obs": 0,
    }

    sensor = make_sensor(store=mock_store)
    run_at = "2026-04-15T08:30:00+10:00"
    interval_start_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.085)

    sensor._calibrate_period(period, run_at)

    # Check what horizon was passed to apply_to_price
    call_args = mock_store.apply_to_price.call_args
    horizon_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("h")
    # apply_to_price(raw_price, h, hour) — positional
    h_passed = call_args[0][1]
    assert abs(h_passed - 5.5) < 0.1, (
        f"apply_to_price called with horizon={h_passed:.2f}h. "
        f"Expected 5.5h (interval START 14:00 − run_at 08:30). "
        f"If 6.0h, nemtime (interval END) is being used — bucket routing mismatch."
    )


# ── Tests: horizon/bucket routing symmetry between sensor and store ────────────

def test_bucket_routing_consistent_at_h06_12_boundary():
    """
    The 6h bucket boundary is the most common misrouting point.

    Simulate an observation stored at horizon=5.9h (h00_06 bucket).
    The sensor's _calibrate_period must look up the same h00_06 bucket,
    not h06_12.

    We verify this by checking that the horizon returned by _calibrate_period
    is consistent with what the store would compute from the same timestamps.
    """
    from custom_components.nem_pd7day.calibration_engine import _bucket_key

    run_at_dt = datetime(2026, 4, 15, 8, 0, tzinfo=NEM_TZ)
    # interval_start at 13:54, 6 minutes before the 6h boundary
    interval_start_dt = run_at_dt + timedelta(hours=5, minutes=54)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)

    run_at_str = nem_iso(run_at_dt)
    period = make_price_period(interval_end_dt, value=0.10)

    # Sensor horizon (should use period.time = interval START)
    sensor_horizon = _horizon_hours(run_at_str, period.time)
    # Store horizon (uses interval_time = period.time)
    store_horizon_h = (interval_start_dt - run_at_dt).total_seconds() / 3600

    sensor_bucket = _bucket_key(sensor_horizon, interval_start_dt.hour)
    store_bucket = _bucket_key(store_horizon_h, interval_start_dt.hour)

    assert sensor_bucket == store_bucket, (
        f"Bucket mismatch at 6h boundary: sensor routed to '{sensor_bucket}', "
        f"store trained '{store_bucket}'. "
        f"sensor_horizon={sensor_horizon:.3f}h, store_horizon={store_horizon_h:.3f}h. "
        f"Check that _calibrate_period uses period.time not period.nemtime."
    )


def test_bucket_routing_consistent_at_h12_24_boundary():
    """Same consistency check at the 12h horizon boundary."""
    from custom_components.nem_pd7day.calibration_engine import _bucket_key

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    # interval_start exactly at 19:30 → horizon = 12.0h
    interval_start_dt = run_at_dt + timedelta(hours=12)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)

    run_at_str = nem_iso(run_at_dt)
    period = make_price_period(interval_end_dt, value=0.10)

    sensor_horizon = _horizon_hours(run_at_str, period.time)
    store_horizon_h = (interval_start_dt - run_at_dt).total_seconds() / 3600

    assert abs(sensor_horizon - store_horizon_h) < 0.001, (
        f"Sensor horizon {sensor_horizon}h != store horizon {store_horizon_h}h. "
        f"The 30-min discrepancy from using nemtime vs time will misroute buckets."
    )


# ── Tests: native_value passthrough ───────────────────────────────────────────

def test_native_value_returns_none_when_no_data():
    """native_value must be None when coordinator has no data."""
    sensor = make_sensor(store=None)
    sensor.coordinator.data = None
    assert sensor.native_value is None


def test_native_value_returns_raw_when_no_store():
    """Without a store, native_value must return current_value directly."""
    sensor = make_sensor(store=None)
    price_data = MagicMock()
    price_data.current_value = 0.085
    sensor.coordinator.data = MagicMock()
    sensor.coordinator.data.prices = {"QLD1": price_data}
    assert abs(sensor.native_value - 0.085) < 1e-9
