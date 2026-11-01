"""One shared coordinator for the 5-minute pre-dispatch (P5MIN) local prices (#275).

P5MIN carries every DUID with a local price adjustment and every region's ROP,
so one fetch serves all config entries. It fetches only while a DUID
sensor is enabled, and like the binding constraints it has no timer of its
own: it is checked after each live dispatch poll, and a failure here never
touches the price path. AEMO writes a run every five minutes, so the listing (about 16 KB gzip)
is read once the run held has reached its first interval's end; the zip
(about 210 KB) only when a newer run is listed.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DISPATCH_UNSUBS_KEY, DOMAIN, NEMWEB_SEMAPHORE_KEY, P5MIN_BASE_URL, SHARED_P5MIN_KEY
from .constraint_client import ConstraintClient
from .executor import run_in_executor
from .local_price import P5LocalPriceRun, latest_p5_file, parse_p5min_local_prices
from .nem_time import now_nem
from .refresh_guard import RefreshGuard

_LOGGER = logging.getLogger(__name__)


class P5MinClient(ConstraintClient):
    """Fetches the newest P5MIN file through the shared NEMWEB gate.

    Its CSV is about 3 MB, mostly constraint rows this integration does not
    read, so it is unzipped and parsed in the executor.
    """

    BASE_URL = P5MIN_BASE_URL
    LABEL = "P5MIN"
    NO_ROWS = "no D,P5MIN,REGIONSOLUTION rows"

    def _latest(self, html: str) -> Optional[str]:
        return latest_p5_file(html)

    async def _parse_off_loop(self, content: str, name: str) -> Optional[P5LocalPriceRun]:
        return await run_in_executor(self._executor_job, parse_p5min_local_prices, content, name, now_nem())


class P5MinCoordinator(DataUpdateCoordinator[Optional[P5LocalPriceRun]]):
    """The newest P5MIN run, fetched when a new one can be expected."""

    def __init__(self, hass: Any, dispatch: Any, client: Any) -> None:
        # Listeners hear of a new run only: the local price sensors are entities of the
        # constraints coordinator, which already wakes them every dispatch poll (#334).
        super().__init__(hass, _LOGGER, name="NEM P5MIN local prices", update_interval=None, always_update=False)
        self.client = client
        self.latest: Optional[P5LocalPriceRun] = None
        self._guard = RefreshGuard(hass, "nem_pd7day_p5min_refresh", self.async_refresh, _LOGGER)
        self._unsub_dispatch: Optional[Callable[[], None]] = (
            dispatch.async_add_listener(self._on_dispatch) if dispatch is not None else None
        )

    def _on_dispatch(self) -> None:
        """A dispatch poll finished. One refresh at a time, as for constraints.

        Nothing is fetched while no DUID sensor listens: the region's most
        constrained DUIDs are added disabled, and cost nothing until one is
        enabled.
        """
        if getattr(self, "_listeners", None):
            self._guard.request()

    def request_refresh(self) -> None:
        """A DUID sensor was added: fetch now rather than at the next dispatch poll."""
        self._guard.request()

    def due(self, now: datetime) -> bool:
        """True once the run held has reached its first interval's end, or there is none."""
        return self.latest is None or now >= self.latest.run_at

    async def _async_update_data(self) -> Optional[P5LocalPriceRun]:
        if self.due(now_nem()):
            run = await self.client.fetch_latest()
            if run is not None and (self.latest is None or run.run_at > self.latest.run_at):
                self.latest = run
                _LOGGER.debug(
                    "P5MIN: run %s, %d DUIDs with a local price adjustment, %d intervals (NEMtime)",
                    run.run_at.strftime("%Y-%m-%dT%H:%M"), len(run.rows), len(run.interval_ends),
                )
        # Nothing new keeps the last run; the sensors' age check decides
        # whether it is still current.
        return self.latest

    def async_shutdown_listener(self) -> None:
        if self._unsub_dispatch is not None:
            self._unsub_dispatch()
            self._unsub_dispatch = None


def shared_p5min(hass: Any, dispatch: Any) -> P5MinCoordinator:
    """The one P5MinCoordinator, created on first use by an entry with DUID sensors.

    Built and cleaned up as shared_predispatch() is: no await between the check
    and the assignment, and the cleanup on the dispatch unsubscribe list.
    """
    domain_data = hass.data[DOMAIN]
    existing = domain_data.get(SHARED_P5MIN_KEY)
    if existing is not None:
        return existing
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = P5MinClient(
        async_get_clientsession(hass),
        executor_job=hass.async_add_executor_job,
        semaphore=domain_data.get(NEMWEB_SEMAPHORE_KEY),
    )
    created = P5MinCoordinator(hass, dispatch, client)
    domain_data[SHARED_P5MIN_KEY] = created

    def _cleanup() -> None:
        created.async_shutdown_listener()
        domain_data.pop(SHARED_P5MIN_KEY, None)

    domain_data.setdefault(DISPATCH_UNSUBS_KEY, []).append(_cleanup)
    # No first fetch here: the first enabled DUID sensor asks for one.
    return created
