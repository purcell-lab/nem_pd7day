"""DataUpdateCoordinator for NEM PD7DAY."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_LAST_SUCCESS_AT,
    FETCH_TIMES_NEM,
    STALE_RUN_GRACE_MIN,
    ATTR_DATA_AGE_HOURS,
    ATTR_IS_STALE,
    ATTR_STALE_REASON,
    NEM_TZ,
    DOMAIN,
    NEMWEB_SEMAPHORE_KEY,
    NOTICE_FETCH_LOCK_KEY,
    NOTICE_FETCH_STAMP_KEY,
    NOTICE_FETCH_WINDOW_S,
    QLD1_INTERCONNECTORS,
    SHARED_FETCH_KEY,
    interconnectors_for_regions,
    region_startup_index,
)
from .dispatch_client import (
    DispatchPrice,
    StaleIntervalError,
    fetch_dispatch_prices,
    parse_settlement,
)
from .pd7day_client import PD7DayClient, PD7DayResult
from . import tod_stats as _tod_stats
from .tod_stats import TodStats
from .nemweb_retry import NemwebFetchError, describe_status

if TYPE_CHECKING:
    from .calibration_engine import RunFeatures
    from .calibration_store import CalibrationStore
    from .forecast_store import ForecastStore
    from .market_notice_client import MarketNoticeClient
    from .notice_store import GridNoticeStore
    from .stpasa_store import StpasaStore

_LOGGER = logging.getLogger(__name__)


def _missed_publish_slot(coordinator: Any, now_utc: datetime) -> str | None:
    """The latest publish slot the served run predates, as "HH:MM", or None.

    A fetch that never runs produces no failure and so never marks the
    coordinator stale (issue #128: the scheduled fetch died silently for a
    day while ``is_stale`` read False). This rule needs no failure: once a
    publish slot is STALE_RUN_GRACE_MIN behind us and the run being served is
    older than that slot, the data is stale whatever the reason. Only the
    PD7DAY coordinator carries a run time; anything else returns None.
    """
    prices = getattr(getattr(coordinator, "data", None), "prices", None)
    if not isinstance(prices, dict) or not prices:
        return None
    served: datetime | None = None
    for price_data in prices.values():
        generated = getattr(price_data, "forecast_generated_at", None)
        if not generated:
            continue
        try:
            run_at = datetime.fromisoformat(str(generated))
        except ValueError:
            continue
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=NEM_TZ)
        if served is None or run_at > served:
            served = run_at
    if served is None:
        return None
    now_nem = now_utc.astimezone(NEM_TZ)
    grace = timedelta(minutes=STALE_RUN_GRACE_MIN)
    latest: datetime | None = None
    for days_back in (0, 1):
        base = (now_nem - timedelta(days=days_back)).replace(second=0, microsecond=0)
        for hour, minute in FETCH_TIMES_NEM:
            slot = base.replace(hour=hour, minute=minute)
            if slot + grace <= now_nem and (latest is None or slot > latest):
                latest = slot
    if latest is None or served >= latest:
        return None
    return latest.strftime("%H:%M")


def staleness_attributes(coordinator: Any) -> dict[str, Any]:
    """data_age_hours, last_success_at, is_stale and stale_reason for entities.

    Issue #105: after one successful fetch no failure ever makes an entity
    unavailable, because the coordinator serves its last good result rather
    than raising. That is the right default, but nothing downstream could tell
    a fresh forecast from one served through a week-long outage. These keys
    make the state legible without changing availability.

    Issue #128: attributes are snapshotted when the entity writes state, so
    ``data_age_hours`` alone reads 0.0 for as long as nothing writes.
    ``last_success_at`` is a timestamp and stays true between writes, and
    ``is_stale`` also fires when a publish slot has passed without its run
    arriving, which is the failure mode a silent scheduler produces.

    Tolerant of coordinators that do not track staleness (test doubles), in
    which case it returns an empty dict and adds nothing.
    """
    last_success_at = getattr(coordinator, "last_success_at", None)
    serving_stale = getattr(coordinator, "serving_stale", None)
    if not isinstance(serving_stale, bool):
        return {}
    now = dt_util.utcnow()
    age: float | None = None
    last_iso: str | None = None
    if isinstance(last_success_at, datetime):
        age = round((now - last_success_at).total_seconds() / 3600.0, 2)
        last_iso = last_success_at.astimezone(NEM_TZ).isoformat()
    missed = _missed_publish_slot(coordinator, now)
    reason = getattr(coordinator, "stale_reason", None)
    if reason is None and missed is not None:
        reason = f"missed {missed} run"
    return {
        ATTR_DATA_AGE_HOURS: age,
        ATTR_LAST_SUCCESS_AT: last_iso,
        ATTR_IS_STALE: serving_stale or missed is not None,
        ATTR_STALE_REASON: reason,
    }


def _mark_success(coordinator: Any) -> None:
    coordinator.last_success_at = dt_util.utcnow()
    coordinator.serving_stale = False
    coordinator.stale_reason = None


def _mark_stale(coordinator: Any, reason: str) -> None:
    coordinator.serving_stale = True
    coordinator.stale_reason = reason


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

    # Staleness tracking (issue #105). Class-level defaults because tests build
    # coordinators with __new__.
    last_success_at: datetime | None = None
    serving_stale: bool = False
    stale_reason: str | None = None

    def staleness_attributes(self) -> dict[str, Any]:
        return staleness_attributes(self)

    def __init__(
        self,
        hass: HomeAssistant,
        regions: list[str],
        store: "CalibrationStore | None" = None,
        interconnector_ids: set[str] | None = None,
        notice_store: "GridNoticeStore | None" = None,
        notice_client: "MarketNoticeClient | None" = None,
        forecast_store: "ForecastStore | None" = None,
        stpasa_store: "StpasaStore | None" = None,
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
        self._forecast_store: "ForecastStore | None" = forecast_store
        self._stpasa_store: "StpasaStore | None" = stpasa_store
        # STPASA interval-start index cache, rebuilt once per STPASA run_datetime.
        # Maps interval START (ISO-8601 NEM +10:00) -> StpasaInterval, plus a
        # sorted list of (epoch_seconds, StpasaInterval) for O(log n) nearest
        # match. Avoids the previous O(intervals x stpasa_intervals) linear scan
        # that ran once per forecast interval per state write.
        self._stpasa_index_run: str | None = None
        # Calibrated forecast memo, keyed by region, owned here rather than by
        # the entities. Three sensors per region ask for the same calibrated
        # forecast and compute it with the same code, so an entity level memo
        # recalibrated roughly 336 intervals three times per region during
        # platform setup. See PD7DayForecastSensor._calibrated_forecast.
        self._calibrated_forecast_cache: dict[str, tuple] = {}
        self._stpasa_index_map: dict[str, Any] = {}
        self._stpasa_index_sorted: list[tuple[float, Any]] = []
        self._first_refresh_done = False
        # 0-based position in the fixed region order — retained for callers that
        # still query it; background refreshes are now staggered in __init__.py.
        self._region_index = region_startup_index(regions[0]) if regions else 0

    def _get_client(self):
        """Return the object this coordinator fetches through.

        Normally the shared fetcher created in __init__.py, which downloads and
        parses PD7DAY once per cycle and serves every region from that one parse.
        It presents the same fetch_all(regions, interconnector_ids) contract as
        PD7DayClient, so everything downstream is unchanged.

        Falls back to a private client when no shared fetcher is registered, so
        the coordinator still works standalone (unit tests, single-entry setups
        constructed directly).
        """
        domain_data = getattr(self.hass, "data", None)
        if isinstance(domain_data, dict):
            shared = domain_data.get(DOMAIN, {}).get(SHARED_FETCH_KEY)
            if shared is not None:
                return shared
        return self._build_own_client()

    def _build_own_client(self) -> PD7DayClient:
        if self._session is None or self._session.closed:
            self._session = async_get_clientsession(self.hass)
        semaphore = None
        domain_data = getattr(self.hass, "data", None)
        if isinstance(domain_data, dict):
            semaphore = domain_data.get(DOMAIN, {}).get(NEMWEB_SEMAPHORE_KEY)
        return PD7DayClient(
            self._session,
            interconnector_ids=self._interconnector_ids,
            semaphore=semaphore,
            # Decompression and CSV parsing run in HA's executor, not on the
            # event loop. async_add_executor_job also tracks the job so HA can
            # await it during shutdown.
            executor_job=self.hass.async_add_executor_job,
        )

    async def _async_update_data(self) -> PD7DayResult:
        client = self._get_client()
        t0 = dt_util.utcnow()
        try:
            # The client retries each NEMWEB request internally now, so there is
            # no retry wrapper here. The previous one re-ran the whole fetch,
            # listing and every file, after a flat 5 s sleep on a 403, and only
            # for PD7DAY. See issue #22.
            result = await client.fetch_all(
                self._regions, self._interconnector_ids
            )
        except NemwebFetchError as exc:
            # Raised by the client once its retry budget is exhausted. Carries
            # the status so the stale-data line can say which one arrived, with
            # 403 and 429 spelled out rather than left as a bare number.
            if self.data is not None:
                _LOGGER.warning(
                    "PD7DAY fetch failed (%s) — serving stale data from last "
                    "successful fetch",
                    describe_status(exc.status) or exc,
                )
                _mark_stale(self, str(describe_status(exc.status) or exc))
                return self.data
            raise UpdateFailed(f"PD7DAY fetch failed: {exc}") from exc
        except aiohttp.ClientResponseError as exc:
            if self.data is not None:
                _LOGGER.warning(
                    "PD7DAY fetch failed (%s %s) — serving stale data from last successful fetch",
                    describe_status(exc.status) or exc.status,
                    exc.message,
                )
                _mark_stale(self, f"{describe_status(exc.status) or exc.status} {exc.message}".strip())
                return self.data
            raise UpdateFailed(f"PD7DAY fetch failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            if self.data is not None:
                _LOGGER.warning(
                    "PD7DAY fetch failed (%s) — serving stale data from last successful fetch",
                    exc,
                )
                _mark_stale(self, str(exc))
                return self.data
            raise UpdateFailed(f"PD7DAY fetch failed: {exc}") from exc

        _mark_success(self)
        elapsed = (dt_util.utcnow() - t0).total_seconds()
        _LOGGER.debug(
            "PD7DAY fetch completed in %.3f seconds — source=%s intervention=%s regions=%s interconnectors=%s",
            elapsed,
            result.source_file,
            result.case.intervention if result.case else "unknown",
            list(result.prices.keys()),
            list(result.interconnectors.keys()),
        )

        # STPASA is fetched centrally (once per cycle) in __init__.py and
        # distributed to every region store; the coordinator only READS its
        # store here.  latest() returns None when no fresh data is available,
        # so the calibration path falls through to isotonic-only silently.

        # Feed forecast history into calibration store
        if self._store is not None:
            stpasa_latest = (
                self._stpasa_store.latest() if self._stpasa_store is not None else None
            )
            for region, price_data in result.prices.items():
                await self._store.ingest_forecast(
                    region=region,
                    price_data=price_data,
                    interconnectors=result.interconnectors,
                    case=result.case,
                    market_summary=result.market_summary,
                    stpasa=stpasa_latest,
                )
            # Recompute time-of-day statistics from updated observations
            self.tod_stats = _tod_stats.compute(self._store.observations, calibration_result=self._store.calibration)

        # Persist the fresh result so the next HA restart can restore sensors
        # instantly (phase 1 of two-phase startup) without a blocking fetch.
        if self._forecast_store is not None:
            await self._forecast_store.save(result)

        # Skip notice fetch during bootstrap first refresh to avoid timeout.
        # The first fetch runs after HA setup completes (second coordinator update).
        if self._first_refresh_done:
            await self.async_fetch_notices()
        else:
            self._first_refresh_done = True

        return result

    async def async_fetch_notices(self) -> None:
        """Fetch new market notices and persist.

        Market notices are not region-specific: the store and client are shared,
        and every region coordinator reads the same notices out of the same
        store. So this runs once per cycle across the whole integration rather
        than once per region, which previously multiplied the directory poll and
        every file fetch by five.
        """
        if self._notice_client is None or self.notice_store is None:
            return

        domain_data = self.hass.data.get(DOMAIN) if self.hass is not None else None
        if not isinstance(domain_data, dict):
            # No shared domain data to coordinate through, so there is no sibling
            # coordinator to deduplicate against. Fetch directly.
            await self._fetch_notices_once()
            return

        lock: asyncio.Lock = domain_data.setdefault(
            NOTICE_FETCH_LOCK_KEY, asyncio.Lock()
        )
        # Serialised rather than checked-then-fetched, so five coordinators
        # arriving together queue behind one fetch and then see its timestamp,
        # instead of all passing a staleness check simultaneously.
        async with lock:
            last = domain_data.get(NOTICE_FETCH_STAMP_KEY)
            now = time.monotonic()
            if last is not None and (now - last) < NOTICE_FETCH_WINDOW_S:
                _LOGGER.debug(
                    "[%s] Market notices fetched %.1f s ago by another region, "
                    "using shared store",
                    self._regions[0],
                    now - last,
                )
                return
            await self._fetch_notices_once()
            domain_data[NOTICE_FETCH_STAMP_KEY] = time.monotonic()

    async def _fetch_notices_once(self) -> None:
        """Poll NEMWEB for current notices and persist any change."""
        assert self._notice_client is not None and self.notice_store is not None
        started = time.monotonic()
        self._notice_client.last_seen_notice_id = self.notice_store.last_seen_notice_id
        new_notices = await self._notice_client.fetch_new_notices()

        # Persist the cursor even when nothing relevant was found. The client has
        # examined those files and will not examine them again, so the store has
        # to record that or the same window is re-read on every cycle.
        cursor_moved = self.notice_store.advance_cursor(
            self._notice_client.last_seen_notice_id
        )
        if new_notices:
            self.notice_store.add_notices(new_notices)
        if new_notices or cursor_moved:
            await self.notice_store.async_save()
        if new_notices:
            _LOGGER.info("Fetched %d new market notices", len(new_notices))
        _LOGGER.debug(
            "[%s] Notice fetch took %.0f ms, cursor %d",
            self._regions[0],
            (time.monotonic() - started) * 1000,
            self.notice_store.last_seen_notice_id,
        )

    @property
    def current_run_features(self) -> "RunFeatures | None":
        """
        Compute RunFeatures from the latest PD7DAY result in coordinator.data.

        Mirrors calibration_engine._compute_run_features so the values fed to
        OLS apply() at runtime match those used during fitting:
          run_max_h6_rrp : max raw forecast for horizon < 6h
          run_mean_rrp   : mean raw forecast for horizon < 24h
          run_spread     : p90 − p10 of raw forecast for horizon < 24h
        Horizon is interval START (period.time) minus the run datetime
        (forecast_generated_at).  Returns None if no usable data.
        """
        from .calibration_engine import RunFeatures, _p90_minus_p10
        from .nem_time import parse_iso

        result = self.data
        if result is None or not result.prices:
            return None
        region = self._regions[0]
        price_data = result.prices.get(region)
        if price_data is None or not price_data.forecast:
            return None
        if not price_data.forecast_generated_at:
            return None
        try:
            run_dt = parse_iso(price_data.forecast_generated_at)
        except Exception:  # noqa: BLE001
            return None

        near: list[float] = []
        day: list[float] = []
        for period in price_data.forecast:
            try:
                start_dt = parse_iso(period.time)
            except Exception:  # noqa: BLE001
                continue
            horizon_hours = (start_dt - run_dt).total_seconds() / 3600.0
            if horizon_hours < 6:
                near.append(period.value)
            if horizon_hours < 24:
                day.append(period.value)

        if not near and not day:
            return None
        return RunFeatures(
            run_max_h6_rrp=max(near) if near else 0.0,
            run_mean_rrp=(sum(day) / len(day)) if day else 0.0,
            run_spread=_p90_minus_p10(day),
        )

    def stpasa_index(self):
        """
        Return a cached STPASA lookup index for the latest STPASA result.

        Returns a tuple ``(result, index_map, sorted_intervals)`` where:
          * ``result`` is the ``StpasaResult`` from the store (or ``None``),
          * ``index_map`` maps interval-START ISO strings to ``StpasaInterval``,
          * ``sorted_intervals`` is a list of ``(epoch_seconds, StpasaInterval)``
            sorted by epoch, for O(log n) nearest-match fallback.

        The index is built once per STPASA ``run_datetime`` and reused across
        every forecast interval and every sensor state write, replacing the old
        O(forecast_intervals x stpasa_intervals) linear scan.
        """
        store = self._stpasa_store
        if store is None:
            return None, {}, []
        result = store.latest()
        if result is None or not result.intervals:
            # Clear the data first and the key last, the same order the
            # rebuild below uses. Here it is only consistency: None never
            # matches a computed cache key, so a reader that lands mid clear
            # recomputes either way. Keeping both branches identical is what
            # makes the invariant readable, which is that the key is only ever
            # visible once the data it names is in place.
            self._stpasa_index_map = {}
            self._stpasa_index_sorted = []
            self._stpasa_index_run = None
            return result, {}, []

        # Rebuild only when the STPASA run changes (fetched_at disambiguates a
        # same-run refetch that could, in principle, change interval contents).
        cache_key = f"{result.run_datetime}|{result.fetched_at}"
        if cache_key != self._stpasa_index_run:
            from .nem_time import interval_start, parse_iso

            index_map: dict[str, Any] = {}
            sorted_intervals: list[tuple[float, Any]] = []
            for si in result.intervals:
                try:
                    start_iso = interval_start(si.interval_datetime)
                    epoch = parse_iso(start_iso).timestamp()
                except (ValueError, TypeError):
                    continue
                index_map[start_iso] = si
                sorted_intervals.append((epoch, si))
            sorted_intervals.sort(key=lambda t: t[0])
            # Publish the run key LAST. It is the freshness token the sensor
            # calibrated forecast memo folds into its cache key, and this method
            # is called both from the event loop and from the calibration warm
            # running in the executor. Assigning the key first left a window in
            # which a reader on the other thread saw the new key alongside the
            # old index, and memoised a forecast built from the old STPASA run
            # under the new run's key, where nothing would ever recompute it.
            # Publishing the key last makes the failure the harmless direction:
            # old key with new data, which is recomputed at the next check.
            self._stpasa_index_map = index_map
            self._stpasa_index_sorted = sorted_intervals
            self._stpasa_index_run = cache_key

        return result, self._stpasa_index_map, self._stpasa_index_sorted


# Seconds after each 5-minute dispatch boundary to poll ELEC_NEM_SUMMARY.
# AEMO typically publishes within ~65–90s of each boundary;
# 75s gives comfortable margin while staying well clear of the 30-minute tariff tick.
_DISPATCH_POLL_DELAY_S = 75


class DispatchCoordinator(DataUpdateCoordinator[dict[str, DispatchPrice]]):
    """5-minute coordinator for AEMO dispatch prices.

    Polling is boundary-aligned: each fetch fires at the next multiple of
    5 minutes past midnight (UTC) plus _DISPATCH_POLL_DELAY_S (35 s).
    NEMWEB publishes TradingIS data ~30 s after each boundary, so the
    +35 s delay ensures fresh data while staying well clear of the
    30-minute tariff tick.

    This replaces the old rolling update_interval approach, which drifted by
    whatever random offset existed at HA startup (observed: up to ~4 min).
    """

    # Class-level defaults: the poll chain state is read by every method, and
    # tests build instances with __new__ without running __init__.
    _pending_cancel: Callable[[], None] | None = None
    _refresh_task: asyncio.Task | None = None
    _stopped: bool = False
    # Staleness tracking (issue #105), same shape as PD7DayCoordinator.
    last_success_at: datetime | None = None
    serving_stale: bool = False
    stale_reason: str | None = None

    def staleness_attributes(self) -> dict[str, Any]:
        return staleness_attributes(self)

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="NEM Dispatch",
            update_interval=None,   # driven by boundary-aligned schedule, not rolling interval
        )
        self.prices: dict[str, DispatchPrice] = {}
        self.last_updated: datetime | None = None
        # The one pending boundary timer. A single slot rather than a list,
        # because each poll replaces the previous timer and a list of spent
        # cancels was never drained (issue #101).
        self._pending_cancel: Callable[[], None] | None = None
        self._refresh_task: asyncio.Task | None = None
        self._stopped = False

    def _next_boundary_utc(self) -> datetime:
        """Return the next 5-minute boundary (UTC) plus _DISPATCH_POLL_DELAY_S."""
        now = dt_util.utcnow()
        # Seconds since midnight UTC
        total_s = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
        # How many seconds until the next 5-min boundary
        remainder = total_s % 300          # seconds into current 5-min window
        until_next = 300 - remainder       # seconds until next boundary
        return now + timedelta(seconds=until_next + _DISPATCH_POLL_DELAY_S)

    def schedule_next_poll(
        self, entry_unsub_list: list[Callable[[], None]] | None = None
    ) -> None:
        """Schedule a one-shot poll at the next 5-minute boundary.

        Call once after async_config_entry_first_refresh().  Each poll
        automatically reschedules the next one.

        The coordinator owns its single pending timer. Before issue #101 the
        first cancel went into the entry's unsubscribe list and every later
        reschedule went into a private list nothing read, so unload cancelled a
        spent timer, the chain kept polling AEMO with no config entry behind
        it, and every reload started another chain beside it.

        entry_unsub_list: optional list to register ``async_shutdown_polling``
        on, so the ConfigEntry can stop the whole chain on unload. Registered
        once, on the first call; later self-reschedules pass nothing.
        """
        if self._stopped:
            return
        fire_at = self._next_boundary_utc()

        @callback
        def _on_fire(_now=None) -> None:
            self._pending_cancel = None
            if self._stopped:
                return
            self._refresh_task = self.hass.async_create_task(self._aligned_refresh())

        self._pending_cancel = async_track_point_in_utc_time(self.hass, _on_fire, fire_at)
        if entry_unsub_list is not None:
            entry_unsub_list.append(self.async_shutdown_polling)
        _LOGGER.debug(
            "Dispatch next boundary poll at %s UTC (+%ds delay)",
            fire_at.strftime("%H:%M:%S"),
            _DISPATCH_POLL_DELAY_S,
        )

    def async_shutdown_polling(self) -> None:
        """Stop the boundary poll chain: cancel the pending timer and any
        in-flight refresh, and refuse to reschedule from here on."""
        self._stopped = True
        if self._pending_cancel is not None:
            self._pending_cancel()
            self._pending_cancel = None
        task = self._refresh_task
        if task is not None and not task.done():
            task.cancel()
        self._refresh_task = None

    @property
    def polling_active(self) -> bool:
        """True while a boundary timer is pending and the chain is not stopped."""
        return not self._stopped and self._pending_cancel is not None

    async def _aligned_refresh(self) -> None:
        """Fetch dispatch data then schedule the next boundary poll."""
        await self.async_refresh()
        # A poll already in flight when unload ran must not resurrect the
        # chain; schedule_next_poll checks the flag itself.
        self.schedule_next_poll()

    async def _async_update_data(self) -> dict[str, DispatchPrice]:
        t0 = dt_util.utcnow()

        # Expected settlement = current 5-min boundary (NEM time).
        # settlement == boundary means the just-closed interval — that's fresh.
        # Only reject data older than boundary (genuinely stale).
        # Strip tzinfo so expected_settlement is tz-naive NEM time
        nem_now = t0.astimezone(NEM_TZ).replace(tzinfo=None)
        boundary_nem = nem_now.replace(
            minute=(nem_now.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        expected_settlement = boundary_nem

        try:
            try:
                prices = await self.hass.async_add_executor_job(
                    fetch_dispatch_prices, expected_settlement
                )
            except StaleIntervalError as stale_exc:
                _LOGGER.debug(
                    "ELEC_NEM_SUMMARY: got stale settlement, expected >= %s (NEMtime) — retrying in 15s (%s)",
                    expected_settlement.strftime("%Y-%m-%dT%H:%M"),
                    stale_exc,
                )
                await asyncio.sleep(15)
                prices = await self.hass.async_add_executor_job(
                    fetch_dispatch_prices, None
                )
                # Check if retry result is still behind expected settlement
                sample = next(iter(prices.values()), None)
                if sample:
                    actual_str = sample.interval_datetime
                    actual_dt = parse_settlement(actual_str)
                    if actual_dt < expected_settlement:
                        _LOGGER.warning(
                            "Dispatch: settlement=%s still behind boundary=%s (NEMtime) after retry — serving anyway",
                            actual_str,
                            expected_settlement.strftime("%Y-%m-%dT%H:%M"),
                        )

            self.prices = prices
            self.last_updated = dt_util.utcnow()
            _mark_success(self)
            # No success-path DEBUG line here, deliberately (issue #33).
            #
            # This block used to log a "Finished fetching ... - N regions"
            # timing line plus one line per region with settlement and price.
            # With five regions that is 6 of the 9 DEBUG lines the dispatch
            # path emitted every 5 minutes, and every one of them was a
            # restatement of something already logged:
            #   * the per-region settlement and price are already in the
            #     single "ELEC_NEM_SUMMARY fetched: ..." line emitted by
            #     dispatch_client.fetch_dispatch_prices(), which covers all
            #     regions in one record;
            #   * the region count and elapsed time are already in the
            #     "Finished fetching %s data in %.3f seconds (success: %s)"
            #     line that DataUpdateCoordinator._async_refresh() emits for
            #     every coordinator in HA core.
            # Failure paths below still log, at WARNING, because those carry
            # information nothing else records.
            return prices
        except Exception as exc:  # noqa: BLE001
            elapsed = (dt_util.utcnow() - t0).total_seconds()
            if self.data is not None:
                _LOGGER.warning(
                    "Finished fetching NEM Dispatch data in %.3f seconds (failed: %s) — serving stale prices",
                    elapsed,
                    exc,
                )
                _mark_stale(self, str(exc))
                return self.data
            _LOGGER.warning(
                "Finished fetching NEM Dispatch data in %.3f seconds (failed, no stale data): %s",
                elapsed,
                exc,
            )
            raise UpdateFailed(f"DispatchIS fetch failed: {exc}") from exc
