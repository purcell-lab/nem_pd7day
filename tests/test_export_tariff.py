"""
Tests for export tariff sensors and Day 2-7 entity_category.

Covers:
  1. Export tariff sensor produces different value from import tariff sensor
     for Ausgrid EA025/EA029 at a peak-hour interval (16:00-21:00)
  2. Export tariff sensor entity_id follows _export_tariff suffix pattern
  3. Day 2-7 sensors have entity_category == EntityCategory.DIAGNOSTIC

Run with:  python -m pytest tests/test_export_tariff.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
import types
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

# Provide EntityCategory so tariff_sensor.py can import it
ec = MagicMock()
ec.DIAGNOSTIC = "diagnostic"
sys.modules["homeassistant.const"].EntityCategory = ec


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

_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

_sensor_mod = _load(
    "custom_components.nem_pd7day.sensor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "sensor.py"),
)

from custom_components.nem_pd7day.tariff_sensor import (
    NemPd7dayExportTariffSensor,
    NemPd7dayTariffSensor,
    TariffForecastDays27Sensor,
    get_tariff_name,
)
from custom_components.nem_pd7day.sensor import SpotPriceForecastDays27Sensor
from custom_components.nem_pd7day.const import (
    CONF_ACTIVE_TARIFF,
    CONF_FORECAST_MODE,
    CONF_REGION,
    COORDINATOR_KEY,
    DEFAULT_ENABLED_TARIFFS,
    DISTRIBUTOR_DISPLAY_NAMES,
    DOMAIN,
    EXPORT_TARIFF_PROGRAMS,
    FORECAST_MODE_DAYS_2_7,
    STORE_KEY,
)

NEM_TZ = timezone(timedelta(hours=10))


# ── Helpers ───────────────────────────────────────────────────────────────────

def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def make_price_period(nemtime_dt: datetime, value: float = 0.10):
    start_dt = nemtime_dt - timedelta(minutes=30)
    return MagicMock(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),
        value=value,
    )


def make_export_sensor(
    region="NSW1",
    distributor="ausgrid",
    import_code="EA025",
    export_code="EA029",
    price_periods=None,
) -> NemPd7dayExportTariffSensor:
    """Construct a NemPd7dayExportTariffSensor bypassing HA CoordinatorEntity init."""
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
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=None, dispatch=None
    )

    sensor = NemPd7dayExportTariffSensor.__new__(NemPd7dayExportTariffSensor)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._import_code = import_code
    sensor._export_code = export_code
    sensor._entry = entry
    sensor._store = None
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{import_code}_export_tariff"
    distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
    from custom_components.nem_pd7day.tariff_sensor import get_export_tariff_name
    export_name = get_export_tariff_name(distributor, export_code)
    if "Export" in export_name:
        sensor._attr_name = f"{distributor_display} {export_name} Tariff ({export_code})"
    else:
        sensor._attr_name = f"{distributor_display} {export_name} Export Tariff ({export_code})"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    sensor.hass.states.get.return_value = None
    return sensor


def make_import_sensor(
    region="NSW1",
    distributor="ausgrid",
    tariff_code="EA025",
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
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=None, dispatch=None
    )

    sensor = NemPd7dayTariffSensor.__new__(NemPd7dayTariffSensor)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._tariff_code = tariff_code
    sensor._entry = entry
    sensor._store = None
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{tariff_code}_tariff"
    distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
    tariff_name = get_tariff_name(distributor, tariff_code)
    sensor._attr_name = f"{distributor_display} {tariff_name} Tariff ({tariff_code})"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    sensor.hass.states.get.return_value = None
    return sensor


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_export_tariff_different_from_import_at_peak():
    """Export tariff (EA029) produces different value from import (EA025) at peak hour."""
    # Create a period at 18:00 NEM time (peak hour, 16:00-21:00 window)
    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.10)  # 0.10 $/kWh = 100 $/MWh

    import_sensor = make_import_sensor(price_periods=[period])
    export_sensor = make_export_sensor(price_periods=[period])

    # Import uses spot_to_tariff, returns higher rate at peak
    import_rate_c = 16.92  # approximate c/kWh for Ausgrid EA025 at peak
    # Export uses spot_to_feed_in_tariff, returns lower rate
    export_rate_c = 14.77  # approximate c/kWh for Ausgrid EA029 at peak

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=import_rate_c):
        import_val = import_sensor.native_value

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=export_rate_c):
        export_val = export_sensor.native_value

    assert import_val is not None
    assert export_val is not None

    # Values should differ
    import_expected = round((import_rate_c / 100 + 0.0293) * 1.1, 6)
    # Export returns raw feed-in tariff: no additional fee, no GST
    export_expected = round(export_rate_c / 100, 6)
    assert abs(import_val - import_expected) < 1e-6
    assert abs(export_val - export_expected) < 1e-6

    # Export should be lower than import at peak
    assert export_val < import_val, (
        f"Export ({export_val}) should be less than import ({import_val}) at peak"
    )
    # Difference should be notable (import has fee+GST, export is raw)
    diff = import_val - export_val
    assert diff > 0.01, f"Difference {diff} $/kWh too small"


def test_export_tariff_entity_id_suffix():
    """Export tariff sensor unique_id follows _export_tariff suffix pattern."""
    sensor = make_export_sensor(
        region="NSW1",
        distributor="ausgrid",
        import_code="EA025",
        export_code="EA029",
    )
    assert sensor._attr_unique_id == "entry_1_NSW1_ausgrid_EA025_export_tariff"
    assert "_export_tariff" in sensor._attr_unique_id


def test_export_tariff_entity_id_all_programs():
    """Verify entity_id pattern for all export programs."""
    expected = {
        ("ausgrid", "EA025", "EA029"): "entry_1_NSW1_ausgrid_EA025_export_tariff",
        ("endeavour", "N71", "N61"): "entry_1_NSW1_endeavour_N71_export_tariff",
        ("essential", "BLNT3AL", "BLNREX2"): "entry_1_NSW1_essential_BLNT3AL_export_tariff",
        ("sapn", "RESELE", "RESELE"): "entry_1_SA1_sapn_RESELE_export_tariff",
    }
    for (dist, imp, exp), expected_uid in expected.items():
        region = "SA1" if dist == "sapn" else "NSW1"
        sensor = make_export_sensor(
            region=region,
            distributor=dist,
            import_code=imp,
            export_code=exp,
        )
        assert sensor._attr_unique_id == expected_uid, (
            f"{dist}/{imp}: expected {expected_uid}, got {sensor._attr_unique_id}"
        )


def test_export_tariff_friendly_names():
    """Verify friendly names match spec — no double 'Export' for BLNREX2."""
    # Ausgrid EA029
    sensor_ausgrid = make_export_sensor(distributor="ausgrid", import_code="EA025", export_code="EA029")
    assert sensor_ausgrid._attr_name == "Ausgrid Residential Electrify Export Tariff (EA029)"

    # Endeavour N61
    sensor_endeavour = make_export_sensor(distributor="endeavour", import_code="N71", export_code="N61")
    assert sensor_endeavour._attr_name == "Endeavour Energy Residential Electrify Export Tariff (N61)"

    # Essential BLNREX2 — name already contains "Export", so no double
    sensor_essential = make_export_sensor(distributor="essential", import_code="BLNT3AL", export_code="BLNREX2")
    # Should be "...Solar Export Tariff (BLNREX2)" not "...Solar Export Export Tariff (BLNREX2)"
    assert "Export Export" not in sensor_essential._attr_name
    assert sensor_essential._attr_name == "Essential Energy LV Residential Solar Export Tariff (BLNREX2)"

    # SAPN RESELE
    sensor_sapn = make_export_sensor(region="SA1", distributor="sapn", import_code="RESELE", export_code="RESELE")
    assert sensor_sapn._attr_name == "SA Power Networks Residential Electrify Export Tariff (RESELE)"


def test_day27_spot_sensor_has_diagnostic_entity_category():
    """SpotPriceForecastDays27Sensor must have entity_category == DIAGNOSTIC."""
    assert hasattr(SpotPriceForecastDays27Sensor, "_attr_entity_category")
    assert SpotPriceForecastDays27Sensor._attr_entity_category == ec.DIAGNOSTIC


def test_day27_tariff_sensor_has_diagnostic_entity_category():
    """TariffForecastDays27Sensor must have entity_category == DIAGNOSTIC."""
    assert hasattr(TariffForecastDays27Sensor, "_attr_entity_category")
    assert TariffForecastDays27Sensor._attr_entity_category == ec.DIAGNOSTIC


def test_export_programs_registered_in_setup():
    """async_setup_entry registers export tariff sensors for the region."""
    import asyncio
    from custom_components.nem_pd7day.sensor import async_setup_entry as sensor_async_setup_entry

    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_export"
    entry.data = {CONF_REGION: "NSW1"}
    entry.options = {
        CONF_FORECAST_MODE: FORECAST_MODE_DAYS_2_7,
        CONF_ACTIVE_TARIFF: "ausgrid/EA025",
    }

    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=MagicMock(), dispatch=None
    )

    hass = MagicMock()
    hass.data = {DOMAIN: {}}

    created = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    asyncio.new_event_loop().run_until_complete(
        sensor_async_setup_entry(hass, entry, _add_entities)
    )

    # Find export sensors — they have _export_code attribute
    export_sensors = [e for e in created if hasattr(e, "_export_code")]
    # NSW1 has 4 export programs: ausgrid/EA025→EA029, endeavour/N71→N61, essential/BLNT3AL→BLNREX2, evoenergy/026→026
    assert len(export_sensors) == 4, (
        f"Expected 4 export sensors for NSW1, got {len(export_sensors)}"
    )
    export_codes = {s._export_code for s in export_sensors}
    assert export_codes == {"EA029", "N61", "BLNREX2", "026"}


def test_sapn_resele_import_sensor_default_enabled():
    """SAPN RESELE should be in DEFAULT_ENABLED_TARIFFS."""
    assert ("sapn", "RESELE") in DEFAULT_ENABLED_TARIFFS


def test_export_tariff_uses_feed_in_function():
    """Export sensor calls spot_to_feed_in_tariff (not spot_to_tariff)."""
    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.10)
    sensor = make_export_sensor(price_periods=[period])

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        val = sensor.native_value
        assert val is not None
        mock_fit.assert_called_once()
        # Verify RRP conversion: 0.10 $/kWh * 1000 = 100 $/MWh
        call_args = mock_fit.call_args
        assert abs(call_args[0][3] - 100.0) < 1e-6


def test_export_tariff_no_fee_or_gst():
    """Export sensor returns raw feed-in tariff without additional fee or GST."""
    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.10)
    sensor = make_export_sensor(price_periods=[period])

    feed_in_rate_c = 14.77  # c/kWh returned by spot_to_feed_in_tariff

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=feed_in_rate_c):
        val = sensor._compute_export_tariff(period)

    # Should be exactly the raw conversion: c/kWh -> $/kWh, no fee, no GST
    expected_raw = round(feed_in_rate_c / 100, 6)
    assert val == expected_raw, (
        f"Export tariff should be raw {expected_raw}, got {val}"
    )

    # Verify it does NOT match the old fee+GST formula
    old_formula = round((feed_in_rate_c / 100 + 0.0293) * 1.1, 6)
    assert val != old_formula, (
        f"Export tariff should NOT include fee+GST ({old_formula})"
    )


def test_export_tariff_stdout_suppressed():
    """Export sensor suppresses stdout from aemo_to_tariff library."""
    import io

    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.10)
    sensor = make_export_sensor(price_periods=[period])

    def noisy_feed_in(*args, **kwargs):
        print("DEBUG: sapower feed_in_tariff lookup")
        return 14.77

    captured = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = captured
    try:
        with patch.object(_tariff_mod, "spot_to_feed_in_tariff", side_effect=noisy_feed_in):
            val = sensor.native_value
            assert val is not None
    finally:
        sys.stdout = real_stdout

    assert captured.getvalue() == "", (
        f"Expected no stdout but got: {captured.getvalue()!r}"
    )


# ── Export tariff calibration tests ──────────────────────────────────────────


def test_export_tariff_uses_calibrated_price():
    """Export tariff passes calibrated $/MWh (not raw) to spot_to_feed_in_tariff."""
    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.01745)  # raw $/kWh
    sensor = make_export_sensor(price_periods=[period])

    # Attach calibration store
    mock_store = MagicMock()
    mock_store.apply_to_price.return_value = {
        "calibrated": 0.01425,
        "p10": None, "p50": None, "p90": None,
        "ols_mae": None, "calibrated_source": "isotonic",
        "n_obs": 100,
    }
    sensor._store = mock_store
    sensor.coordinator.data.prices["NSW1"].forecast_generated_at = nem_iso(
        peak_nemtime - timedelta(hours=6)
    )

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        val = sensor.native_value
        assert val is not None
        # Verify calibrated price: 0.01425 * 1000 = 14.25 $/MWh
        call_args = mock_fit.call_args
        assert abs(call_args[0][3] - 14.25) < 1e-6, (
            f"Expected calibrated RRP 14.25 $/MWh, got {call_args[0][3]}"
        )


def test_export_tariff_forecast_spot_shows_calibrated():
    """Export forecast 'spot' attribute uses calibrated value."""
    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.01745)
    sensor = make_export_sensor(price_periods=[period])

    mock_store = MagicMock()
    mock_store.apply_to_price.return_value = {
        "calibrated": 0.01425,
        "p10": None, "p50": None, "p90": None,
        "ols_mae": None, "calibrated_source": "isotonic",
        "n_obs": 100,
    }
    sensor._store = mock_store
    sensor.coordinator.data.prices["NSW1"].forecast_generated_at = nem_iso(
        peak_nemtime - timedelta(hours=6)
    )

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=10.0):
        attrs = sensor.extra_state_attributes
        for entry in attrs["forecast"]:
            assert abs(entry["spot"] - 0.01425) < 1e-6, (
                f"Export forecast spot should be calibrated 0.01425, got {entry['spot']}"
            )
            # New per-interval fields
            assert "spot_raw" in entry
            assert "period" in entry
            assert "network_rate" in entry
            # spot_raw is the uncalibrated input value, not the calibrated one
            assert abs(entry["spot_raw"] - round(0.01745, 6)) < 1e-6


def test_export_tariff_no_store_uses_raw():
    """Without calibration store, export tariff falls back to raw value."""
    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.10)
    sensor = make_export_sensor(price_periods=[period])
    assert sensor._store is None

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        val = sensor.native_value
        assert val is not None
        # Raw value: 0.10 * 1000 = 100 $/MWh
        call_args = mock_fit.call_args
        assert abs(call_args[0][3] - 100.0) < 1e-6


# ── Export tariff cache tests ──────────────────────────────────────────────


def test_compute_export_tariff_cache_hit():
    """Calling _compute_export_tariff twice with same period calls library only once."""
    peak_nemtime = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)
    period = make_price_period(peak_nemtime, value=0.10)
    sensor = make_export_sensor(price_periods=[period])
    sensor._period_export_tariff_cache = None

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        result1 = sensor._compute_export_tariff(period)
        result2 = sensor._compute_export_tariff(period)
        assert result1 is not None
        assert result1 == result2
        assert mock_fit.call_count == 1, (
            f"Expected spot_to_feed_in_tariff called once (cache hit), got {mock_fit.call_count}"
        )


def test_apply_export_tariff_to_spot_cache_hit():
    """Calling _apply_export_tariff_to_spot twice with same inputs calls library only once."""
    now = datetime(2026, 5, 24, 14, 12, 0, tzinfo=NEM_TZ)
    sensor = make_export_sensor(price_periods=[])
    sensor._export_tariff_cache = None

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        result1 = sensor._apply_export_tariff_to_spot(0.10, now)
        result2 = sensor._apply_export_tariff_to_spot(0.10, now)
        assert result1 is not None
        assert result1 == result2
        assert mock_fit.call_count == 1, (
            f"Expected spot_to_feed_in_tariff called once (cache hit), got {mock_fit.call_count}"
        )
