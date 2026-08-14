"""Timing checkpoints for integration setup.

Startup slowness in this integration has repeatedly turned out to be somewhere
other than where it looked. The PD7DAY parse was blamed for stalling startup,
moved to the executor in v3.1.4, and the median refresh got slightly worse; the
real cost was doing the same work once per region, fixed in v3.1.5. The cost
then turned out to sit almost entirely in the market notice fetch.

Guessing has been more expensive than measuring, so setup is instrumented. Each
phase logs its own duration and the running total, at debug level, so a slow
startup can be attributed from the log alone without another round of
bisecting.

Timings use a monotonic clock, so they are unaffected by clock adjustments.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

_LOGGER = logging.getLogger(__name__)

# Phases slower than this are surfaced at info level rather than debug, so a
# genuinely slow startup is visible without enabling debug logging. Chosen to
# sit above normal disk-store loads (single-digit ms) and below anything that
# would bother a user.
SLOW_PHASE_S = 1.0


class StartupTrace:
    """Records setup phase durations for one config entry.

    Not thread-safe and not intended to be: it is only ever touched from the
    event loop during a single async_setup_entry call.
    """

    def __init__(self, label: str, logger: logging.Logger | None = None) -> None:
        self._label = label
        self._log = logger or _LOGGER
        self._t0 = time.monotonic()
        self._last = self._t0
        self.phases: list[tuple[str, float]] = []

    def checkpoint(self, phase: str, detail: str = "") -> float:
        """Record the time since the previous checkpoint and return it."""
        now = time.monotonic()
        elapsed = now - self._last
        total = now - self._t0
        self._last = now
        self.phases.append((phase, elapsed))

        suffix = f" ({detail})" if detail else ""
        level = logging.INFO if elapsed >= SLOW_PHASE_S else logging.DEBUG
        self._log.log(
            level,
            "[STARTUP] %s: %s took %.0f ms, total %.0f ms%s",
            self._label,
            phase,
            elapsed * 1000,
            total * 1000,
            suffix,
        )
        return elapsed

    @contextmanager
    def phase(self, phase: str, detail: str = ""):
        """Time a block, recording it even if the block raises.

        Used for setup steps that can fail, so a failed startup still reports
        where the time went instead of losing the trace with the exception.
        """
        start = time.monotonic()
        try:
            yield
        finally:
            self._last = start
            self.checkpoint(phase, detail)

    @property
    def total(self) -> float:
        return time.monotonic() - self._t0

    def summary(self) -> str:
        """Phases ordered slowest first, so the culprit reads first."""
        if not self.phases:
            return "no phases recorded"
        ranked = sorted(self.phases, key=lambda p: p[1], reverse=True)
        parts = [f"{name} {dur * 1000:.0f} ms" for name, dur in ranked if dur >= 0.001]
        return ", ".join(parts) if parts else "all phases under 1 ms"

    def log_summary(self) -> None:
        self._log.info(
            "[STARTUP] %s: setup complete in %.0f ms. Slowest first: %s",
            self._label,
            self.total * 1000,
            self.summary(),
        )
