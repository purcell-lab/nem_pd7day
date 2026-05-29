"""DataUpdateCoordinator for NEM PD7DAY."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, QLD1_INTERCONNECTORS, interconnectors_for_regions
from .dispatch_client import DispatchPrice, StaleIntervalError, fetch_dispatch_prices
from .pd7day_client import PD7DayClient, PD7DayResult
from . import tod_stats as _tod_stats
from .tod_stats import TodStats

if TYPE_CHECKING:
    from .calibration_store import CalibrationStore
    from .market_notice_client import MarketNoticeClient
    from .notice_store import GridNoticeStore

_LOGGER = logging.getLogger(__name__)


class PD7DayCoordinator(DataUpdateCoordinator[PD7DayResult]):
    """
    Coordinator for NEM PD7DAY data.

    update_interval is set to None — polling is entirely disabled.
    Refreshes are triggered explicitly by async_track_time callbacks in
    __init__.py at the three AEMO publish times (07:30, 13:00, 18:00 NEM
    local time) plus once at startup via async_config_entry_first_refresh().

    This means the integration makes exactly 3 network requests per day
    instead of 48 (one every 30 minutes).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        regions: list[str],
        store: "CalibrationStore | None" = None,
        interconnector_ids: set[str] | None = None,
        notice_store: "GridNoticeStore | None" = None,
        notice_client: "MarketNoticeClient | None" = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,   # no automatic polling — time-triggered only
        )
        self._regions = regions
        derived_ids = interconnectors_for_regions(regions)
        self._interconnector_ids = interconnector_ids or derived_ids or QLD1_INTERCONNECTORS
        self._store = store
        self._session: aiohttp.ClientSession | None = None
        # Cached time-of-day statistics, updated after each refit
        self.tod_stats: TodStats = TodStats()
        self.notice_store: "GridNoticeStore | None" = notice_store
        self._notice_client: "MarketNoticeClient | None" = notice_client
        self._first_refresh_done = False

    def _get_client(self) -> PD7DayClient:
        if self._session is None or self._session.closed:
            self._session = async_get_clientsession(self.hass)
        return PD7DayClient(
            self._session,
            interconnector_ids=self._interconnector_ids,
        )

    async def _async_update_data(self) -> PD7DayResult:
        client = self._get_client()
        t0 = datetime.now(timezone.utc)
        try:
            result = await client.fetch_all(self._regions)
        except aiohttp.ClientResponseError as exc:
            if self.data is not None:
                _LOGGER.warning(
                    "PD7DAY fetch failed (%s %s) — serving stale data from last successful fetch",
                    exc.status,
                    exc.message,
                )
                return self.data
            raise UpdateFailed(f"PD7DAY fetch failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            if self.data is not None:
                _LOGGER.warning(
                    "PD7DAY fetch failed (%s) — serving stale data from last successful fetch",
                    exc,
                )
                return self.data
            raise UpdateFailed(f"PD7DAY fetch failed: {exc}") from exc

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        _LOGGER.debug(
            "PD7DAY fetch completed in %.3f seconds — source=%s intervention=%s regions=%s interconnectors=%s",
            elapsed,
            result.source_file,
            result.case.intervention if result.case else "unknown",
            list(result.prices.keys()),
            list(result.interconnectors.keys()),
        )

        # Feed forecast history into calibration store
        if self._store is not None:
            for region, price_data in result.prices.items():
                await self._store.ingest_forecast(
                    region=region,
                    price_data=price_data,
                    interconnectors=result.interconnectors,
                    case=result.case,
                    market_summary=result.market_summary,
                )
            # Recompute time-of-day statistics from updated observations
            self.tod_stats = _tod_stats.compute(self._store.observations, calibration_result=self._store.calibration)

        # Skip notice fetch during bootstrap first refresh to avoid timeout.
        # The first fetch runs after HA setup completes (second coordinator update).
        if self._first_refresh_done:
            await self.async_fetch_notices()
        else:
            self._first_refresh_done = True

        return result

    async def async_fetch_notices(self) -> None:
        """Fetch new market notices and persist."""
        if self._notice_client is None or self.notice_store is None:
            return
        # Upgrade path: if store has a non-zero cursor but zero stored notices,
        # the previous bootstrap skipped the 7-day backfill. Reset to trigger it.
        total_notices = sum(
            len(v) for v in self.notice_store._notices.values()
        )
        if self.notice_store.last_seen_notice_id > 0 and total_notices == 0:
            self.notice_store.reset_cursor_for_backfill()
        self._notice_client.last_seen_notice_id = self.notice_store.last_seen_notice_id
        new_notices = await self._notice_client.fetch_new_notices()
        if new_notices:
            self.notice_store.add_notices(new_notices)
            await self.notice_store.async_save()
            _LOGGER.info("Fetched %d new market notices", len(new_notices))


# Seconds after each 5-minute dispatch boundary to poll TradingIS.
# NEMWEB publishes TradingIS results at ~30 s into each 5-minute window;
# 35 s gives a small margin above that while still arriving well before
# the 30-minute tariff boundary tick.
_DISPATCH_POLL_DELAY_S = 35


class DispatchCoordinator(DataUpdateCoordinator):
    """5-minute coordinator for AEMO dispatch prices.

    Polling is boundary-aligned: each fetch fires at the next multiple of
    5 minutes past midnight (UTC) plus _DISPATCH_POLL_DELAY_S (35 s).
    NEMWEB publishes TradingIS data ~30 s after each boundary, so the
    +35 s delay ensures fresh data while staying well clear of the
    30-minute tariff tick.

    This replaces the old rolling update_interval approach, which drifted by
    whatever random offset existed at HA startup (observed: up to ~4 min).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="NEM Dispatch",
            update_interval=None,   # driven by boundary-aligned schedule, not rolling interval
        )
        self.prices: dict[str, DispatchPrice] = {}
        self.last_updated: datetime | None = None
        self._unsub_poll: list = []

    def _next_boundary_utc(self) -> datetime:
        """Return the next 5-minute boundary (UTC) plus _DISPATCH_POLL_DELAY_S."""
        now = datetime.now(timezone.utc)
        # Seconds since midnight UTC
        total_s = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
        # How many seconds until the next 5-min boundary
        remainder = total_s % 300          # seconds into current 5-min window
        until_next = 300 - remainder       # seconds until next boundary
        return now + timedelta(seconds=until_next + _DISPATCH_POLL_DELAY_S)

    def schedule_next_poll(self, entry_unsub_list: list | None = None) -> None:
        """Schedule a one-shot poll at the next 5-minute boundary.

        Call once after async_config_entry_first_refresh().  Each poll
        automatically reschedules the next one.

        entry_unsub_list: optional list to append the cancel callback to, so
        the ConfigEntry can unsubscribe on unload.  If None, uses self._unsub_poll.
        """
        fire_at = self._next_boundary_utc()

        @callback
        def _on_fire(_now=None) -> None:
            self.hass.async_create_task(self._aligned_refresh())

        cancel = async_track_point_in_utc_time(self.hass, _on_fire, fire_at)
        target = entry_unsub_list if entry_unsub_list is not None else self._unsub_poll
        target.append(cancel)
        _LOGGER.debug(
            "Dispatch next boundary poll at %s UTC (+%ds delay)",
            fire_at.strftime("%H:%M:%S"),
            _DISPATCH_POLL_DELAY_S,
        )

    async def _aligned_refresh(self) -> None:
        """Fetch dispatch data then schedule the next boundary poll."""
        await self.async_refresh()
        self.schedule_next_poll()

    async def _async_update_data(self):
        t0 = datetime.now(timezone.utc)

        # Expected settlement = current 5-min boundary (NEM time) + 5 min
        nem_now = t0 + timedelta(hours=10)
        boundary_nem = nem_now.replace(
            minute=(nem_now.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        expected_settlement = boundary_nem + timedelta(minutes=5)

        try:
            try:
                prices = await self.hass.async_add_executor_job(
                    fetch_dispatch_prices, expected_settlement
                )
            except StaleIntervalError:
                _LOGGER.debug(
                    "Dispatch: ELEC_NEM_SUMMARY not yet updated for %s (NEMtime) — retrying in 15s",
                    expected_settlement.strftime("%Y-%m-%dT%H:%M"),
                )
                await asyncio.sleep(15)
                prices = await self.hass.async_add_executor_job(
                    fetch_dispatch_prices, None
                )
                # Check if retry result is still behind expected settlement
                sample = next(iter(prices.values()), None)
                if sample:
                    actual_str = sample.interval_datetime
                    try:
                        actual_dt = datetime.strptime(actual_str, "%Y-%m-%dT%H:%M:%S")
                    except ValueError:
                        actual_dt = datetime.strptime(actual_str, "%Y/%m/%d %H:%M:%S")
                    if actual_dt < expected_settlement:
                        _LOGGER.warning(
                            "Dispatch: settlement still behind after retry (got %s, expected %s) — serving anyway",
                            actual_str,
                            expected_settlement.strftime("%Y-%m-%dT%H:%M"),
                        )

            self.prices = prices
            self.last_updated = datetime.now(timezone.utc)
            elapsed = (self.last_updated - t0).total_seconds()
            _LOGGER.debug(
                "Finished fetching NEM Dispatch data in %.3f seconds — %d regions",
                elapsed,
                len(prices),
            )
            for region_id, dp in sorted(prices.items()):
                _LOGGER.debug(
                    "  Dispatch: %s settlement=%s (NEMtime) — $%.4f/kWh",
                    region_id,
                    dp.interval_datetime.replace("/", "-").replace(" ", "T")[:16],
                    dp.rrp,
                )
            return prices
        except Exception as exc:  # noqa: BLE001
            elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
            if self.data is not None:
                _LOGGER.warning(
                    "Finished fetching NEM Dispatch data in %.3f seconds (failed: %s) — serving stale prices",
                    elapsed,
                    exc,
                )
                return self.data
            _LOGGER.warning(
                "Finished fetching NEM Dispatch data in %.3f seconds (failed, no stale data): %s",
                elapsed,
                exc,
            )
            raise UpdateFailed(f"DispatchIS fetch failed: {exc}") from exc
