"""
DailyFetchScheduler: one pending timer per publish slot, one cancel for all.

Issue #106: the closure this replaced appended every timer's cancel to
entry.async_on_unload, three a day and never removed. These tests drive the
real scheduler with an injected timer registry and clock.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

from custom_components.nem_pd7day.fetch_scheduler import (
    DailyFetchScheduler,
    _default_track,
    next_utc_fire,
)

SLOTS = [(21, 30), (3, 0), (8, 0)]


class _Timers:
    """Stand-in for async_track_point_in_utc_time that records live timers."""

    def __init__(self) -> None:
        self.live: dict[int, tuple] = {}
        self.registered = 0
        self._next = 0

    def __call__(self, hass, action, when):
        self.registered += 1
        key = self._next
        self._next += 1
        self.live[key] = (action, when)

        def cancel() -> None:
            self.live.pop(key, None)

        return cancel

    def fire_due(self, now: datetime) -> int:
        fired = 0
        for key, (action, when) in list(self.live.items()):
            if when <= now:
                self.live.pop(key)
                action(now)
                fired += 1
        return fired


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _build(start=datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc)):
    timers = _Timers()
    clock = _Clock(start)
    fired: list = []
    sched = DailyFetchScheduler(
        hass=object(), slots=SLOTS,
        action=lambda h, m: fired.append((h, m)),
        track=timers, now=clock,
    )
    return sched, timers, clock, fired


def test_next_utc_fire_rolls_to_tomorrow_when_slot_has_passed():
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    assert next_utc_fire(8, 0, now) == datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc)
    assert next_utc_fire(21, 30, now) == datetime(2026, 9, 3, 21, 30, tzinfo=timezone.utc)


def test_start_arms_one_timer_per_slot():
    sched, timers, _, _ = _build()
    sched.start()
    assert sched.pending_count == 3
    assert len(timers.live) == 3


def test_pending_never_grows_across_days():
    """The property #106 asked for: no growth in registered cancels over time.
    Six days of firings leave exactly three live timers."""
    sched, timers, clock, fired = _build()
    sched.start()
    for day in range(6):
        for hour, minute in sorted(SLOTS):
            clock.now = datetime(2026, 9, 4 + day, hour, minute, tzinfo=timezone.utc)
            timers.fire_due(clock.now)
    assert len(fired) == 18
    assert sched.pending_count == 3
    assert len(timers.live) == 3
    assert timers.registered == 21  # 3 initial + 18 re-arms


def test_cancel_all_leaves_nothing_live():
    sched, timers, clock, fired = _build()
    sched.start()
    clock.now = datetime(2026, 9, 4, 3, 0, tzinfo=timezone.utc)
    assert timers.fire_due(clock.now) == 1
    sched.cancel_all()
    assert timers.live == {}
    assert sched.pending_count == 0
    assert sched.stopped


def test_fire_after_cancel_does_not_rearm():
    """A timer already popped from HA's loop when unload runs must not
    resurrect the chain if it still fires."""
    sched, timers, clock, fired = _build()
    sched.start()
    action, when = next(iter(timers.live.values()))
    sched.cancel_all()
    action(when)
    assert fired == []
    assert sched.pending_count == 0


def test_rearm_replaces_rather_than_stacks():
    sched, timers, _, _ = _build()
    sched.start()
    sched.start()
    assert sched.pending_count == 3
    assert len(timers.live) == 3


# ── The timer action must run on the event loop ──────────────────────────────

def test_default_track_registers_a_callback_typed_hass_job(monkeypatch):
    """The action reaches HA as a HassJob declared as a Callback.

    Regression for issue #126. HA classifies a plain function handed to
    async_track_point_in_utc_time as an executor job and runs it on a worker
    thread; the fetch task created there was destroyed while pending and no
    scheduled PD7DAY fetch ran from v3.6.0 on. The injected ``track`` in the
    other tests calls the action directly and cannot see the job type, so
    this test drives the real ``_default_track`` against stand-in HA modules
    and pins what it registers.
    """
    registered: list = []

    class _HassJobType:
        Callback = "callback"
        Executor = "executor"

    class _HassJob:
        def __init__(self, target, name=None, *, cancel_on_shutdown=None, job_type=None):
            self.target = target
            self.name = name
            self.job_type = job_type

    def _track(hass, action, when):
        registered.append((hass, action, when))
        return lambda: None

    core = types.ModuleType("homeassistant.core")
    core.HassJob = _HassJob
    core.HassJobType = _HassJobType
    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_point_in_utc_time = _track
    helpers = types.ModuleType("homeassistant.helpers")
    ha = types.ModuleType("homeassistant")
    for name, mod in (
        ("homeassistant", ha),
        ("homeassistant.core", core),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.event", event),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    def action(_now=None):
        return None

    when = datetime(2026, 9, 5, 21, 30, tzinfo=timezone.utc)
    hass = object()
    cancel = _default_track(hass, action, when)
    assert callable(cancel)
    assert len(registered) == 1
    got_hass, job, got_when = registered[0]
    assert got_hass is hass and got_when == when
    assert isinstance(job, _HassJob), "the action must be wrapped in a HassJob"
    assert job.job_type == _HassJobType.Callback, (
        "the scheduled fetch must be declared a callback so HA runs it on the "
        f"event loop, got job_type {job.job_type!r}"
    )
    assert job.target is action
