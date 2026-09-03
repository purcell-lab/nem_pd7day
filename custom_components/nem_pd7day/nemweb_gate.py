"""Shared NEMWEB request gate: concurrency cap plus a minimum request gap.

Why this exists
---------------
NEMWEB sits behind Akamai and answers 403 Forbidden, not 429, when it decides
a caller is asking too often. Investigation under issue #22 found that from an
unaffected IP every User-Agent returns 200, so the worst 403s are rate and IP
based rather than User-Agent based. The User-Agent added in #20 is necessary
hygiene but does not address the rate.

The integration already capped *concurrency* at
``NEMWEB_MAX_CONCURRENT_REQUESTS = 2`` through a shared semaphore. Concurrency
is not frequency: two slots turning over quickly can still issue dozens of
requests a second, which is exactly what a directory listing followed by a
batch of file fetches does. This gate adds the missing frequency bound, a
minimum interval between the *starts* of successive NEMWEB requests, applied
globally across all five region coordinators and every report type.

Why it is a drop-in for the semaphore
-------------------------------------
Every NEMWEB client in this integration already takes a ``semaphore`` and uses
it only as ``async with``. This object satisfies that protocol, so it replaces
the shared semaphore in ``hass.data`` without touching a single client
constructor. Clients that are handed nothing still fall back to their own
private semaphore or a nullcontext, as before.

Why it cannot raise the steady-state request rate
-------------------------------------------------
A minimum gap only ever delays a request. It never issues one. Steady state is
a handful of fetches three times a day per report type, spaced far wider than
the gap, so in normal operation the gate is a no-op that adds no measurable
latency. It only takes effect during a burst, which is the case that provokes
the 403 in the first place.

The pacing lock is held across the wait deliberately. Releasing it before
sleeping would let every waiting task compute the same target time and wake
together, which is the thundering herd the gap exists to prevent.

What the gate does not cover
----------------------------
The DispatchIS fallback in ``dispatch_client._fetch_dispatchis`` fetches a
NEMWEB directory listing and a zip through ``urllib`` on an executor thread,
because that module has no aiohttp and runs synchronously. It cannot take an
async context manager, so it sits outside this gate and outside the request
gap. It is meant to be rare, a fallback for when ELEC_NEM_SUMMARY is down or
stale, so in normal operation it adds nothing to the rate; but anyone
auditing the NEMWEB request budget should not read this gate as covering
every path (issue #110).
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class NemwebGate:
    """Async context manager bounding both concurrency and request frequency.

    ``max_concurrent`` requests may be in flight at once, and successive
    acquisitions are spaced at least ``min_gap_s`` apart. ``clock`` and
    ``sleep`` are injectable so the pacing can be tested without spending real
    time.
    """

    def __init__(
        self,
        max_concurrent: int,
        min_gap_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._min_gap = max(0.0, float(min_gap_s))
        self._clock = clock
        self._sleep = sleep
        self._pace_lock = asyncio.Lock()
        # Monotonic time before which the next request must not start. Zero
        # means the gate has never been used, so the first request is immediate.
        self._next_allowed: float = 0.0
        # Diagnostics only. Counting these is how a future investigation can
        # tell "we were throttled by NEMWEB" apart from "we throttled
        # ourselves", which the semaphore alone could never express.
        self.acquisitions: int = 0
        self.paced_waits: int = 0
        self.total_paced_wait_s: float = 0.0

    @property
    def min_gap_s(self) -> float:
        return self._min_gap

    async def __aenter__(self) -> "NemwebGate":
        await self._semaphore.acquire()
        try:
            await self._pace()
        except BaseException:
            # Never leak a slot if pacing is cancelled mid-wait, or the gate
            # would silently narrow itself on every cancelled refresh.
            self._semaphore.release()
            raise
        self.acquisitions += 1
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        self._semaphore.release()
        return False

    async def _pace(self) -> None:
        """Wait until the minimum gap since the previous request has elapsed."""
        if self._min_gap <= 0.0:
            return
        async with self._pace_lock:
            now = self._clock()
            wait = self._next_allowed - now
            if wait > 0.0:
                self.paced_waits += 1
                self.total_paced_wait_s += wait
                await self._sleep(wait)
                now = self._clock()
            # max() so a long idle period does not bank credit for a burst.
            self._next_allowed = max(now, self._next_allowed) + self._min_gap

    def diagnostics(self) -> dict[str, float | int]:
        """Counters for the diagnostics payload and for tests."""
        return {
            "min_gap_s": self._min_gap,
            "acquisitions": self.acquisitions,
            "paced_waits": self.paced_waits,
            "total_paced_wait_s": round(self.total_paced_wait_s, 3),
        }
