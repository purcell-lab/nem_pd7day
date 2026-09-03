"""
Tests for number.py — AdditionalFeeNumber (RestoreNumber).

Verifies:
  - Default value is DEFAULT_ADDITIONAL_FEE (0.0293)
  - Restores previously saved value on startup

Run with:  python -m pytest tests/test_number.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
from unittest.mock import MagicMock, AsyncMock

# ── Module loader ─────────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Stub HA modules before loading any integration module
sys.modules.setdefault("aiohttp", MagicMock())
for ha_mod in [
    "homeassistant", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.storage", "homeassistant.helpers.event",
    "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform", "homeassistant.helpers.device_registry",
    "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.util", "homeassistant.util.dt",
    "homeassistant.components", "homeassistant.components.sensor",
    "homeassistant.components.number",
]:
    sys.modules.setdefault(ha_mod, MagicMock())

device_registry_mock = MagicMock()
device_registry_mock.DeviceInfo = dict
sys.modules["homeassistant.helpers.device_registry"] = device_registry_mock

import enum


class _NumberMode(str, enum.Enum):
    AUTO = "auto"
    BOX = "box"
    SLIDER = "slider"


class _NumberEntity:
    pass


class _RestoreNumber(_NumberEntity):
    """Minimal stub of RestoreNumber for testing."""
    _attr_native_value = None

    async def async_added_to_hass(self) -> None:
        pass

    async def async_get_last_number_data(self):
        return None

    def async_write_ha_state(self):
        pass


number_mock = sys.modules["homeassistant.components.number"]
number_mock.NumberEntity = _NumberEntity
number_mock.NumberMode = _NumberMode
number_mock.RestoreNumber = _RestoreNumber

_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)

_number_mod = _load(
    "custom_components.nem_pd7day.number",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "number.py"),
)

from custom_components.nem_pd7day.number import AdditionalFeeNumber
from custom_components.nem_pd7day.const import DEFAULT_ADDITIONAL_FEE, DOMAIN

import pytest


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_additional_fee_default_value():
    """Verify initial value is DEFAULT_ADDITIONAL_FEE (0.0293)."""
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entity = AdditionalFeeNumber(entry, "QLD1")

    assert entity._attr_native_value == DEFAULT_ADDITIONAL_FEE
    assert abs(entity._attr_native_value - 0.0293) < 1e-9
    assert entity._attr_unique_id == "nem_pd7day_QLD1_additional_usage_fee"
    assert entity._attr_name == "Additional Usage Fees"
    assert entity._attr_native_unit_of_measurement == "$/kWh"
    assert entity._attr_native_min_value == 0.0
    assert entity._attr_native_max_value == 1.0
    assert entity._attr_native_step == 0.0001

    # Verify device_info ties to the regional device
    info = entity.device_info
    assert (DOMAIN, "entry_1_QLD1") in info["identifiers"]


@pytest.mark.asyncio
async def test_additional_fee_restore():
    """Verify async_added_to_hass restores a previously saved value."""
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entity = AdditionalFeeNumber(entry, "NSW1")

    # Before restore — should have default
    assert entity._attr_native_value == DEFAULT_ADDITIONAL_FEE

    # Simulate restore data
    last_data = MagicMock()
    last_data.native_value = 0.0500

    entity.async_get_last_number_data = AsyncMock(return_value=last_data)

    await entity.async_added_to_hass()

    assert abs(entity._attr_native_value - 0.0500) < 1e-9


@pytest.mark.asyncio
async def test_additional_fee_restore_none():
    """Verify async_added_to_hass keeps default when no prior data exists."""
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entity = AdditionalFeeNumber(entry, "SA1")

    entity.async_get_last_number_data = AsyncMock(return_value=None)

    await entity.async_added_to_hass()

    assert entity._attr_native_value == DEFAULT_ADDITIONAL_FEE
