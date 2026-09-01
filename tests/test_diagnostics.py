"""
Tests for diagnostics.py — async_get_config_entry_diagnostics().

Verifies the diagnostics download returns a dict with the expected top-level
keys and that the region key matches the config entry.

Pure Python — HA modules are stubbed, no Home Assistant install required.

Run with:  python -m pytest tests/test_diagnostics.py -v
"""
from __future__ import annotations

import asyncio
import enum as _enum
import importlib.util
import os
import sys
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Stub HA + aiohttp so the integration modules import cleanly ───────────────
# conftest.py's autouse fixture imports custom_components.nem_pd7day.sensor, so
# register the same broad set of HA stubs other tests rely on, including real
# (non-MagicMock) base classes for the sensor/coordinator chain to avoid
# metaclass conflicts.
sys.modules.setdefault("aiohttp", MagicMock())
for ha_mod in [
    "homeassistant", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.storage", "homeassistant.helpers.event",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.device_registry",
    "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.util", "homeassistant.util.dt",
    "homeassistant.components", "homeassistant.components.sensor",
]:
    sys.modules.setdefault(ha_mod, MagicMock())

_device_registry_mock = MagicMock()
_device_registry_mock.DeviceInfo = dict
sys.modules["homeassistant.helpers.device_registry"] = _device_registry_mock


# ── homeassistant.loader stub ─────────────────────────────────────────────────
# diagnostics.py reads the integration version via async_get_integration() so
# that it does not touch the filesystem on the event loop. The stub records how
# many times it was awaited so a test can assert no disk read replaces it.
MANIFEST_VERSION = "9.9.9-test"


class _FakeIntegration:
    def __init__(self, version: str) -> None:
        self.manifest = {"version": version, "domain": "nem_pd7day"}


async def _fake_async_get_integration(hass, domain):
    _loader_mock.calls.append(domain)
    return _FakeIntegration(MANIFEST_VERSION)


_loader_mock = MagicMock()
_loader_mock.calls = []
_loader_mock.async_get_integration = _fake_async_get_integration
sys.modules["homeassistant.loader"] = _loader_mock


class _SensorDeviceClass(str, _enum.Enum):
    MONETARY = "monetary"
    ENERGY = "energy"
    TIMESTAMP = "timestamp"


class _SensorStateClass(str, _enum.Enum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


_sensor_mock = MagicMock()
_sensor_mock.SensorDeviceClass = _SensorDeviceClass
_sensor_mock.SensorStateClass = _SensorStateClass
_sensor_mock.SensorEntity = object
sys.modules["homeassistant.components.sensor"] = _sensor_mock


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

# Load const (pure python) then diagnostics.
_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_diag_mod = _load(
    "custom_components.nem_pd7day.diagnostics",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "diagnostics.py"),
)

from custom_components.nem_pd7day.const import (  # noqa: E402
    CONF_REGION,
    COORDINATOR_KEY,
    DOMAIN,
    STORE_KEY,
)
from custom_components.nem_pd7day.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)


def _make_hass_and_entry(region: str = "NSW1"):
    """Build a mock hass + config entry wired with coordinator/store/stpasa."""
    entry = MagicMock()
    entry.entry_id = "entry_abc"
    entry.data = {CONF_REGION: region}
    entry.options = {CONF_REGION: region}

    # Calibration store with a summary.
    store = MagicMock()
    store.summary_attributes.return_value = {
        "status": "active",
        "fitted_at": "2026-06-01T08:00:00+10:00",
        "observation_count": 42,
        "active_buckets": 3,
    }

    # STPASA store with a fresh latest() result.
    stpasa_latest = MagicMock()
    stpasa_latest.run_datetime = "2026-06-12T13:30:00+10:00"
    stpasa_store = MagicMock()
    stpasa_store.latest.return_value = stpasa_latest

    # Coordinator with PD7DAY data for the region.
    price_data = MagicMock()
    price_data.forecast_generated_at = "2026-06-12T13:00:00+10:00"
    result = MagicMock()
    result.prices = {region: price_data}
    coordinator = MagicMock()
    coordinator.data = result

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_KEY: coordinator,
                STORE_KEY: store,
                "stpasa_store": stpasa_store,
            }
        }
    }
    return hass, entry


def test_diagnostics_returns_dict():
    """Result is a dict containing all expected top-level keys."""
    hass, entry = _make_hass_and_entry()
    result = run_async(async_get_config_entry_diagnostics(hass, entry))

    assert isinstance(result, dict)
    expected_keys = {
        "entry_data",
        "region",
        "calibration_summary",
        "stpasa_run_datetime",
        "pd7day_run_datetime",
        "integration_version",
    }
    assert expected_keys <= set(result.keys())

    # Spot-check the wired values flow through.
    assert result["calibration_summary"]["observation_count"] == 42
    assert result["stpasa_run_datetime"] == "2026-06-12T13:30:00+10:00"
    assert result["pd7day_run_datetime"] == "2026-06-12T13:00:00+10:00"
    assert result["integration_version"] == MANIFEST_VERSION


def test_integration_version_uses_loader_not_disk():
    """
    The version comes from the loader's cached manifest, not a disk read.

    async_get_config_entry_diagnostics is a coroutine, so the previous
    manifest_path.read_text() ran on the event loop and Home Assistant flagged
    it as a blocking call from a custom integration. Guard the fix by asserting
    the loader is consulted and that no filesystem read is attempted.
    """
    _loader_mock.calls.clear()
    hass, entry = _make_hass_and_entry()

    real_open = open
    opened: list[str] = []

    def _tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    import builtins
    import pathlib

    original_read_text = pathlib.Path.read_text

    def _forbidden_read_text(self, *args, **kwargs):
        raise AssertionError(f"diagnostics read {self} from disk on the event loop")

    builtins.open = _tracking_open
    pathlib.Path.read_text = _forbidden_read_text
    try:
        result = run_async(async_get_config_entry_diagnostics(hass, entry))
    finally:
        builtins.open = real_open
        pathlib.Path.read_text = original_read_text

    assert result["integration_version"] == MANIFEST_VERSION
    assert _loader_mock.calls == [DOMAIN]
    assert not [p for p in opened if p.endswith("manifest.json")]


def test_integration_version_none_when_loader_fails():
    """A loader failure degrades to None rather than breaking the download."""
    hass, entry = _make_hass_and_entry(region="QLD1")

    async def _boom(hass_arg, domain):
        raise RuntimeError("integration not found")

    original = _loader_mock.async_get_integration
    _diag_mod.async_get_integration = _boom
    try:
        result = run_async(async_get_config_entry_diagnostics(hass, entry))
    finally:
        _diag_mod.async_get_integration = original

    assert result["integration_version"] is None
    # The rest of the payload still comes through.
    assert result["region"] == "QLD1"


def test_diagnostics_region_key():
    """The region key matches the config entry region."""
    hass, entry = _make_hass_and_entry(region="VIC1")
    result = run_async(async_get_config_entry_diagnostics(hass, entry))
    assert result["region"] == "VIC1"
    assert result["entry_data"][CONF_REGION] == "VIC1"
