"""DataUpdateCoordinator for NEM PD7DAY."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
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
from . import coordinator_jobs as _jobs
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


def _domain_value(hass: Any, key: str) -> Any:
    """``hass.data[DOMAIN][key]``, or None when it is not there or hass carries
    no data dict (test doubles)."""
    domain_data = getattr(hass, "data", None)
    if isinstance(domain_data, dict):
        return domain_data.get(DOMAIN, {}).get(key)
    return None


def restore_cached(coordinator: Any, cached: PD7DayResult) -> None:
    """Serve a forecast restored from the cache, phase 1 of two-phase startup (#365).

    The cache is saved on each successful fetch, so its ``updated_at`` is when
    that success was, and it becomes ``last_success_at``. A cache restored
    across the overnight gap is hours old, and ``data_age_hours`` then says
    so rather than reading None until the background refresh lands.
    ``is_stale`` needs nothing more: the missed slot rule reads the run time,
    so a restored run that a publish has since superseded reads stale until
    the refresh replaces it. A cache without a readable ``updated_at`` leaves
    ``last_success_at`` as it was.
    """
    try:
        fetched_at = datetime.fromisoformat(str(cached.updated_at)) if cached.updated_at else None
    except ValueError:
        fetched_at = None
    if fetched_at is not None:
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=NEM_TZ)
        coordinator.last_success_at = fetched_at.astimezone(timezone.utc)
    coordinator.async_set_updated_data(cached)


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
        # STPASA interval index cache and the run key it was built for, rebuilt
        # once per STPASA run by stpasa_index (coordinator_jobs.build_stpasa_index).
        self._stpasa_index_run: str | None = None
        self._stpasa_index_map: dict[str, Any] = {}
        self._stpasa_index_sorted: list[tuple[float, Any]] = []
        # Calibrated forecast memo, keyed by region, owned here rather than by
        # the entities. Three sensors per region ask for the same calibrated
        # forecast and compute it with the same code, so an entity level memo
        # recalibrated roughly 336 intervals three times per region during
        # platform setup. See PD7DayForecastSensor._calibrated_forecast.
        self._calibrated_forecast_cache: dict[str, tuple] = {}
        # current_run_features' cache: (key, RunFeatures | None), key being
        # (forecast_generated_at, interval count). Declared here alongside
        # its sibling above rather than left to spring into existence via
        # getattr on first use, so the object's state is visible in __init__.
        self._run_features_cache: tuple[tuple[str, int], Any] | None = None
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
        shared = _domain_value(self.hass, SHARED_FETCH_KEY)
        if shared is not None:
            return shared
        return self._build_own_client()

    def _build_own_client(self) -> PD7DayClient:
        if self._session is None or self._session.closed:
            self._session = async_get_clientsession(self.hass)
        return PD7DayClient(
            self._session,
            interconnector_ids=self._interconnector_ids,
            semaphore=_domain_value(self.hass, NEMWEB_SEMAPHORE_KEY),
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

        # Feed forecast history into calibration store, then recompute
        # tod_stats from the updated observations.
        if self._store is not None:
            self.tod_stats = await _jobs.feed_calibration_store(
                self._store, self._stpasa_store, result
            )

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
        """Fetch new market notices and persist, once per cycle across every
        region coordinator (see coordinator_jobs.fetch_notices)."""
        await _jobs.fetch_notices(
            self.hass, self._notice_client, self.notice_store, self._regions
        )

    @property
    def current_run_features(self) -> "RunFeatures | None":
        """
        RunFeatures for the latest PD7DAY result in coordinator.data, computed
        once per run by coordinator_jobs.cached_run_features (what they are is
        in compute_run_features there). Returns None if no usable data.

        Cached against the run identity (its generated-at stamp and interval
        count). calibrate_interval reads this once per interval, so without
        the cache a cold calibration of a 336 interval run parsed every
        interval's timestamp 336 times over, 115,795 parses, which was 92 per
        cent of a cold tariff state write and the source of the 0.4 to 0.9 s
        event loop stalls in issue #135.
        """
        # getattr, not a direct read: tests reuse this property on stand-in
        # coordinator classes that never run PD7DayCoordinator.__init__, and
        # must still degrade to an uncached first read rather than raising.
        features, self._run_features_cache = _jobs.cached_run_features(
            self.data, self._regions, getattr(self, "_run_features_cache", None)
        )
        return features

    def stpasa_index(self):
        """``(result, index_map, sorted_intervals)`` for the latest STPASA result.

        ``result`` is the store's ``StpasaResult`` (or ``None``), and the index
        is coordinator_jobs.build_stpasa_index's, built once per STPASA run and
        reused across every forecast interval and every sensor state write.
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
            index_map, sorted_intervals = _jobs.build_stpasa_index(result)
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
# When the poll still sees the previous interval, AEMO has not published yet:
# retry this often, this many times (to about 2 minutes past the boundary),
# before serving what it has (#219).
_DISPATCH_RETRY_DELAY_S = 15
_DISPATCH_RETRIES = 3


class DispatchCoordinator(DataUpdateCoordinator[dict[str, DispatchPrice]]):
    """5-minute coordinator for AEMO dispatch prices.

    Polling is boundary-aligned: each fetch fires at the next multiple of
    5 minutes past midnight (UTC) plus _DISPATCH_POLL_DELAY_S (75 s). The
    price AEMO publishes after boundary B is the interval ending B + 5 min
    (SETTLEMENTDATE is the interval end), usually 60 to 90 s after B; a poll
    that still sees the interval ending at B retries (#219).

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

    async def _retry_until_fresh(
        self, expected_settlement: datetime, stale_exc: StaleIntervalError,
    ) -> dict[str, DispatchPrice]:
        """Refetch until the interval ending at *expected_settlement* is published.

        Up to _DISPATCH_RETRIES attempts, _DISPATCH_RETRY_DELAY_S apart. If
        AEMO is still behind after the last one, serve what it returns and say
        so: that interval is missed.
        """
        expected_str = expected_settlement.strftime("%Y-%m-%dT%H:%M")
        for attempt in range(1, _DISPATCH_RETRIES + 1):
            _LOGGER.debug(
                "ELEC_NEM_SUMMARY: got stale settlement, expected >= %s (NEMtime) — "
                "retry %d of %d in %ds (%s)",
                expected_str, attempt, _DISPATCH_RETRIES, _DISPATCH_RETRY_DELAY_S, stale_exc,
            )
            await asyncio.sleep(_DISPATCH_RETRY_DELAY_S)
            try:
                return await self.hass.async_add_executor_job(
                    fetch_dispatch_prices, expected_settlement
                )
            except StaleIntervalError as exc:
                stale_exc = exc
        prices = await self.hass.async_add_executor_job(fetch_dispatch_prices, None)
        sample = next(iter(prices.values()), None)
        if sample and parse_settlement(sample.interval_datetime) < expected_settlement:
            _LOGGER.warning(
                "Dispatch: settlement=%s still behind expected=%s (NEMtime) after %d "
                "retries — serving anyway; that interval is missed",
                sample.interval_datetime, expected_str, _DISPATCH_RETRIES,
            )
        return prices

    async def _async_update_data(self) -> dict[str, DispatchPrice]:
        t0 = dt_util.utcnow()

        # SETTLEMENTDATE is the interval END (dispatch_client). The price AEMO
        # publishes just after boundary B is the interval ending B + 5 min, so
        # that is the fresh one. Expecting B instead accepted the previous
        # interval whenever AEMO published after the poll, and the interval
        # ending B + 5 was then never fetched: about 1 in 4 on the live
        # install (#219). Tz-naive NEM time, as parse_settlement returns.
        nem_now = t0.astimezone(NEM_TZ).replace(tzinfo=None)
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
            except StaleIntervalError as stale_exc:
                prices = await self._retry_until_fresh(expected_settlement, stale_exc)

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
