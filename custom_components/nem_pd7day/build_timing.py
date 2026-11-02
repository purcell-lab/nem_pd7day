"""Step timing for an attribute build, logged at debug when the build is slow (#363).

On HA-OCPP the NSW1 spot forecast's state write once took 3.0 s, where its own
work measures about 8 ms here: 15 to 30 times what the Green should take. The
log said only that the write was slow. A build that takes ``SLOW_BUILD_SECONDS``
or more now logs, at debug, the time of each step and two totals:

* wall time, from the first step to the last;
* CPU time of this thread over the same span.

A wall time far above the CPU time means the event loop thread was waiting,
for the GIL or the CPU, while the build ran: starved, not slow. Steps that sum
to the wall time and a CPU time close to it mean the build itself was slow, and
the steps say where.
"""
from __future__ import annotations

import logging
import time

SLOW_BUILD_SECONDS = 0.2


class BuildTimer:
    """Times the named steps of one build; ``finish`` logs them when the build was slow."""

    __slots__ = ("_what", "_wall0", "_cpu0", "_mark", "_steps")

    def __init__(self, what: str) -> None:
        self._what = what
        self._wall0 = self._mark = time.perf_counter()
        self._cpu0 = time.thread_time()
        self._steps: list[tuple[str, float]] = []

    def step(self, name: str) -> None:
        """Mark the end of step ``name``: the time since the previous mark is its own."""
        now = time.perf_counter()
        self._steps.append((name, now - self._mark))
        self._mark = now

    def finish(self, logger: logging.Logger) -> float:
        """The build's wall time, in seconds; logged at debug with its steps when slow."""
        wall = time.perf_counter() - self._wall0
        if wall >= SLOW_BUILD_SECONDS and logger.isEnabledFor(logging.DEBUG):
            cpu = time.thread_time() - self._cpu0
            logger.debug(
                "%s: attribute build took %.3f s wall and %.3f s CPU on this thread (%s). "
                "Wall far above CPU means the event loop was starved rather than slow (#363)",
                self._what, wall, cpu,
                ", ".join(f"{name} {seconds:.3f} s" for name, seconds in self._steps) or "no steps",
            )
        return wall
