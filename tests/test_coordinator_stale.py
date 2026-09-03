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
import importlib.util
from datetime import timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Fake aiohttp module (no real aiohttp dependency) ─────────────────────────
# Coordinator.py does `except aiohttp.ClientResponseError as exc:` and reads
# exc.status / exc.message, so our fake must be a real exception class with
# those attributes.


class _FakeClientResponseError(Exception):
    """Minimal stand-in for aiohttp.ClientResponseError."""

    def __init__(self, request_info=None, history=(), *, status=0, message=""):
        self.request_info = request_info
        self.history = history
        self.status = status
        self.message = message
        super().__init__(f"{status}, message='{message}'")


_aiohttp_stub = MagicMock()
_aiohttp_stub.ClientResponseError = _FakeClientResponseError
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

# Force-reload coordinator so it picks up our aiohttp stub
_load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

from custom_components.nem_pd7day.nemweb_retry import NemwebFetchError
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
    coord._forecast_store = None
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
    """Create a ClientResponseError using the fake aiohttp class."""
    return _FakeClientResponseError(
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


def test_pd7day_coordinator_returns_stale_on_nemweb_fetch_error(caplog):
    """The client now raises NemwebFetchError once its retries are spent.

    Issue #22 moved retrying out of the coordinator and into the clients, so the
    stale-data fallback has to recognise the client's own exhaustion error, not
    only a raw aiohttp status error. Without this branch a sustained 403 would
    surface as UpdateFailed and mark every entity unavailable rather than
    holding the last good forecast.
    """
    import logging

    coord = _make_pd7day_coordinator()
    stale = MagicMock(name="stale_pd7day_result")
    coord.data = stale

    mock_client = MagicMock()
    mock_client.fetch_all = AsyncMock(
        side_effect=NemwebFetchError(
            "PD7DAY directory listing unavailable after retry",
            retryable=False,
            status=403,
        )
    )
    coord._get_client = lambda: mock_client

    with caplog.at_level(logging.WARNING):
        result = run_async(coord._async_update_data())

    assert result is stale
    warnings = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    stale_lines = [m for m in warnings if "serving stale data" in m]
    assert len(stale_lines) == 1, "one stale-data warning, not a burst"
    # The reason must be legible: "403" alone does not tell an operator that
    # NEMWEB is rate blocking rather than that the report path is wrong.
    assert "bot or rate block" in stale_lines[0]


def test_pd7day_coordinator_raises_on_nemweb_fetch_error_no_stale():
    """With no previous data there is nothing to serve, so it must fail."""
    coord = _make_pd7day_coordinator()
    coord.data = None

    mock_client = MagicMock()
    mock_client.fetch_all = AsyncMock(
        side_effect=NemwebFetchError("exhausted", retryable=False, status=403)
    )
    coord._get_client = lambda: mock_client

    with pytest.raises(_UpdateFailed):
        run_async(coord._async_update_data())


def test_pd7day_coordinator_no_longer_wraps_fetch_all_in_its_own_retry():
    """Retrying belongs to the client, so the wrapper must be gone.

    Leaving it in place would double the retry budget and re-download every
    file on each attempt, which is what provoked the 403 to begin with.
    """
    coord = _make_pd7day_coordinator()
    assert not hasattr(coord, "_fetch_all_with_retry")

    calls = []

    async def fetch_all(regions, interconnectors):
        calls.append((tuple(regions), tuple(interconnectors)))
        raise NemwebFetchError("exhausted", retryable=False, status=403)

    mock_client = MagicMock()
    mock_client.fetch_all = fetch_all
    coord._get_client = lambda: mock_client
    coord.data = MagicMock(name="stale")

    run_async(coord._async_update_data())

    assert len(calls) == 1, "the coordinator must attempt the fetch exactly once"


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


# ── Staleness is legible (issue #105) ────────────────────────────────────────

def _coord_module():
    return sys.modules["custom_components.nem_pd7day.coordinator"]


def test_stale_serving_is_flagged_with_reason_and_age():
    """Serving stale data keeps the entity available, but the attributes
    must say so: is_stale True, the failure as stale_reason, and the age of
    the data being served, measured from the last success."""
    from datetime import datetime
    from unittest.mock import patch

    coord = _make_pd7day_coordinator()
    coord.data = MagicMock(name="stale_pd7day_result")
    coord.last_success_at = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)

    exc = _make_client_response_error(status=403, message="Forbidden")
    mock_client = MagicMock()
    mock_client.fetch_all = AsyncMock(side_effect=exc)
    coord._get_client = lambda: mock_client

    now = datetime(2026, 9, 3, 6, 30, tzinfo=timezone.utc)
    with patch.object(_coord_module().dt_util, "utcnow", return_value=now):
        run_async(coord._async_update_data())
        attrs = coord.staleness_attributes()

    assert attrs["is_stale"] is True
    assert "403" in attrs["stale_reason"]
    assert attrs["data_age_hours"] == 6.5


def test_success_clears_staleness_and_resets_age():
    from datetime import datetime
    from unittest.mock import patch

    coord = _make_pd7day_coordinator()
    coord.serving_stale = True
    coord.stale_reason = "403 Forbidden"
    result = MagicMock(name="fresh_result")
    result.prices = {}
    result.interconnectors = {}
    result.case = None
    result.source_file = "PUBLIC_PD7DAY.zip"
    mock_client = MagicMock()
    mock_client.fetch_all = AsyncMock(return_value=result)
    coord._get_client = lambda: mock_client

    now = datetime(2026, 9, 3, 7, 30, tzinfo=timezone.utc)
    with patch.object(_coord_module().dt_util, "utcnow", return_value=now):
        run_async(coord._async_update_data())
        attrs = coord.staleness_attributes()

    assert attrs == {"data_age_hours": 0.0, "is_stale": False, "stale_reason": None}
    assert coord.last_success_at == now


def test_staleness_attributes_before_first_success():
    """Restored-from-cache startup: nothing fetched yet, so no age and not
    stale rather than a misleading zero."""
    coord = _make_pd7day_coordinator()
    assert coord.staleness_attributes() == {
        "data_age_hours": None, "is_stale": False, "stale_reason": None,
    }


def test_staleness_helper_ignores_coordinators_that_do_not_track_it():
    from custom_components.nem_pd7day.coordinator import staleness_attributes
    assert staleness_attributes(MagicMock()) == {}
    assert staleness_attributes(object()) == {}


def test_dispatch_coordinator_flags_stale_prices():
    coord = _make_dispatch_coordinator()
    coord.data = MagicMock(name="stale_dispatch_prices")
    coord.hass.async_add_executor_job = AsyncMock(side_effect=Exception("timeout"))
    run_async(coord._async_update_data())
    attrs = coord.staleness_attributes()
    assert attrs["is_stale"] is True
    assert attrs["stale_reason"] == "timeout"
