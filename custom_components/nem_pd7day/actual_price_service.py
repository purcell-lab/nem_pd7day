"""
ActualPriceService — orchestrates TradingIS fetches and optional Amber fallback.

Schedules TradingIS price fetches at HH:02 and HH:32 (2 minutes after each
30-minute boundary to allow AEMO to publish the last 5-min dispatch file).

Falls back to reading an Amber sensor entity if TradingIS fetch fails and
an Amber entity is configured.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)

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
    Falls back to Amber listener if TradingIS fetch fails.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        store: CalibrationStore,
        regions: list[str],
        session: aiohttp.ClientSession,
        amber_entity: str | None,
        calibration_region: str | None = None,
    ) -> None:
        self._hass = hass
        self._store = store
        self._regions = regions
        self._client = TradingISClient(session)
        self._amber_entity = amber_entity if amber_entity else None
        self._calibration_region = calibration_region

    async def async_setup(self, entry: ConfigEntry) -> None:
        """Register 30-min TradingIS scheduler + optional Amber listener."""
        # Schedule TradingIS fetches at HH:02 and HH:32
        cancel_timer = async_track_time_change(
            self._hass,
            self._on_tradingis_tick,
            minute=TRADINGIS_FETCH_MINUTES,
            second=0,
        )
        entry.async_on_unload(cancel_timer)

        # Optional Amber state-change listener as fallback
        if self._amber_entity:
            @callback
            def _on_amber_state_change(event):
                """Fallback: record Amber price for current interval."""
                new_state = event.data.get("new_state")
                if new_state is None or new_state.state in (
                    STATE_UNAVAILABLE, STATE_UNKNOWN, "unknown", "unavailable", ""
                ):
                    return
                try:
                    actual_rrp = float(new_state.state)
                except (ValueError, TypeError):
                    return
                interval_iso = current_nem_interval()
                self._hass.async_create_task(
                    self._store.async_record_actual(
                        interval_iso, actual_rrp,
                        calibration_region=self._calibration_region,
                        source="amber",
                    )
                )

            cancel_amber = async_track_state_change_event(
                self._hass,
                [self._amber_entity],
                _on_amber_state_change,
            )
            entry.async_on_unload(cancel_amber)

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
                _LOGGER.debug(
                    "TradingIS price for %s interval %s: %.6f $/kWh",
                    region, interval_iso, price,
                )
                await self._store.async_record_actual(
                    interval_iso, price,
                    calibration_region=self._calibration_region,
                    source="tradingis",
                )
            else:
                _LOGGER.debug(
                    "TradingIS fetch returned None for %s interval %s",
                    region, interval_iso,
                )
                # Fallback to Amber if configured
                if self._amber_entity:
                    state = self._hass.states.get(self._amber_entity)
                    if state and state.state not in (
                        STATE_UNAVAILABLE, STATE_UNKNOWN,
                        "unknown", "unavailable", "",
                    ):
                        try:
                            amber_price = float(state.state)
                            _LOGGER.debug(
                                "Using Amber fallback for %s: %.6f",
                                region, amber_price,
                            )
                            await self._store.async_record_actual(
                                interval_iso, amber_price,
                                calibration_region=self._calibration_region,
                                source="amber",
                            )
                        except (ValueError, TypeError):
                            pass
