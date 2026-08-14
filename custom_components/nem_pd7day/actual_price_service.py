"""
ActualPriceService — orchestrates TradingIS fetches for actual prices.

Schedules TradingIS price fetches at HH:02 and HH:32 (2 minutes after each
30-minute boundary to allow AEMO to publish the last 5-min dispatch file).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change

import aiohttp

from .calibration_store import CalibrationStore
from .const import TRADINGIS_FETCH_MINUTES
from .nem_time import NEM_TZ, to_nem_iso, current_nem_interval
from .tradingis_client import TradingISClient

_LOGGER = logging.getLogger(__name__)


class ActualPriceService:
    """
    Schedules TradingIS fetches at HH:02 and HH:32 NEM time,
    calls store.async_record_actual() with the result.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        store: CalibrationStore,
        regions: list[str],
        session: aiohttp.ClientSession,
        calibration_region: str | None = None,
    ) -> None:
        self._hass = hass
        self._store = store
        self._regions = regions
        self._client = TradingISClient(
            session, executor_job=hass.async_add_executor_job
        )
        self._calibration_region = calibration_region

    async def async_setup(self, entry: ConfigEntry) -> None:
        """Register 30-min TradingIS scheduler."""
        cancel_timer = async_track_time_change(
            self._hass,
            self._on_tradingis_tick,
            minute=TRADINGIS_FETCH_MINUTES,
            second=0,
        )
        entry.async_on_unload(cancel_timer)

    async def _on_tradingis_tick(self, now: datetime) -> None:
        """
        Called at HH:02 and HH:32 UTC by HA's time-change tracker.
        Computes the just-closed 30-min interval, fetches TradingIS prices.
        """
        # Convert UTC now to NEM time
        now_nem = now.astimezone(NEM_TZ)

        # Compute the just-closed 30-min interval
        boundary = now_nem.replace(
            minute=(now_nem.minute // 30) * 30,
            second=0,
            microsecond=0,
        )
        interval_start = boundary - timedelta(minutes=30)
        interval_iso = to_nem_iso(interval_start)

        for region in self._regions:
            price = await self._client.fetch_interval_price(region, interval_start)

            if price is not None:
                await self._store.async_record_actual(
                    interval_iso, price,
                    calibration_region=self._calibration_region,
                    source="tradingis",
                )
            else:
                _LOGGER.debug(
                    "  TradingIS: %s no data for interval_end=%s (NEMtime)",
                    region, interval_iso,
                )
