"""
ActualPriceService: one shared TradingIS fetch for every entry.

At HH:02 and HH:32 NEM time, two minutes after each 30-minute boundary so AEMO
has published the last 5-minute dispatch file, it records the interval that
just closed into each registered region's calibration store.

Before #335 every config entry had its own service and client, so a five-region
install made five directory listings and five downloads of the same TradingIS
zip every half hour. The file holds every region, so one service now serves
all entries: one listing and one zip per half hour, parsed once.

The timer runs on UTC minutes. NEM time is UTC+10 all year, so UTC :02 and :32
are NEM :02 and :32 whatever time zone Home Assistant is set to; the old
per-entry timer used Home Assistant's local time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_utc_time_change

import aiohttp

from .calibration_store import CalibrationStore
from .const import DOMAIN, SHARED_TRADINGIS_KEY, TRADINGIS_FETCH_MINUTES
from .nem_time import NEM_TZ, to_nem_iso
from .tradingis_client import TradingISClient

_LOGGER = logging.getLogger(__name__)


def closed_interval_start(now: datetime) -> datetime:
    """The start, in NEM time, of the 30-minute interval that closed at the boundary before ``now``."""
    now_nem = now.astimezone(NEM_TZ)
    boundary = now_nem.replace(minute=(now_nem.minute // 30) * 30, second=0, microsecond=0)
    return boundary - timedelta(minutes=30)


class ActualPriceService:
    """The one TradingIS fetch, recording each half hour into every registered region's store."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        semaphore: Any | None = None,
    ) -> None:
        self._hass = hass
        # The shared NEMWEB semaphore is passed straight through so TradingIS
        # fetches, and the retries added for issue #36, count against the same
        # concurrency cap as every other NEMWEB caller.
        self._client = TradingISClient(
            session,
            executor_job=hass.async_add_executor_job,
            semaphore=semaphore,
        )
        # entry_id -> (region, store). An entry records its own region only.
        self._entries: dict[str, tuple[str, CalibrationStore]] = {}
        self._cancel_timer: Callable[[], None] | None = None

    @property
    def regions(self) -> list[str]:
        return sorted({region for region, _store in self._entries.values()})

    def register(self, entry_id: str, region: str, store: CalibrationStore) -> Callable[[], None]:
        """Record ``region`` into ``store`` from now on; returns the function that stops it.

        The timer starts with the first entry and stops with the last.
        """
        self._entries[entry_id] = (region, store)
        if self._cancel_timer is None:
            self._cancel_timer = async_track_utc_time_change(
                self._hass, self._on_tradingis_tick, minute=TRADINGIS_FETCH_MINUTES, second=0,
            )

        def _unregister() -> None:
            self._entries.pop(entry_id, None)
            if not self._entries and self._cancel_timer is not None:
                self._cancel_timer()
                self._cancel_timer = None

        return _unregister

    async def _on_tradingis_tick(self, now: datetime) -> None:
        """Fetch the just-closed interval once and record it for every registered entry."""
        if not self._entries:
            return
        interval_start = closed_interval_start(now)
        interval_iso = to_nem_iso(interval_start)
        prices = await self._client.fetch_interval_prices(interval_start)
        for region, store in list(self._entries.values()):
            price = prices.get(region)
            if price is not None:
                await store.async_record_actual(
                    interval_iso, price,
                    calibration_region=region,
                    source="tradingis",
                )
            else:
                _LOGGER.debug(
                    "  TradingIS: %s no data for interval_end=%s (NEMtime)",
                    region, interval_iso,
                )


def shared_actual_price_service(
    hass: HomeAssistant, session: aiohttp.ClientSession, semaphore: Any | None = None,
) -> ActualPriceService:
    """The one ActualPriceService, created by the first entry. No await between check and set."""
    domain_data = hass.data[DOMAIN]
    service = domain_data.get(SHARED_TRADINGIS_KEY)
    if service is None:
        service = ActualPriceService(hass, session, semaphore=semaphore)
        domain_data[SHARED_TRADINGIS_KEY] = service
    return service
