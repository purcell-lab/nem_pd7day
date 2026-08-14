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
    DOMAIN,
    FORECAST_MODE_DAYS_2_7,
    get_region,
    interconnectors_for_regions,
    NEMWEB_MAX_CONCURRENT_REQUESTS,
    NEMWEB_SEMAPHORE_KEY,
    REFIT_INTERVAL,
    REGION_STARTUP_ORDER,
    SHARED_FETCH_KEY,
    region_startup_index,
)
from .coordinator import DispatchCoordinator, PD7DayCoordinator
from .forecast_store import ForecastStore
from .market_notice_client import MarketNoticeClient
from .notice_store import GridNoticeStore
from .pd7day_client import PD7DayClient
from .pd7day_shared import ALL_INTERCONNECTORS, SharedPD7DayFetch
from .stpasa_client import StpasaClient
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

    # ── Shared NEMWEB request semaphore ──────────────────────────────────────
    # One semaphore across all region coordinators caps simultaneous requests
    # to www.nemweb.com.au, preventing burst 403s when HA starts the entries
    # concurrently. Created once in an async context, then reused.
    if NEMWEB_SEMAPHORE_KEY not in hass.data[DOMAIN]:
        hass.data[DOMAIN][NEMWEB_SEMAPHORE_KEY] = asyncio.Semaphore(
            NEMWEB_MAX_CONCURRENT_REQUESTS
        )

    # ── Migration: inject default forecast_mode for existing installs ────────
    if CONF_FORECAST_MODE not in entry.options:
        new_options = dict(entry.options)
        new_options[CONF_FORECAST_MODE] = FORECAST_MODE_DAYS_2_7
        hass.config_entries.async_update_entry(entry, options=new_options)

    region: str = get_region(entry)
    interconnector_ids = interconnectors_for_regions([region])

    # ── Calibration store ────────────────────────────────────────────────────
    store = CalibrationStore(hass, region)
    await store.async_load()

    # ── Forecast cache store (per region) ────────────────────────────────────
    forecast_store = ForecastStore(hass, region)

    # ── STPASA store (per region) ─────────────────────────────────────────────
    stpasa_store = StpasaStore(hass, region)
    await stpasa_store.load()
    # Register this region's store for central STPASA distribution. The STPASA
    # ZIP holds every NEM region, so one download (below) populates them all.
    stpasa_stores = hass.data[DOMAIN].setdefault("stpasa_stores", {})
    stpasa_stores[region] = stpasa_store

    # ── Shared market notice store + client ──────────────────────────────────
    # All five region coordinators share ONE notice store + client so the
    # NEMWEB Market_Notice directory is polled once per cycle, not five times.
    session = async_get_clientsession(hass)
    if "notice_store" not in hass.data[DOMAIN]:
        shared_notice_store = GridNoticeStore(hass)
        await shared_notice_store.async_load()
        hass.data[DOMAIN]["notice_store"] = shared_notice_store
    if "notice_client" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["notice_client"] = MarketNoticeClient(session)
    notice_store = hass.data[DOMAIN]["notice_store"]
    notice_client = hass.data[DOMAIN]["notice_client"]

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
    else:
        # No usable cache (first install or long outage): block on first refresh.
        await coordinator.async_config_entry_first_refresh()

    # ── Dispatch coordinator (5-minute polling, shared across all entries) ─────
    _SHARED_DISPATCH = "_shared_dispatch"
    dispatch: DispatchCoordinator = hass.data[DOMAIN].get(_SHARED_DISPATCH)  # type: ignore[assignment]
    if dispatch is None:
        dispatch = DispatchCoordinator(hass)
        await dispatch.async_config_entry_first_refresh()
        # Start boundary-aligned polling once — shared coordinator self-reschedules.
        _dispatch_unsubs: list = []
        dispatch.schedule_next_poll(entry_unsub_list=_dispatch_unsubs)
        # Store cancel callbacks at domain level — cleaned up when last entry unloads.
        hass.data[DOMAIN][_SHARED_DISPATCH] = dispatch
        hass.data[DOMAIN]["_dispatch_unsubs"] = _dispatch_unsubs

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
        try:
            all_results = await client.fetch_all_regions()
            for r, result in all_results.items():
                store_for_region = stores.get(r)
                if store_for_region is not None:
                    await store_for_region.save(result)
            if all_results:
                _LOGGER.debug(
                    "STPASA: fetched and distributed to %d region stores",
                    len(all_results),
                )
            else:
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
            _LOGGER.warning("STPASA central fetch failed (non-fatal): %s", exc)

    # Only register the STPASA fetch listener on the first (QLD1) coordinator so
    # the download fires once per cycle, not once per region coordinator.
    if region_startup_index(region) == 0:
        @callback
        def _on_pd7day_update() -> None:
            entry.async_create_background_task(
                hass,
                _fetch_and_distribute_stpasa(),
                name="nem_pd7day_stpasa_central_fetch",
            )

        entry.async_on_unload(coordinator.async_add_listener(_on_pd7day_update))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
        await coordinator.async_refresh()

    # ── Scheduled fetches at AEMO publish times ──────────────────────────────

    def _next_utc_fire(hour: int, minute: int) -> datetime:
        """Return the next UTC datetime for the given UTC hour:minute."""
        from datetime import timezone as _tz
        now_utc = datetime.now(_tz.utc)
        candidate = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_utc:
            candidate += timedelta(days=1)
        return candidate

    def _schedule_fetch(hour: int, minute: int) -> None:
        """Schedule (or re-schedule) a single fetch point 24 h apart."""
        fire_at = _next_utc_fire(hour, minute)

        @callback
        def _on_fire(_now=None):
            _LOGGER.info(
                "PD7DAY scheduled fetch triggered — NEM time: %s",
                now_nem().strftime("%Y-%m-%dT%H:%M:%S+10:00"),
            )
            async def _fetch_then_refit():
                await coordinator.async_refresh()
                await _do_refit()
            hass.async_create_task(_fetch_then_refit())
            _schedule_fetch(hour, minute)

        cancel = async_track_point_in_utc_time(hass, _on_fire, fire_at)
        entry.async_on_unload(cancel)
        _LOGGER.debug(
            "PD7DAY next fetch at %s UTC (NEM %02d:%02d)",
            fire_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            (hour + 10) % 24,
            minute,
        )

    utc_times = fetch_times_as_utc()  # ["21:30:00", "03:00:00", "08:00:00"]
    for utc_time_str in utc_times:
        t = dt_util.parse_time(utc_time_str)
        if t is None:
            continue
        _schedule_fetch(t.hour, t.minute)

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
    )
    await actual_service.async_setup(entry)

    # ── Periodic calibration refit (daily) ───────────────────────────────────
    @callback
    def _refit(_now=None):
        """Refit calibration models and refresh sensors (24-hour timer)."""
        hass.async_create_task(_do_refit())

    # Always refit on startup to populate iso_model (not persisted to storage).
    # Run as a background task so integration setup completes immediately.
    # Guard: skip if fewer than MIN_OBS observations (nothing to fit).
    if store.observation_count >= 10:
        hass.async_create_task(_do_refit())

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
            for _unsub in hass.data[DOMAIN].pop("_dispatch_unsubs", []):
                _unsub()
            hass.data[DOMAIN].pop("_shared_dispatch", None)
            hass.data[DOMAIN].pop("notice_store", None)
            hass.data[DOMAIN].pop("notice_client", None)
            hass.data[DOMAIN].pop("stpasa_client", None)
            hass.data[DOMAIN].pop("stpasa_stores", None)
            hass.data[DOMAIN].pop(SHARED_FETCH_KEY, None)
            if hass.services.has_service(DOMAIN, "force_refit"):
                hass.services.async_remove(DOMAIN, "force_refit")
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options changes by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)
