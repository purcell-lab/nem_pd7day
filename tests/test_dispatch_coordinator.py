"""
Tests for coordinator.py's DispatchCoordinator: the fetch path and the
boundary-aligned 5-minute poll chain.

History, condensed from test_dispatch_and_modes.py: the coordinator once ran
on a rolling update_interval that drifted by whatever offset existed at HA
startup (observed up to ~4 min), so polls are now aligned to 5-minute UTC
boundaries plus _DISPATCH_POLL_DELAY_S. Issue #101: every poll appended its
timer's cancel to the entry's unsub list, so unload cancelled the spent first
timer and the rescheduled one survived; the coordinator now holds the single
pending cancel and registers async_shutdown_polling with the entry once.

The stale-data path (a failed fetch with prices already held) is covered in
test_coordinator_stale.py.

Run with:  python -m pytest tests/test_dispatch_coordinator.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from support import UpdateFailed, install_ha_stubs, load_chain, run_async

install_ha_stubs()

(
    _nem_time,
    _engine_mod,
    _client_mod,
    _const_mod,
    _store_mod,
    _dispatch_mod,
    _coord_mod,
) = load_chain(
    "nem_time",
    "calibration_engine",
    "pd7day_client",
    "const",
    "calibration_store",
    "dispatch_client",
    "coordinator",
)

DispatchCoordinator = _coord_mod.DispatchCoordinator
DispatchPrice = _dispatch_mod.DispatchPrice
POLL_DELAY = timedelta(seconds=_coord_mod._DISPATCH_POLL_DELAY_S)


def make_coordinator(hass=None) -> DispatchCoordinator:
    coord = DispatchCoordinator.__new__(DispatchCoordinator)
    coord.hass = hass if hass is not None else MagicMock()
    coord.region = "QLD1"
    coord.prices = {}
    coord.last_updated = None
    coord.data = None
    return coord


# ── Fetch path ────────────────────────────────────────────────────────────────

def test_update_interval_is_none():
    """Polling is boundary-aligned, so the coordinator must not carry a rolling interval."""
    coord = DispatchCoordinator(MagicMock())
    assert coord.update_interval is None


def test_fetch_failure_without_stale_data_raises_update_failed():
    hass = MagicMock()
    hass.async_add_executor_job = MagicMock(side_effect=ConnectionError("offline"))
    coord = make_coordinator(hass)

    with pytest.raises(UpdateFailed, match="DispatchIS fetch failed"):
        run_async(coord._async_update_data())


def test_successful_fetch_stores_prices_and_last_updated():
    fake_prices = {"QLD1": DispatchPrice("QLD1", "2026/05/21 09:30:00", 0.085)}
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=fake_prices)
    coord = make_coordinator(hass)

    result = run_async(coord._async_update_data())

    assert result is fake_prices
    assert coord.prices["QLD1"].rrp == 0.085
    assert coord.last_updated is not None


# ── Boundary-aligned poll scheduling ─────────────────────────────────────────

@pytest.mark.parametrize(
    "now, boundary",
    [
        pytest.param(
            datetime(2026, 5, 21, 12, 3, 20, 500000, tzinfo=timezone.utc),
            datetime(2026, 5, 21, 12, 5, tzinfo=timezone.utc),
            id="mid window",
        ),
        pytest.param(
            datetime(2026, 5, 21, 12, 5, tzinfo=timezone.utc),
            datetime(2026, 5, 21, 12, 10, tzinfo=timezone.utc),
            id="exactly on a boundary schedules the next one",
        ),
        pytest.param(
            datetime(2026, 5, 21, 23, 58, tzinfo=timezone.utc),
            datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
            id="across midnight",
        ),
    ],
)
def test_next_boundary_utc_is_the_next_5_min_boundary_plus_delay(now, boundary):
    coord = make_coordinator()
    with patch.object(_coord_mod.dt_util, "utcnow", return_value=now):
        fire_at = coord._next_boundary_utc()

    assert fire_at == boundary + POLL_DELAY
    assert fire_at > now


def test_schedule_next_poll_registers_the_shutdown_hook_not_the_timer_cancel():
    """The entry list gets async_shutdown_polling; the pending cancel stays on the coordinator (#101)."""
    coord = make_coordinator()
    cancel_fn = MagicMock()
    unsub_list: list = []

    with patch.object(_coord_mod, "async_track_point_in_utc_time", return_value=cancel_fn):
        coord.schedule_next_poll(entry_unsub_list=unsub_list)

    assert unsub_list == [coord.async_shutdown_polling]
    assert coord._pending_cancel is cancel_fn
    assert coord.polling_active


class _TimerRegistry:
    """Stand-in for async_track_point_in_utc_time that tracks live timers."""

    def __init__(self) -> None:
        self.live: list = []

    def __call__(self, hass, action, when):
        cancel = MagicMock(name="cancel")
        entry = {"action": action, "cancel": cancel}
        self.live.append(entry)
        cancel.side_effect = lambda: self.live.remove(entry)
        return cancel

    def fire_all(self) -> None:
        for entry in list(self.live):
            self.live.remove(entry)
            entry["action"](None)


class _chain_patches:
    """Patch the timer helper and make HA's ``callback`` decorator a no-op.

    ``homeassistant.core`` is a MagicMock here, so ``callback`` would
    otherwise swallow the timer action and nothing could fire.
    """

    def __init__(self, timers: _TimerRegistry) -> None:
        self._patches = [
            patch.object(_coord_mod, "async_track_point_in_utc_time", timers),
            patch.object(_coord_mod, "callback", lambda fn: fn),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _chain_coordinator() -> DispatchCoordinator:
    coord = make_coordinator()
    coord.hass.async_create_task = MagicMock(
        side_effect=lambda coro: (coro.close(), MagicMock(done=lambda: True))[1]
    )
    coord.async_refresh = AsyncMock()
    return coord


def test_unload_after_reschedule_cancels_the_live_timer():
    """The timer registered by a self-reschedule is the one unload cancels, not the spent first one (#101)."""
    timers = _TimerRegistry()
    coord = _chain_coordinator()
    unsub_list: list = []

    with _chain_patches(timers):
        coord.schedule_next_poll(entry_unsub_list=unsub_list)
        assert len(timers.live) == 1
        # First boundary fires; the coordinator reschedules itself.
        timers.fire_all()
        run_async(coord._aligned_refresh())
        assert len(timers.live) == 1, "reschedule must replace, not stack"
        # Unload runs whatever was registered at setup.
        for unsub in unsub_list:
            unsub()

    assert timers.live == [], "the rescheduled timer survived unload"
    assert not coord.polling_active


def test_refresh_in_flight_at_unload_does_not_resurrect_the_chain():
    timers = _TimerRegistry()
    coord = _chain_coordinator()
    unsub_list: list = []

    with _chain_patches(timers):
        coord.schedule_next_poll(entry_unsub_list=unsub_list)
        timers.fire_all()
        # Unload lands while the refresh for that boundary is still running.
        for unsub in unsub_list:
            unsub()
        run_async(coord._aligned_refresh())

    assert timers.live == []
    assert coord._pending_cancel is None


def test_reload_leaves_exactly_one_live_timer():
    """Unload then reload: one coordinator, one timer, no orphaned chain."""
    timers = _TimerRegistry()
    with _chain_patches(timers):
        first = _chain_coordinator()
        unsubs: list = []
        first.schedule_next_poll(entry_unsub_list=unsubs)
        timers.fire_all()
        run_async(first._aligned_refresh())
        for unsub in unsubs:
            unsub()

        second = _chain_coordinator()
        unsubs2: list = []
        second.schedule_next_poll(entry_unsub_list=unsubs2)
        timers.fire_all()
        run_async(second._aligned_refresh())

    assert len(timers.live) == 1
    assert timers.live[0]["cancel"] is second._pending_cancel
