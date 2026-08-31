"""
Tests for the STPASA refresh coordination (issue #37).

Three separate defects are covered here:

1. A stale cache (90 min to 4 h old) was detected, warned about, and then
   ignored. Nothing downstream read the staleness, so no refetch happened and
   the first fresh STPASA of the session waited for the next PD7DAY coordinator
   update, at least 30 s away on the cached startup path.
2. The startup calibration refit, which reads STPASA as an OLS stage-2 feature,
   was queued as a background task immediately and so consumed that stale cache.
3. The only fetch trigger in the integration was registered behind
   `region_startup_index(region) == 0`, meaning QLD1 alone. With the QLD1 entry
   disabled or removed, no region ever fetched STPASA.

The coordination logic lives in stpasa_refresh.py with no homeassistant
imports, so these tests exercise the production code directly rather than a
copy of it. The two source-level guards at the end assert the old shapes are
gone, so reverting the fix cannot leave these tests passing.

Run with:  python -m pytest tests/test_stpasa_refresh.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nem_pd7day.stpasa_refresh import (
    FETCH_FAILED,
    FETCH_FRESH,
    FETCH_TIMEOUT,
    MIN_FETCH_INTERVAL,
    STALE_FETCH_DELAY_S,
    StpasaRefreshCoordination,
    run_refit_when_stpasa_ready,
    should_trigger_central_fetch,
)
from custom_components.nem_pd7day.stpasa_store import StpasaStore

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]


def _stale_payload(region: str, age: timedelta = timedelta(minutes=150)) -> dict:
    """A persisted STPASA payload whose age falls in the stale window."""
    return {
        "region": region,
        "run_datetime": "2026-06-16T12:00:00+10:00",
        "intervals": [],
        "fetched_at": (datetime.now(timezone.utc) - age).isoformat(),
    }


def _make_store(region: str, refresh, payload: dict | None) -> StpasaStore:
    store = StpasaStore.__new__(StpasaStore)
    store._hass = MagicMock()
    store._region = region
    store._latest = None
    store._refresh = refresh
    store.loaded_stale = False
    inner = AsyncMock()
    inner.async_load = AsyncMock(return_value=payload)
    store._store = inner
    return store


# ---------------------------------------------------------------------------
# 1. A stale load must produce a signal that drives a refetch
# ---------------------------------------------------------------------------

class TestStaleLoadDrivesRefetch:
    @pytest.mark.asyncio
    async def test_stale_load_sets_loaded_stale_and_requests_fetch(self):
        """Loading a stale cache must request an immediate fetch, not just warn."""
        refresh = StpasaRefreshCoordination()
        store = _make_store("QLD1", refresh, _stale_payload("QLD1"))

        result = await store.load()

        assert result is not None and result.is_stale is True
        assert store.loaded_stale is True, (
            "load() must record that the cache was stale so async_setup_entry "
            "can force a fetch"
        )
        assert refresh.immediate_fetch_requested is True
        assert refresh.fetch_pending is True, (
            "a stale cache with no fetch outcome yet must leave a fetch pending"
        )
        assert refresh.stale_regions == ("QLD1",)

    @pytest.mark.asyncio
    async def test_fresh_load_requests_nothing(self):
        """A fresh cache must not force a download or defer the refit."""
        refresh = StpasaRefreshCoordination()
        store = _make_store(
            "QLD1", refresh, _stale_payload("QLD1", timedelta(minutes=30))
        )

        result = await store.load()

        assert result is not None and result.is_stale is False
        assert store.loaded_stale is False
        assert refresh.fetch_pending is False

    @pytest.mark.asyncio
    async def test_expired_load_returns_none_not_zeros(self):
        """Past 4 h the cache is dropped, so callers see None, never 0 values."""
        refresh = StpasaRefreshCoordination()
        store = _make_store(
            "QLD1", refresh, _stale_payload("QLD1", timedelta(hours=5))
        )

        assert await store.load() is None
        assert store.latest() is None
        assert refresh.fetch_pending is False

    def test_fetch_pending_clears_once_the_fetch_resolves(self):
        refresh = StpasaRefreshCoordination()
        refresh.note_stale_load("QLD1")
        assert refresh.fetch_pending is True

        refresh.mark_fresh()
        assert refresh.fetch_pending is False
        assert refresh.outcome == FETCH_FRESH


# ---------------------------------------------------------------------------
# 2. The stale warning must be emitted once, not once per region
# ---------------------------------------------------------------------------

class TestStaleWarningEmittedOnce:
    @pytest.mark.asyncio
    async def test_five_region_stores_warn_once(self, caplog):
        """Five per-region stores share one coordination object, so one warning.

        Each config entry builds its own StpasaStore and loads its own persisted
        copy, so before the fix an install with all five regions logged five
        identical warnings, which reads like a loop bug.
        """
        refresh = StpasaRefreshCoordination()
        caplog.set_level(logging.WARNING, logger="custom_components.nem_pd7day.stpasa_store")

        for region in REGIONS:
            store = _make_store(region, refresh, _stale_payload(region))
            await store.load()

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "stale cache" in r.getMessage()
        ]
        assert len(warnings) == 1, (
            f"expected exactly one stale-cache warning across {len(REGIONS)} "
            f"regions, got {len(warnings)}"
        )
        # Every region still contributes to the refetch request.
        assert refresh.stale_regions == tuple(REGIONS)
        assert refresh.fetch_pending is True

    def test_note_stale_load_returns_true_only_for_the_first_caller(self):
        refresh = StpasaRefreshCoordination()
        assert refresh.note_stale_load("QLD1") is True
        assert [refresh.note_stale_load(r) for r in REGIONS[1:]] == [False] * 4


# ---------------------------------------------------------------------------
# 3. The refit must wait for fresh data, and must not wait forever
# ---------------------------------------------------------------------------

class TestRefitGate:
    @pytest.mark.asyncio
    async def test_refit_waits_for_fresh_stpasa_then_runs(self):
        """The refit must not start until the forced fetch delivers fresh data."""
        refresh = StpasaRefreshCoordination()
        refresh.note_stale_load("QLD1")
        events: list[str] = []

        async def _do_refit() -> None:
            events.append("refit")

        task = asyncio.create_task(
            run_refit_when_stpasa_ready(refresh, _do_refit, timeout=5.0)
        )
        # Give the gate a chance to run ahead of the fetch.
        await asyncio.sleep(0.05)
        assert events == [], (
            "the refit ran before STPASA resolved, so it was fitted on the "
            "stale cache"
        )

        events.append("fresh")
        refresh.mark_fresh()
        outcome = await task

        assert outcome == FETCH_FRESH
        assert events == ["fresh", "refit"], (
            f"fresh STPASA must land before the refit, got {events}"
        )

    @pytest.mark.asyncio
    async def test_refit_proceeds_when_the_fetch_fails(self):
        """A failed fetch must release the refit rather than block startup."""
        refresh = StpasaRefreshCoordination()
        refresh.note_stale_load("QLD1")
        calls: list[str] = []

        async def _do_refit() -> None:
            calls.append("refit")

        seen: list[str] = []
        task = asyncio.create_task(
            run_refit_when_stpasa_ready(
                refresh, _do_refit, timeout=5.0, on_outcome=seen.append
            )
        )
        await asyncio.sleep(0.05)
        assert calls == []

        refresh.mark_failed("NEMWEB 403")
        outcome = await task

        assert outcome == FETCH_FAILED
        assert seen == [FETCH_FAILED], (
            "the caller must be told the fetch failed so it can say so in the log"
        )
        assert calls == ["refit"], "the refit must still run after a failed fetch"
        assert refresh.failure_reason == "NEMWEB 403"

    @pytest.mark.asyncio
    async def test_refit_proceeds_after_the_wait_times_out(self):
        """With no outcome at all the refit runs anyway, bounded by the timeout.

        iso_model is not persisted to storage, so an install that never refits is
        worse off than one refitted on a cache up to 4 h old.
        """
        refresh = StpasaRefreshCoordination()
        refresh.note_stale_load("QLD1")
        calls: list[str] = []

        async def _do_refit() -> None:
            calls.append("refit")

        seen: list[str] = []
        outcome = await run_refit_when_stpasa_ready(
            refresh, _do_refit, timeout=0.05, on_outcome=seen.append
        )

        assert outcome == FETCH_TIMEOUT
        assert seen == [FETCH_TIMEOUT]
        assert calls == ["refit"]

    @pytest.mark.asyncio
    async def test_wait_returns_immediately_when_already_resolved(self):
        refresh = StpasaRefreshCoordination()
        refresh.mark_fresh()
        assert await refresh.wait_for_fetch(timeout=0.01) == FETCH_FRESH

    def test_stale_fetch_delay_is_shorter_than_the_refit_wait(self):
        """The forced fetch must fit inside the window the refit is willing to wait."""
        from custom_components.nem_pd7day.stpasa_refresh import REFIT_WAIT_TIMEOUT_S

        assert STALE_FETCH_DELAY_S < REFIT_WAIT_TIMEOUT_S


# ---------------------------------------------------------------------------
# 4. Any loaded region carries the trigger, and it stays one download
# ---------------------------------------------------------------------------

class TestCentralFetchTrigger:
    def test_trigger_fires_when_the_first_startup_region_is_absent(self):
        """With QLD1 not loaded, the remaining regions must still fetch STPASA.

        This is the failure in issue #37: the trigger was gated on
        `region_startup_index(region) == 0`, so disabling the QLD1 entry stopped
        STPASA refreshes for the whole install with no error anywhere.
        """
        loaded = {"NSW1", "VIC1", "SA1", "TAS1"}

        assert should_trigger_central_fetch("NSW1", registered_regions=loaded) is True
        assert any(
            should_trigger_central_fetch(r, registered_regions=loaded) for r in loaded
        )

    def test_trigger_fires_for_a_lone_tasmanian_entry(self):
        assert should_trigger_central_fetch(
            "TAS1", registered_regions={"TAS1"}
        ) is True

    def test_unregistered_region_does_not_trigger(self):
        assert should_trigger_central_fetch(
            "QLD1", registered_regions={"NSW1"}
        ) is False

    def test_five_regions_produce_one_download_per_cycle(self):
        """All five listeners fire, but only the first claims the download.

        The STPASA ZIP holds every region, so five downloads would be five times
        the NEMWEB traffic for identical bytes. This is what keeps the trigger
        broadened to every region from reintroducing them.
        """
        refresh = StpasaRefreshCoordination()
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)

        claims = [refresh.claim_fetch(now + timedelta(seconds=5 * i)) for i in range(5)]

        assert claims == [True, False, False, False, False], (
            f"expected exactly one claim across five regions, got {claims}"
        )

    def test_claim_blocked_while_a_fetch_is_in_flight(self):
        refresh = StpasaRefreshCoordination()
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        assert refresh.claim_fetch(now) is True
        assert refresh.fetch_in_flight is True
        # An hour later, still in flight: no second download.
        assert refresh.claim_fetch(now + timedelta(hours=1)) is False

    def test_next_cycle_can_claim_once_the_window_passes(self):
        refresh = StpasaRefreshCoordination()
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        assert refresh.claim_fetch(now) is True
        refresh.mark_fresh()
        # Inside the suppression window a repeat listener is ignored.
        assert refresh.claim_fetch(now + MIN_FETCH_INTERVAL - timedelta(seconds=1)) is False
        # The next genuine cycle gets through.
        assert refresh.claim_fetch(now + MIN_FETCH_INTERVAL + timedelta(seconds=1)) is True

    def test_failed_fetch_releases_the_in_flight_flag(self):
        refresh = StpasaRefreshCoordination()
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        assert refresh.claim_fetch(now) is True
        refresh.mark_failed("timeout")
        assert refresh.fetch_in_flight is False
        assert refresh.claim_fetch(now + MIN_FETCH_INTERVAL * 2) is True


# ---------------------------------------------------------------------------
# 5. Source guards: the old broken shapes must not come back
# ---------------------------------------------------------------------------

class TestOldBehaviourIsGone:
    def _init_src(self) -> str:
        path = os.path.join(_ROOT, "custom_components", "nem_pd7day", "__init__.py")
        with open(path) as handle:
            return handle.read()

    def test_stpasa_trigger_is_not_gated_on_startup_index_zero(self):
        """The QLD1-only gate must be gone from the STPASA fetch registration."""
        src = self._init_src()
        assert "region_startup_index(region) == 0" not in src, (
            "the STPASA fetch trigger is gated on startup index 0 again, so "
            "disabling the QLD1 entry stops STPASA refreshes (issue #37)"
        )
        assert "should_trigger_central_fetch(" in src

    def test_setup_records_the_fetch_outcome(self):
        """Without mark_fresh/mark_failed a deferred refit would wait for nothing."""
        src = self._init_src()
        assert "mark_fresh()" in src
        assert "mark_failed(" in src
        assert "run_refit_when_stpasa_ready(" in src

    def test_store_acts_on_a_stale_load(self):
        path = os.path.join(
            _ROOT, "custom_components", "nem_pd7day", "stpasa_store.py"
        )
        with open(path) as handle:
            src = handle.read()
        assert "note_stale_load(" in src, (
            "stpasa_store only warns about a stale cache again, so nothing "
            "triggers the refetch"
        )
