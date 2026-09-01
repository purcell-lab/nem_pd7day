"""Tests for StpasaStore TTL and stale-cache behaviour."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult
from custom_components.nem_pd7day.stpasa_store import (
    STPASA_CACHE_TTL,
    STAPASA_STALE_TTL,
    StpasaStore,
    _cache_status,
    _is_fresh,
)


def _make_result(age: timedelta) -> StpasaResult:
    """Build a StpasaResult with fetched_at = now - age."""
    fetched = (datetime.now(timezone.utc) - age).isoformat()
    interval = StpasaInterval(
        interval_datetime="2026-06-17T04:30:00+10:00",
        run_datetime="2026-06-16T12:00:00+10:00",
        demand10=5800.0,
        demand50=5600.0,
        demand90=5400.0,
        surpluscapacity=2800.0,
        ss_solar_uigf=0.0,
        ss_wind_uigf=1100.0,
    )
    return StpasaResult(
        region="QLD1",
        run_datetime="2026-06-16T12:00:00+10:00",
        intervals=[interval],
        fetched_at=fetched,
    )


# ---------------------------------------------------------------------------
# _cache_status
# ---------------------------------------------------------------------------

class TestCacheStatus:
    def test_fresh(self):
        fetched = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        assert _cache_status(fetched) == "fresh"

    def test_boundary_fresh(self):
        fetched = (datetime.now(timezone.utc) - STPASA_CACHE_TTL + timedelta(seconds=5)).isoformat()
        assert _cache_status(fetched) == "fresh"

    def test_stale(self):
        fetched = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        assert _cache_status(fetched) == "stale"

    def test_boundary_stale(self):
        fetched = (datetime.now(timezone.utc) - STAPASA_STALE_TTL + timedelta(seconds=5)).isoformat()
        assert _cache_status(fetched) == "stale"

    def test_expired(self):
        fetched = (datetime.now(timezone.utc) - STAPASA_STALE_TTL - timedelta(minutes=1)).isoformat()
        assert _cache_status(fetched) == "expired"

    def test_empty_string_expired(self):
        assert _cache_status("") == "expired"

    def test_bad_string_expired(self):
        assert _cache_status("not-a-date") == "expired"


# ---------------------------------------------------------------------------
# _is_fresh (unchanged behaviour)
# ---------------------------------------------------------------------------

class TestIsFresh:
    def test_fresh(self):
        fetched = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        assert _is_fresh(fetched) is True

    def test_stale_not_fresh(self):
        fetched = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        assert _is_fresh(fetched) is False


# ---------------------------------------------------------------------------
# StpasaStore.latest()
# ---------------------------------------------------------------------------

class TestStpasaStoreLatest:
    def _make_store(self) -> StpasaStore:
        hass = MagicMock()
        store = StpasaStore.__new__(StpasaStore)
        store._hass = hass
        store._region = "QLD1"
        store._store = MagicMock()
        store._latest = None
        return store

    def test_latest_none_when_no_data(self):
        store = self._make_store()
        assert store.latest() is None

    def test_latest_returns_fresh_result(self):
        store = self._make_store()
        store._latest = _make_result(timedelta(minutes=30))
        result = store.latest()
        assert result is not None
        assert result.is_stale is False

    def test_latest_returns_stale_with_flag(self):
        """Between 90 min and 4 h: returns result with is_stale=True."""
        store = self._make_store()
        store._latest = _make_result(timedelta(minutes=150))
        result = store.latest()
        assert result is not None
        assert result.is_stale is True
        assert len(result.intervals) == 1

    def test_latest_returns_none_when_expired(self):
        """Beyond 4 h: returns None, not stale data."""
        store = self._make_store()
        store._latest = _make_result(timedelta(hours=5))
        assert store.latest() is None

    def test_latest_is_stale_false_after_fresh_save(self):
        """After a fresh save, is_stale should be False."""
        store = self._make_store()
        store._latest = _make_result(timedelta(minutes=30))
        result = store.latest()
        assert result is not None
        assert result.is_stale is False


# ---------------------------------------------------------------------------
# StpasaStore.load() — stale accepted, expired discarded
# ---------------------------------------------------------------------------

class TestStpasaStoreLoad:
    def _make_store(self) -> StpasaStore:
        hass = MagicMock()
        store = StpasaStore.__new__(StpasaStore)
        store._hass = hass
        store._region = "QLD1"
        store._latest = None
        return store

    @pytest.mark.asyncio
    async def test_load_fresh(self):
        store = self._make_store()
        result_obj = _make_result(timedelta(minutes=30))
        data = {
            "region": "QLD1",
            "run_datetime": result_obj.run_datetime,
            "intervals": [
                {
                    "interval_datetime": i.interval_datetime,
                    "run_datetime": i.run_datetime,
                    "demand10": i.demand10,
                    "demand50": i.demand50,
                    "demand90": i.demand90,
                    "surpluscapacity": i.surpluscapacity,
                    "ss_solar_uigf": i.ss_solar_uigf,
                    "ss_wind_uigf": i.ss_wind_uigf,
                }
                for i in result_obj.intervals
            ],
            "fetched_at": result_obj.fetched_at,
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=data)
        store._store = mock_store
        loaded = await store.load()
        assert loaded is not None
        assert loaded.is_stale is False

    @pytest.mark.asyncio
    async def test_load_stale_accepted(self):
        """Stale data (90 min–4 h) should be loaded with is_stale=True."""
        store = self._make_store()
        result_obj = _make_result(timedelta(minutes=150))
        data = {
            "region": "QLD1",
            "run_datetime": result_obj.run_datetime,
            "intervals": [],
            "fetched_at": result_obj.fetched_at,
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=data)
        store._store = mock_store
        loaded = await store.load()
        assert loaded is not None
        assert loaded.is_stale is True

    @pytest.mark.asyncio
    async def test_load_expired_discarded(self):
        """Expired data (>4 h) should return None."""
        store = self._make_store()
        result_obj = _make_result(timedelta(hours=5))
        data = {
            "region": "QLD1",
            "run_datetime": result_obj.run_datetime,
            "intervals": [],
            "fetched_at": result_obj.fetched_at,
        }
        mock_store = AsyncMock()
        mock_store.async_load = AsyncMock(return_value=data)
        store._store = mock_store
        loaded = await store.load()
        assert loaded is None


# ── Missing numerics stay unavailable, not zero (issue #43) ─────────────────

def test_result_from_dict_returns_none_for_absent_numeric_fields():
    """
    A truncated cache payload must read back as None, not 0.0.

    0 MW is a real reading for demand, availability and reserve, so a missing
    field defaulted to 0.0 is indistinguishable from a genuine zero and feeds
    the calibration fit as if it were one. Issue #43.
    """
    from custom_components.nem_pd7day.stpasa_store import _result_from_dict

    result = _result_from_dict({
        "region": "QLD1",
        "run_datetime": "2026-06-16T12:00:00+10:00",
        "fetched_at": "2026-06-16T02:00:00+00:00",
        "intervals": [{
            "interval_datetime": "2026-06-17T04:30:00+10:00",
            "run_datetime": "2026-06-16T12:00:00+10:00",
            "demand50": 6000.0,
        }],
    })

    si = result.intervals[0]
    assert si.demand50 == 6000.0
    for field_name in (
        "demand10", "demand90", "surpluscapacity", "ss_solar_uigf", "ss_wind_uigf"
    ):
        assert getattr(si, field_name) is None, (
            f"{field_name} must be None when absent, not 0.0"
        )


def test_result_from_dict_preserves_a_genuine_zero():
    """A real 0.0 in the payload must survive as 0.0, not become None."""
    from custom_components.nem_pd7day.stpasa_store import _result_from_dict

    result = _result_from_dict({
        "intervals": [{
            "interval_datetime": "2026-06-17T04:30:00+10:00",
            "run_datetime": "2026-06-16T12:00:00+10:00",
            "demand10": 0.0,
            "demand50": 0.0,
            "demand90": 0.0,
            "surpluscapacity": 0.0,
            "ss_solar_uigf": 0.0,
            "ss_wind_uigf": 0.0,
        }],
    })

    si = result.intervals[0]
    assert si.ss_solar_uigf == 0.0
    assert si.ss_solar_uigf is not None


def test_result_from_dict_returns_none_for_unparseable_values():
    """A non-numeric value is missing data, not zero."""
    from custom_components.nem_pd7day.stpasa_store import _result_from_dict

    result = _result_from_dict({
        "intervals": [{
            "interval_datetime": "2026-06-17T04:30:00+10:00",
            "run_datetime": "2026-06-16T12:00:00+10:00",
            "demand50": "",
            "demand10": "n/a",
        }],
    })

    assert result.intervals[0].demand50 is None
    assert result.intervals[0].demand10 is None
