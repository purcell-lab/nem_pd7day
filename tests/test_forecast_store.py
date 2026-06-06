"""
Tests for ForecastStore (forecast_store.py) and the two-phase startup logic.

Covers:
  - PD7DayResult serialise → save → load round-trip preserves the object tree
  - Cache staleness: updated_at older than 35 min returns None from load()
  - Fresh cache (updated_at within 35 min) is restored
  - Two-phase startup: cache hit → async_set_updated_data, no first_refresh
  - No-cache path: first install → async_config_entry_first_refresh

Pure Python — HA modules are stubbed, no Home Assistant install required.

Run with:  python -m pytest tests/test_forecast_store.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Stub HA + aiohttp so the integration modules import cleanly ───────────────
# conftest.py's autouse fixture imports custom_components.nem_pd7day.sensor, so
# we register the same broad set of HA stubs that test_sensor.py relies on —
# otherwise running this file in isolation fails on sensor.py's HA imports.
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

import enum as _enum

_device_registry_mock = MagicMock()
_device_registry_mock.DeviceInfo = dict
sys.modules["homeassistant.helpers.device_registry"] = _device_registry_mock


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


# Configurable in-memory fake Store: each instance keyed by storage key.
class _FakeStore:
    _backing: dict[str, dict] = {}

    def __init__(self, hass, version, key):
        self._key = key

    async def async_load(self):
        return _FakeStore._backing.get(self._key)

    async def async_save(self, data):
        _FakeStore._backing[self._key] = data


_storage_mock = MagicMock()
_storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = _storage_mock


# DataUpdateCoordinator / CoordinatorEntity stubs — real classes so the sensor
# chain (imported by conftest's autouse fixture) builds without metaclass clashes.
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

_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)
_fs_mod = _load(
    "custom_components.nem_pd7day.forecast_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "forecast_store.py"),
)

from custom_components.nem_pd7day.forecast_store import ForecastStore, _CACHE_MAX_AGE_S
from custom_components.nem_pd7day.pd7day_client import (
    CaseSolutionData,
    CheapestWindow,
    GasForecastPeriod,
    InterconnectorData,
    InterconnectorPeriod,
    MarketSummaryData,
    PD7DayData,
    PD7DayResult,
    PricePeriod,
)

NEM_TZ = timezone(timedelta(hours=10))


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def _make_result(updated_at: str) -> PD7DayResult:
    """Build a fully-populated PD7DayResult covering all nested dataclasses."""
    base = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    periods = [
        PricePeriod(
            nemtime=nem_iso(base + timedelta(minutes=30 * i)),
            time=nem_iso(base + timedelta(minutes=30 * i - 30)),
            value=round(0.10 + 0.01 * i, 6),
        )
        for i in range(4)
    ]
    price = PD7DayData(
        region="QLD1",
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        forecast_generated_at=nem_iso(base),
        interval_minutes=30,
        current_value=periods[0].value,
        next_value=periods[1].value,
        min_24h_value=0.10,
        max_24h_value=0.13,
        cheapest_2h_window=CheapestWindow(
            start=periods[0].time,
            end=periods[3].time,
            nemtime_start=periods[0].nemtime,
            nemtime_end=periods[3].nemtime,
            avg_value=0.115,
            points=4,
        ),
        forecast=periods,
    )
    market = MarketSummaryData(
        run_datetime=nem_iso(base),
        forecast=[
            GasForecastPeriod(
                nemtime=nem_iso(base + timedelta(days=d)),
                time=nem_iso(base + timedelta(days=d) - timedelta(minutes=30)),
                value_tj=100.0 + d,
            )
            for d in range(2)
        ],
    )
    ic = InterconnectorData(
        interconnector_id="NSW1-QLD1",
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        run_datetime=nem_iso(base),
        forecast=[
            InterconnectorPeriod(
                nemtime=nem_iso(base),
                time=nem_iso(base - timedelta(minutes=30)),
                mwflow=120.0,
                meteredmwflow=119.0,
                mwlosses=2.0,
                marginalvalue=0.0,
                violationdegree=0.0,
                exportlimit=1000.0,
                importlimit=-1000.0,
                marginalloss=0.05,
            )
        ],
    )
    return PD7DayResult(
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        case=CaseSolutionData(
            run_datetime=nem_iso(base),
            intervention=False,
            last_changed=nem_iso(base),
        ),
        prices={"QLD1": price},
        market_summary=market,
        interconnectors={"NSW1-QLD1": ic},
        updated_at=updated_at,
    )


def _fresh_iso() -> str:
    return nem_iso(datetime.now(NEM_TZ) - timedelta(minutes=5))


def _stale_iso() -> str:
    return nem_iso(datetime.now(NEM_TZ) - timedelta(seconds=_CACHE_MAX_AGE_S + 120))


def _new_store(region="QLD1") -> ForecastStore:
    _FakeStore._backing.clear()
    return ForecastStore(MagicMock(), region)


# ── Round-trip ────────────────────────────────────────────────────────────────


def test_save_load_round_trip_preserves_tree():
    store = _new_store()
    original = _make_result(_fresh_iso())
    run_async(store.save(original))
    restored = run_async(store.load())

    assert restored is not None
    assert restored.source_file == original.source_file
    assert restored.updated_at == original.updated_at
    # CaseSolutionData
    assert restored.case.intervention is False
    assert restored.case.run_datetime == original.case.run_datetime
    # PD7DayData + nested PricePeriod / CheapestWindow
    rp = restored.prices["QLD1"]
    op = original.prices["QLD1"]
    assert rp.region == "QLD1"
    assert rp.current_value == op.current_value
    assert len(rp.forecast) == len(op.forecast)
    assert rp.forecast[0].value == op.forecast[0].value
    assert rp.forecast[0].nemtime == op.forecast[0].nemtime
    assert rp.cheapest_2h_window.avg_value == op.cheapest_2h_window.avg_value
    assert rp.cheapest_2h_window.points == 4
    # MarketSummaryData + GasForecastPeriod
    assert len(restored.market_summary.forecast) == 2
    assert restored.market_summary.forecast[0].value_tj == 100.0
    # InterconnectorData + InterconnectorPeriod
    ric = restored.interconnectors["NSW1-QLD1"]
    assert ric.interconnector_id == "NSW1-QLD1"
    assert ric.forecast[0].mwflow == 120.0
    assert ric.forecast[0].marginalloss == 0.05


def test_round_trip_handles_optional_none_fields():
    """Optional fields (next_value, cheapest window, case, market_summary) = None."""
    store = _new_store()
    minimal = PD7DayResult(
        source_file="PUBLIC_PD7DAY_X.ZIP",
        case=None,
        prices={
            "QLD1": PD7DayData(
                region="QLD1",
                source_file="PUBLIC_PD7DAY_X.ZIP",
                forecast_generated_at=None,
                interval_minutes=30,
                current_value=0.05,
                next_value=None,
                min_24h_value=None,
                max_24h_value=None,
                cheapest_2h_window=None,
                forecast=[],
            )
        },
        market_summary=None,
        interconnectors={},
        updated_at=_fresh_iso(),
    )
    run_async(store.save(minimal))
    restored = run_async(store.load())

    assert restored is not None
    assert restored.case is None
    assert restored.market_summary is None
    assert restored.interconnectors == {}
    assert restored.prices["QLD1"].next_value is None
    assert restored.prices["QLD1"].cheapest_2h_window is None
    assert restored.prices["QLD1"].forecast == []


# ── Staleness ──────────────────────────────────────────────────────────────────


def test_load_returns_none_when_stale():
    """updated_at older than 35 minutes → load() returns None."""
    store = _new_store()
    run_async(store.save(_make_result(_stale_iso())))
    assert run_async(store.load()) is None


def test_load_returns_result_when_fresh():
    """updated_at within 35 minutes → load() returns the result."""
    store = _new_store()
    run_async(store.save(_make_result(_fresh_iso())))
    restored = run_async(store.load())
    assert restored is not None
    assert restored.source_file == "PUBLIC_PD7DAY_20260415.ZIP"


def test_load_returns_none_when_no_cache():
    """Empty backing store → load() returns None (first install)."""
    store = _new_store()
    assert run_async(store.load()) is None


def test_load_returns_none_when_updated_at_missing():
    store = _new_store()
    # Save a payload with no updated_at (simulating a corrupt/legacy cache).
    _FakeStore._backing["nem_pd7day.forecast.qld1"] = {"source_file": "x", "prices": {}}
    assert run_async(store.load()) is None


def test_per_region_keys_are_isolated():
    """Two regions use distinct storage keys."""
    _FakeStore._backing.clear()
    qld = ForecastStore(MagicMock(), "QLD1")
    nsw = ForecastStore(MagicMock(), "NSW1")
    run_async(qld.save(_make_result(_fresh_iso())))
    assert run_async(nsw.load()) is None
    assert run_async(qld.load()) is not None


# ── Two-phase startup decision logic ───────────────────────────────────────────
#
# These tests exercise the branch that async_setup_entry takes based on the
# cache load result, without standing up the full HA setup machinery.


def _startup_branch(coordinator, forecast_store, region, create_bg_task):
    """Mirror the two-phase startup decision in async_setup_entry."""
    cached = run_async(forecast_store.load())
    if cached is not None:
        coordinator.async_set_updated_data(cached)
        region_index = _const_mod.REGION_STARTUP_ORDER.get(region, 0)
        delay = 30 + region_index * 5
        create_bg_task(delay)
    else:
        run_async(coordinator.async_config_entry_first_refresh())


def test_two_phase_cache_hit_sets_data_no_first_refresh():
    store = _new_store("NSW1")
    run_async(store.save(_make_result(_fresh_iso())))

    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    delays: list[float] = []

    _startup_branch(coordinator, store, "NSW1", lambda d: delays.append(d))

    coordinator.async_set_updated_data.assert_called_once()
    coordinator.async_config_entry_first_refresh.assert_not_called()
    # NSW1 index = 1 → delay 35s
    assert delays == [35]


def test_two_phase_no_cache_calls_first_refresh():
    store = _new_store("QLD1")  # empty backing

    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    delays: list[float] = []

    _startup_branch(coordinator, store, "QLD1", lambda d: delays.append(d))

    coordinator.async_set_updated_data.assert_not_called()
    coordinator.async_config_entry_first_refresh.assert_called_once()
    assert delays == []


def test_two_phase_stale_cache_calls_first_refresh():
    store = _new_store("VIC1")
    run_async(store.save(_make_result(_stale_iso())))

    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    delays: list[float] = []

    _startup_branch(coordinator, store, "VIC1", lambda d: delays.append(d))

    coordinator.async_set_updated_data.assert_not_called()
    coordinator.async_config_entry_first_refresh.assert_called_once()
