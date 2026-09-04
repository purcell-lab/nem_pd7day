"""Daily fetch timer at AEMO's PD7DAY publish times.

AEMO publishes PD7DAY three times a day at fixed NEM clock times, so the
integration fires one fetch per publish slot per day. Each firing re-arms its
own slot for the next day.

Why this is a class rather than the closure it used to be
---------------------------------------------------------
The closure in __init__.py registered every timer's cancel with
``entry.async_on_unload``. That list is only drained at unload, so three
slots meant three dead callbacks a day, about 550 per entry over six months,
times five regions (issue #106). The scheduler holds one pending cancel per
slot, replaces it on re-arm, and exposes a single ``cancel_all`` for the
entry to register once.

This module deliberately imports nothing from Home Assistant at module scope,
following shared_dispatch.py, so it can be driven directly by tests with an
injected timer function and clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from typing import Any, Callable

Slot = tuple[int, int]
CancelFn = Callable[[], None]
TrackFn = Callable[[Any, Callable[..., None], datetime], CancelFn]


def _default_track(hass: Any, action: Callable[..., None], when: datetime) -> CancelFn:
    """Register ``action`` with Home Assistant's point-in-time tracker.

    The action is handed over as a callback-typed HassJob, explicitly. Home
    Assistant classifies a timer action by inspecting it: a coroutine
    function runs on the loop, a function marked ``@callback`` runs on the
    loop, and any other plain function is run in the executor thread pool.
    ``_arm`` passes ``partial(self._on_fire, slot)``, a plain method, which
    HA unwrapped and classified as an executor job, so the publish-time
    action ran on a worker thread; ``entry.async_create_background_task``
    called from that thread produced a task that was destroyed while pending
    and no scheduled PD7DAY fetch ran from v3.6.0 until this was found
    (issue #126). The closure this scheduler replaced was decorated
    ``@callback``. Declaring the job type here, next to the only HA import,
    keeps the module importable without Home Assistant in the tests and
    makes the requirement visible rather than implied by a decorator.
    """
    from homeassistant.core import HassJob, HassJobType
    from homeassistant.helpers.event import async_track_point_in_utc_time

    job = HassJob(
        action, "nem_pd7day scheduled fetch", job_type=HassJobType.Callback
    )
    return async_track_point_in_utc_time(hass, job, when)


def _default_now() -> datetime:
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow()


def next_utc_fire(hour: int, minute: int, now: datetime) -> datetime:
    """Return the next UTC datetime at the given UTC hour:minute after *now*."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class DailyFetchScheduler:
    """Fire ``action(hour, minute)`` once a day at each UTC slot.

    ``action`` is called on the event loop from the timer callback (see
    ``_default_track`` for why that is declared rather than assumed); it must
    not block. Scheduling the fetch itself as a task is the caller's job, so
    the caller decides which lifecycle the task is tied to.
    """

    def __init__(
        self,
        hass: Any,
        slots: list[Slot],
        action: Callable[[int, int], None],
        *,
        track: TrackFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._hass = hass
        self._slots = list(slots)
        self._action = action
        self._track = track or _default_track
        self._now = now or _default_now
        self._pending: dict[Slot, CancelFn] = {}
        self._stopped = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Arm every slot. Idempotent per slot: re-arming replaces the timer."""
        for slot in self._slots:
            self._arm(slot)

    def cancel_all(self) -> None:
        """Cancel whatever is pending and refuse to re-arm from here on.

        Registered once with ``entry.async_on_unload``.
        """
        self._stopped = True
        pending, self._pending = self._pending, {}
        for cancel in pending.values():
            cancel()

    # ── Inspection ───────────────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def next_fire_at(self, slot: Slot) -> datetime:
        return next_utc_fire(slot[0], slot[1], self._now())

    # ── Internals ────────────────────────────────────────────────────────────

    def _arm(self, slot: Slot) -> None:
        if self._stopped:
            return
        previous = self._pending.pop(slot, None)
        if previous is not None:
            previous()
        fire_at = self.next_fire_at(slot)
        self._pending[slot] = self._track(
            self._hass, partial(self._on_fire, slot), fire_at
        )

    def _on_fire(self, slot: Slot, _now: Any = None) -> None:
        self._pending.pop(slot, None)
        if self._stopped:
            return
        self._action(*slot)
        self._arm(slot)
