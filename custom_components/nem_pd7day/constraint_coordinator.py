"""One shared coordinator for binding constraints, refreshed after each dispatch poll.

The DispatchIS file carries every region, so one fetch serves all five config
entries, as for dispatch prices. It is not a second timer: it refreshes when the
shared DispatchCoordinator notifies, so constraints follow the price poll's
boundary schedule, and a failure here never touches the price path.

ConstraintTracker adds what one file cannot say: how long each constraint has
been binding (``first_bound``), its marginal value one interval earlier
(``previous_marginal_value_mwh``), and its binding minutes in the current NEM
day. That history is held in memory from the first interval this run saw,
``tracked_since``; a missed interval ends a run rather than bridging it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Optional

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .constraint_client import (
    INTERVAL,
    BindingConstraint,
    ConstraintClient,
    ConstraintSnapshot,
    describe_constraint,
)
from .const import DISPATCH_UNSUBS_KEY, DOMAIN, NEMWEB_SEMAPHORE_KEY, SHARED_CONSTRAINTS_KEY
from .nem_time import NEM_TZ

_LOGGER = logging.getLogger(__name__)


def _nem_day(interval_end: datetime) -> date:
    """The NEM calendar day an interval belongs to, by its start."""
    return (interval_end - INTERVAL).astimezone(NEM_TZ).date()


@dataclass
class _Run:
    first_start: datetime          # start of the first interval in this binding run
    last_end: datetime             # end of the latest interval it bound in
    marginal_value: float
    previous_marginal_value: Optional[float]
    day: date
    intervals_today: int


@dataclass
class ConstraintTracker:
    """Binding runs across successive snapshots."""

    runs: dict[str, _Run] = field(default_factory=dict)
    tracked_since: Optional[datetime] = None
    latest: Optional[ConstraintSnapshot] = None

    def add(self, snapshot: ConstraintSnapshot) -> bool:
        """Fold in one interval. False for a repeat or an older interval."""
        end = snapshot.interval_end
        if self.latest is not None and end <= self.latest.interval_end:
            return False
        if self.tracked_since is None:
            self.tracked_since = end - INTERVAL
        day = _nem_day(end)
        for c in snapshot.constraints:
            run = self.runs.get(c.constraint_id)
            continuing = run is not None and run.last_end == end - INTERVAL
            today = run.intervals_today if run is not None and run.day == day else 0
            self.runs[c.constraint_id] = _Run(
                first_start=run.first_start if continuing and run else end - INTERVAL,
                last_end=end,
                marginal_value=c.marginal_value,
                previous_marginal_value=run.marginal_value if continuing and run else None,
                day=day,
                intervals_today=today + 1,
            )
        # A constraint that stopped binding keeps its day total until the day ends.
        self.runs = {cid: r for cid, r in self.runs.items() if r.day == day}
        self.latest = snapshot
        return True

    def row(self, c: BindingConstraint) -> dict[str, Any]:
        """One published constraint: the file's values, its ID's meaning, its run."""
        info = describe_constraint(c.constraint_id)
        run = self.runs.get(c.constraint_id)
        return {
            "constraint_id": c.constraint_id,
            "regions": list(info.regions),
            "category": info.category,
            "cause": info.cause,
            "co_optimised": info.co_optimised,
            "system_normal": info.system_normal,
            "rhs": c.rhs,
            "lhs": c.lhs,
            "marginal_value_mwh": c.marginal_value,
            "previous_marginal_value_mwh": run.previous_marginal_value if run else None,
            "violation_degree": c.violation_degree,
            "first_bound": run.first_start.astimezone(NEM_TZ).isoformat() if run else None,
            "bound_minutes_today": run.intervals_today * 5 if run else None,
        }


class ConstraintCoordinator(DataUpdateCoordinator[Optional[ConstraintSnapshot]]):
    """Binding constraints for every region, one DispatchIS fetch per interval."""

    def __init__(self, hass: Any, dispatch: Any, client: Any) -> None:
        super().__init__(hass, _LOGGER, name="NEM Binding Constraints", update_interval=None)
        self.client = client
        self.tracker = ConstraintTracker()
        self._refreshing = False
        self._unsub_dispatch: Optional[Callable[[], None]] = (
            dispatch.async_add_listener(self._on_dispatch) if dispatch is not None else None
        )

    def _on_dispatch(self) -> None:
        """A dispatch poll finished: read that interval's constraints.

        One refresh at a time; a notification while one is running is dropped,
        because the running one reads the same newest file.
        """
        if self._refreshing:
            return
        self._refreshing = True
        self.hass.async_create_background_task(
            self._refresh_once(), "nem_pd7day_binding_constraints_refresh"
        )

    async def _refresh_once(self) -> None:
        try:
            await self.async_refresh()
        finally:
            self._refreshing = False

    async def _async_update_data(self) -> Optional[ConstraintSnapshot]:
        snapshot = await self.client.fetch_latest()
        if snapshot is not None and self.tracker.add(snapshot):
            _LOGGER.debug(
                "Binding constraints: %d of %d binding, interval ending %s (NEMtime)",
                len(snapshot.constraints), snapshot.evaluated,
                snapshot.interval_end.strftime("%Y-%m-%dT%H:%M"),
            )
        # Nothing new keeps the last interval; the sensor's age check decides
        # whether that is still current.
        return self.tracker.latest

    def async_shutdown_listener(self) -> None:
        if self._unsub_dispatch is not None:
            self._unsub_dispatch()
            self._unsub_dispatch = None


def shared_constraints(hass: Any, dispatch: Any) -> ConstraintCoordinator:
    """The one ConstraintCoordinator, created on first use by the sensor platform.

    No await between the check and the assignment, so concurrent platform
    setups cannot each build one. Its cleanup goes on the dispatch unsubscribe
    list, which unloading the last entry already drains.
    """
    domain_data = hass.data[DOMAIN]
    existing = domain_data.get(SHARED_CONSTRAINTS_KEY)
    if existing is not None:
        return existing
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = ConstraintClient(
        async_get_clientsession(hass),
        executor_job=hass.async_add_executor_job,
        semaphore=domain_data.get(NEMWEB_SEMAPHORE_KEY),
    )
    created = ConstraintCoordinator(hass, dispatch, client)
    domain_data[SHARED_CONSTRAINTS_KEY] = created

    def _cleanup() -> None:
        created.async_shutdown_listener()
        domain_data.pop(SHARED_CONSTRAINTS_KEY, None)

    domain_data.setdefault(DISPATCH_UNSUBS_KEY, []).append(_cleanup)
    hass.async_create_background_task(created.async_refresh(), "nem_pd7day_binding_constraints_first")
    return created
