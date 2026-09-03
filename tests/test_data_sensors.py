"""
Tests for the diagnostic data sensors in sensor.py.

`PD7DayDataSensor` exposes the full PD7DAY forecast list as an unrecorded
``forecast`` attribute; `StpasaDataSensor` exposes the STPASA intervals list as
an unrecorded ``intervals`` attribute.  Both report the relevant run_datetime as
state and fall back to STATE_UNAVAILABLE when no data is present.

Run with:  python -m pytest tests/test_data_sensors.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
from datetime import datetime, timezone
from unittest.mock import MagicMock

# ── Module loader ─────────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


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

# Real STATE_UNAVAILABLE so state comparisons behave like production
const_mock = MagicMock()
const_mock.STATE_UNAVAILABLE = "unavailable"
sys.modules["homeassistant.const"] = const_mock

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


class _FakeCoordinator:
    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.last_update_success = True
        self.data = None

    def __class_getitem__(cls, item):
        return cls

    async def async_config_entry_first_refresh(self):
        pass

    async def async_refresh(self):
        pass


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

_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)

ha_storage_mock = MagicMock()


class _FakeStore:
    def __init__(self, hass, version, key):
        pass

    async def async_load(self):
        return None

    async def async_save(self, data):
        pass


ha_storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = ha_storage_mock

_cal_store_mod = _load(
    "custom_components.nem_pd7day.calibration_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"),
)
_coord_mod = _load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)
_stpasa_client_mod = _load(
    "custom_components.nem_pd7day.stpasa_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "stpasa_client.py"),
)
_sensor_mod = _load(
    "custom_components.nem_pd7day.sensor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "sensor.py"),
)

from custom_components.nem_pd7day.const import DOMAIN
from custom_components.nem_pd7day.pd7day_client import PD7DayData, PricePeriod
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult
from custom_components.nem_pd7day.sensor import (
    PD7DayDataSensor,
    StpasaDataSensor,
)

STATE_UNAVAILABLE = _sensor_mod.STATE_UNAVAILABLE
REGION = "QLD1"
RUN_DT = "2026-06-12T10:00:00+10:00"


# ── Helpers ─────────────────────────────────────────────────────────────────


def make_price_period(value: float = 0.10) -> PricePeriod:
    return PricePeriod(
        nemtime="2026-06-12T10:30:00+10:00",
        time="2026-06-12T10:00:00+10:00",
        value=value,
    )


def make_pd7day_data(run_dt: str | None = RUN_DT) -> PD7DayData:
    return PD7DayData(
        region=REGION,
        source_file="PUBLIC_PD7DAY.zip",
        forecast_generated_at=run_dt,
        interval_minutes=30,
        current_value=0.10,
        next_value=0.11,
        min_24h_value=0.05,
        max_24h_value=0.20,
        cheapest_2h_window=None,
        forecast=[make_price_period()],
    )


def make_pd7day_sensor(coordinator_data=None, store=None) -> PD7DayDataSensor:
    coordinator = MagicMock()
    coordinator.data = coordinator_data
    coordinator.interconnectors = {}
    sensor = PD7DayDataSensor.__new__(PD7DayDataSensor)
    sensor.coordinator = coordinator
    sensor._region = REGION
    sensor._store = store
    sensor._entry = MagicMock(entry_id="entry_test")
    return sensor


def make_stpasa_result(run_dt: str = RUN_DT) -> StpasaResult:
    return StpasaResult(
        region=REGION,
        run_datetime=run_dt,
        intervals=[
            StpasaInterval(
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


def make_stpasa_sensor(stpasa_result=None) -> StpasaDataSensor:
    sensor = StpasaDataSensor.__new__(StpasaDataSensor)
    sensor.coordinator = MagicMock()
    sensor._region = REGION
    sensor._entry = MagicMock(entry_id="entry_test")
    store = MagicMock()
    store.latest.return_value = stpasa_result
    hass = MagicMock()
    hass.data = {DOMAIN: {"stpasa_stores": {REGION: store}}}
    sensor.hass = hass
    return sensor


# ── PD7DayDataSensor ──────────────────────────────────────────────────────────


def test_pd7day_data_sensor_state():
    coordinator_data = MagicMock()
    coordinator_data.prices = {REGION: make_pd7day_data()}
    sensor = make_pd7day_sensor(coordinator_data=coordinator_data)
    assert sensor.native_value == RUN_DT

    attrs = sensor.extra_state_attributes
    assert attrs["run_datetime"] == RUN_DT
    assert attrs["region"] == REGION
    assert attrs["interval_count"] == 1
    entry = attrs["forecast"][0]
    assert entry["raw_rrp"] == 0.10
    assert set(entry.keys()) == {
        "time", "nemtime", "raw_rrp", "calibrated",
        "p10", "p90", "calibrated_source", "band_source", "horizon_hours",
    }


def test_pd7day_data_sensor_unavailable():
    sensor = make_pd7day_sensor(coordinator_data=None)
    assert sensor.native_value == STATE_UNAVAILABLE
    assert sensor.extra_state_attributes == {}


def test_pd7day_data_sensor_unrecorded():
    assert "forecast" in PD7DayDataSensor._unrecorded_attributes


# ── StpasaDataSensor ──────────────────────────────────────────────────────────


def test_stpasa_data_sensor_state():
    sensor = make_stpasa_sensor(stpasa_result=make_stpasa_result())
    assert sensor.native_value == RUN_DT

    attrs = sensor.extra_state_attributes
    assert attrs["run_datetime"] == RUN_DT
    assert attrs["region"] == REGION
    assert attrs["interval_count"] == 1
    interval = attrs["intervals"][0]
    assert interval["demand50"] == 5500.0
    assert interval["surpluscapacity"] == 1200.0
    assert set(interval.keys()) == {
        "interval_datetime", "demand10", "demand50", "demand90",
        "surpluscapacity", "ss_solar_uigf", "ss_wind_uigf",
    }


def test_stpasa_data_sensor_unavailable():
    sensor = make_stpasa_sensor(stpasa_result=None)
    assert sensor.native_value == STATE_UNAVAILABLE
    assert sensor.extra_state_attributes == {}


def test_stpasa_data_sensor_unrecorded():
    assert "intervals" in StpasaDataSensor._unrecorded_attributes
