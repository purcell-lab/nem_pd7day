"""
Tests for ActualPriceService — interval calculation, TradingIS integration,
and source field propagation.

Run with:  python -m pytest tests/test_actual_price_service.py -v
"""
from __future__ import annotations

import os
import sys
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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

# ── Stub HA imports ──────────────────────────────────────────────────────────

sys.modules.setdefault("aiohttp", MagicMock())
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()

_const_mock = MagicMock()
_const_mock.STATE_UNAVAILABLE = "unavailable"
_const_mock.STATE_UNKNOWN = "unknown"
sys.modules["homeassistant.const"] = _const_mock

sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.helpers.event"] = MagicMock()
sys.modules["homeassistant.helpers.aiohttp_client"] = MagicMock()
sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()
sys.modules["homeassistant.util"] = MagicMock()
sys.modules["homeassistant.util.dt"] = MagicMock()

# Register parent packages so relative imports resolve without triggering __init__.py
import types as _types
_cc = _types.ModuleType("custom_components")
_cc.__path__ = [os.path.join(_ROOT, "custom_components")]
sys.modules.setdefault("custom_components", _cc)
_pkg = _types.ModuleType("custom_components.nem_pd7day")
_pkg.__path__ = [os.path.join(_ROOT, "custom_components", "nem_pd7day")]
sys.modules.setdefault("custom_components.nem_pd7day", _pkg)

ha_storage_mock = MagicMock()
class _FakeStore:
    def __init__(self, hass, version, key): pass
    async def async_load(self): return None
    async def async_save(self, data): pass
ha_storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = ha_storage_mock

# Load integration modules — const first to avoid circular imports
_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)
_store_mod = _load(
    "custom_components.nem_pd7day.calibration_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"),
)
_tradingis_mod = _load(
    "custom_components.nem_pd7day.tradingis_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "tradingis_client.py"),
)
_service_mod = _load(
    "custom_components.nem_pd7day.actual_price_service",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "actual_price_service.py"),
)

from custom_components.nem_pd7day.actual_price_service import ActualPriceService
from custom_components.nem_pd7day.nem_time import NEM_TZ, to_nem_iso
from custom_components.nem_pd7day.calibration_store import CalibrationStore
from custom_components.nem_pd7day.calibration_engine import CalibrationEngine

# ── Helpers ──────────────────────────────────────────────────────────────────

def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def make_store() -> CalibrationStore:
    """Create a CalibrationStore with mocked HA storage."""
    store = CalibrationStore.__new__(CalibrationStore)
    store._hass = MagicMock()
    store._region = "QLD1"
    store._obs_store = AsyncMock()
    store._obs_store.async_load = AsyncMock(return_value=None)
    store._obs_store.async_save = AsyncMock()
    store._coeff_store = AsyncMock()
    store._coeff_store.async_load = AsyncMock(return_value=None)
    store._coeff_store.async_save = AsyncMock()
    store._fh_store = AsyncMock()
    store._fh_store.async_load = AsyncMock(return_value=None)
    store._fh_store.async_save = AsyncMock()
    store._engine = CalibrationEngine()
    store._observations = []
    store._calibration = None
    store._forecast_history = {}
    store._actual_accum = {}
    return store


def make_service(
    store=None,
    regions=None,
    fetch_price_return=None,
):
    """Create an ActualPriceService with mocked dependencies."""
    hass = MagicMock()
    if store is None:
        store = make_store()
    if regions is None:
        regions = ["QLD1"]
    session = MagicMock()

    service = ActualPriceService(hass, store, regions, session)

    # Mock the TradingIS client
    service._client = MagicMock()
    service._client.fetch_interval_price = AsyncMock(return_value=fetch_price_return)

    return service, store


# ── Tests: interval calculation ──────────────────────────────────────────────

def test_interval_calculation_at_32():
    """
    At HH:32, the just-closed interval should be HH:00-HH:30.
    Given now=2026-04-18T17:32:00+10:00, interval_start = 2026-04-18T17:00:00+10:00
    """
    # The tick fires in UTC; HA provides UTC datetimes
    now_utc = datetime(2026, 4, 18, 7, 32, 0, tzinfo=timezone.utc)  # 17:32 NEM
    now_nem = now_utc.astimezone(NEM_TZ)

    boundary = now_nem.replace(
        minute=(now_nem.minute // 30) * 30,
        second=0,
        microsecond=0,
    )
    interval_start = boundary - timedelta(minutes=30)

    expected = datetime(2026, 4, 18, 17, 0, 0, tzinfo=NEM_TZ)
    assert interval_start == expected, (
        f"At 17:32 NEM, interval_start should be 17:00, got {interval_start}"
    )


def test_interval_calculation_at_02():
    """
    At HH:02, the just-closed interval should be (HH-1):30-HH:00.
    Given now=2026-04-18T18:02:00+10:00, interval_start = 2026-04-18T17:30:00+10:00
    """
    now_utc = datetime(2026, 4, 18, 8, 2, 0, tzinfo=timezone.utc)  # 18:02 NEM
    now_nem = now_utc.astimezone(NEM_TZ)

    boundary = now_nem.replace(
        minute=(now_nem.minute // 30) * 30,
        second=0,
        microsecond=0,
    )
    interval_start = boundary - timedelta(minutes=30)

    expected = datetime(2026, 4, 18, 17, 30, 0, tzinfo=NEM_TZ)
    assert interval_start == expected, (
        f"At 18:02 NEM, interval_start should be 17:30, got {interval_start}"
    )


def test_tradingis_success_records_observation():
    """
    Successful TradingIS fetch should call async_record_actual
    with the correct args and source="tradingis".
    """
    service, store = make_service(fetch_price_return=0.09769)
    store.async_record_actual = AsyncMock(return_value=1)

    # Simulate tick at 17:32 UTC+10 = 07:32 UTC
    now_utc = datetime(2026, 4, 18, 7, 32, 0, tzinfo=timezone.utc)

    run_async(service._on_tradingis_tick(now_utc))

    store.async_record_actual.assert_called_once()
    call_args = store.async_record_actual.call_args
    interval_iso = call_args[0][0]
    price = call_args[0][1]
    source = call_args[1].get("source", call_args[0][2] if len(call_args[0]) > 2 else None)

    assert interval_iso == "2026-04-18T17:00:00+10:00", (
        f"Expected interval 17:00, got {interval_iso}"
    )
    assert abs(price - 0.09769) < 1e-9
    assert source == "tradingis"


def test_tradingis_failure_no_observation():
    """
    When TradingIS fetch returns None, async_record_actual should NOT be called.
    """
    service, store = make_service(fetch_price_return=None)
    store.async_record_actual = AsyncMock(return_value=0)

    now_utc = datetime(2026, 4, 18, 7, 32, 0, tzinfo=timezone.utc)
    run_async(service._on_tradingis_tick(now_utc))

    store.async_record_actual.assert_not_called()


def test_source_field_tradingis():
    """
    End-to-end: observation dict contains actual_source="tradingis".
    Uses a real CalibrationStore to verify the source field flows through.
    """
    store = make_store()

    # Set up forecast history for the interval
    interval_start_dt = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    interval_iso = nem_iso(interval_start_dt)
    run_at_dt = datetime(2026, 4, 18, 13, 0, tzinfo=NEM_TZ)
    run_at_iso = nem_iso(run_at_dt)

    store._forecast_history[interval_iso] = [{
        "run_at": run_at_iso,
        "forecast_price": 0.108,
        "gas_tj": None,
        "qni_mwflow": None,
        "qni_violation": None,
        "is_intervention": False,
        "region": "QLD1",
    }]

    # Record an actual price with source="tradingis"
    result = run_async(store.async_record_actual(interval_iso, 0.09769, source="tradingis"))

    assert result >= 1
    assert len(store._observations) >= 1
    obs = store._observations[0]
    assert obs.get("actual_source") == "tradingis", (
        f"Expected actual_source='tradingis', got {obs.get('actual_source')!r}"
    )


def test_source_field_backward_compat():
    """
    Calling async_record_actual without source parameter should default to "unknown".
    """
    store = make_store()

    interval_start_dt = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    interval_iso = nem_iso(interval_start_dt)
    run_at_iso = nem_iso(datetime(2026, 4, 18, 13, 0, tzinfo=NEM_TZ))

    store._forecast_history[interval_iso] = [{
        "run_at": run_at_iso,
        "forecast_price": 0.108,
        "gas_tj": None,
        "qni_mwflow": None,
        "qni_violation": None,
        "is_intervention": False,
        "region": "QLD1",
    }]

    # Call without source — backward compat
    result = run_async(store.async_record_actual(interval_iso, 0.09769))

    assert result >= 1
    obs = store._observations[0]
    assert obs.get("actual_source") == "unknown", (
        f"Default source should be 'unknown', got {obs.get('actual_source')!r}"
    )


def test_multiple_regions():
    """Service should fetch prices for all configured regions."""
    service, store = make_service(
        regions=["QLD1", "NSW1"],
        fetch_price_return=0.09769,
    )
    store.async_record_actual = AsyncMock(return_value=1)

    now_utc = datetime(2026, 4, 18, 7, 32, 0, tzinfo=timezone.utc)
    run_async(service._on_tradingis_tick(now_utc))

    # Should be called once per region
    assert store.async_record_actual.call_count == 2


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
