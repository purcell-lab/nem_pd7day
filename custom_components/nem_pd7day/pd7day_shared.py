"""
Shared PD7DAY fetch: one download and one parse per cycle, served to all regions.

Why this exists
---------------
Every configured region gets its own ``PD7DayCoordinator``, and each one used to
download and parse the PD7DAY archive independently. The archive is the same
file for all of them: it holds every NEM region and every interconnector. On a
five-region install that meant five downloads of ~4.6 MB and five parses of the
same ~45 MB CSV per cycle.

Measured against ``PUBLIC_PD7DAY_20260814174110`` (4.61 MB compressed,
45.43 MB expanded, 329,505 lines):

  * parsing one region plus its interconnectors ...... 631 ms
  * parsing all five regions plus all interconnectors  700 ms

So five single-region parses cost about 3,154 ms of CPU per cycle, while one
all-region parse costs about 700 ms and yields strictly more data. This module
does the second thing and hands each coordinator a filtered view.

How sharing is decided
----------------------
Two mechanisms, in order:

1. **Burst window.** All five coordinators are triggered by the same events:
   the staggered background refreshes at startup (30 s to 50 s apart) and the
   three scheduled fetches, where every entry registers its own timer for the
   same instant. A short window absorbs that fan-out without a network request.

2. **Newest-filename check.** Once the window has passed, the directory listing
   is re-read and the newest filename compared against the parse already held.
   AEMO publishes PD7DAY roughly three times a day, so a caller arriving hours
   later usually finds the same file and can reuse the existing parse rather
   than re-downloading and re-parsing bytes it has already seen. A genuinely new
   publication is picked up immediately, with no dependence on window length.

The listing request is small (~39 KB) and every coordinator already made one on
every refresh before this change, so mechanism 2 costs nothing new.

Concurrency
-----------
A double-checked lock serialises callers. The first through does the work; the
rest re-check and find a fresh result. Failures are deliberately not cached: the
exception propagates and the lock is released with the previous state intact, so
the caller's own retry (see ``PD7DayCoordinator._fetch_all_with_retry``) still
does real work.

Interface
---------
``SharedPD7DayFetch.fetch_all(regions, interconnector_ids)`` matches
``PD7DayClient.fetch_all``, so the coordinator treats it as a drop-in client and
keeps its existing 403 retry and stale-data fallback unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Callable

from .const import REGION_INTERCONNECTORS, REGIONS, interconnectors_for_regions
from .pd7day_client import PD7DayClient, PD7DayResult

_LOGGER = logging.getLogger(__name__)

# Every interconnector referenced by any region, so one parse satisfies any
# caller's subset and the shared result never needs re-parsing for coverage.
ALL_INTERCONNECTORS: set[str] = interconnectors_for_regions(
    list(REGION_INTERCONNECTORS)
)

# Seconds during which an existing parse is reused without even re-reading the
# directory listing. Sized to cover the widest observed fan-out: the staggered
# startup refreshes span 20 s (30 s to 50 s after setup), and the scheduled
# fetches fire within milliseconds of each other. 60 s covers both with margin.
# Correctness does not rest on this number: once it lapses, the newest-filename
# check takes over and a new publication is still picked up immediately.
BURST_WINDOW_S = 60.0


@dataclass
class SharedFetchStats:
    """Counters describing how the shared result was served.

    Exposed for tests and diagnostics. ``downloads`` is the number that matters:
    it must not scale with the number of configured regions.
    """

    listings: int = 0
    downloads: int = 0
    burst_hits: int = 0
    same_file_hits: int = 0

    @property
    def served(self) -> int:
        """Total calls served, however they were satisfied."""
        return self.downloads + self.burst_hits + self.same_file_hits


def result_for_regions(
    full: PD7DayResult,
    regions: list[str],
    interconnector_ids: set[str] | None = None,
) -> PD7DayResult:
    """
    Narrow an all-region result to the regions and interconnectors a caller wants.

    The coordinator ingests every region present in ``prices`` into its own
    single-region calibration store, so handing it the unfiltered result would
    cross-contaminate regions. Filtering here preserves the exact shape each
    coordinator saw before the fetch was centralised.

    ``PD7DayData`` and ``InterconnectorData`` values are shared by reference
    rather than copied: they are treated as read-only once parsed, and copying
    the forecast lists for five callers would give back much of the CPU this
    module exists to save.
    """
    if interconnector_ids is None:
        interconnector_ids = interconnectors_for_regions(regions)
    wanted = set(regions)
    return PD7DayResult(
        source_file=full.source_file,
        case=full.case,
        prices={r: d for r, d in full.prices.items() if r in wanted},
        market_summary=full.market_summary,
        interconnectors={
            ic: d for ic, d in full.interconnectors.items() if ic in interconnector_ids
        },
        updated_at=full.updated_at,
    )


class SharedPD7DayFetch:
    """One PD7DAY download and parse per cycle, shared across region coordinators."""

    def __init__(
        self,
        client: PD7DayClient,
        burst_window_s: float = BURST_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._burst_window_s = burst_window_s
        self._clock = clock
        self._lock = asyncio.Lock()
        self._full: PD7DayResult | None = None
        self._confirmed_at: float | None = None
        self.stats = SharedFetchStats()

    # ── Public interface (mirrors PD7DayClient.fetch_all) ────────────────────

    async def fetch_all(
        self,
        regions: list[str],
        interconnector_ids: set[str] | None = None,
    ) -> PD7DayResult:
        """Return PD7DAY data for ``regions``, sharing one fetch across callers."""
        full = await self._current_full()
        return result_for_regions(full, regions, interconnector_ids)

    # ── Shared state ────────────────────────────────────────────────────────

    def _within_burst_window(self) -> bool:
        if self._full is None or self._confirmed_at is None:
            return False
        return (self._clock() - self._confirmed_at) < self._burst_window_s

    def _reconfirm(self) -> PD7DayResult:
        """Mark the held parse as current and stamp it with the time of confirming.

        ``updated_at`` records when this data was last confirmed to be the newest
        published file, not when it was downloaded. ``ForecastStore`` uses it to
        decide whether the cache on disk is worth restoring after a restart, so
        it has to advance whenever the data is re-confirmed, otherwise reusing a
        parse would make a genuinely current forecast look stale.
        """
        from .nem_time import now_nem, to_nem_iso

        assert self._full is not None
        self._full = replace(self._full, updated_at=to_nem_iso(now_nem()))
        self._confirmed_at = self._clock()
        return self._full

    async def _current_full(self) -> PD7DayResult:
        """Return an all-region result, downloading and parsing at most once."""
        if self._within_burst_window():
            self.stats.burst_hits += 1
            return self._full  # type: ignore[return-value]

        async with self._lock:
            # Re-check: another caller may have completed while we waited.
            if self._within_burst_window():
                self.stats.burst_hits += 1
                return self._full  # type: ignore[return-value]

            file_meta = await self._client.newest_file()
            self.stats.listings += 1

            if self._full is not None and file_meta["name"] == self._full.source_file:
                self.stats.same_file_hits += 1
                _LOGGER.debug(
                    "PD7DAY: newest file is still %s, reusing the existing parse",
                    file_meta["name"],
                )
                return self._reconfirm()

            full = await self._client.fetch_all(
                REGIONS, ALL_INTERCONNECTORS, file_meta=file_meta
            )
            self.stats.downloads += 1
            self._full = full
            self._confirmed_at = self._clock()
            _LOGGER.debug(
                "PD7DAY: downloaded and parsed %s: %d regions, %d interconnectors",
                full.source_file,
                len(full.prices),
                len(full.interconnectors),
            )
            return full
