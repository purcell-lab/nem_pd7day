"""
NEM PD7DAY STPASA refresh coordination
======================================
Cross region state for the single shared STPASA download.

The STPASA ZIP published by AEMO holds every NEM region, so one download
populates all five region stores. That made the fetch trigger a special case:
before v3.1.7 it was registered only on the config entry whose startup index
was 0, meaning QLD1. If the QLD1 entry was disabled, removed, or simply slow to
set up, nothing fetched STPASA at all and every region kept serving whatever
was on disk (issue #37).

This module owns the small amount of state needed to solve that without
reintroducing five downloads per cycle:

- which region carries the trigger: any loaded region does, so removing QLD1
  changes nothing,
- how the download stays single: the first listener to fire claims the fetch and
  the others are suppressed for MIN_FETCH_INTERVAL,
- whether a stale cache was loaded at startup, which forces an immediate fetch
  instead of only logging a warning,
- whether the startup calibration refit may run yet, because a refit that reads
  a cache older than the 90 minute fresh TTL bakes stale STPASA into the OLS
  stage-2 model for the rest of the cycle.

There are no homeassistant imports here on purpose, so all of the above is
directly unit testable rather than reachable only through async_setup_entry.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)

# hass.data[DOMAIN] key for the shared StpasaRefreshCoordination instance.
STPASA_REFRESH_KEY = "stpasa_refresh"

# A listener is registered on every loaded region, so the same PD7DAY update
# cycle can call in up to five times. The staggered background refreshes at
# startup are 30 s to 50 s apart and the scheduled fetches fire together, so a
# 10 minute claim window collapses a cycle to one download while still allowing
# the next genuine cycle through. STPASA itself is republished roughly every
# 2 hours, so nothing useful is lost by suppressing repeats inside 10 minutes.
MIN_FETCH_INTERVAL = timedelta(minutes=10)

# Give the remaining config entries time to register their stores before the
# forced stale-cache fetch runs, so one download still refreshes all five
# regions rather than only the entry that happened to notice the stale cache.
# Entries set up concurrently, and the measured spread on a cached startup is
# under a second, so 5 s is generous.
STALE_FETCH_DELAY_S = 5.0

# Upper bound on how long the startup refit waits for fresh STPASA. Two minutes
# covers STALE_FETCH_DELAY_S plus a NEMWEB download that is queued behind the
# shared semaphore. Past that we refit anyway: iso_model is not persisted, so
# never refitting is worse than refitting on a cache up to 4 hours old.
REFIT_WAIT_TIMEOUT_S = 120.0

FETCH_FRESH = "fresh"
FETCH_FAILED = "failed"
FETCH_TIMEOUT = "timeout"


def should_trigger_central_fetch(
    region: str, *, registered_regions: Iterable[str]
) -> bool:
    """Return True when *region* should carry the shared STPASA fetch trigger.

    Any region that has registered its store qualifies. This replaces the old
    `region_startup_index(region) == 0` test, which bound the only fetch trigger
    in the integration to QLD1 and so stopped STPASA refreshes entirely when the
    QLD1 entry was not loaded. Keeping the download to one per cycle is the job
    of claim_fetch, not of privileging one region.
    """
    return region in set(registered_regions)


class StpasaRefreshCoordination:
    """Shared, per-install state for the one STPASA fetch."""

    def __init__(self, min_fetch_interval: timedelta = MIN_FETCH_INTERVAL) -> None:
        self._min_fetch_interval = min_fetch_interval
        self._last_claim_at: datetime | None = None
        self._fetch_in_flight = False
        self._stale_warned = False
        self._stale_regions: list[str] = []
        self._immediate_fetch_requested = False
        self._outcome: str | None = None
        self._failure_reason: str | None = None
        self._done = asyncio.Event()

    # ── stale cache handling ────────────────────────────────────────────────

    def note_stale_load(self, region: str) -> bool:
        """Record that *region* loaded a stale cache; return True to warn.

        Returns True only for the first region, because the store is built per
        config entry and every one of the five loads its own persisted copy.
        Five identical warnings read like a loop bug, so only the first speaks.
        Every call still requests the immediate fetch.
        """
        self._stale_regions.append(region)
        self._immediate_fetch_requested = True
        if self._stale_warned:
            return False
        self._stale_warned = True
        return True

    @property
    def stale_regions(self) -> tuple[str, ...]:
        """Regions that loaded a stale cache, in load order."""
        return tuple(self._stale_regions)

    @property
    def immediate_fetch_requested(self) -> bool:
        """True once any region has loaded a stale cache."""
        return self._immediate_fetch_requested

    @property
    def fetch_pending(self) -> bool:
        """True when an immediate fetch is wanted and has not yet resolved."""
        return self._immediate_fetch_requested and self._outcome is None

    # ── single-flight fetch claim ───────────────────────────────────────────

    def claim_fetch(self, now: datetime | None = None) -> bool:
        """Try to claim the shared download. True means the caller must fetch.

        False means another region already has this cycle, so the caller returns
        without touching NEMWEB.
        """
        moment = now or datetime.now(timezone.utc)
        if self._fetch_in_flight:
            return False
        if (
            self._last_claim_at is not None
            and moment - self._last_claim_at < self._min_fetch_interval
        ):
            return False
        self._last_claim_at = moment
        self._fetch_in_flight = True
        return True

    @property
    def fetch_in_flight(self) -> bool:
        return self._fetch_in_flight

    def mark_fresh(self) -> None:
        """Record that a fetch delivered fresh STPASA and release any waiters."""
        self._fetch_in_flight = False
        self._failure_reason = None
        self._outcome = FETCH_FRESH
        self._done.set()

    def mark_failed(self, reason: str | None = None) -> None:
        """Record that a fetch definitively failed and release any waiters.

        Failure is not fatal: the waiting refit proceeds on the stale cache
        rather than blocking startup for as long as NEMWEB stays unhappy.
        """
        self._fetch_in_flight = False
        self._failure_reason = reason
        self._outcome = FETCH_FAILED
        self._done.set()

    @property
    def outcome(self) -> str | None:
        """FETCH_FRESH, FETCH_FAILED, or None while no fetch has resolved."""
        return self._outcome

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    # ── refit gating ────────────────────────────────────────────────────────

    async def wait_for_fetch(self, timeout: float = REFIT_WAIT_TIMEOUT_S) -> str:
        """Wait for a fetch outcome, returning FETCH_TIMEOUT if none arrives."""
        if self._outcome is not None:
            return self._outcome
        try:
            await asyncio.wait_for(self._done.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return FETCH_TIMEOUT
        return self._outcome or FETCH_TIMEOUT


async def run_refit_when_stpasa_ready(
    refresh: StpasaRefreshCoordination,
    do_refit: Callable[[], Awaitable[None]],
    *,
    timeout: float = REFIT_WAIT_TIMEOUT_S,
    on_outcome: Callable[[str], None] | None = None,
) -> str:
    """Hold the startup calibration refit until STPASA has resolved, then refit.

    The refit is the most expensive calibration work the integration does and it
    reads STPASA as an OLS stage-2 feature, so running it against a cache that
    is past the 90 minute fresh TTL is the single worst place to consume stale
    data. Waiting is bounded in every direction: a fresh fetch releases it
    immediately, a failed fetch releases it immediately, and *timeout* releases
    it regardless. The refit always runs in the end, because iso_model is not
    persisted and an install with no fitted model is worse off than one fitted
    on a cache up to 4 hours old.

    Returns the outcome so the caller can log which of the three paths was taken.
    """
    outcome = await refresh.wait_for_fetch(timeout)
    if on_outcome is not None:
        on_outcome(outcome)
    await do_refit()
    return outcome
