"""
Shared DispatchCoordinator claim (issue #34).

Five config entries set up concurrently. Before the fix, the "is it there yet"
check ran before an await and the assignment ran after it, so all five entries
built their own DispatchCoordinator and started their own self-rescheduling
boundary timer. Dispatch was then fetched five times per 5-minute boundary, four
coordinators were unreachable from hass.data while still polling, and only the
last writer's cancel callbacks survived to be unsubscribed on unload.

These tests drive the real `async_shared_dispatch` helper rather than a copy of
its logic, injecting a counting factory so the single-instance property is
asserted against production code.
"""
import asyncio
import logging

import pytest

from custom_components.nem_pd7day.shared_dispatch import async_shared_dispatch
from custom_components.nem_pd7day.const import (
    DISPATCH_UNSUBS_KEY,
    DOMAIN,
    SHARED_DISPATCH_KEY,
)
from custom_components.nem_pd7day.startup_trace import StartupTrace

REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]


class _FakeHass:
    def __init__(self) -> None:
        self.data: dict = {DOMAIN: {}}


class _StubDispatch:
    """Counts constructions, refreshes and poll schedules across all instances."""

    constructed = 0
    refreshed = 0
    scheduled = 0

    @classmethod
    def reset(cls) -> None:
        cls.constructed = cls.refreshed = cls.scheduled = 0

    def __init__(self, hass) -> None:
        type(self).constructed += 1
        self.hass = hass

    async def async_config_entry_first_refresh(self) -> None:
        # Yield control so any competing task gets the chance to pass a check
        # that is not properly guarded. This is what reproduced the original bug.
        await asyncio.sleep(0)
        type(self).refreshed += 1

    def schedule_next_poll(self, entry_unsub_list=None) -> None:
        type(self).scheduled += 1
        if entry_unsub_list is not None:
            entry_unsub_list.append(lambda: None)


@pytest.fixture(autouse=True)
def _reset_stub():
    _StubDispatch.reset()
    yield
    _StubDispatch.reset()


def _run(coro):
    return asyncio.run(coro)


async def _setup_all_concurrently(hass, lock):
    """Mimic HA setting up all five config entries concurrently."""
    return await asyncio.gather(
        *(
            async_shared_dispatch(
                hass,
                lock,
                StartupTrace(region, logging.getLogger(__name__)),
                factory=_StubDispatch,
            )
            for region in REGIONS
        )
    )


def test_five_concurrent_entries_create_one_coordinator():
    """The whole point of #34: one coordinator, one first refresh, one timer."""
    hass = _FakeHass()

    results = _run(_setup_all_concurrently(hass, asyncio.Lock()))

    assert _StubDispatch.constructed == 1
    assert _StubDispatch.refreshed == 1
    assert _StubDispatch.scheduled == 1
    # Every entry must receive the same live instance, not a private copy.
    assert len({id(r) for r in results}) == 1
    assert results[0] is hass.data[DOMAIN][SHARED_DISPATCH_KEY]


def test_cancel_callbacks_are_registered_once_at_domain_level():
    """Unload must be able to cancel the timer that was actually started."""
    hass = _FakeHass()

    _run(_setup_all_concurrently(hass, asyncio.Lock()))

    unsubs = hass.data[DOMAIN][DISPATCH_UNSUBS_KEY]
    assert len(unsubs) == 1
    # The list is reachable from the domain data, which is what async_unload_entry
    # pops. A per-entry list would have been overwritten four times.
    assert all(callable(u) for u in unsubs)


def test_existing_coordinator_is_reused_without_refetching():
    """A later entry joining an already-set-up domain must not refetch."""
    hass = _FakeHass()
    lock = asyncio.Lock()

    first = _run(
        async_shared_dispatch(
            hass,
            lock,
            StartupTrace("QLD1", logging.getLogger(__name__)),
            factory=_StubDispatch,
        )
    )
    assert _StubDispatch.constructed == 1

    second = _run(
        async_shared_dispatch(
            hass,
            lock,
            StartupTrace("NSW1", logging.getLogger(__name__)),
            factory=_StubDispatch,
        )
    )

    assert second is first
    assert _StubDispatch.constructed == 1
    assert _StubDispatch.refreshed == 1
    assert _StubDispatch.scheduled == 1


def test_unguarded_claim_would_fail_this_property():
    """
    Guard against the fix being silently reverted.

    This reproduces the pre-fix ordering (check, await, then assign) and asserts
    that it does produce five coordinators, so the tests above are known to be
    testing something real rather than passing trivially.
    """
    hass = _FakeHass()

    async def _unguarded(region):
        if SHARED_DISPATCH_KEY not in hass.data[DOMAIN]:
            created = _StubDispatch(hass)
            await created.async_config_entry_first_refresh()
            hass.data[DOMAIN][SHARED_DISPATCH_KEY] = created
            created.schedule_next_poll(entry_unsub_list=[])
        return hass.data[DOMAIN][SHARED_DISPATCH_KEY]

    async def _all():
        return await asyncio.gather(*(_unguarded(r) for r in REGIONS))

    _run(_all())

    assert _StubDispatch.constructed == len(REGIONS)
    assert _StubDispatch.scheduled == len(REGIONS)
