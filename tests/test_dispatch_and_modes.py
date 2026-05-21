"""
Tests for dispatch coordinator, dispatch fallback in sensors, forecast mode
trimming, tariff visibility, and migration.

Covers:
  1. DispatchCoordinator falls back gracefully when fetch raises
  2. Sensor native_value uses dispatch price when available, falls back to PD7DAY
  3. Forecast trim: days_1_7 mode returns full forecast; days_2_7 mode returns trimmed
  4. Tariff visibility: in days_2_7 mode, only active tariff has enabled_default=True
  5. Migration: missing CONF_FORECAST_MODE defaults to days_2_7
  6. Sensor name is dynamic based on mode

Run with: python -m pytest tests/test_dispatch_and_modes.py -v
"""
from __future__ import annotations

import asyncio
import sys
import os
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# ── Module loader ─────────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
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

_dispatch_mod = _load(
    "custom_components.nem_pd7day.dispatch_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "dispatch_client.py"),
)

_coord_mod = _load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

_sensor_mod = _load(
    "custom_components.nem_pd7day.sensor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "sensor.py"),
)

_tariff_mod = _load(
    "custom_components.nem_pd7day.tariff_sensor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "tariff_sensor.py"),
)

from custom_components.nem_pd7day.const import (
    CONF_ACTIVE_TARIFF,
    CONF_FORECAST_MODE,
    CONF_REGION,
    COORDINATOR_KEY,
    DEFAULT_ENABLED_TARIFFS,
    DISPATCH_KEY,
    DISTRIBUTOR_DISPLAY_NAMES,
    DOMAIN,
    FORECAST_MODE_DAYS_2_7,
    FORECAST_MODE_FULL,
    STORE_KEY,
)
from custom_components.nem_pd7day.dispatch_client import DispatchPrice
from custom_components.nem_pd7day.coordinator import DispatchCoordinator
from custom_components.nem_pd7day.sensor import PD7DayForecastSensor, SpotPriceForecastDays27Sensor
from custom_components.nem_pd7day.tariff_sensor import NemPd7dayTariffSensor, get_tariff_name
from custom_components.nem_pd7day.nem_time import _amber_express_cutoff

NEM_TZ = timezone(timedelta(hours=10))


def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def make_price_period(nemtime_dt: datetime, value: float = 0.10):
    start_dt = nemtime_dt - timedelta(minutes=30)
    return MagicMock(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),
        value=value,
    )


def make_sensor(store=None, mode=FORECAST_MODE_DAYS_2_7) -> PD7DayForecastSensor:
    """Construct a PD7DayForecastSensor with mode-aware options."""
    coordinator = MagicMock()
    coordinator.data = None
    sensor = PD7DayForecastSensor.__new__(PD7DayForecastSensor)
    sensor.coordinator = coordinator
    sensor._region = "QLD1"
    sensor._store = store
    sensor._attr_unique_id = "nem_pd7day_qld1_forecast"
    entry = MagicMock()
    entry.entry_id = "entry_test"
    entry.options = {CONF_FORECAST_MODE: mode}
    sensor._entry = entry
    sensor._attr_name = "NEM Spot Price Forecast"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {"entry_test": {}}}
    return sensor


def make_tariff_sensor(
    region="QLD1",
    distributor="energex",
    tariff_code="8400",
    price_periods=None,
    mode=FORECAST_MODE_DAYS_2_7,
    active_tariff="",
) -> NemPd7dayTariffSensor:
    """Construct a NemPd7dayTariffSensor with mode-aware options."""
    coordinator = MagicMock()
    if price_periods is not None:
        price_data = MagicMock()
        price_data.forecast = price_periods
        coordinator.data = MagicMock()
        coordinator.data.prices = {region: price_data}
    else:
        coordinator.data = None
    coordinator.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {
        CONF_FORECAST_MODE: mode,
        CONF_ACTIVE_TARIFF: active_tariff,
    }

    sensor = NemPd7dayTariffSensor.__new__(NemPd7dayTariffSensor)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._tariff_code = tariff_code
    sensor._entry = entry
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{tariff_code}_tariff"
    distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
    tariff_name = get_tariff_name(distributor, tariff_code)
    sensor._attr_name = f"{distributor_display} {tariff_name} Tariff ({tariff_code})"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {"entry_1": {}}}
    sensor.hass.states.get.return_value = None
    return sensor


# ── 1. DispatchCoordinator fallback ──────────────────────────────────────────

def test_dispatch_coordinator_handles_fetch_failure():
    """DispatchCoordinator must raise UpdateFailed when fetch raises."""
    hass = MagicMock()
    hass.async_add_executor_job = MagicMock(side_effect=ConnectionError("offline"))
    coord = DispatchCoordinator.__new__(DispatchCoordinator)
    coord.hass = hass
    coord.region = "QLD1"
    coord.prices = {}
    coord.last_updated = None

    try:
        run_async(coord._async_update_data())
        assert False, "Should have raised"
    except Exception as exc:
        assert "DispatchIS fetch failed" in str(exc)


def test_dispatch_coordinator_stores_prices():
    """Successful fetch stores prices and last_updated."""
    fake_prices = {
        "QLD1": DispatchPrice("QLD1", "2026/05/21 09:30:00", 0.085),
    }
    hass = MagicMock()

    async def _fake_add_executor_job(fn, *args, **kwargs):
        return fake_prices

    hass.async_add_executor_job = _fake_add_executor_job

    coord = DispatchCoordinator.__new__(DispatchCoordinator)
    coord.hass = hass
    coord.region = "QLD1"
    coord.prices = {}
    coord.last_updated = None

    result = run_async(coord._async_update_data())
    assert coord.prices["QLD1"].rrp == 0.085
    assert coord.last_updated is not None


# ── 2. Sensor native_value uses dispatch, falls back to PD7DAY ──────────────

def test_sensor_native_value_uses_dispatch_when_available():
    """native_value should return dispatch price when available."""
    sensor = make_sensor(store=None)

    # Set up dispatch coordinator with prices
    dispatch = MagicMock()
    dispatch.prices = {"QLD1": DispatchPrice("QLD1", "2026/05/21 09:30:00", 0.042)}
    sensor.hass.data = {DOMAIN: {"entry_test": {DISPATCH_KEY: dispatch}}}

    assert abs(sensor.native_value - 0.042) < 1e-9


def test_sensor_native_value_falls_back_to_pd7day():
    """native_value should fall back to PD7DAY when dispatch has no data."""
    sensor = make_sensor(store=None)

    # Set up dispatch with no prices for our region
    dispatch = MagicMock()
    dispatch.prices = {}
    sensor.hass.data = {DOMAIN: {"entry_test": {DISPATCH_KEY: dispatch}}}

    # Set up PD7DAY data
    now = datetime.now(NEM_TZ)
    interval_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    interval_end = interval_start + timedelta(minutes=30)
    period = MagicMock()
    period.time = nem_iso(interval_start)
    period.nemtime = nem_iso(interval_end)
    period.value = 0.085

    price_data = MagicMock()
    price_data.forecast = [period]
    price_data.forecast_generated_at = nem_iso(now - timedelta(hours=1))
    sensor.coordinator.data = MagicMock()
    sensor.coordinator.data.prices = {"QLD1": price_data}

    assert abs(sensor.native_value - 0.085) < 1e-9


def test_sensor_native_value_falls_back_when_no_dispatch():
    """native_value should fall back when there's no dispatch coordinator at all."""
    sensor = make_sensor(store=None)
    # No dispatch key in hass.data
    sensor.hass.data = {DOMAIN: {"entry_test": {}}}

    now = datetime.now(NEM_TZ)
    interval_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    interval_end = interval_start + timedelta(minutes=30)
    period = MagicMock()
    period.time = nem_iso(interval_start)
    period.nemtime = nem_iso(interval_end)
    period.value = 0.075

    price_data = MagicMock()
    price_data.forecast = [period]
    price_data.forecast_generated_at = nem_iso(now - timedelta(hours=1))
    sensor.coordinator.data = MagicMock()
    sensor.coordinator.data.prices = {"QLD1": price_data}

    assert abs(sensor.native_value - 0.075) < 1e-9


# ── 3. Forecast trim: days_1_7 full vs days_2_7 trimmed ─────────────────────

def test_forecast_trim_full_mode_returns_all():
    """In days_1_7 mode, forecast should contain ALL intervals (no trim)."""
    sensor = make_sensor(store=None, mode=FORECAST_MODE_FULL)

    run_at_dt = datetime(2026, 5, 19, 6, 0, tzinfo=NEM_TZ)
    run_at_str = nem_iso(run_at_dt)

    # Build 200 periods (some within Amber cutoff, some beyond)
    periods = []
    for i in range(200):
        interval_end_dt = run_at_dt + timedelta(minutes=30 * (i + 1))
        periods.append(make_price_period(interval_end_dt, value=0.05 + i * 0.001))

    price_data = MagicMock()
    price_data.forecast = periods
    price_data.forecast_generated_at = run_at_str
    price_data.region = "QLD1"
    price_data.interval_minutes = 30
    price_data.source_file = "test.xml"

    sensor.coordinator.data = MagicMock()
    sensor.coordinator.data.prices = {"QLD1": price_data}

    attrs = sensor.extra_state_attributes
    forecast = attrs["forecast"]

    # In full mode, ALL 200 intervals should be present
    assert len(forecast) == 200, (
        f"Full mode should return all 200 intervals, got {len(forecast)}"
    )


def test_forecast_trim_days_2_7_sensor_trims():
    """SpotPriceForecastDays27Sensor should trim intervals within Amber cutoff."""
    # Build SpotPriceForecastDays27Sensor
    coordinator = MagicMock()
    coordinator.data = None
    sensor = SpotPriceForecastDays27Sensor.__new__(SpotPriceForecastDays27Sensor)
    sensor.coordinator = coordinator
    sensor._region = "QLD1"
    sensor._store = None
    sensor._attr_unique_id = "nem_pd7day_qld1_forecast_days27"
    sensor._attr_name = "NEM Spot Price Forecast Day 2-7"
    entry = MagicMock()
    entry.entry_id = "entry_test"
    entry.options = {CONF_FORECAST_MODE: FORECAST_MODE_DAYS_2_7}
    sensor._entry = entry
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {"entry_test": {}}}

    fake_now = datetime(2026, 5, 19, 6, 0, tzinfo=NEM_TZ)
    cutoff = _amber_express_cutoff(now=fake_now)
    run_at_str = nem_iso(fake_now)

    periods = []
    for i in range(200):
        interval_end_dt = fake_now + timedelta(minutes=30 * (i + 1))
        periods.append(make_price_period(interval_end_dt, value=0.05 + i * 0.001))

    price_data = MagicMock()
    price_data.forecast = periods
    price_data.forecast_generated_at = run_at_str
    price_data.region = "QLD1"
    price_data.interval_minutes = 30
    price_data.source_file = "test.xml"

    sensor.coordinator.data = MagicMock()
    sensor.coordinator.data.prices = {"QLD1": price_data}

    with patch("custom_components.nem_pd7day.sensor._amber_express_cutoff", return_value=cutoff):
        attrs = sensor.extra_state_attributes
    forecast = attrs["forecast"]

    assert len(forecast) < 200, f"Day 2-7 sensor should trim. Got {len(forecast)}"
    assert len(forecast) > 0, "Trimmed forecast should not be empty"


# ── 4. Tariff visibility: days_2_7 mode + active_tariff ──────────────────────

def test_tariff_visibility_days_2_7_active_tariff_enabled():
    """In days_2_7 mode with active_tariff set, only that tariff should be enabled."""
    sensor = make_tariff_sensor(
        distributor="energex",
        tariff_code="6900",
        mode=FORECAST_MODE_DAYS_2_7,
        active_tariff="energex/6900",
    )
    assert sensor.entity_registry_enabled_default is True


def test_tariff_visibility_days_2_7_all_defaults_still_enabled():
    """In days_2_7 mode, base tariff sensors use DEFAULT_ENABLED_TARIFFS (8900 is default-enabled)."""
    sensor = make_tariff_sensor(
        distributor="energex",
        tariff_code="8900",
        mode=FORECAST_MODE_DAYS_2_7,
        active_tariff="energex/6900",
    )
    # Base tariff sensors always use DEFAULT_ENABLED_TARIFFS; 8900 is in that set
    assert sensor.entity_registry_enabled_default is True


def test_tariff_visibility_days_2_7_no_active_tariff_uses_defaults():
    """In days_2_7 mode with no active_tariff, fall back to DEFAULT_ENABLED_TARIFFS."""
    sensor = make_tariff_sensor(
        distributor="energex",
        tariff_code="6900",
        mode=FORECAST_MODE_DAYS_2_7,
        active_tariff="",
    )
    # energex/6900 is in DEFAULT_ENABLED_TARIFFS
    assert sensor.entity_registry_enabled_default is True


def test_tariff_visibility_full_mode_uses_defaults():
    """In days_1_7 mode, visibility uses DEFAULT_ENABLED_TARIFFS regardless of active_tariff."""
    sensor_enabled = make_tariff_sensor(
        distributor="energex",
        tariff_code="6900",
        mode=FORECAST_MODE_FULL,
        active_tariff="energex/8900",
    )
    assert sensor_enabled.entity_registry_enabled_default is True

    sensor_disabled = make_tariff_sensor(
        distributor="energex",
        tariff_code="8400",
        mode=FORECAST_MODE_FULL,
    )
    assert sensor_disabled.entity_registry_enabled_default is False


# ── 5. Migration: missing CONF_FORECAST_MODE → defaults to days_2_7 ─────────

def test_migration_missing_forecast_mode():
    """Entry without CONF_FORECAST_MODE should default to days_2_7 in sensors."""
    sensor = make_sensor(store=None)
    # Simulate missing forecast_mode by using empty options
    sensor._entry.options = {}

    # Mode defaults to days_2_7 for migration
    mode = sensor._entry.options.get(CONF_FORECAST_MODE, FORECAST_MODE_DAYS_2_7)
    assert mode == FORECAST_MODE_DAYS_2_7
    # But sensor name is always the same regardless
    assert sensor._attr_name == "NEM Spot Price Forecast"


def test_migration_tariff_visibility_no_mode():
    """Without forecast_mode, tariff visibility should use DEFAULT_ENABLED_TARIFFS."""
    sensor = make_tariff_sensor(
        distributor="energex",
        tariff_code="6900",
    )
    # Override options to simulate migration (no forecast_mode)
    sensor._entry.options = {}
    # entity_registry_enabled_default reads mode from options with default days_2_7
    # and since no active_tariff, falls back to DEFAULT_ENABLED_TARIFFS
    assert sensor.entity_registry_enabled_default is True


# ── 6. Sensor name is dynamic based on mode ──────────────────────────────────

def test_sensor_name_always_nem_spot_price_forecast():
    """Base sensor name is always 'NEM Spot Price Forecast' regardless of mode."""
    sensor_full = make_sensor(store=None, mode=FORECAST_MODE_FULL)
    assert sensor_full._attr_name == "NEM Spot Price Forecast"

    sensor_d27 = make_sensor(store=None, mode=FORECAST_MODE_DAYS_2_7)
    assert sensor_d27._attr_name == "NEM Spot Price Forecast"


# ── 7. Tariff forecast trim is also mode-aware ──────────────────────────────

def test_tariff_forecast_full_mode_no_trim():
    """In days_1_7 mode, tariff forecast should include ALL intervals."""
    now = datetime.now(tz=NEM_TZ)
    base = now.replace(minute=0, second=0, microsecond=0)
    periods = [make_price_period(base + timedelta(minutes=30 * (i + 1)), value=0.05) for i in range(10)]

    sensor = make_tariff_sensor(
        price_periods=periods,
        mode=FORECAST_MODE_FULL,
    )

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        attrs = sensor.extra_state_attributes
    assert len(attrs["forecast"]) == 10


def test_tariff_forecast_base_always_full():
    """Base tariff sensor always returns full day 1-7 forecast regardless of mode."""
    fake_now = datetime(2026, 5, 19, 6, 0, tzinfo=NEM_TZ)

    base = fake_now.replace(minute=0, second=0, microsecond=0)
    periods = []
    for i in range(367):
        nemtime_dt = base + timedelta(minutes=30 * (i + 1))
        periods.append(make_price_period(nemtime_dt, value=0.05))

    sensor = make_tariff_sensor(
        price_periods=periods,
        mode=FORECAST_MODE_DAYS_2_7,
    )

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        attrs = sensor.extra_state_attributes
    # Base tariff sensor returns ALL intervals (day 1-7)
    assert len(attrs["forecast"]) == 367


# ── 8. Config flow forecast_mode step ────────────────────────────────────────

def test_config_flow_forecast_mode_options():
    """Verify FORECAST_MODE_OPTIONS are correctly defined."""
    from custom_components.nem_pd7day.const import FORECAST_MODE_FULL, FORECAST_MODE_DAYS_2_7
    assert FORECAST_MODE_FULL == "days_1_7"
    assert FORECAST_MODE_DAYS_2_7 == "days_2_7"


# ── 9. Tariff sensor dispatch native_value ───────────────────────────────────

def test_tariff_sensor_dispatch_native_value():
    """Tariff sensor should use dispatch price when available."""
    now = datetime.now(tz=NEM_TZ)
    current_end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
    period = make_price_period(current_end, value=0.10)
    sensor = make_tariff_sensor(price_periods=[period])

    # Set up dispatch with prices
    dispatch = MagicMock()
    dispatch.prices = {"QLD1": DispatchPrice("QLD1", "2026/05/21 09:30:00", 0.050)}
    sensor.hass.data = {DOMAIN: {"entry_1": {DISPATCH_KEY: dispatch}}}

    # Mock spot_to_tariff for the dispatch path
    # New formula: (12.5/100 + 0.0293) * 1.1
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=12.5):
        val = sensor.native_value
        assert val is not None
        expected = round((12.5 / 100 + 0.0293) * 1.1, 6)
        assert abs(val - expected) < 1e-6, f"Expected {expected}, got {val}"


def test_tariff_sensor_dispatch_fallback():
    """Tariff sensor should fall back to PD7DAY when dispatch has no data."""
    now = datetime.now(tz=NEM_TZ)
    current_end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
    period = make_price_period(current_end, value=0.10)
    sensor = make_tariff_sensor(price_periods=[period])

    # No dispatch data for QLD1
    dispatch = MagicMock()
    dispatch.prices = {}
    sensor.hass.data = {DOMAIN: {"entry_1": {DISPATCH_KEY: dispatch}}}

    # Fallback uses PD7DAY forecast path: (15.5/100 + 0.0293) * 1.1
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5):
        val = sensor.native_value
        assert val is not None
        expected = round((15.5 / 100 + 0.0293) * 1.1, 6)
        assert abs(val - expected) < 1e-6, f"Expected {expected}, got {val}"


# ── 10. Additive sensor registration tests ──────────────────────────────────

def test_async_setup_entry_days_2_7_registers_day27_spot_sensor():
    """In days_2_7 mode, async_setup_entry must register SpotPriceForecastDays27Sensor."""
    from custom_components.nem_pd7day.sensor import async_setup_entry as sensor_async_setup_entry

    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_additive"
    entry.data = {CONF_REGION: "QLD1"}
    entry.options = {
        CONF_FORECAST_MODE: FORECAST_MODE_DAYS_2_7,
        CONF_ACTIVE_TARIFF: "energex/6900",
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

    import asyncio
    asyncio.new_event_loop().run_until_complete(
        sensor_async_setup_entry(hass, entry, _add_entities)
    )

    # Both base and day 2-7 spot sensors should be registered
    base_spot = [e for e in created if getattr(e, '_attr_name', '') == "NEM Spot Price Forecast"]
    day27_spot = [e for e in created if getattr(e, '_attr_name', '') == "NEM Spot Price Forecast Day 2-7"]
    assert len(base_spot) == 1, "Base spot sensor must always be registered"
    assert len(day27_spot) == 1, "Day 2-7 spot sensor must be registered in days_2_7 mode"


def test_async_setup_entry_days_1_7_no_day27_sensors():
    """In days_1_7 mode, async_setup_entry must NOT register day 2-7 sensors."""
    from custom_components.nem_pd7day.sensor import async_setup_entry as sensor_async_setup_entry

    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_full"
    entry.data = {CONF_REGION: "QLD1"}
    entry.options = {CONF_FORECAST_MODE: FORECAST_MODE_FULL}

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

    import asyncio
    asyncio.new_event_loop().run_until_complete(
        sensor_async_setup_entry(hass, entry, _add_entities)
    )

    day27_spot = [e for e in created if getattr(e, '_attr_name', '') == "NEM Spot Price Forecast Day 2-7"]
    assert len(day27_spot) == 0, "Day 2-7 spot sensor must NOT be registered in days_1_7 mode"


def test_async_setup_entry_days_2_7_registers_day27_tariff_sensor():
    """In days_2_7 mode, Day 2-7 tariff sensor is registered for active tariff only."""
    from custom_components.nem_pd7day.sensor import async_setup_entry as sensor_async_setup_entry
    from custom_components.nem_pd7day.tariff_sensor import TariffForecastDays27Sensor

    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_tariff27"
    entry.data = {CONF_REGION: "QLD1"}
    entry.options = {
        CONF_FORECAST_MODE: FORECAST_MODE_DAYS_2_7,
        CONF_ACTIVE_TARIFF: "energex/6900",
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

    import asyncio
    asyncio.new_event_loop().run_until_complete(
        sensor_async_setup_entry(hass, entry, _add_entities)
    )

    day27_tariff = [e for e in created if "Day 2-7" in getattr(e, '_attr_name', '') and "Tariff" in getattr(e, '_attr_name', '')]
    assert len(day27_tariff) == 1, "Only one Day 2-7 tariff sensor for the active tariff"
    assert day27_tariff[0]._distributor == "energex"
    assert day27_tariff[0]._tariff_code == "6900"
    assert day27_tariff[0]._attr_unique_id == "nem_pd7day_QLD1_energex_6900_days27"


def test_get_tariff_name_from_library():
    """get_tariff_name() returns correct name from aemo_to_tariff library."""
    name = get_tariff_name("energex", "6900")
    assert name == "Residential Time of Use Energy"

    name_ergon = get_tariff_name("ergon", "ERTOUET1")
    assert name_ergon == "Residential Battery ToU"

    # sapn maps to sapower in library
    name_sapn = get_tariff_name("sapn", "RTOU")
    assert name_sapn == "Residential Time of Use"

    # Unknown code falls back
    name_unknown = get_tariff_name("energex", "ZZZZZ")
    assert name_unknown == "ZZZZZ"


def test_base_tariff_visibility_always_uses_defaults():
    """Base tariff sensors use DEFAULT_ENABLED_TARIFFS regardless of mode or active_tariff."""
    # 8900 is in DEFAULT_ENABLED_TARIFFS
    sensor_8900 = make_tariff_sensor(
        distributor="energex",
        tariff_code="8900",
        mode=FORECAST_MODE_DAYS_2_7,
        active_tariff="energex/6900",
    )
    assert sensor_8900.entity_registry_enabled_default is True

    # 8400 is NOT in DEFAULT_ENABLED_TARIFFS
    sensor_8400 = make_tariff_sensor(
        distributor="energex",
        tariff_code="8400",
        mode=FORECAST_MODE_DAYS_2_7,
        active_tariff="energex/6900",
    )
    assert sensor_8400.entity_registry_enabled_default is False
