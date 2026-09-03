"""NEM PD7DAY Price Forecast — Home Assistant integration."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.event import (
    async_track_time_interval,
    async_track_point_in_utc_time,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .calibration_store import CalibrationStore
from .const import (
    CONF_FORECAST_MODE,
    DEFAULT_REGION,
    DISPATCH_UNSUBS_KEY,
    DOMAIN,
    FORECAST_MODE_DAYS_2_7,
    get_region,
    interconnectors_for_regions,
    NEMWEB_MAX_CONCURRENT_REQUESTS,
    NEMWEB_MIN_REQUEST_GAP_S,
    NEMWEB_SEMAPHORE_KEY,
    REFIT_INTERVAL,
    REGION_STARTUP_ORDER,
    SETUP_LOCK_KEY,
    SHARED_DISPATCH_KEY,
    SHARED_FETCH_KEY,
)
from .coordinator import DispatchCoordinator, PD7DayCoordinator
from .fetch_scheduler import DailyFetchScheduler
from .forecast_store import ForecastStore
from .market_notice_client import MarketNoticeClient
from .nemweb_gate import NemwebGate
from .notice_store import GridNoticeStore
from .pd7day_client import PD7DayClient
from .pd7day_shared import ALL_INTERCONNECTORS, SharedPD7DayFetch
from .shared_dispatch import async_shared_dispatch
from .startup_trace import StartupTrace
from .stpasa_client import StpasaClient
from .stpasa_refresh import (
    FETCH_FAILED,
    FETCH_FRESH,
    REFIT_WAIT_TIMEOUT_S,
    STALE_FETCH_DELAY_S,
    STPASA_REFRESH_KEY,
    StpasaRefreshCoordination,
    run_refit_when_stpasa_ready,
    should_trigger_central_fetch,
)
from .stpasa_store import StpasaStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA, Platform.NUMBER]


@dataclass
class NemPd7dayEntryData:
    """Per-config-entry runtime data stored on ConfigEntry.runtime_data."""

    coordinator: PD7DayCoordinator
    store: CalibrationStore
    forecast_store: ForecastStore
    stpasa_store: StpasaStore
    dispatch: DispatchCoordinator
    notice_store: GridNoticeStore
    region: str
    unsubs: list = field(default_factory=list)


# Typed alias for entries belonging to this integration.
NemPd7dayConfigEntry = ConfigEntry[NemPd7dayEntryData]


async def _delayed_refresh(coordinator: PD7DayCoordinator, delay_s: float) -> None:
    """Sleep delay_s then trigger a background coordinator refresh (phase 2)."""
    await asyncio.sleep(delay_s)
    await coordinator.async_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: NemPd7dayConfigEntry) -> bool:
    """Set up NEM PD7DAY from a config entry."""
    from .actual_price_service import ActualPriceService
    from .nem_time import fetch_times_as_utc, now_nem

    hass.data.setdefault(DOMAIN, {})

    # ── Shared NEMWEB request gate ───────────────────────────────────────────
    # One gate across all region coordinators bounds both simultaneous requests
    # and request frequency against www.nemweb.com.au, preventing the burst
    # 403s that HA starting five entries concurrently used to provoke. Created
    # once in an async context, then reused.
    #
    # NemwebGate is a drop-in for the asyncio.Semaphore that used to live under
    # this key: every NEMWEB client only ever uses it as `async with`, so the
    # added minimum request gap needed no client change. See nemweb_gate.py and
    # issue #22.
    if NEMWEB_SEMAPHORE_KEY not in hass.data[DOMAIN]:
        hass.data[DOMAIN][NEMWEB_SEMAPHORE_KEY] = NemwebGate(
            NEMWEB_MAX_CONCURRENT_REQUESTS,
            NEMWEB_MIN_REQUEST_GAP_S,
        )

    # ── Migration: inject default forecast_mode for existing installs ────────
    if CONF_FORECAST_MODE not in entry.options:
        new_options = dict(entry.options)
        new_options[CONF_FORECAST_MODE] = FORECAST_MODE_DAYS_2_7
        hass.config_entries.async_update_entry(entry, options=new_options)

    region: str = get_region(entry)
    interconnector_ids = interconnectors_for_regions([region])

    # Setup is instrumented because startup cost in this integration has twice
    # been somewhere other than where it appeared to be. See startup_trace.py.
    trace = StartupTrace(region, _LOGGER)

    # ── Calibration store ────────────────────────────────────────────────────
    store = CalibrationStore(hass, region)
    await store.async_load()
    trace.checkpoint(
        "calibration store load", f"{store.observation_count} observations"
    )

    # ── Forecast cache store (per region) ────────────────────────────────────
    forecast_store = ForecastStore(hass, region)

    # ── STPASA store (per region) ─────────────────────────────────────────────
    # One coordination object across all five entries. It carries the stale-cache
    # signal that forces a refetch, the single-flight claim that keeps the shared
    # download to one per cycle, and the gate the startup refit waits on.
    stpasa_refresh: StpasaRefreshCoordination = hass.data[DOMAIN].setdefault(
        STPASA_REFRESH_KEY, StpasaRefreshCoordination()
    )
    stpasa_store = StpasaStore(hass, region, refresh=stpasa_refresh)
    await stpasa_store.load()
    trace.checkpoint(
        "stpasa store load",
        "stale cache, refetch forced" if stpasa_store.loaded_stale else None,
    )
    # Register this region's store for central STPASA distribution. The STPASA
    # ZIP holds every NEM region, so one download (below) populates them all.
    stpasa_stores = hass.data[DOMAIN].setdefault("stpasa_stores", {})
    stpasa_stores[region] = stpasa_store
    # De-register when this entry unloads, not only when the last one does.
    # Otherwise the central STPASA fetch keeps writing .storage for a region
    # the user has removed (issue #106). The trigger for that fetch is
    # registered on any loaded region (#37), so popping here does not stop
    # STPASA refreshes for the regions that remain.
    entry.async_on_unload(lambda: stpasa_stores.pop(region, None))

    # ── Shared market notice store + client ──────────────────────────────────
    # All five region coordinators share ONE notice store + client so the
    # NEMWEB Market_Notice directory is polled once per cycle, not five times.
    session = async_get_clientsession(hass)
    # A plain "if key not in data" check is not enough here: async_load() awaits,
    # so with five entries setting up concurrently every one of them can pass the
    # check before any of them assigns, and five stores get loaded from disk where
    # only the last is kept. A lock makes the check-and-set atomic.
    setup_lock: asyncio.Lock = hass.data[DOMAIN].setdefault(
        SETUP_LOCK_KEY, asyncio.Lock()
    )
    async with setup_lock:
        if "notice_store" not in hass.data[DOMAIN]:
            shared_notice_store = GridNoticeStore(hass)
            await shared_notice_store.async_load()
            hass.data[DOMAIN]["notice_store"] = shared_notice_store
        if "notice_client" not in hass.data[DOMAIN]:
            hass.data[DOMAIN]["notice_client"] = MarketNoticeClient(
                session,
                semaphore=hass.data[DOMAIN].get(NEMWEB_SEMAPHORE_KEY),
            )
    notice_store = hass.data[DOMAIN]["notice_store"]
    notice_client = hass.data[DOMAIN]["notice_client"]
    trace.checkpoint("notice store and client")

    # ── Shared PD7DAY fetcher (one download and parse serves all regions) ────
    # The PD7DAY archive holds every region and every interconnector, so parsing
    # it once for the union costs barely more than parsing it for one region
    # (700 ms vs 631 ms measured) and replaces five such parses per cycle.
    if SHARED_FETCH_KEY not in hass.data[DOMAIN]:
        hass.data[DOMAIN][SHARED_FETCH_KEY] = SharedPD7DayFetch(
            PD7DayClient(
                session,
                interconnector_ids=ALL_INTERCONNECTORS,
                semaphore=hass.data[DOMAIN].get(NEMWEB_SEMAPHORE_KEY),
                executor_job=hass.async_add_executor_job,
            )
        )

    # ── Shared STPASA client (one fetch serves all regions) ──────────────────
    if "stpasa_client" not in hass.data[DOMAIN]:
        semaphore = hass.data[DOMAIN].get(NEMWEB_SEMAPHORE_KEY)
        hass.data[DOMAIN]["stpasa_client"] = StpasaClient(
            session,
            semaphore=semaphore,
            executor_job=hass.async_add_executor_job,
        )

    # ── Coordinator (no automatic polling) ───────────────────────────────────
    coordinator = PD7DayCoordinator(
        hass,
        [region],
        store,
        interconnector_ids=interconnector_ids,
        notice_store=notice_store,
        notice_client=notice_client,
        forecast_store=forecast_store,
        stpasa_store=stpasa_store,
    )

    # ── Two-phase startup ─────────────────────────────────────────────────────
    cached = await forecast_store.load()
    trace.checkpoint(
        "forecast cache load",
        "cache hit" if cached is not None else "cache miss, must fetch",
    )
    if cached is not None:
        # Phase 1: restore cache instantly — sensors available immediately.
        coordinator.async_set_updated_data(cached)
        # Phase 2: schedule a non-blocking background refresh, staggered per
        # region so the 5 coordinators don't all hit NEMWEB at once.
        region_index = REGION_STARTUP_ORDER.get(region, 0)
        delay = 30 + region_index * 5  # 30s, 35s, 40s, 45s, 50s
        entry.async_create_background_task(
            hass,
            _delayed_refresh(coordinator, delay),
            name=f"nem_pd7day_{region}_background_refresh",
        )
        _LOGGER.debug(
            "[STARTUP] %s: restored cached forecast, no NEMWEB request during "
            "setup; background refresh queued for +%d s",
            region,
            delay,
        )
        trace.checkpoint("cache restore and background refresh queued")
    else:
        # No usable cache (first install or long outage): block on first refresh.
        # This is the one path that puts a PD7DAY download inside setup, so say so
        # plainly rather than leaving it to be inferred from timings.
        _LOGGER.info(
            "[STARTUP] %s: no usable forecast cache, so setup will block on a "
            "NEMWEB download. Sensors stay unavailable until it completes.",
            region,
        )
        with trace.phase("blocking first refresh (NEMWEB download)"):
            await coordinator.async_config_entry_first_refresh()

    # ── Dispatch coordinator (5-minute polling, shared across all entries) ─────
    # The claim has to be lock-guarded because the first refresh awaits. See
    # shared_dispatch.py and issue #34 for what went wrong without the lock.
    dispatch = await async_shared_dispatch(hass, setup_lock, trace)

    entry.runtime_data = NemPd7dayEntryData(
        coordinator=coordinator,
        store=store,
        forecast_store=forecast_store,
        stpasa_store=stpasa_store,
        dispatch=dispatch,
        notice_store=notice_store,
        region=region,
    )

    # ── Central STPASA fetch + distribution ──────────────────────────────────
    # The STPASA ZIP contains every NEM region. Download it ONCE per PD7DAY
    # update cycle and populate all registered region stores, instead of each
    # region coordinator downloading the full file independently.
    async def _fetch_and_distribute_stpasa(_now=None) -> None:
        """Download STPASA once and populate all region stores."""
        client: StpasaClient | None = hass.data[DOMAIN].get("stpasa_client")
        stores: dict[str, StpasaStore] = hass.data[DOMAIN].get("stpasa_stores", {})
        if client is None or not stores:
            return
        # Every loaded region carries the trigger now, so this can be entered up
        # to five times per cycle. The claim keeps the download itself single.
        if not stpasa_refresh.claim_fetch():
            _LOGGER.debug(
                "STPASA: another region already holds this cycle's fetch, "
                "skipping the duplicate download for %s",
                region,
            )
            return
        try:
            all_results = await client.fetch_all_regions()
            for r, result in all_results.items():
                store_for_region = stores.get(r)
                if store_for_region is not None:
                    await store_for_region.save(result)
            if all_results:
                stpasa_refresh.mark_fresh()
                _LOGGER.debug(
                    "STPASA: fetched and distributed to %d region stores",
                    len(all_results),
                )
            else:
                # No regions came back, so treat it as a definitive failure for
                # this cycle: any refit waiting on fresh data must not hang.
                stpasa_refresh.mark_failed("no regions in STPASA response")
                # Logged once per cycle here (not per forecast interval in
                # sensor._stpasa_features_for_interval) to avoid log flooding.
                # An empty result means this cycle got no fresh STPASA, so OLS
                # stage-2 falls back to cached/stale STPASA or isotonic-only.
                _LOGGER.warning(
                    "STPASA: no fresh data this cycle — OLS calibration will use "
                    "cached/stale STPASA or fall back to isotonic-only. See earlier "
                    "STPASA fetch warnings for the cause (e.g. NEMWEB 403)."
                )
        except Exception as exc:  # noqa: BLE001
            stpasa_refresh.mark_failed(str(exc))
            _LOGGER.warning("STPASA central fetch failed (non-fatal): %s", exc)

    # The fetch trigger used to be registered only where region_startup_index
    # was 0, meaning QLD1. Disabling or removing the QLD1 entry therefore stopped
    # STPASA refreshes for every region, silently (issue #37). Register it on any
    # loaded region instead. The download stays single because
    # _fetch_and_distribute_stpasa claims the cycle before touching NEMWEB.
    if should_trigger_central_fetch(region, registered_regions=stpasa_stores):
        @callback
        def _on_pd7day_update() -> None:
            entry.async_create_background_task(
                hass,
                _fetch_and_distribute_stpasa(),
                name=f"nem_pd7day_stpasa_central_fetch_{region}",
            )

        entry.async_on_unload(coordinator.async_add_listener(_on_pd7day_update))

    # A stale cache used to produce a warning and nothing else, so the first
    # fresh STPASA of the session did not arrive until the next PD7DAY update,
    # which on the cached startup path is at least 30 s away and can be a full
    # cycle away. Force the fetch now instead. The short delay lets the other
    # entries register their stores first, so this one download still refreshes
    # all five regions.
    if stpasa_refresh.fetch_pending:

        async def _forced_stale_fetch() -> None:
            await asyncio.sleep(STALE_FETCH_DELAY_S)
            await _fetch_and_distribute_stpasa()

        entry.async_create_background_task(
            hass,
            _forced_stale_fetch(),
            name=f"nem_pd7day_stpasa_stale_refetch_{region}",
        )

    trace.checkpoint("coordinators ready")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    trace.checkpoint("platform setup (sensor entities)")

    # ── Shared calibration refit coroutine ──────────────────────────────────

    async def _do_refit() -> None:
        """Refit calibration models and refresh sensors."""
        if store.observation_count < 10:
            _LOGGER.debug(
                "Skipping calibration refit — only %d observations (need >= 10)",
                store.observation_count,
            )
            return
        _LOGGER.info(
            "PD7DAY calibration refit starting (%d observations)",
            store.observation_count,
        )
        await store.async_refit()
        _LOGGER.info(
            "PD7DAY calibration refit complete — %d active buckets",
            store.active_bucket_count,
        )
        # Re-push the data already held rather than calling async_refresh(),
        # which would re-download from NEMWEB. A refit changes how the forecast
        # is calibrated, not the forecast itself, so notifying listeners is
        # enough to get recalibrated values onto the sensors.
        #
        # This matters most at startup: the refit below is kicked off as a task
        # during setup, so a refresh here fires a full NEMWEB fetch per region
        # within seconds of starting, bypassing the staggered background refresh
        # that the two-phase cached startup sets up.
        if coordinator.data is not None:
            coordinator.async_set_updated_data(coordinator.data)
        else:
            await coordinator.async_refresh()

    # ── Scheduled fetches at AEMO publish times ──────────────────────────────

    def _on_publish_time(hour: int, minute: int) -> None:
        _LOGGER.info(
            "PD7DAY scheduled fetch triggered — NEM time: %s",
            now_nem().strftime("%Y-%m-%dT%H:%M:%S+10:00"),
        )

        async def _fetch_then_refit():
            await coordinator.async_refresh()
            await _do_refit()

        # Tied to the entry so an unload cancels a fetch still in flight rather
        # than letting it write to a torn-down store (issue #106).
        entry.async_create_background_task(
            hass,
            _fetch_then_refit(),
            name=f"nem_pd7day_{region}_scheduled_fetch_{hour:02d}{minute:02d}",
        )

    utc_times = fetch_times_as_utc()  # ["21:30:00", "03:00:00", "08:00:00"]
    slots: list[tuple[int, int]] = []
    for utc_time_str in utc_times:
        t = dt_util.parse_time(utc_time_str)
        if t is None:
            continue
        slots.append((t.hour, t.minute))

    # One pending timer per publish slot, replaced on every re-arm, and one
    # unload hook for the lot. The closure this replaces appended every
    # timer's cancel to entry.async_on_unload, three a day, never removed.
    fetch_scheduler = DailyFetchScheduler(hass, slots, _on_publish_time)
    fetch_scheduler.start()
    entry.async_on_unload(fetch_scheduler.cancel_all)
    for slot in slots:
        _LOGGER.debug(
            "PD7DAY next fetch at %s UTC (NEM %02d:%02d)",
            fetch_scheduler.next_fire_at(slot).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (slot[0] + 10) % 24,
            slot[1],
        )

    _LOGGER.info(
        "PD7DAY scheduled fetches registered at %s NEM time (%s UTC)",
        "07:30, 13:00, 18:00",
        ", ".join(utc_times),
    )

    # ── Actual price service (TradingIS) ────────────────────────────────────
    session = async_get_clientsession(hass)
    actual_service = ActualPriceService(
        hass, store, [region], session,
        calibration_region=region,
        semaphore=hass.data[DOMAIN].get(NEMWEB_SEMAPHORE_KEY),
    )
    await actual_service.async_setup(entry)

    # ── Periodic calibration refit (daily) ───────────────────────────────────
    @callback
    def _refit(_now=None):
        """Refit calibration models and refresh sensors (24-hour timer)."""
        entry.async_create_background_task(
            hass, _do_refit(), name=f"nem_pd7day_{region}_daily_refit"
        )

    # Always refit on startup to populate iso_model (not persisted to storage).
    # Run as a background task so integration setup completes immediately.
    # Guard: skip if fewer than MIN_OBS observations (nothing to fit).
    if store.observation_count >= 10:
        if stpasa_refresh.fetch_pending:
            # The refit reads STPASA as an OLS stage-2 feature, and it is the
            # most expensive calibration work the integration does, so it is the
            # worst operation to feed a cache that is past the 90 minute fresh
            # TTL. Hold it until the forced fetch resolves either way.
            @callback
            def _log_refit_gate(outcome: str) -> None:
                if outcome == FETCH_FRESH:
                    _LOGGER.info(
                        "[STARTUP] %s: fresh STPASA arrived, running the "
                        "deferred calibration refit (%d observations)",
                        region,
                        store.observation_count,
                    )
                elif outcome == FETCH_FAILED:
                    _LOGGER.warning(
                        "[STARTUP] %s: forced STPASA refetch failed (%s), "
                        "running the calibration refit anyway on the stale "
                        "cache rather than blocking startup",
                        region,
                        stpasa_refresh.failure_reason or "no reason reported",
                    )
                else:
                    _LOGGER.warning(
                        "[STARTUP] %s: no STPASA fetch outcome within %.0f s, "
                        "running the calibration refit anyway on the stale "
                        "cache rather than blocking startup",
                        region,
                        REFIT_WAIT_TIMEOUT_S,
                    )

            entry.async_create_background_task(
                hass,
                run_refit_when_stpasa_ready(
                    stpasa_refresh,
                    _do_refit,
                    timeout=REFIT_WAIT_TIMEOUT_S,
                    on_outcome=_log_refit_gate,
                ),
                name=f"nem_pd7day_{region}_startup_refit_gated",
            )
            _LOGGER.info(
                "[STARTUP] %s: STPASA cache was stale, so the calibration "
                "refit is deferred until the forced fetch resolves (up to "
                "%.0f s, %d observations)",
                region,
                REFIT_WAIT_TIMEOUT_S,
                store.observation_count,
            )
        else:
            entry.async_create_background_task(
                hass, _do_refit(), name=f"nem_pd7day_{region}_startup_refit"
            )
            _LOGGER.debug(
                "[STARTUP] %s: calibration refit queued as a background task "
                "(%d observations)",
                region,
                store.observation_count,
            )

    entry.async_on_unload(
        async_track_time_interval(hass, _refit, REFIT_INTERVAL)
    )

    # ── Manual refit service ───────────────────────────────────────────────
    async def _handle_force_refit(call: ServiceCall) -> None:
        """Handle nem_pd7day.force_refit service call."""
        entry_id = call.data.get("entry_id")
        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
            if entry_id and cfg_entry.entry_id != entry_id:
                continue
            entry_data: NemPd7dayEntryData | None = getattr(
                cfg_entry, "runtime_data", None
            )
            if not entry_data:
                continue
            coord = entry_data.coordinator
            st = entry_data.store
            _LOGGER.info(
                "force_refit service: refitting entry %s (%d observations)",
                cfg_entry.entry_id,
                st.observation_count,
            )
            await st.async_refit()
            await coord.async_refresh()

    if not hass.services.has_service(DOMAIN, "force_refit"):
        hass.services.async_register(DOMAIN, "force_refit", _handle_force_refit)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    trace.checkpoint("services and listeners")
    trace.log_summary()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: NemPd7dayConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Per-entry runtime data lives on entry.runtime_data and is released by
        # HA automatically once the entry is unloaded. Determine whether any
        # OTHER config entry is still loaded to decide on shared-object cleanup.
        remaining = [
            cfg_entry
            for cfg_entry in hass.config_entries.async_entries(DOMAIN)
            if cfg_entry.entry_id != entry.entry_id
            and cfg_entry.state == ConfigEntryState.LOADED
        ]
        if not remaining:
            # Last entry unloaded — cancel shared dispatch poll and clean up
            for _unsub in hass.data[DOMAIN].pop(DISPATCH_UNSUBS_KEY, []):
                _unsub()
            hass.data[DOMAIN].pop(SHARED_DISPATCH_KEY, None)
            hass.data[DOMAIN].pop("notice_store", None)
            hass.data[DOMAIN].pop("notice_client", None)
            hass.data[DOMAIN].pop("stpasa_client", None)
            hass.data[DOMAIN].pop("stpasa_stores", None)
            hass.data[DOMAIN].pop(STPASA_REFRESH_KEY, None)
            hass.data[DOMAIN].pop(SHARED_FETCH_KEY, None)
            if hass.services.has_service(DOMAIN, "force_refit"):
                hass.services.async_remove(DOMAIN, "force_refit")
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options changes by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)
