"""NEM PD7DAY Price Forecast — Home Assistant integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
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
    COORDINATOR_KEY,
    DEFAULT_REGION,
    DOMAIN,
    get_region,
    interconnectors_for_regions,
    REFIT_INTERVAL,
    STORE_KEY,
)
from .coordinator import PD7DayCoordinator
from .nem_time import fetch_times_as_utc, now_nem

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CAMERA]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NEM PD7DAY from a config entry."""
    from .actual_price_service import ActualPriceService

    hass.data.setdefault(DOMAIN, {})

    region: str = get_region(entry)
    interconnector_ids = interconnectors_for_regions([region])

    # ── Calibration store ────────────────────────────────────────────────────
    store = CalibrationStore(hass, region)
    await store.async_load()

    # ── Coordinator (no automatic polling) ───────────────────────────────────
    coordinator = PD7DayCoordinator(
        hass,
        [region],
        store,
        interconnector_ids=interconnector_ids,
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        COORDINATOR_KEY: coordinator,
        STORE_KEY: store,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ── Shared calibration refit coroutine ──────────────────────────────────

    async def _do_refit() -> None:
        """Refit calibration models and refresh sensors."""
        if store.observation_count < 10:
            _LOGGER.debug(
                "Skipping calibration refit — only %d observations (need ≥ 10)",
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

    def _next_utc_fire(hour: int, minute: int) -> "datetime":
        """Return the next UTC datetime for the given UTC hour:minute."""
        from datetime import datetime, timezone as _tz
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

    if store.observation_count >= 10 and store.calibration is None:
        _refit()

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
            entry_data = hass.data[DOMAIN].get(cfg_entry.entry_id)
            if not entry_data:
                continue
            coord = entry_data[COORDINATOR_KEY]
            st = entry_data[STORE_KEY]
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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN] and hass.services.has_service(DOMAIN, "force_refit"):
            hass.services.async_remove(DOMAIN, "force_refit")
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options changes by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)
