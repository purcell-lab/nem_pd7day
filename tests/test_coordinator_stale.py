"""
Tests for stale-data fallback in PD7DayCoordinator and DispatchCoordinator.

When NEMWEB returns a transient HTTP error (e.g. 403), coordinators should
return previously-fetched stale data instead of raising UpdateFailed (which
marks all sensors unavailable).  Only on the very first fetch (no stale data)
should UpdateFailed be raised.

Run with:  python -m pytest tests/test_coordinator_stale.py -v
"""
from __future__ import annotations

import sys
import os
import asyncio
import importlib
import importlib.util
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

# Get the REAL aiohttp module regardless of whether sys.modules was stubbed.
# Other test files (test_coordinator.py) may have replaced sys.modules["aiohttp"]
# with a MagicMock before this file is collected.
_saved = sys.modules.pop("aiohttp", None)
import aiohttp as _real_aiohttp  # noqa: E402
if _saved is not None:
    sys.modules["aiohttp"] = _saved
else:
    # Restore the freshly-imported real module
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Bootstrap stubs (same pattern as test_coordinator.py) ────────────────────

# Stub aiohttp — but keep the real ClientResponseError and ClientSession
_aiohttp_stub = MagicMock()
_aiohttp_stub.ClientResponseError = _real_aiohttp.ClientResponseError
_aiohttp_stub.ClientSession = _real_aiohttp.ClientSession
sys.modules["aiohttp"] = _aiohttp_stub

for ha_mod in [
    "homeassistant", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.storage", "homeassistant.helpers.event",
    "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform", "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.util", "homeassistant.util.dt",
    "homeassistant.components", "homeassistant.components.sensor",
]:
    sys.modules.setdefault(ha_mod, MagicMock())


class _UpdateFailed(Exception):
    pass


class _FakeCoordinator:
    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.last_update_success = True
        self.data = None

    def __class_getitem__(cls, item):
        return cls

    async def async_config_entry_first_refresh(self):
        pass


uc_mock = MagicMock()
uc_mock.DataUpdateCoordinator = _FakeCoordinator
uc_mock.UpdateFailed = _UpdateFailed
sys.modules["homeassistant.helpers.update_coordinator"] = uc_mock

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

# Load dependent modules
_load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)
_load(
    "custom_components.nem_pd7day.calibration_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"),
)
_load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)
_load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_load(
    "custom_components.nem_pd7day.market_notice_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "market_notice_client.py"),
)
_load(
    "custom_components.nem_pd7day.notice_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "notice_store.py"),
)

# Force-reload coordinator so it picks up our aiohttp stub with real ClientResponseError
_load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

from custom_components.nem_pd7day.coordinator import (
    PD7DayCoordinator,
    DispatchCoordinator,
)

import pytest


NEM_TZ = timezone(timedelta(hours=10))


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_pd7day_coordinator() -> PD7DayCoordinator:
    hass = MagicMock()
    coord = PD7DayCoordinator.__new__(PD7DayCoordinator)
    coord.hass = hass
    coord.logger = MagicMock()
    coord.name = "nem_pd7day"
    coord.update_interval = None
    coord.last_update_success = True
    coord.data = None
    coord._regions = ["QLD1"]
    coord._interconnector_ids = {"NSW1-QLD1"}
    coord._store = None
    coord._session = None
    coord.notice_store = None
    coord._notice_client = None
    coord._first_refresh_done = False
    return coord


def _make_dispatch_coordinator() -> DispatchCoordinator:
    hass = MagicMock()
    coord = DispatchCoordinator.__new__(DispatchCoordinator)
    coord.hass = hass
    coord.logger = MagicMock()
    coord.name = "NEM Dispatch QLD1"
    coord.update_interval = timedelta(minutes=5)
    coord.last_update_success = True
    coord.data = None
    coord.region = "QLD1"
    coord.prices = {}
    coord.last_updated = None
    return coord


def _make_client_response_error(status=403, message="Forbidden"):
    """Create a ClientResponseError using the real aiohttp class."""
    return _real_aiohttp.ClientResponseError(
        request_info=MagicMock(),
        history=(),
        status=status,
        message=message,
    )


# ── PD7DayCoordinator stale-data tests ──────────────────────────────────────


def test_pd7day_coordinator_returns_stale_on_http_error():
    """
    When fetch_all raises ClientResponseError (e.g. 403) and stale data exists,
    the coordinator must return stale data instead of raising UpdateFailed.
    """
    coord = _make_pd7day_coordinator()
    stale = MagicMock(name="stale_pd7day_result")
    coord.data = stale

    exc = _make_client_response_error(status=403, message="Forbidden")

    # Patch _get_client to return a client whose fetch_all raises
    mock_client = MagicMock()
    mock_client.fetch_all = AsyncMock(side_effect=exc)
    coord._get_client = lambda: mock_client

    result = run_async(coord._async_update_data())

    assert result is stale, (
        "Coordinator must return stale data on HTTP error when stale data exists"
    )


def test_pd7day_coordinator_raises_on_http_error_no_stale():
    """
    When fetch_all raises ClientResponseError and there is no stale data
    (first fetch), the coordinator must raise UpdateFailed.
    """
    coord = _make_pd7day_coordinator()
    coord.data = None

    exc = _make_client_response_error(status=403, message="Forbidden")

    mock_client = MagicMock()
    mock_client.fetch_all = AsyncMock(side_effect=exc)
    coord._get_client = lambda: mock_client

    with pytest.raises(_UpdateFailed):
        run_async(coord._async_update_data())


# ── DispatchCoordinator stale-data tests ─────────────────────────────────────


def test_dispatch_coordinator_returns_stale_on_error():
    """
    When fetch_dispatch_prices raises an exception and stale data exists,
    the DispatchCoordinator must return stale data.
    """
    coord = _make_dispatch_coordinator()
    stale = MagicMock(name="stale_dispatch_prices")
    coord.data = stale

    coord.hass.async_add_executor_job = AsyncMock(
        side_effect=Exception("timeout"),
    )

    result = run_async(coord._async_update_data())

    assert result is stale, (
        "DispatchCoordinator must return stale data on error when stale data exists"
    )
