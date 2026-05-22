"""DataUpdateCoordinator for NEM PD7DAY."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, QLD1_INTERCONNECTORS, interconnectors_for_regions
from .dispatch_client import DispatchPrice, fetch_dispatch_prices
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

        _LOGGER.debug(
            "PD7DAY updated: source=%s intervention=%s regions=%s interconnectors=%s",
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


class DispatchCoordinator(DataUpdateCoordinator):
    """5-minute coordinator for AEMO dispatch prices."""

    def __init__(self, hass: HomeAssistant, region: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"NEM Dispatch {region}",
            update_interval=timedelta(minutes=5),
        )
        self.region = region
        self.prices: dict[str, DispatchPrice] = {}
        self.last_updated: datetime | None = None

    async def _async_update_data(self):
        try:
            prices = await self.hass.async_add_executor_job(fetch_dispatch_prices)
            self.prices = prices
            self.last_updated = datetime.now(timezone.utc)
            return prices
        except Exception as exc:  # noqa: BLE001
            if self.data is not None:
                _LOGGER.warning(
                    "Dispatch fetch failed (%s) — serving stale dispatch price",
                    exc,
                )
                return self.data
            _LOGGER.warning("Dispatch fetch failed (no stale data): %s", exc)
            raise UpdateFailed(f"DispatchIS fetch failed: {exc}") from exc
