"""Single shared DispatchCoordinator claim.

One DispatchCoordinator serves every configured region, because the dispatch
file it downloads carries all five. Claiming that shared slot correctly is
fiddly enough to be worth isolating here.

The claim used to be an unguarded "is it there yet" check followed by an await
and only then the assignment. All five config entries set up concurrently, so
every one of them passed the check before any of them assigned, and each built
its own coordinator and started its own self-rescheduling boundary timer. The
result was dispatch fetched five times per 5-minute boundary, four coordinators
unreachable from hass.data while still polling, and only the last writer's
cancel callbacks left for unload to unsubscribe. See issue #34.

This module deliberately imports nothing from Home Assistant and does not
import coordinator.py at module scope, so the claim can be driven directly by
tests, concurrently, without stubbing the HA package.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from .const import DISPATCH_UNSUBS_KEY, DOMAIN, SHARED_DISPATCH_KEY

if TYPE_CHECKING:  # pragma: no cover
    from .coordinator import DispatchCoordinator


async def async_shared_dispatch(
    hass: Any,
    setup_lock: asyncio.Lock,
    trace: Any,
    factory: Callable[[Any], Any] | None = None,
) -> DispatchCoordinator:
    """Return the one DispatchCoordinator shared by every config entry.

    Guarded by the same lock as the shared notice store, and for the same
    reason: the first refresh awaits, so the check and the assignment have to be
    made atomic or concurrent entry setups each build their own instance.

    ``factory`` defaults to the real DispatchCoordinator, imported lazily so
    this module stays free of Home Assistant imports. Tests pass a counting stub
    so the single-instance property is asserted against this function rather
    than against a reimplementation of it.
    """
    if factory is None:
        from .coordinator import DispatchCoordinator as _DispatchCoordinator

        factory = _DispatchCoordinator

    async with setup_lock:
        if SHARED_DISPATCH_KEY not in hass.data[DOMAIN]:
            created = factory(hass)
            # Awaited during setup, so this is a NEMWEB request inside setup
            # even on the cached path. Small file, but not free, so it is
            # measured like every other setup phase.
            with trace.phase("dispatch first refresh (NEMWEB download)"):
                await created.async_config_entry_first_refresh()
            # Publish before scheduling, so the coordinator is reachable from
            # hass.data for the whole time its timer chain is live.
            hass.data[DOMAIN][SHARED_DISPATCH_KEY] = created
            # Domain level, and appended to rather than replaced, so unload
            # cancels the timer that was actually started.
            dispatch_unsubs: list = hass.data[DOMAIN].setdefault(
                DISPATCH_UNSUBS_KEY, []
            )
            # Start boundary-aligned polling once. The shared coordinator
            # reschedules itself from then on.
            created.schedule_next_poll(entry_unsub_list=dispatch_unsubs)

    return hass.data[DOMAIN][SHARED_DISPATCH_KEY]
