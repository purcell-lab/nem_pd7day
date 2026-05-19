"""
Tests for tariff_sensor.py — NemPd7dayTariffSensor.

Verifies:
  - Current tariff value computed correctly (spot_to_tariff output / 100)
  - Forecast attribute has correct structure and interval count
  - Device info matches regional device identifiers
  - Region → distributor mapping correctness
  - Handles missing coordinator data gracefully

Run with:  python -m pytest tests/test_tariff_sensor.py -v
"""
from __future__ import annotations

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
_coord_mod = _load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

_tariff_mod = _load(
    "custom_components.nem_pd7day.tariff_sensor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "tariff_sensor.py"),
)

from custom_components.nem_pd7day.tariff_sensor import NemPd7dayTariffSensor
from custom_components.nem_pd7day.const import (
    DEFAULT_ENABLED_TARIFFS,
    DISTRIBUTOR_TARIFFS,
    DOMAIN,
    REGION_DISTRIBUTORS,
    TARIFF_NAMES,
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


def make_tariff_sensor(
    region="QLD1",
    distributor="energex",
    tariff_code="8400",
    price_periods=None,
) -> NemPd7dayTariffSensor:
    """Construct a NemPd7dayTariffSensor bypassing HA CoordinatorEntity init."""
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

    sensor = NemPd7dayTariffSensor.__new__(NemPd7dayTariffSensor)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._tariff_code = tariff_code
    sensor._entry = entry
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{tariff_code}_tariff"
    tariff_name = TARIFF_NAMES.get(distributor, {}).get(tariff_code, tariff_code)
    sensor._attr_name = f"{distributor.title()} {tariff_code} {tariff_name} Tariff"
    return sensor


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_tariff_sensor_current_value():
    """Verify sensor returns spot_to_tariff output / 100 as $/kWh."""
    now = datetime.now(tz=NEM_TZ)
    current_end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
    period = make_price_period(current_end, value=0.10)  # 0.10 $/kWh
    sensor = make_tariff_sensor(price_periods=[period])

    # spot_to_tariff(dt, distributor, tariff, rrp_mwh) returns c/kWh
    # We mock it to return 15.5 c/kWh → 0.155 $/kWh
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as mock_stt:
        val = sensor.native_value
        assert val is not None
        assert abs(val - 0.155) < 1e-6
        # Verify RRP conversion: 0.10 $/kWh * 1000 = 100 $/MWh
        call_args = mock_stt.call_args
        assert abs(call_args[0][3] - 100.0) < 1e-6  # rrp_mwh


def test_tariff_sensor_forecast_attribute():
    """Verify forecast attribute has correct number of intervals and structure."""
    now = datetime.now(tz=NEM_TZ)
    base = now.replace(minute=0, second=0, microsecond=0)
    periods = [make_price_period(base + timedelta(minutes=30 * i), value=0.05 + i * 0.01) for i in range(5)]
    sensor = make_tariff_sensor(price_periods=periods)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        attrs = sensor.extra_state_attributes
        assert attrs["distributor"] == "Energex"
        assert attrs["network"] == "energex"
        assert attrs["tariff_code"] == "8400"
        assert attrs["region"] == "QLD1"
        assert len(attrs["forecast"]) == 5
        for entry in attrs["forecast"]:
            assert "interval_time" in entry
            assert "tariff_$/kwh" in entry
            assert abs(entry["tariff_$/kwh"] - 0.10) < 1e-6  # 10 c/kWh / 100


def test_tariff_sensor_device_info():
    """Verify tariff sensor uses the same device identifiers as other sensors."""
    sensor = make_tariff_sensor(region="QLD1")
    info = sensor.device_info
    assert ("nem_pd7day", "entry_1_QLD1") in info["identifiers"]


def test_region_distributor_mapping():
    """Verify REGION_DISTRIBUTORS mapping correctness."""
    assert "energex" in REGION_DISTRIBUTORS["QLD1"]
    assert "ergon" in REGION_DISTRIBUTORS["QLD1"]
    assert len(REGION_DISTRIBUTORS["QLD1"]) == 2

    assert "ausgrid" in REGION_DISTRIBUTORS["NSW1"]
    assert "endeavour" in REGION_DISTRIBUTORS["NSW1"]
    assert "essential" in REGION_DISTRIBUTORS["NSW1"]
    assert len(REGION_DISTRIBUTORS["NSW1"]) == 3

    assert "jemena" in REGION_DISTRIBUTORS["VIC1"]
    assert "powercor" in REGION_DISTRIBUTORS["VIC1"]
    assert "united" in REGION_DISTRIBUTORS["VIC1"]
    assert "ausnet" in REGION_DISTRIBUTORS["VIC1"]
    assert "victoria" in REGION_DISTRIBUTORS["VIC1"]
    assert len(REGION_DISTRIBUTORS["VIC1"]) == 5

    assert REGION_DISTRIBUTORS["SA1"] == ["sapn"]
    assert REGION_DISTRIBUTORS["TAS1"] == ["tasnetworks"]


def test_tariff_sensor_handles_missing_data():
    """coordinator.data is None → native_value returns None."""
    sensor = make_tariff_sensor(price_periods=None)
    assert sensor.native_value is None


def test_tariff_sensor_handles_empty_forecast():
    """Empty forecast list → native_value returns None."""
    sensor = make_tariff_sensor(price_periods=[])
    assert sensor.native_value is None


def test_tariff_sensor_handles_spot_to_tariff_exception():
    """spot_to_tariff raises → native_value returns None (no crash)."""
    now = datetime.now(tz=NEM_TZ)
    current_end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
    period = make_price_period(current_end, value=0.10)
    sensor = make_tariff_sensor(price_periods=[period])

    with patch.object(_tariff_mod, "spot_to_tariff", side_effect=ValueError("unknown tariff")):
        val = sensor.native_value
        assert val is None


def test_tariff_sensor_unique_id_format():
    """Verify unique_id includes entry_id, region, distributor, tariff_code."""
    sensor = make_tariff_sensor(region="NSW1", distributor="ausgrid", tariff_code="EA010")
    assert sensor._attr_unique_id == "entry_1_NSW1_ausgrid_EA010_tariff"


def test_distributor_tariff_counts():
    """Verify QLD1 tariff count: energex (17) + ergon (10) = 27."""
    qld_count = sum(
        len(DISTRIBUTOR_TARIFFS[d])
        for d in REGION_DISTRIBUTORS["QLD1"]
    )
    assert qld_count == 27


def test_all_distributors_have_tariffs():
    """Every distributor in REGION_DISTRIBUTORS must have entries in DISTRIBUTOR_TARIFFS."""
    for region, distributors in REGION_DISTRIBUTORS.items():
        for dist in distributors:
            assert dist in DISTRIBUTOR_TARIFFS, f"{dist} from {region} not in DISTRIBUTOR_TARIFFS"
            assert len(DISTRIBUTOR_TARIFFS[dist]) > 0, f"{dist} has empty tariff list"


def test_forecast_attribute_with_none_coordinator_data():
    """extra_state_attributes returns empty forecast list when coordinator.data is None."""
    sensor = make_tariff_sensor(price_periods=None)
    attrs = sensor.extra_state_attributes
    assert attrs["forecast"] == []
    assert attrs["distributor"] == "Energex"
    assert attrs["network"] == "energex"
    assert attrs["tariff_code"] == "8400"
    assert attrs["region"] == "QLD1"


def test_tariff_sensor_names():
    """Verify name format includes human-readable tariff name from TARIFF_NAMES."""
    sensor_6900 = make_tariff_sensor(distributor="energex", tariff_code="6900")
    assert sensor_6900._attr_name == "Energex 6900 Residential Time of Use Energy Tariff"

    sensor_rtou = make_tariff_sensor(distributor="sapn", tariff_code="RTOU")
    assert sensor_rtou._attr_name == "Sapn RTOU Residential Time of Use Tariff"

    # Unknown tariff code falls back to code itself
    sensor_unknown = make_tariff_sensor(distributor="energex", tariff_code="ZZZZ")
    assert sensor_unknown._attr_name == "Energex ZZZZ ZZZZ Tariff"


def test_default_enabled_tariffs():
    """Verify 6900/energex is default-enabled, 8400/energex is default-disabled."""
    sensor_enabled = make_tariff_sensor(distributor="energex", tariff_code="6900")
    assert sensor_enabled.entity_registry_enabled_default is True

    sensor_disabled = make_tariff_sensor(distributor="energex", tariff_code="8400")
    assert sensor_disabled.entity_registry_enabled_default is False


def test_all_non_default_disabled():
    """Spot-check several non-default tariffs return False for entity_registry_enabled_default."""
    non_default_cases = [
        ("energex", "8400"),     # Residential Flat
        ("ergon", "3900"),       # Residential Transitional Demand
        ("ausgrid", "EA010"),    # Residential Flat
        ("endeavour", "N70"),    # Residential Flat
        ("essential", "BLNN2AU"), # Residential Anytime
        ("sapn", "RSR"),         # Residential Single Rate
        ("tasnetworks", "TAS87"), # Residential ToU Demand
    ]
    for distributor, tariff_code in non_default_cases:
        sensor = make_tariff_sensor(distributor=distributor, tariff_code=tariff_code)
        assert sensor.entity_registry_enabled_default is False, (
            f"{distributor}/{tariff_code} should be default-disabled"
        )


def test_tariff_periods_in_attributes():
    """Verify tariff_periods is a list with correct keys and rate conversion."""
    import datetime as _dt

    fake_periods = [
        ("Peak", _dt.time(14, 0), _dt.time(20, 0), 25.0),
        ("OffPeak", _dt.time(20, 0), _dt.time(14, 0), 5.0),
    ]
    now = datetime.now(tz=NEM_TZ)
    base = now.replace(minute=0, second=0, microsecond=0)
    periods = [make_price_period(base + timedelta(minutes=30), value=0.05)]
    sensor = make_tariff_sensor(price_periods=periods)

    with patch.object(_tariff_mod, "get_periods", return_value=fake_periods):
        with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
            attrs = sensor.extra_state_attributes
            tp = attrs["tariff_periods"]
            assert isinstance(tp, list)
            assert len(tp) == 2
            for entry in tp:
                assert "period" in entry
                assert "start" in entry
                assert "end" in entry
                assert "network_rate_$/kwh" in entry
            # Check rate conversion: 25.0 c/kWh → 0.25 $/kWh
            assert abs(tp[0]["network_rate_$/kwh"] - 0.25) < 1e-6
            assert abs(tp[1]["network_rate_$/kwh"] - 0.05) < 1e-6


def test_loss_factors_in_attributes():
    """Verify dlf/mlf/combined present and combined = dlf * mlf * market."""
    sensor = make_tariff_sensor(price_periods=None)
    with patch.object(_tariff_mod, "get_periods", return_value=[]):
        with patch.object(_tariff_mod, "get_daily_fee", return_value=None):
            attrs = sensor.extra_state_attributes
            dlf = attrs["distribution_loss_factor_dlf"]
            mlf = attrs["metering_loss_factor_mlf"]
            market = attrs["market_loss_factor"]
            combined = attrs["combined_loss_multiplier"]
            assert isinstance(dlf, float)
            assert isinstance(mlf, float)
            assert isinstance(combined, float)
            assert abs(combined - round(dlf * mlf * market, 6)) < 1e-9


def test_forecast_description_in_attributes():
    """Verify description mentions 'forecast' and 'DLF'."""
    sensor = make_tariff_sensor(price_periods=None)
    with patch.object(_tariff_mod, "get_periods", return_value=[]):
        with patch.object(_tariff_mod, "get_daily_fee", return_value=None):
            attrs = sensor.extra_state_attributes
            desc = attrs["forecast_description"]
            assert isinstance(desc, str)
            assert "forecast" in desc.lower()
            assert "DLF" in desc
            assert "MLF" in desc
            assert "Energex" in desc


def test_daily_supply_charge_in_attributes():
    """Verify daily_supply_charge_$ is a float or None."""
    sensor = make_tariff_sensor(price_periods=None)

    # With a valid daily fee
    with patch.object(_tariff_mod, "get_periods", return_value=[]):
        with patch.object(_tariff_mod, "get_daily_fee", return_value=0.556):
            attrs = sensor.extra_state_attributes
            charge = attrs["daily_supply_charge_$"]
            assert isinstance(charge, float)
            assert abs(charge - 0.556) < 1e-6

    # With exception → None
    with patch.object(_tariff_mod, "get_periods", return_value=[]):
        with patch.object(_tariff_mod, "get_daily_fee", side_effect=ValueError("nope")):
            attrs = sensor.extra_state_attributes
            assert attrs["daily_supply_charge_$"] is None
