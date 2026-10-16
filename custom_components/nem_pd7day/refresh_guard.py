"""One refresh at a time, and a bound on how long one may hold the slot (#256).

The binding constraints and pre-dispatch coordinators have no timer of their
own: each refreshes when a dispatch poll notifies it, and a notification that
arrives while a refresh is running is dropped, since the running one reads the
same newest file. On 15 Oct 2026 one refresh never returned, so every later
notification was dropped and all five region constraint sensors froze, with
nothing logged, until the config entries were reloaded.

The NEMWEB fetches now carry explicit timeouts, so a refresh should end within
about five minutes even when every attempt times out. The guard is the backstop
for whatever else can hang: a notification that finds a refresh running for
longer than STUCK_AFTER_S cancels it, logs a warning and starts a new one.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

# Three attempts at a 30 s listing and a 60 s zip, with backoff, is about five
# minutes; twice that is not a slow refresh but a stuck one.
STUCK_AFTER_S = 600.0


class RefreshGuard:
    """Starts ``refresh`` as a background task, at most one at a time."""

    def __init__(
        self,
        hass: Any,
        name: str,
        refresh: Callable[[], Awaitable[Any]],
        logger: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hass = hass
        self._name = name
        self._refresh = refresh
        self._logger = logger
        self._clock = clock
        self._started: Optional[float] = None
        self._task: Any = None
        # A cancelled refresh finishes after its replacement has started; only
        # the refresh of the current generation may release the slot.
        self._generation = 0

    @property
    def running(self) -> bool:
        return self._started is not None

    def request(self) -> None:
        """Start a refresh, unless one is running and not yet stuck."""
        now = self._clock()
        if self._started is not None:
            running_for = now - self._started
            if running_for < STUCK_AFTER_S:
                return
            self._logger.warning(
                "%s: refresh still running after %.0f s, cancelled and restarted",
                self._name, running_for,
            )
            if self._task is not None:
                self._task.cancel()
        self._generation += 1
        self._started = now
        self._task = self._hass.async_create_background_task(self._run(self._generation), self._name)

    async def _run(self, generation: int) -> None:
        try:
            await self._refresh()
        finally:
            if generation == self._generation:
                self._started = None
                self._task = None
