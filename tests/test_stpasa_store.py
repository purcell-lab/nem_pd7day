"""
StpasaStore TTL and stale-cache behaviour, and the StpasaRefreshCoordination
that turns a stale load into one shared refetch.

Cache TTL: 90 minutes fresh, up to 4 hours stale (is_stale=True), then
discarded. Missing numerics in a cached payload stay None, not 0.0 (issue
#43): 0 MW is a real reading for demand, availability and reserve, so a
truncated payload defaulted to zero would feed the calibration fit as if it
were one.

Refresh coordination (issue #37) fixes three defects at once:
  1. A stale cache was detected, warned about, and then ignored. Nothing
     downstream read the staleness, so no refetch happened and the first
     fresh STPASA of the session waited for the next PD7DAY coordinator
     update, at least 30 s away on the cached startup path.
  2. The startup calibration refit, which reads STPASA as an OLS stage-2
     feature, was queued immediately and so consumed that stale cache.
  3. The only fetch trigger was registered behind
     ``region_startup_index(region) == 0``, meaning QLD1 alone. With the QLD1
     entry disabled or removed, no region ever fetched STPASA.
The coordination logic lives in stpasa_refresh.py with no homeassistant
imports, so the production code is exercised directly. The source guards at
the end assert the old shapes are gone, so reverting the fix cannot leave
these tests passing.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from support import PKG_DIR, install_ha_stubs, load_chain, run_async

install_ha_stubs()

(
    _const,
    _nem_time,
    _executor,
    _retry,
    _client_mod,
    _refresh_mod,
    _store_mod,
) = load_chain(
    "const",
    "nem_time",
    "executor",
    "nemweb_retry",
    "stpasa_client",
    "stpasa_refresh",
    "stpasa_store",
)

StpasaInterval = _client_mod.StpasaInterval
StpasaResult = _client_mod.StpasaResult
StpasaStore = _store_mod.StpasaStore
STPASA_CACHE_TTL = _store_mod.STPASA_CACHE_TTL
STAPASA_STALE_TTL = _store_mod.STAPASA_STALE_TTL
StpasaRefreshCoordination = _refresh_mod.StpasaRefreshCoordination
FETCH_FRESH = _refresh_mod.FETCH_FRESH
FETCH_FAILED = _refresh_mod.FETCH_FAILED
FETCH_TIMEOUT = _refresh_mod.FETCH_TIMEOUT
MIN_FETCH_INTERVAL = _refresh_mod.MIN_FETCH_INTERVAL

REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]

FRESH = timedelta(minutes=30)
STALE = timedelta(minutes=150)
EXPIRED = timedelta(hours=5)


# ── Builders ──────────────────────────────────────────────────────────────────

def _fetched_at(age: timedelta) -> str:
    return (datetime.now(timezone.utc) - age).isoformat()


def make_result(age: timedelta, region: str = "QLD1") -> StpasaResult:
    """A one-interval StpasaResult with fetched_at = now - age."""
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
        region=region,
        run_datetime="2026-06-16T12:00:00+10:00",
        intervals=[interval],
        fetched_at=_fetched_at(age),
    )


def make_stpasa_store(
    region: str = "QLD1",
    *,
    refresh: StpasaRefreshCoordination | None = None,
    payload: dict | None = None,
    latest: StpasaResult | None = None,
) -> StpasaStore:
    """A StpasaStore built with __new__ whose HA Store loads ``payload``.

    Candidate for support.py alongside make_store.
    """
    store = StpasaStore.__new__(StpasaStore)
    store._hass = MagicMock()
    store._region = region
    store._latest = latest
    store._refresh = refresh
    store.loaded_stale = False
    inner = AsyncMock()
    inner.async_load = AsyncMock(return_value=payload)
    store._store = inner
    return store


# ── _cache_status / _is_fresh ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("fetched_at", "expected"),
    [
        pytest.param(FRESH, "fresh", id="30min"),
        pytest.param(STPASA_CACHE_TTL - timedelta(seconds=5), "fresh", id="just-inside-fresh-ttl"),
        pytest.param(timedelta(minutes=120), "stale", id="120min"),
        pytest.param(STAPASA_STALE_TTL - timedelta(seconds=5), "stale", id="just-inside-stale-ttl"),
        pytest.param(STAPASA_STALE_TTL + timedelta(minutes=1), "expired", id="past-stale-ttl"),
        pytest.param("", "expired", id="empty-string"),
        pytest.param("not-a-date", "expired", id="unparseable"),
    ],
)
def test_cache_status(fetched_at, expected):
    if isinstance(fetched_at, timedelta):
        fetched_at = _fetched_at(fetched_at)
    assert _store_mod._cache_status(fetched_at) == expected


@pytest.mark.parametrize(
    ("age", "expected"),
    [pytest.param(FRESH, True, id="fresh"), pytest.param(timedelta(minutes=120), False, id="stale")],
)
def test_is_fresh(age, expected):
    assert _store_mod._is_fresh(_fetched_at(age)) is expected


# ── StpasaStore.latest() ──────────────────────────────────────────────────────

def test_latest_none_when_no_data():
    assert make_stpasa_store().latest() is None


@pytest.mark.parametrize(
    ("age", "is_stale"),
    [
        pytest.param(FRESH, False, id="fresh"),
        pytest.param(STALE, True, id="stale-90min-to-4h"),
    ],
)
def test_latest_flags_staleness(age, is_stale):
    store = make_stpasa_store(latest=make_result(age))
    result = store.latest()
    assert result is not None
    assert result.is_stale is is_stale
    assert len(result.intervals) == 1


def test_latest_returns_none_when_expired():
    """Beyond 4 h: None, not stale data."""
    assert make_stpasa_store(latest=make_result(EXPIRED)).latest() is None


# ── _result_from_dict: missing numerics stay None (issue #43) ─────────────────

def test_result_from_dict_returns_none_for_absent_numeric_fields():
    """A truncated cache payload must read back as None, not 0."""
    result = _store_mod._result_from_dict({
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
    for field_name in ("demand10", "demand90", "surpluscapacity", "ss_solar_uigf", "ss_wind_uigf"):
        assert getattr(si, field_name) is None, f"{field_name} must be None when absent, not 0.0"


def test_result_from_dict_preserves_a_genuine_zero():
    """A real 0.0 in the payload must survive as 0.0, not become None."""
    result = _store_mod._result_from_dict({
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
    result = _store_mod._result_from_dict({
        "intervals": [{
            "interval_datetime": "2026-06-17T04:30:00+10:00",
            "run_datetime": "2026-06-16T12:00:00+10:00",
            "demand50": "",
            "demand10": "n/a",
        }],
    })

    assert result.intervals[0].demand50 is None
    assert result.intervals[0].demand10 is None


# ── StpasaStore.load(): stale accepted and acted on, expired discarded ────────

def test_stale_load_sets_loaded_stale_and_requests_fetch():
    """Loading a stale cache must request an immediate fetch, not just warn."""
    refresh = StpasaRefreshCoordination()
    store = make_stpasa_store(refresh=refresh, payload=asdict(make_result(STALE)))

    result = run_async(store.load())

    assert result is not None and result.is_stale is True
    assert len(result.intervals) == 1
    assert store.loaded_stale is True, (
        "load() must record that the cache was stale so async_setup_entry can force a fetch"
    )
    assert refresh.immediate_fetch_requested is True
    assert refresh.fetch_pending is True, "a stale cache with no fetch outcome yet must leave a fetch pending"
    assert refresh.stale_regions == ("QLD1",)


def test_fresh_load_requests_nothing():
    """A fresh cache must not force a download or defer the refit."""
    refresh = StpasaRefreshCoordination()
    store = make_stpasa_store(refresh=refresh, payload=asdict(make_result(FRESH)))

    result = run_async(store.load())

    assert result is not None and result.is_stale is False
    assert store.loaded_stale is False
    assert refresh.fetch_pending is False


def test_expired_load_returns_none_not_zeros():
    """Past 4 h the cache is dropped, so callers see None, never 0 values."""
    refresh = StpasaRefreshCoordination()
    store = make_stpasa_store(refresh=refresh, payload=asdict(make_result(EXPIRED)))

    assert run_async(store.load()) is None
    assert store.latest() is None
    assert refresh.fetch_pending is False


def test_fetch_pending_clears_once_the_fetch_resolves():
    refresh = StpasaRefreshCoordination()
    refresh.note_stale_load("QLD1")
    assert refresh.fetch_pending is True

    refresh.mark_fresh()
    assert refresh.fetch_pending is False
    assert refresh.outcome == FETCH_FRESH


# ── The stale warning is emitted once, not once per region ───────────────────

def test_five_region_stores_warn_once(caplog):
    """Each config entry builds its own StpasaStore and loads its own persisted
    copy, so before the fix an install with all five regions logged five
    identical warnings, which reads like a loop bug."""
    refresh = StpasaRefreshCoordination()
    caplog.set_level(logging.WARNING, logger=_store_mod._LOGGER.name)

    async def load_all():
        for region in REGIONS:
            await make_stpasa_store(region, refresh=refresh, payload=asdict(make_result(STALE, region))).load()

    run_async(load_all())

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "stale cache" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected exactly one stale-cache warning across {len(REGIONS)} regions, got {len(warnings)}"
    )
    # Every region still contributes to the refetch request.
    assert refresh.stale_regions == tuple(REGIONS)
    assert refresh.fetch_pending is True


def test_note_stale_load_returns_true_only_for_the_first_caller():
    refresh = StpasaRefreshCoordination()
    assert refresh.note_stale_load("QLD1") is True
    assert [refresh.note_stale_load(r) for r in REGIONS[1:]] == [False] * 4


# ── The refit waits for fresh data, and does not wait forever ────────────────

def test_refit_waits_for_fresh_stpasa_then_runs():
    """The refit must not start until the forced fetch delivers fresh data."""
    events: list[str] = []

    async def scenario():
        refresh = StpasaRefreshCoordination()
        refresh.note_stale_load("QLD1")

        async def _do_refit() -> None:
            events.append("refit")

        task = asyncio.create_task(
            _refresh_mod.run_refit_when_stpasa_ready(refresh, _do_refit, timeout=5.0)
        )
        # Give the gate a chance to run ahead of the fetch.
        await asyncio.sleep(0.05)
        assert events == [], "the refit ran before STPASA resolved, so it was fitted on the stale cache"

        events.append("fresh")
        refresh.mark_fresh()
        return await task

    outcome = run_async(scenario())

    assert outcome == FETCH_FRESH
    assert events == ["fresh", "refit"], f"fresh STPASA must land before the refit, got {events}"


def test_refit_proceeds_when_the_fetch_fails():
    """A failed fetch must release the refit rather than block startup."""
    calls: list[str] = []
    seen: list[str] = []

    async def scenario():
        refresh = StpasaRefreshCoordination()
        refresh.note_stale_load("QLD1")

        async def _do_refit() -> None:
            calls.append("refit")

        task = asyncio.create_task(
            _refresh_mod.run_refit_when_stpasa_ready(
                refresh, _do_refit, timeout=5.0, on_outcome=seen.append
            )
        )
        await asyncio.sleep(0.05)
        assert calls == []

        refresh.mark_failed("NEMWEB 403")
        return await task, refresh

    outcome, refresh = run_async(scenario())

    assert outcome == FETCH_FAILED
    assert seen == [FETCH_FAILED], "the caller must be told the fetch failed so it can say so in the log"
    assert calls == ["refit"], "the refit must still run after a failed fetch"
    assert refresh.failure_reason == "NEMWEB 403"


def test_refit_proceeds_after_the_wait_times_out():
    """With no outcome at all the refit runs anyway, bounded by the timeout.
    iso_model is not persisted to storage, so an install that never refits is
    worse off than one refitted on a cache up to 4 h old."""
    calls: list[str] = []
    seen: list[str] = []

    async def scenario():
        refresh = StpasaRefreshCoordination()
        refresh.note_stale_load("QLD1")

        async def _do_refit() -> None:
            calls.append("refit")

        return await _refresh_mod.run_refit_when_stpasa_ready(
            refresh, _do_refit, timeout=0.05, on_outcome=seen.append
        )

    outcome = run_async(scenario())

    assert outcome == FETCH_TIMEOUT
    assert seen == [FETCH_TIMEOUT]
    assert calls == ["refit"]


def test_wait_returns_immediately_when_already_resolved():
    async def scenario():
        refresh = StpasaRefreshCoordination()
        refresh.mark_fresh()
        return await refresh.wait_for_fetch(timeout=0.01)

    assert run_async(scenario()) == FETCH_FRESH


def test_stale_fetch_delay_is_shorter_than_the_refit_wait():
    """The forced fetch must fit inside the window the refit is willing to wait."""
    assert _refresh_mod.STALE_FETCH_DELAY_S < _refresh_mod.REFIT_WAIT_TIMEOUT_S


# ── Any loaded region carries the trigger, and it stays one download ─────────

def test_trigger_fires_when_the_first_startup_region_is_absent():
    """With QLD1 not loaded, the remaining regions must still fetch STPASA.
    This is the failure in issue #37: the trigger was gated on
    `region_startup_index(region) == 0`, so disabling the QLD1 entry stopped
    STPASA refreshes for the whole install with no error anywhere."""
    should_trigger = _refresh_mod.should_trigger_central_fetch
    loaded = {"NSW1", "VIC1", "SA1", "TAS1"}

    assert should_trigger("NSW1", registered_regions=loaded) is True
    assert any(should_trigger(r, registered_regions=loaded) for r in loaded)
    assert should_trigger("TAS1", registered_regions={"TAS1"}) is True
    assert should_trigger("QLD1", registered_regions={"NSW1"}) is False


def test_five_regions_produce_one_download_per_cycle():
    """All five listeners fire, but only the first claims the download. The
    STPASA ZIP holds every region, so five downloads would be five times the
    NEMWEB traffic for identical bytes."""
    refresh = StpasaRefreshCoordination()
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)

    claims = [refresh.claim_fetch(now + timedelta(seconds=5 * i)) for i in range(5)]

    assert claims == [True, False, False, False, False], (
        f"expected exactly one claim across five regions, got {claims}"
    )


def test_claim_blocked_while_a_fetch_is_in_flight():
    refresh = StpasaRefreshCoordination()
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert refresh.claim_fetch(now) is True
    assert refresh.fetch_in_flight is True
    # An hour later, still in flight: no second download.
    assert refresh.claim_fetch(now + timedelta(hours=1)) is False


def test_next_cycle_can_claim_once_the_window_passes():
    refresh = StpasaRefreshCoordination()
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert refresh.claim_fetch(now) is True
    refresh.mark_fresh()
    # Inside the suppression window a repeat listener is ignored.
    assert refresh.claim_fetch(now + MIN_FETCH_INTERVAL - timedelta(seconds=1)) is False
    # The next genuine cycle gets through.
    assert refresh.claim_fetch(now + MIN_FETCH_INTERVAL + timedelta(seconds=1)) is True


def test_failed_fetch_releases_the_in_flight_flag():
    refresh = StpasaRefreshCoordination()
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    assert refresh.claim_fetch(now) is True
    refresh.mark_failed("timeout")
    assert refresh.fetch_in_flight is False
    assert refresh.claim_fetch(now + MIN_FETCH_INTERVAL * 2) is True


# ── Source guards: the old broken shapes must not come back (issue #37) ──────

def _source(name: str) -> str:
    with open(os.path.join(PKG_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def test_stpasa_trigger_is_not_gated_on_startup_index_zero():
    """The QLD1-only gate must be gone from the STPASA fetch registration."""
    src = _source("__init__.py")
    assert "region_startup_index(region) == 0" not in src, (
        "the STPASA fetch trigger is gated on startup index 0 again, so "
        "disabling the QLD1 entry stops STPASA refreshes (issue #37)"
    )
    assert "should_trigger_central_fetch(" in src


def test_setup_records_the_fetch_outcome():
    """Without mark_fresh/mark_failed a deferred refit would wait for nothing."""
    src = _source("__init__.py")
    assert "mark_fresh()" in src
    assert "mark_failed(" in src
    assert "run_refit_when_stpasa_ready(" in src


def test_store_acts_on_a_stale_load():
    assert "note_stale_load(" in _source("stpasa_store.py"), (
        "stpasa_store only warns about a stale cache again, so nothing triggers the refetch"
    )
