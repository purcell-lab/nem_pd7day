"""Move CPU-bound work off the Home Assistant event loop.

NEMWEB serves gzipped CSV inside ZIP archives. The PD7DAY archive is ~4.8 MB
compressed and expands to ~47 MB across ~339,000 lines; decompressing and
parsing it costs roughly 800 ms of solid CPU. Doing that inside an ``async def``
blocks the event loop for the whole duration, which stalls Home Assistant
startup (every region coordinator pays it on its first refresh) and trips the
blocking-call detector.

The clients deliberately do not hold a ``hass`` reference, so the executor
hand-off is injected instead. Pass ``hass.async_add_executor_job``; it is
``loop.run_in_executor(None, ...)`` plus task tracking, which lets Home
Assistant await outstanding jobs during shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")

# Signature of hass.async_add_executor_job.
ExecutorJob = Callable[..., Any]


async def run_in_executor(
    executor_job: ExecutorJob | None,
    func: Callable[..., _T],
    *args: Any,
) -> _T:
    """Run *func* in a worker thread and await the result.

    Uses *executor_job* when supplied so the job is tracked by Home Assistant.
    Falls back to the running loop's default executor otherwise — Home
    Assistant installs its own pool as the loop default, and the fallback keeps
    the clients usable without a ``hass`` reference (unit tests, scripts).
    """
    if executor_job is not None:
        return await executor_job(func, *args)
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)
