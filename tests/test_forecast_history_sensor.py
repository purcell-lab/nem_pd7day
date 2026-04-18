"""
Tests for PD7DayForecastHistorySensor — native_value, attributes, empty store.

Run with:  python -m pytest tests/test_forecast_history_sensor.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
from unittest.mock import MagicMock

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
sensor_mock.SensorEntity = object
sys.modules["homeassistant.components.sensor"] = sensor_mock

# Stub CoordinatorEntity
class _FakeCoordinatorEntity:
    def __init__(self, coordinator=None, **kwargs):
        self.coordinator = coordinator
    def __class_getitem__(cls, item):
        return cls
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

class _FakeCoordinator:
    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.last_update_success = True
        self.data = None
    def __class_getitem__(cls, item):
        return cls
    async def async_config_entry_first_refresh(self): pass
    async def async_refresh(self): pass

_uc_mock = MagicMock()
_uc_mock.DataUpdateCoordinator = _FakeCoordinator
_uc_mock.UpdateFailed = Exception
_uc_mock.CoordinatorEntity = _FakeCoordinatorEntity
sys.modules["homeassistant.helpers.update_coordinator"] = _uc_mock

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

ha_storage_mock = MagicMock()
class _FakeStore:
    def __init__(self, hass, version, key): pass
    async def async_load(self): return None
    async def async_save(self, data): pass
ha_storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = ha_storage_mock

_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
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

from custom_components.nem_pd7day.sensor import PD7DayForecastHistorySensor
from custom_components.nem_pd7day.const import storage_keys


def _make_sensor(forecast_history=None):
    """Create a PD7DayForecastHistorySensor with mocked store."""
    coordinator = MagicMock()
    coordinator.last_update_success = True

    store = MagicMock()
    store._forecast_history = forecast_history or {}

    entry = MagicMock()
    entry.entry_id = "test_entry"

    sensor = PD7DayForecastHistorySensor.__new__(PD7DayForecastHistorySensor)
    sensor.coordinator = coordinator
    sensor._entry = entry
    sensor._region = "QLD1"
    sensor._store = store
    sensor._attr_unique_id = "nem_pd7day_qld1_forecast_history"
    sensor._attr_name = "Forecast History"
    sensor.entity_id = "sensor.nem_pd7day_qld1_forecast_history"
    return sensor


def test_forecast_history_sensor_native_value():
    """native_value should equal total forecast entries across all intervals."""
    sensor = _make_sensor(forecast_history={
        "2026-04-18T17:00:00+10:00": [
            {"run_at": "x", "price": 0.09},
        ],
    })
    assert sensor.native_value == 1


def test_forecast_history_sensor_native_value_multiple():
    """native_value should sum entries across all interval keys."""
    sensor = _make_sensor(forecast_history={
        "2026-04-18T17:00:00+10:00": [
            {"run_at": "a", "price": 0.09},
            {"run_at": "b", "price": 0.10},
        ],
        "2026-04-18T17:30:00+10:00": [
            {"run_at": "c", "price": 0.08},
        ],
    })
    assert sensor.native_value == 3


def test_forecast_history_sensor_attributes():
    """Attributes should reflect forecast history metadata."""
    sensor = _make_sensor(forecast_history={
        "2026-04-18T17:00:00+10:00": [
            {"run_at": "x", "price": 0.09},
        ],
    })
    attrs = sensor.extra_state_attributes
    assert attrs["interval_keys"] == 1
    assert attrs["oldest_interval"] == "2026-04-18T17:00:00+10:00"
    assert attrs["newest_interval"] == "2026-04-18T17:00:00+10:00"
    assert attrs["runs_per_interval_avg"] == 1.0
    assert attrs["storage_key"] == storage_keys("QLD1")[2]
    assert attrs["region"] == "QLD1"


def test_forecast_history_sensor_empty_store():
    """Empty forecast history should return 0 and sensible attributes."""
    sensor = _make_sensor(forecast_history={})
    assert sensor.native_value == 0
    attrs = sensor.extra_state_attributes
    assert attrs["interval_keys"] == 0
    assert attrs["oldest_interval"] is None
    assert attrs["newest_interval"] is None
    assert attrs["runs_per_interval_avg"] == 0
    assert attrs["storage_key"] == storage_keys("QLD1")[2]


def test_forecast_history_sensor_none_store():
    """If store is None, native_value should be 0."""
    coordinator = MagicMock()
    coordinator.last_update_success = True
    entry = MagicMock()
    entry.entry_id = "test_entry"

    sensor = PD7DayForecastHistorySensor.__new__(PD7DayForecastHistorySensor)
    sensor.coordinator = coordinator
    sensor._entry = entry
    sensor._region = "QLD1"
    sensor._store = None
    sensor._attr_unique_id = "nem_pd7day_qld1_forecast_history"
    sensor._attr_name = "Forecast History"
    sensor.entity_id = "sensor.nem_pd7day_qld1_forecast_history"

    assert sensor.native_value == 0
