"""One shared coordinator for the 30-minute pre-dispatch run.

PredispatchIS carries every region and interconnector, so one fetch serves all
config entries. Like the binding constraints it has no timer of its own: it is
checked after each live dispatch poll, and a failure here never touches the
price path. AEMO writes a run every 30 minutes, so the NEMWEB listing is only
read once the run held is 30 minutes old; until the next run appears that is
one listing request per dispatch poll, usually one or two a half hour.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DISPATCH_UNSUBS_KEY, DOMAIN, NEMWEB_SEMAPHORE_KEY, SHARED_PREDISPATCH_KEY
from .nem_time import now_nem
from .predispatch_client import PredispatchClient, PredispatchSnapshot
from .refresh_guard import RefreshGuard

_LOGGER = logging.getLogger(__name__)

RUN_EVERY = timedelta(minutes=30)


class PredispatchCoordinator(DataUpdateCoordinator[Optional[PredispatchSnapshot]]):
    """The newest pre-dispatch run, fetched when a new one can be expected."""

    def __init__(self, hass: Any, dispatch: Any, client: Any) -> None:
        # Listeners hear of a new run only, not of every dispatch poll that found none:
        # the sensors take their per-poll heartbeat from the constraints coordinator (#334).
        super().__init__(hass, _LOGGER, name="NEM Predispatch", update_interval=None, always_update=False)
        self.client = client
        self.latest: Optional[PredispatchSnapshot] = None
        self._guard = RefreshGuard(hass, "nem_pd7day_predispatch_refresh", self.async_refresh, _LOGGER)
        self._unsub_dispatch: Optional[Callable[[], None]] = (
            dispatch.async_add_listener(self._on_dispatch) if dispatch is not None else None
        )

    def _on_dispatch(self) -> None:
        """A dispatch poll finished. One refresh at a time, as for constraints.

        Every refresh notifies the sensors, which is how they step to the next
        30-minute period without a new file. A refresh stuck past the guard's
        bound is cancelled and restarted (refresh_guard, #256).
        """
        self._guard.request()

    def due(self, now: datetime) -> bool:
        """True when the run held is 30 minutes old, or there is none."""
        return self.latest is None or now - self.latest.run_at >= RUN_EVERY

    async def _async_update_data(self) -> Optional[PredispatchSnapshot]:
        if self.due(now_nem()):
            snapshot = await self.client.fetch_latest()
            if snapshot is not None and (self.latest is None or snapshot.run_at > self.latest.run_at):
                self.latest = snapshot
                _LOGGER.debug(
                    "Predispatch: run %s, %d interconnectors, %d periods (NEMtime)",
                    snapshot.run_at.strftime("%Y-%m-%dT%H:%M"),
                    len(snapshot.interconnectors), len(snapshot.period_ends),
                )
        # Nothing new keeps the last run; the sensors' age check decides
        # whether it is still current.
        return self.latest

    def async_shutdown_listener(self) -> None:
        if self._unsub_dispatch is not None:
            self._unsub_dispatch()
            self._unsub_dispatch = None


def shared_predispatch(hass: Any, dispatch: Any) -> PredispatchCoordinator:
    """The one PredispatchCoordinator, created on first use by the sensor platform.

    Built and cleaned up as shared_constraints() is: no await between the check
    and the assignment, and the cleanup on the dispatch unsubscribe list.
    """
    domain_data = hass.data[DOMAIN]
    existing = domain_data.get(SHARED_PREDISPATCH_KEY)
    if existing is not None:
        return existing
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = PredispatchClient(
        async_get_clientsession(hass),
        executor_job=hass.async_add_executor_job,
        semaphore=domain_data.get(NEMWEB_SEMAPHORE_KEY),
    )
    created = PredispatchCoordinator(hass, dispatch, client)
    domain_data[SHARED_PREDISPATCH_KEY] = created

    def _cleanup() -> None:
        created.async_shutdown_listener()
        domain_data.pop(SHARED_PREDISPATCH_KEY, None)

    domain_data.setdefault(DISPATCH_UNSUBS_KEY, []).append(_cleanup)
    hass.async_create_background_task(created.async_refresh(), "nem_pd7day_predispatch_first")
    return created
