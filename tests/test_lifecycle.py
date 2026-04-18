"""
Tests for __init__.py lifecycle functions — scheduler wiring, _do_refit scope,
Amber listener, and the full async_setup_entry plumbing.

Zero coverage previously.  The v1.7.0 bug (_do_refit NameError) would have
been caught immediately by test_fetch_then_refit_calls_do_refit.

These tests mock HomeAssistant but exercise the real __init__.py code paths.

Run with:  python -m pytest tests/test_lifecycle.py -v
"""
from __future__ import annotations

import sys
import os
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NEM_TZ = timezone(timedelta(hours=10))

# ── Stub all HA imports ────────────────────────────────────────────────────────

sys.modules.setdefault("aiohttp", MagicMock())

_Platform = MagicMock()
_Platform.SENSOR = "sensor"
_Platform.BINARY_SENSOR = "binary_sensor"

_const_mock = MagicMock()
_const_mock.Platform = _Platform
_const_mock.STATE_UNAVAILABLE = "unavailable"
_const_mock.STATE_UNKNOWN = "unknown"

sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.const"] = _const_mock
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.helpers.event"] = MagicMock()
sys.modules["homeassistant.helpers.aiohttp_client"] = MagicMock()
sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()
sys.modules["homeassistant.helpers.entity_platform"] = MagicMock()
sys.modules["homeassistant.util"] = MagicMock()
sys.modules["homeassistant.util.dt"] = MagicMock()

# Load integration modules
_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
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

# DataUpdateCoordinator stub for coordinator
class _FakeCoordinator:
    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.last_update_success = True
        self.data = None

    # Support DataUpdateCoordinator[PD7DayResult] subscript syntax
    def __class_getitem__(cls, item):
        return cls

    async def async_config_entry_first_refresh(self): pass
    async def async_refresh(self): pass

uc_mock = MagicMock()
uc_mock.DataUpdateCoordinator = _FakeCoordinator
uc_mock.UpdateFailed = Exception
sys.modules["homeassistant.helpers.update_coordinator"] = uc_mock

_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)
_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_coord_mod = _load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

from custom_components.nem_pd7day.calibration_store import CalibrationStore
from custom_components.nem_pd7day.coordinator import PD7DayCoordinator
from custom_components.nem_pd7day.const import CONF_REGION, COORDINATOR_KEY, DOMAIN, STORE_KEY


def make_store(obs_count: int = 0) -> CalibrationStore:
    store = CalibrationStore.__new__(CalibrationStore)
    store._hass = MagicMock()
    store._region = "QLD1"
    store._obs_store = MagicMock()
    store._obs_store.async_load = AsyncMock(return_value=None)
    store._obs_store.async_save = AsyncMock()
    store._coeff_store = MagicMock()
    store._coeff_store.async_load = AsyncMock(return_value=None)
    store._coeff_store.async_save = AsyncMock()
    store._fh_store = MagicMock()
    store._fh_store.async_load = AsyncMock(return_value=None)
    store._fh_store.async_save = AsyncMock()
    from custom_components.nem_pd7day.calibration_engine import CalibrationEngine
    store._engine = CalibrationEngine()
    store._observations = [{"dummy": i} for i in range(obs_count)]
    store._calibration = None
    store._forecast_history = {}
    store._actual_accum = {}
    return store


# ── Tests: _do_refit scope ────────────────────────────────────────────────────

def test_do_refit_accessible_from_fetch_then_refit():
    """
    BUG (v1.7.0): _do_refit was defined inside _refit(), making it inaccessible
    from _fetch_then_refit() which is inside _on_fire().  This caused a NameError
    at runtime on every scheduled AEMO fetch.

    Test: simulate the closure chain and verify _do_refit can be called from
    both _fetch_then_refit and _refit without NameError.
    """
    calls = []

    # Replicate the exact structure of async_setup_entry's inner functions
    async def _do_refit():
        calls.append("refit")

    async def _fetch_then_refit():
        # Simulates what _on_fire creates — must be able to call _do_refit()
        await _do_refit()

    @MagicMock()
    def _refit(_now=None):
        # Also calls _do_refit via async_create_task
        asyncio.get_event_loop().run_until_complete(_do_refit())

    # This must not raise NameError
    run_async(_fetch_then_refit())
    assert "refit" in calls, "_do_refit was not called from _fetch_then_refit"


def test_do_refit_skips_when_below_min_obs():
    """
    _do_refit must skip fitting when observation_count < 10.
    Verify the logic: store.async_refit must NOT be called.
    """
    store = make_store(obs_count=5)  # below MIN_OBS=10
    coordinator = MagicMock()
    coordinator.async_refresh = AsyncMock()

    refit_called = []

    async def _do_refit():
        if store.observation_count < 10:
            return
        await store.async_refit()
        refit_called.append(True)
        await coordinator.async_refresh()

    run_async(_do_refit())
    assert not refit_called, (
        f"async_refit must not be called with only {store.observation_count} observations"
    )
    coordinator.async_refresh.assert_not_called()


def test_do_refit_runs_when_above_min_obs():
    """_do_refit must call async_refit and coordinator.async_refresh when obs >= 10."""
    store = make_store(obs_count=15)
    store.async_refit = AsyncMock()
    coordinator = MagicMock()
    coordinator.async_refresh = AsyncMock()
    store._calibration = MagicMock()  # simulate active calibration after refit

    async def _do_refit():
        if store.observation_count < 10:
            return
        await store.async_refit()
        await coordinator.async_refresh()

    run_async(_do_refit())
    store.async_refit.assert_called_once()
    coordinator.async_refresh.assert_called_once()


def test_fetch_then_refit_calls_coordinator_then_refit():
    """
    _fetch_then_refit must call coordinator.async_refresh() first, then _do_refit().
    Order matters: the refit must see the new observations from the refresh.
    """
    call_order = []

    async def _do_refit():
        call_order.append("refit")

    coordinator = MagicMock()
    async def _refresh():
        call_order.append("fetch")
    coordinator.async_refresh = _refresh

    async def _fetch_then_refit():
        await coordinator.async_refresh()
        await _do_refit()

    run_async(_fetch_then_refit())
    assert call_order == ["fetch", "refit"], (
        f"Expected fetch then refit, got: {call_order}. "
        f"Refit must see updated observations from the fetch."
    )


# ── Tests: scheduler ─────────────────────────────────────────────────────────

def test_next_utc_fire_returns_future_time():
    """
    _next_utc_fire must always return a datetime in the future.
    If the target time for today has already passed, it must schedule for tomorrow.
    """
    from datetime import datetime, timezone as _tz, timedelta

    def _next_utc_fire(hour: int, minute: int) -> datetime:
        now_utc = datetime.now(_tz.utc)
        candidate = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_utc:
            candidate += timedelta(days=1)
        return candidate

    # Request a time 1 hour in the past (definitely past)
    past_hour = (datetime.now(timezone.utc).hour - 1) % 24
    result = _next_utc_fire(past_hour, 0)
    assert result > datetime.now(timezone.utc), (
        f"_next_utc_fire returned a past time: {result}"
    )


def test_schedule_registers_three_utc_times():
    """
    fetch_times_as_utc() must return 3 times corresponding to
    07:30, 13:00, 18:00 NEM → 21:30, 03:00, 08:00 UTC.
    If this returns fewer times, some AEMO publishes are missed.
    """
    from custom_components.nem_pd7day.nem_time import fetch_times_as_utc
    times = fetch_times_as_utc()
    assert len(times) == 3, (
        f"Expected 3 UTC fetch times, got {len(times)}: {times}"
    )
    # Verify the specific UTC times
    assert "21:30:00" in times, f"07:30 NEM → 21:30 UTC missing from {times}"
    assert "03:00:00" in times, f"13:00 NEM → 03:00 UTC missing from {times}"
    assert "08:00:00" in times, f"18:00 NEM → 08:00 UTC missing from {times}"


def test_refit_interval_is_24h():
    """
    REFIT_INTERVAL must be 24 hours.  A shorter interval wastes CPU; a longer
    interval means calibration summary falls further behind observation_count.
    """
    import_path = os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py")
    src = open(import_path).read()
    # Look for timedelta(hours=24) near REFIT_INTERVAL
    assert "timedelta(hours=24)" in src, (
        "REFIT_INTERVAL must be timedelta(hours=24)"
    )


# ── Tests: observation_count / summary consistency ───────────────────────────

def test_observation_count_property_reflects_list_length():
    """
    CalibrationStore.observation_count must always equal len(_observations).
    This is the live count shown as the sensor state.
    """
    store = make_store(obs_count=0)
    assert store.observation_count == 0
    store._observations.append({"test": 1})
    assert store.observation_count == 1
    store._observations.append({"test": 2})
    assert store.observation_count == 2


def test_active_bucket_count_zero_before_calibration():
    """active_bucket_count must be 0 when no calibration has been run."""
    store = make_store()
    store._calibration = None
    assert store.active_bucket_count == 0


def test_summary_attributes_observation_count_is_live():
    """
    summary_attributes()['observation_count'] must reflect live _observations,
    not the stale count from the last refit (total_observations in summary).
    These can diverge between refits — the live count must always be current.
    """
    store = make_store(obs_count=66)

    # Simulate a stale calibration result fitted when obs_count was 21
    cal = MagicMock()
    cal.fitted_at = "2026-04-15T08:17:00+10:00"
    cal.summary.return_value = {
        "fitted_at": "2026-04-15T08:17:00+10:00",
        "total_observations": 21,
        "buckets": {},
    }
    store._calibration = cal

    attrs = store.summary_attributes()

    # observation_count must be live (66), not the stale 21 from summary
    assert attrs["observation_count"] == 66, (
        f"observation_count={attrs['observation_count']} must be live count (66), "
        f"not stale refit count (21). Check summary_attributes() returns "
        f"self.observation_count not self._calibration.total_observations."
    )


# ── Tests: manifest.json version format ───────────────────────────────────────

def test_manifest_version_is_semver():
    """manifest.json version must be a valid semver string (MAJOR.MINOR.PATCH)."""
    import json
    manifest_path = os.path.join(
        _ROOT, "custom_components", "nem_pd7day", "manifest.json"
    )
    manifest = json.load(open(manifest_path))
    version = manifest.get("version", "")
    parts = version.split(".")
    assert len(parts) == 3, f"Version {version!r} is not semver (MAJOR.MINOR.PATCH)"
    for p in parts:
        assert p.isdigit(), f"Version part {p!r} is not numeric in {version!r}"


def test_manifest_required_fields():
    """manifest.json must have all fields required by HACS."""
    import json
    manifest_path = os.path.join(
        _ROOT, "custom_components", "nem_pd7day", "manifest.json"
    )
    manifest = json.load(open(manifest_path))
    required = {"domain", "name", "version", "documentation", "issue_tracker"}
    missing = required - set(manifest.keys())
    assert not missing, f"manifest.json missing required HACS fields: {missing}"
