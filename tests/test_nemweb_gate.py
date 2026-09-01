"""Tests for NemwebGate: the shared NEMWEB concurrency and frequency bound.

Issue #22. The gate replaces the plain asyncio.Semaphore that used to sit on
hass.data under NEMWEB_SEMAPHORE_KEY. Concurrency was already capped at two;
what was missing was a bound on request *frequency*, which is what NEMWEB
answers with a 403.

The clock and sleep are injected throughout so the pacing is asserted exactly
rather than by measuring wall time, which would make these tests slow and
flaky on a loaded runner.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.nem_pd7day.nemweb_gate import NemwebGate  # noqa: E402


class FakeClock:
    """Monotonic clock advanced only by the fake sleep, never by real time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        # Yield so other tasks can run, matching asyncio.sleep's behaviour.
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gate(max_concurrent: int = 2, min_gap_s: float = 0.25):
    clock = FakeClock()
    gate = NemwebGate(max_concurrent, min_gap_s, clock=clock, sleep=clock.sleep)
    return gate, clock


def test_first_request_is_not_delayed():
    """A cold gate must not add latency to the first request of a cycle."""
    gate, clock = make_gate()

    async def scenario():
        async with gate:
            pass

    asyncio.run(scenario())

    assert clock.sleeps == [], "first acquisition should not sleep"
    assert gate.paced_waits == 0
    assert gate.acquisitions == 1


def test_successive_requests_are_spaced_by_the_minimum_gap():
    """Back-to-back acquisitions are paced, which is the point of the gate.

    Three immediate acquisitions produce two waits of the full gap. This is the
    burst case: a directory listing followed by file fetches, which is what used
    to draw the 403.
    """
    gate, clock = make_gate(min_gap_s=0.25)

    async def scenario():
        for _ in range(3):
            async with gate:
                pass

    asyncio.run(scenario())

    assert clock.sleeps == pytest.approx([0.25, 0.25])
    assert gate.paced_waits == 2
    assert gate.total_paced_wait_s == pytest.approx(0.5)
    assert gate.acquisitions == 3


def test_a_request_after_a_long_idle_period_is_immediate():
    """Steady state must be a no-op: the gap only ever binds inside a burst."""
    gate, clock = make_gate(min_gap_s=0.25)

    async def scenario():
        async with gate:
            pass
        # Steady state is a fetch every few hours, far wider than the gap.
        clock.advance(3600.0)
        async with gate:
            pass

    asyncio.run(scenario())

    assert clock.sleeps == [], "an idle gate must not delay the next request"
    assert gate.paced_waits == 0


def test_idle_time_does_not_bank_credit_for_a_burst():
    """A long idle period must not buy a free burst afterwards.

    Without the max() in _pace, _next_allowed would trail far behind the clock
    after an idle period and the next several requests would all pass through
    unpaced, defeating the gate at exactly the moment a burst starts.
    """
    gate, clock = make_gate(min_gap_s=0.25)

    async def scenario():
        async with gate:
            pass
        clock.advance(3600.0)
        # Two requests back to back after the idle period.
        async with gate:
            pass
        async with gate:
            pass

    asyncio.run(scenario())

    # The first post-idle request is free, the second is paced.
    assert clock.sleeps == pytest.approx([0.25])
    assert gate.paced_waits == 1


def test_partial_gap_waits_only_the_remainder():
    """The gate waits the remaining gap, not the whole gap again."""
    gate, clock = make_gate(min_gap_s=1.0)

    async def scenario():
        async with gate:
            pass
        clock.advance(0.4)
        async with gate:
            pass

    asyncio.run(scenario())

    assert clock.sleeps == pytest.approx([0.6])


def test_zero_gap_is_a_pure_semaphore():
    """min_gap_s=0 must short-circuit, so the gate can be disabled outright."""
    gate, clock = make_gate(min_gap_s=0.0)

    async def scenario():
        for _ in range(5):
            async with gate:
                pass

    asyncio.run(scenario())

    assert clock.sleeps == []
    assert gate.paced_waits == 0
    assert gate.acquisitions == 5
    assert gate.min_gap_s == 0.0


def test_negative_gap_is_clamped_to_zero():
    """A misconfigured negative gap must not become a negative sleep."""
    gate, clock = make_gate(min_gap_s=-5.0)

    async def scenario():
        async with gate:
            pass
        async with gate:
            pass

    asyncio.run(scenario())

    assert gate.min_gap_s == 0.0
    assert clock.sleeps == []


def test_concurrency_is_still_capped():
    """The gate must not regress the concurrency cap it replaces."""
    gate, clock = make_gate(max_concurrent=2, min_gap_s=0.0)
    in_flight = 0
    peak = 0

    async def one_request():
        nonlocal in_flight, peak
        async with gate:
            in_flight += 1
            peak = max(peak, in_flight)
            # Force a suspension so overlap is observable.
            await asyncio.sleep(0)
            in_flight -= 1

    async def scenario():
        await asyncio.gather(*(one_request() for _ in range(10)))

    asyncio.run(scenario())

    assert peak <= 2, f"concurrency cap breached, peak was {peak}"
    assert gate.acquisitions == 10


def test_max_concurrent_below_one_is_rejected():
    """Zero slots would deadlock every fetch, so it must fail loudly."""
    with pytest.raises(ValueError):
        NemwebGate(0, 0.25)


def test_cancellation_during_pacing_does_not_leak_a_slot():
    """A cancelled refresh must not permanently narrow the gate.

    The gate acquires its semaphore slot before pacing. If a task is cancelled
    while waiting out the gap and the slot is not released, every cancelled
    refresh would shrink the effective concurrency until nothing could fetch at
    all. That failure mode is silent and cumulative, so it is asserted directly.
    """
    clock = FakeClock()
    started = asyncio.Event()

    async def blocking_sleep(seconds: float) -> None:
        started.set()
        # Never returns, so the cancellation lands inside the pacing wait.
        await asyncio.Event().wait()

    gate = NemwebGate(1, 0.25, clock=clock, sleep=blocking_sleep)

    async def scenario():
        # Prime the gate so the next acquisition has to wait out the gap.
        async with gate:
            pass

        async def paced():
            async with gate:
                pass

        task = asyncio.create_task(paced())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The slot must be back. With a leak this times out instead.
        await asyncio.wait_for(gate._semaphore.acquire(), timeout=1.0)
        gate._semaphore.release()

    asyncio.run(scenario())


def test_diagnostics_reports_the_gap_and_the_waits():
    """Diagnostics has to distinguish our own throttling from NEMWEB's.

    Without these counters a future 403 investigation cannot tell whether the
    integration was blocked by NEMWEB or was pacing itself.
    """
    gate, clock = make_gate(min_gap_s=0.25)

    async def scenario():
        for _ in range(3):
            async with gate:
                pass

    asyncio.run(scenario())

    diag = gate.diagnostics()
    assert diag["min_gap_s"] == pytest.approx(0.25)
    assert diag["acquisitions"] == 3
    assert diag["paced_waits"] == 2
    assert diag["total_paced_wait_s"] == pytest.approx(0.5)


def test_gate_is_reusable_and_reentrant_across_tasks():
    """Ten paced requests across concurrent tasks still respect the gap.

    The pacing lock is held across the wait on purpose. If it were released
    before sleeping, every waiting task would compute the same target time and
    wake together, which is the thundering herd the gap exists to prevent.
    """
    clock = FakeClock()
    gate = NemwebGate(2, 0.25, clock=clock, sleep=clock.sleep)
    start_times: list[float] = []

    async def one_request():
        async with gate:
            start_times.append(clock.now)

    async def scenario():
        await asyncio.gather(*(one_request() for _ in range(6)))

    asyncio.run(scenario())

    assert len(start_times) == 6
    ordered = sorted(start_times)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    assert all(g >= 0.25 - 1e-9 for g in gaps), f"gaps too tight: {gaps}"


# ── diagnostics exposure ─────────────────────────────────────────────────────


def _hass_with(domain_data):
    class _Hass:
        def __init__(self):
            self.data = domain_data

    return _Hass()


def test_diagnostics_payload_includes_the_gate_counters():
    """The counters are only useful if they reach a downloadable diagnostic."""
    from custom_components.nem_pd7day.const import DOMAIN, NEMWEB_SEMAPHORE_KEY
    from custom_components.nem_pd7day.diagnostics import _nemweb_gate

    gate, _ = make_gate(min_gap_s=0.25)

    async def scenario():
        async with gate:
            pass
        async with gate:
            pass

    asyncio.run(scenario())

    hass = _hass_with({DOMAIN: {NEMWEB_SEMAPHORE_KEY: gate}})
    payload = _nemweb_gate(hass)

    assert payload is not None
    assert payload["acquisitions"] == 2
    assert payload["paced_waits"] == 1
    assert payload["min_gap_s"] == pytest.approx(0.25)


def test_diagnostics_tolerates_a_missing_or_plain_semaphore():
    """Diagnostics must never raise, including mid-upgrade.

    An install that has not yet reloaded still has a bare asyncio.Semaphore
    under this key, and a config entry can be diagnosed before the shared
    objects exist at all.
    """
    from custom_components.nem_pd7day.const import DOMAIN, NEMWEB_SEMAPHORE_KEY
    from custom_components.nem_pd7day.diagnostics import _nemweb_gate

    assert _nemweb_gate(_hass_with({})) is None
    assert _nemweb_gate(_hass_with({DOMAIN: {}})) is None
    assert (
        _nemweb_gate(
            _hass_with({DOMAIN: {NEMWEB_SEMAPHORE_KEY: asyncio.Semaphore(2)}})
        )
        is None
    )
