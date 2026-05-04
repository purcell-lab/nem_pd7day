"""
Tests for forecast history persistence across HA restarts.

Covers:
  - _forecast_history survives save → fresh store → load round-trip
  - Actuals recorded after restart match forecast entries from previous session
  - Pruning is persisted (old entries removed before save)
  - Dedup works across restart boundary (same run_at re-ingested after load)

Run with:  python -m pytest tests/test_forecast_history_persistence.py -v
"""
from __future__ import annotations

import sys
import os
import asyncio
import importlib.util
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta, timezone

# ── Module loader (avoids HA import chain) ────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

# Stub out all HA modules so CalibrationStore can be imported without HA installed
_ha_mock = MagicMock()
sys.modules.setdefault("homeassistant", _ha_mock)
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()

# Provide a fake Store whose async_load returns None (awaitable)
class _FakeStore:
    def __init__(self, hass, version, key):
        self._key = key
    async def async_load(self):
        return None
    async def async_save(self, data):
        pass

_storage_mock = MagicMock()
_storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = _storage_mock

sys.modules["homeassistant.helpers.event"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.util"] = MagicMock()
sys.modules["homeassistant.util.dt"] = MagicMock()

_store_mod = _load(
    "custom_components.nem_pd7day.calibration_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"),
)

from custom_components.nem_pd7day.calibration_engine import CalibrationEngine
from custom_components.nem_pd7day.calibration_store import CalibrationStore

# ── Helpers ───────────────────────────────────────────────────────────────────

NEM_TZ = timezone(timedelta(hours=10))


def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def make_price_period(nemtime_dt: datetime, value: float = 0.10):
    start_dt = nemtime_dt - timedelta(minutes=30)
    return MagicMock(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),
        value=value,
    )


def make_price_data(run_at_dt: datetime, periods):
    return MagicMock(
        forecast_generated_at=nem_iso(run_at_dt),
        forecast=periods,
    )


def make_store(fh_load_data=None) -> CalibrationStore:
    """Create a CalibrationStore with mocked HA storage.

    fh_load_data: if provided, the forecast history store will return this
    data on async_load() — simulates a restart with persisted history.
    """
    hass = MagicMock()
    store = CalibrationStore.__new__(CalibrationStore)
    store._hass = hass
    store._region = "QLD1"
    store._obs_store = AsyncMock()
    store._obs_store.async_load = AsyncMock(return_value=None)
    store._obs_store.async_save = AsyncMock()
    store._coeff_store = AsyncMock()
    store._coeff_store.async_load = AsyncMock(return_value=None)
    store._coeff_store.async_save = AsyncMock()
    store._fh_store = AsyncMock()
    store._fh_store.async_load = AsyncMock(return_value=fh_load_data)
    store._fh_store.async_save = AsyncMock()
    store._engine = CalibrationEngine()
    store._observations = []
    store._calibration = None
    store._forecast_history = {}
    store._actual_accum = {}
    return store


BASE_DT = datetime(2026, 4, 14, 18, 0, tzinfo=NEM_TZ)

# Pin _now_nem() close to test dates so forecast-history pruning doesn't discard data
_store_mod._now_nem = lambda: BASE_DT + timedelta(hours=1)


# ── Tests: forecast history persistence ──────────────────────────────────────

def test_forecast_history_survives_restart():
    """
    Simulate ingest → save → create fresh CalibrationStore → load.
    Verify _forecast_history contains the same keys and entries.
    """
    # Session 1: ingest forecast and capture what was saved
    store1 = make_store()
    run_dt = BASE_DT
    interval_end = BASE_DT + timedelta(hours=3, minutes=30)
    period = make_price_period(interval_end, value=0.108)

    run_async(store1.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, [period]),
        interconnectors={},
        case=None,
    ))

    # Capture what was saved to the fh_store
    assert store1._fh_store.async_save.called, (
        "ingest_forecast must call _save_forecast_history"
    )
    saved_data = store1._fh_store.async_save.call_args[0][0]

    # Session 2: fresh store loaded from saved data
    store2 = make_store(fh_load_data=saved_data)
    run_async(store2.async_load())

    # Verify keys match
    assert set(store2._forecast_history.keys()) == set(store1._forecast_history.keys()), (
        f"Loaded keys {set(store2._forecast_history.keys())} != "
        f"original keys {set(store1._forecast_history.keys())}"
    )

    # Verify entries match
    for key in store1._forecast_history:
        orig_entries = store1._forecast_history[key]
        loaded_entries = store2._forecast_history[key]
        assert len(loaded_entries) == len(orig_entries), (
            f"Key {key}: loaded {len(loaded_entries)} entries, expected {len(orig_entries)}"
        )
        for i, (orig, loaded) in enumerate(zip(orig_entries, loaded_entries)):
            assert orig["run_at"] == loaded["run_at"], (
                f"Key {key}[{i}]: run_at mismatch"
            )
            assert abs(orig["forecast_price"] - loaded["forecast_price"]) < 1e-9, (
                f"Key {key}[{i}]: forecast_price mismatch"
            )


def test_actual_recorded_after_restart():
    """
    Full round-trip: ingest forecast in session 1, simulate restart,
    call async_record_actual for a 72h-ahead interval in session 2.
    Verify observation is recorded with correct horizon_hours in h48_96 range.
    """
    # Session 1: ingest a forecast for an interval 72h ahead
    store1 = make_store()
    run_dt = datetime(2026, 4, 14, 7, 30, tzinfo=NEM_TZ)
    # Interval 72h after run_at: 2026-04-17 07:30 NEM
    interval_start_dt = datetime(2026, 4, 17, 7, 30, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.095)

    run_async(store1.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, [period]),
        interconnectors={},
        case=None,
    ))

    saved_data = store1._fh_store.async_save.call_args[0][0]

    # Session 2: fresh store, load from saved data
    store2 = make_store(fh_load_data=saved_data)
    run_async(store2.async_load())

    # Record an actual for that interval — 72h horizon
    interval_iso = nem_iso(interval_start_dt)
    run_async(store2.async_record_actual(interval_iso, 0.090))

    assert len(store2._observations) == 1, (
        f"Expected 1 observation after restart, got {len(store2._observations)}. "
        f"forecast_history keys: {list(store2._forecast_history.keys())[:5]}"
    )
    obs = store2._observations[0]
    # Horizon: interval_start(2026-04-17 07:30) - run_at(2026-04-14 07:30) = 72.0h
    assert abs(obs["horizon_hours"] - 72.0) < 0.01, (
        f"Expected horizon 72.0h (h48_96 bucket), got {obs['horizon_hours']}"
    )
    # 72h falls in h48_96 bucket (48 <= 72 < 96)
    assert 48 <= obs["horizon_hours"] < 96, (
        f"Horizon {obs['horizon_hours']}h should be in h48_96 bucket"
    )


def test_forecast_history_pruning_persisted():
    """
    Verify that intervals older than MAX_FORECAST_AGE_DAYS are pruned before
    saving, so the saved history doesn't grow unbounded.
    """
    store = make_store()

    # Manually inject an old forecast history entry (20 days ago)
    old_dt = datetime(2026, 3, 25, 12, 0, tzinfo=NEM_TZ)
    old_key = nem_iso(old_dt)
    store._forecast_history[old_key] = [{
        "run_at": nem_iso(old_dt - timedelta(hours=6)),
        "forecast_price": 0.10,
        "gas_tj": None,
        "qni_mwflow": None,
        "qni_violation": None,
        "is_intervention": False,
        "region": "QLD1",
    }]

    # Now ingest a fresh forecast — this triggers pruning
    run_dt = BASE_DT
    interval_end = BASE_DT + timedelta(hours=3, minutes=30)
    period = make_price_period(interval_end, value=0.108)

    run_async(store.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, [period]),
        interconnectors={},
        case=None,
    ))

    # Old key must have been pruned
    assert old_key not in store._forecast_history, (
        f"Old key {old_key} should have been pruned (> MAX_FORECAST_AGE_DAYS)"
    )

    # Verify the saved data also doesn't contain the old key
    saved_data = store._fh_store.async_save.call_args[0][0]
    assert old_key not in saved_data.get("forecast_history", {}), (
        "Pruned key must not appear in saved data"
    )

    # Fresh entry must still be present
    fresh_key = list(store._forecast_history.keys())[0]
    assert fresh_key in saved_data["forecast_history"], (
        "Fresh forecast entry must be in saved data"
    )


def test_dedup_after_restart():
    """
    Ingest same run_at twice across a restart boundary — verify no duplicate entries.
    Session 1: ingest forecast. Session 2: load, then ingest same run_at again.
    Must still have exactly 1 entry for that interval.
    """
    # Session 1: ingest
    store1 = make_store()
    run_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    interval_end = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period = make_price_period(interval_end, value=0.110)
    price_data = make_price_data(run_dt, [period])

    run_async(store1.ingest_forecast("QLD1", price_data, {}, None))

    saved_data = store1._fh_store.async_save.call_args[0][0]

    # Session 2: load, then re-ingest same run_at
    store2 = make_store(fh_load_data=saved_data)
    run_async(store2.async_load())

    # Re-ingest same forecast (simulates startup re-fetch of same AEMO file)
    run_async(store2.ingest_forecast("QLD1", price_data, {}, None))

    key = nem_iso(interval_end - timedelta(minutes=30))
    assert key in store2._forecast_history
    assert len(store2._forecast_history[key]) == 1, (
        f"Expected 1 entry after restart + re-ingest of same run_at, "
        f"got {len(store2._forecast_history[key])}. "
        f"Dedup must work across restart boundary."
    )


def test_async_load_with_empty_storage():
    """
    When forecast history storage is empty (first install), async_load
    must initialize _forecast_history as an empty dict without errors.
    """
    store = make_store(fh_load_data=None)
    run_async(store.async_load())
    assert store._forecast_history == {}, (
        "Empty storage must result in empty forecast_history dict"
    )


def test_multiple_intervals_persisted():
    """
    Ingest multiple intervals in one forecast, verify all are persisted and
    restored correctly.
    """
    store1 = make_store()
    run_dt = BASE_DT

    # 5 consecutive intervals
    periods = []
    for i in range(5):
        interval_end = BASE_DT + timedelta(hours=i + 1, minutes=30)
        periods.append(make_price_period(interval_end, value=0.10 + i * 0.01))

    run_async(store1.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, periods),
        interconnectors={},
        case=None,
    ))

    assert len(store1._forecast_history) == 5, (
        f"Expected 5 interval keys, got {len(store1._forecast_history)}"
    )

    saved_data = store1._fh_store.async_save.call_args[0][0]

    # Restore into a new store
    store2 = make_store(fh_load_data=saved_data)
    run_async(store2.async_load())

    assert len(store2._forecast_history) == 5, (
        f"After restore, expected 5 interval keys, got {len(store2._forecast_history)}"
    )

    # Verify all keys match
    assert set(store2._forecast_history.keys()) == set(store1._forecast_history.keys())


def test_h96plus_actual_recorded_after_restart():
    """
    Verify that a 120h-ahead (h96plus bucket) actual can be recorded after
    restart. This is the primary scenario the bug prevented.
    """
    store1 = make_store()
    run_dt = datetime(2026, 4, 14, 7, 30, tzinfo=NEM_TZ)
    # Interval 120h after run_at: 2026-04-19 07:30 NEM
    interval_start_dt = datetime(2026, 4, 19, 7, 30, tzinfo=NEM_TZ)
    interval_end_dt = interval_start_dt + timedelta(minutes=30)
    period = make_price_period(interval_end_dt, value=0.082)

    run_async(store1.ingest_forecast(
        region="QLD1",
        price_data=make_price_data(run_dt, [period]),
        interconnectors={},
        case=None,
    ))

    saved_data = store1._fh_store.async_save.call_args[0][0]

    # Session 2: fresh store
    store2 = make_store(fh_load_data=saved_data)
    run_async(store2.async_load())

    interval_iso = nem_iso(interval_start_dt)
    run_async(store2.async_record_actual(interval_iso, 0.075))

    assert len(store2._observations) == 1
    obs = store2._observations[0]
    # 120h falls in h96plus bucket (>= 96h)
    assert abs(obs["horizon_hours"] - 120.0) < 0.01, (
        f"Expected horizon 120.0h, got {obs['horizon_hours']}"
    )
    assert obs["horizon_hours"] >= 96, (
        f"Horizon {obs['horizon_hours']}h should be in h96plus bucket (>= 96h)"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
