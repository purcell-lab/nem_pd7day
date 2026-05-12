"""
NEM PD7DAY sensor platform.

Sensors
-------
PD7DayForecastSensor          -- regional spot price, calibrated + confidence interval
PD7DayInterconnectorSensor    -- interconnector MW flow + constraint forecast
PD7DayCalibrationSensor       -- calibration status, observation count, MAE by bucket
PD7DayTodSensor               -- time-of-day actual price stats (mean/spread per 30-min slot)
NemPd7dayGridNoticesSensor    -- active MSL/LOR market notice count + structured attributes
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from .nem_time import now_nem, parse_iso, to_nem_iso

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import tod_stats as _tod_stats
from .const import (
    ATTR_ATTRIBUTION,
    ATTR_CAL_ACTIVE_BUCKETS,
    ATTR_CAL_CALIBRATED,
    ATTR_CAL_FITTED_AT,
    ATTR_CAL_MAE,
    ATTR_CAL_N_OBS,
    ATTR_CAL_OBS_COUNT,
    ATTR_CAL_P10,
    ATTR_CAL_P50,
    ATTR_CAL_P90,
    ATTR_CAL_SOURCE,
    ATTR_CAL_STATUS,
    ATTR_CAL_SUMMARY,
    ATTR_CAL_TOTAL_BUCKETS,
    ATTR_CHEAPEST_2H,
    ATTR_EXPORTLIMIT,
    ATTR_FORECAST,
    ATTR_FORECAST_GENERATED_AT,
    ATTR_IC_FORECAST,
    ATTR_IMPORTLIMIT,
    ATTR_INTERCONNECTOR_ID,
    ATTR_INTERVAL_MINUTES,
    ATTR_IS_CONSTRAINED,
    ATTR_LAST_CHANGED,
    ATTR_MARGINALVALUE,
    ATTR_MAX_24H,
    ATTR_MAX_VIOLATION_7D,
    ATTR_METEREDMWFLOW,
    ATTR_MIN_24H,
    ATTR_MWFLOW,
    ATTR_MWLOSSES,
    ATTR_NEXT_VALUE,
    ATTR_REGION,
    ATTR_RUN_DATETIME,
    ATTR_SOURCE_FILE,
    ATTR_VIOLATIONDEGREE,
    COORDINATOR_KEY,
    DEVICE_CONFIGURATION_URL,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    get_region,
    interconnectors_for_regions,
    QLD1_INTERCONNECTORS,
    STORE_KEY,
    storage_keys,
)
from .coordinator import PD7DayCoordinator

if TYPE_CHECKING:
    from .notice_store import GridNoticeStore

_LOGGER = logging.getLogger(__name__)


def _horizon_hours(run_at_str: str | None, interval_time_str: str) -> float:
    """
    Compute forecast horizon in hours between run_at and interval_time.
    Both inputs are ISO-8601 +10:00 strings; subtraction of tz-aware
    datetimes is unambiguous regardless of the HA system timezone.
    """
    if not run_at_str:
        return 0.0
    try:
        run_at = parse_iso(run_at_str)
        interval = parse_iso(interval_time_str)
        return max(0.0, (interval - run_at).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PD7DayCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR_KEY]
    store = hass.data[DOMAIN][entry.entry_id].get(STORE_KEY)
    region: str = get_region(entry)

    entities: list[SensorEntity] = []

    entities.append(PD7DayForecastSensor(coordinator, store, entry, region))
    entities.append(PD7DayRegionSourceFileDatetimeSensor(coordinator, entry, region))
    entities.append(PD7DayRegionDataUpdatedDatetimeSensor(coordinator, entry, region))


    # Interconnectors for this region
    region_ic_ids = interconnectors_for_regions([region])
    live_ic_ids = set(coordinator.data.interconnectors) if (
        coordinator.data and coordinator.data.interconnectors
    ) else set()
    for ic_id in sorted(region_ic_ids):
        if ic_id in (live_ic_ids or region_ic_ids):
            entities.append(PD7DayInterconnectorSensor(coordinator, entry, region, ic_id))

    entities.append(PD7DayCalibrationSensor(coordinator, store, entry, region))
    entities.append(PD7DayTodSensor(coordinator, entry, region))

    notice_store = hass.data[DOMAIN][entry.entry_id].get("notice_store")
    if notice_store is not None:
        entities.append(NemPd7dayGridNoticesSensor(coordinator, region, notice_store))

    async_add_entities(entities, update_before_add=True)


# ---------------------------------------------------------------------------
# Price forecast sensor — with calibration
# ---------------------------------------------------------------------------

class PD7DayForecastSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """
    Regional spot price forecast.

    State: calibrated $/kWh when calibration is active, raw PD7DAY otherwise.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = None
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_icon = "mdi:transmission-tower"
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_attribution = ATTR_ATTRIBUTION

    def __init__(self, coordinator, store, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        self._store = store
        slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{slug}_forecast"
        self._attr_name = "Price Forecast"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _price_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.prices.get(self._region)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._price_data is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to 30-min interval ticks so state updates without a new fetch."""
        await super().async_added_to_hass()

        async def _handle_tick(_now) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _handle_tick,
                minute=[0, 30],
                second=5,
            )
        )

    def _current_period(self, forecast: list):
        """Return the forecast period whose interval covers the current NEM time."""
        now = now_nem()
        for period in forecast:
            try:
                interval_start = parse_iso(period.time)
                interval_end = parse_iso(period.nemtime)
                if interval_start <= now < interval_end:
                    return period
            except (ValueError, TypeError):
                continue
        # Fallback: first period (covers startup before first interval boundary)
        return forecast[0] if forecast else None

    @property
    def native_value(self) -> float | None:
        d = self._price_data
        if d is None:
            return None
        period = self._current_period(d.forecast)
        if period is None:
            return None
        if self._store:
            h = _horizon_hours(d.forecast_generated_at, period.time)
            try:
                hour = parse_iso(period.time).hour
            except (ValueError, TypeError):
                hour = now_nem().hour
            cal = self._store.apply_to_price(period.value, h, hour)
            return cal["calibrated"]
        return period.value

    def _calibrate_period(self, period, run_at_str: str | None) -> dict:
        """Build the enriched forecast dict for one PricePeriod."""
        h = _horizon_hours(run_at_str, period.time)
        try:
            hour = parse_iso(period.time).hour
        except (ValueError, TypeError):
            hour = 0

        base = {
            "nemtime": to_nem_iso(parse_iso(period.nemtime)),
            "time": to_nem_iso(parse_iso(period.time)),
            "raw_value": period.value,
            "horizon_hours": round(h, 1),
        }

        if self._store:
            cal = self._store.apply_to_price(period.value, h, hour)
            base.update({
                ATTR_CAL_CALIBRATED: cal["calibrated"],
                ATTR_CAL_P10: cal["p10"],
                ATTR_CAL_P50: cal["p50"],
                ATTR_CAL_P90: cal["p90"],
                ATTR_CAL_MAE: cal.get("ols_mae"),
                ATTR_CAL_SOURCE: cal["calibrated_source"],
                ATTR_CAL_N_OBS: cal["n_obs"],
                "value": cal["calibrated"],
            })
        else:
            base["value"] = period.value

        return base

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._price_data
        if d is None:
            return {}

        run_at = d.forecast_generated_at
        calibrated_forecast = [
            self._calibrate_period(p, run_at) for p in d.forecast
        ]

        return {
            ATTR_REGION: d.region,
            ATTR_FORECAST_GENERATED_AT: run_at,
            ATTR_INTERVAL_MINUTES: d.interval_minutes,
            ATTR_NEXT_VALUE: (
                calibrated_forecast[1].get(ATTR_CAL_CALIBRATED, calibrated_forecast[1].get("value"))
                if len(calibrated_forecast) > 1
                else None
            ),
            ATTR_MIN_24H: d.min_24h_value,
            ATTR_MAX_24H: d.max_24h_value,
            ATTR_CHEAPEST_2H: (
                {
                    "nemtime_start": d.cheapest_2h_window.nemtime_start,
                    "nemtime_end": d.cheapest_2h_window.nemtime_end,
                    "start": d.cheapest_2h_window.start,
                    "end": d.cheapest_2h_window.end,
                    "avg_value": d.cheapest_2h_window.avg_value,
                    "points": d.cheapest_2h_window.points,
                }
                if d.cheapest_2h_window
                else None
            ),
            ATTR_FORECAST: calibrated_forecast,
            ATTR_SOURCE_FILE: d.source_file,
            "calibration_active": (
                self._store is not None
                and self._store.calibration is not None
                and self._store.active_bucket_count > 0
            ),
        }



class PD7DayRegionSourceFileDatetimeSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Per-region diagnostic timestamp for latest source file run datetime."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:file-clock-outline"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: PD7DayCoordinator, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_source_file_datetime"
        self._attr_name = "Source File Datetime"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _price_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.prices.get(self._region)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._price_data is not None

    @property
    def native_value(self):
        d = self._price_data
        if d is None or not d.forecast_generated_at:
            return None
        return parse_iso(d.forecast_generated_at)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._price_data
        if d is None:
            return {}
        return {
            ATTR_REGION: self._region,
            ATTR_SOURCE_FILE: d.source_file,
        }


class PD7DayRegionDataUpdatedDatetimeSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Per-region diagnostic timestamp for latest coordinator data refresh."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:update"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: PD7DayCoordinator, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_data_updated_datetime"
        self._attr_name = "Data Updated"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        updated_at = getattr(self.coordinator.data, "updated_at", None)
        if not updated_at:
            return None
        return parse_iso(updated_at)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_REGION: self._region,
            ATTR_SOURCE_FILE: self.coordinator.data.source_file if self.coordinator.data else None,
        }


# ---------------------------------------------------------------------------
# Interconnector sensor
# ---------------------------------------------------------------------------

class PD7DayInterconnectorSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Interconnector MW flow and constraint forecast."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "MW"
    _attr_device_class = None
    _attr_icon = "mdi:transmission-tower-export"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
        ic_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        self._ic_id = ic_id
        ic_slug = ic_id.lower().replace("-", "_")
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_ic_{ic_slug}"
        self._attr_name = f"Interconnector {ic_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.interconnectors.get(self._ic_id)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._data is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to 30-min interval ticks so state updates without a new fetch."""
        await super().async_added_to_hass()

        async def _handle_tick(_now) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _handle_tick,
                minute=[0, 30],
                second=5,
            )
        )

    def _current_ic_period(self, forecast: list):
        """Return the interconnector period covering the current NEM time."""
        now = now_nem()
        for period in forecast:
            try:
                interval_start = parse_iso(period.time)
                interval_end = parse_iso(period.nemtime)
                if interval_start <= now < interval_end:
                    return period
            except (ValueError, TypeError):
                continue
        return forecast[0] if forecast else None

    @property
    def native_value(self) -> float | None:
        d = self._data
        if d is None:
            return None
        period = self._current_ic_period(d.forecast)
        return period.mwflow if period else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._data
        if d is None:
            return {}
        current = self._current_ic_period(d.forecast)
        return {
            ATTR_INTERCONNECTOR_ID: d.interconnector_id,
            ATTR_RUN_DATETIME: d.run_datetime,
            ATTR_SOURCE_FILE: d.source_file,
            ATTR_IS_CONSTRAINED: d.is_constrained,
            ATTR_VIOLATIONDEGREE: current.violationdegree if current else None,
            ATTR_MAX_VIOLATION_7D: d.max_violation_7d,
            ATTR_MWFLOW: current.mwflow if current else None,
            ATTR_METEREDMWFLOW: current.meteredmwflow if current else None,
            ATTR_MWLOSSES: current.mwlosses if current else None,
            ATTR_MARGINALVALUE: current.marginalvalue if current else None,
            ATTR_EXPORTLIMIT: current.exportlimit if current else None,
            ATTR_IMPORTLIMIT: current.importlimit if current else None,
            ATTR_IC_FORECAST: [
                {
                    "nemtime": to_nem_iso(parse_iso(p.nemtime)),
                    "time": to_nem_iso(parse_iso(p.time)),
                    "mwflow": p.mwflow,
                    "violationdegree": p.violationdegree,
                    "marginalvalue": p.marginalvalue,
                    "exportlimit": p.exportlimit,
                    "importlimit": p.importlimit,
                }
                for p in d.forecast
            ],
        }


# ---------------------------------------------------------------------------
# Calibration diagnostic sensor
# ---------------------------------------------------------------------------

class PD7DayCalibrationSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Calibration pipeline status sensor."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        store,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        self._store = store
        region_slug = region.lower()
        self._attr_unique_id = f"nem_pd7day_{region_slug}_calibration"
        self._attr_name = "Calibration"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> int:
        return self._store.observation_count if self._store else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._store:
            return {ATTR_CAL_STATUS: "store_unavailable"}
        attrs = self._store.summary_attributes()
        attrs[ATTR_REGION] = self._region
        # Merge forecast history metadata
        fh = self._store._forecast_history if self._store else {}
        if fh:
            attrs["forecast_history_entries"] = int(
                sum(len(v) for v in fh.values())
            )
            attrs["forecast_history_intervals"] = len(fh)
            attrs["forecast_history_oldest"] = min(fh.keys())
            attrs["forecast_history_newest"] = max(fh.keys())
            attrs["forecast_history_runs_avg"] = round(
                sum(len(v) for v in fh.values()) / len(fh), 1
            )
        else:
            attrs["forecast_history_entries"] = 0
            attrs["forecast_history_intervals"] = 0
            attrs["forecast_history_oldest"] = None
            attrs["forecast_history_newest"] = None
            attrs["forecast_history_runs_avg"] = 0
        return attrs


# ---------------------------------------------------------------------------
# Time-of-day actual price statistics sensor
# ---------------------------------------------------------------------------

class PD7DayTodSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """
    Reports the mean actual price for the current 30-minute time-of-day slot
    ($/kWh), with full per-slot statistics as attributes.

    State:   mean actual $/kWh for the current slot (or None before enough data)
    Attrs:   unique_intervals, date_from, date_to, slots (list of 48 dicts)

    Updates every 30 minutes alongside the forecast sensors via
    async_track_time_change, and on every coordinator refresh.
    """

    _attr_has_entity_name = True
    _attr_name = "Price ToD Stats"
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-time-four-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._entry = entry
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_tod_stats"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
        )

    @property
    def native_value(self) -> float | None:
        tod = getattr(self.coordinator, "tod_stats", None)
        if tod is None:
            return None
        slot = tod.slot_for_now(now_nem())
        if slot is None or slot.n == 0:
            return None
        return round(slot.mean, 6)

    @property
    def extra_state_attributes(self) -> dict:
        tod = getattr(self.coordinator, "tod_stats", None)
        if tod is None:
            return {}
        return tod.as_attributes()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_tick(_now) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _handle_tick,
                minute=[0, 30],
                second=5,
            )
        )


# ---------------------------------------------------------------------------
# Grid Notices sensor — active MSL/LOR notice count
# ---------------------------------------------------------------------------

class NemPd7dayGridNoticesSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """
    Sensor reporting count of active MSL/LOR market notices for the region.

    State: integer count of active (non-cancelled) notices within next 7 days.
    Attributes: structured notice list + summary counts by type/level.
    """

    _attr_has_entity_name = True
    _attr_name = "Grid Notices"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "notices"

    def __init__(
        self,
        coordinator: "PD7DayCoordinator",
        region: str,
        notice_store: "GridNoticeStore",
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._notice_store = notice_store
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_grid_notices"

    @property
    def native_value(self) -> int:
        """Count of active non-cancelled notices within next 7 days."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=10)))
        horizon = now + timedelta(days=7)
        return len(self._notice_store.get_active_notices(
            self._region, from_dt=now, to_dt=horizon
        ))

    @property
    def extra_state_attributes(self) -> dict:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone(timedelta(hours=10)))
        horizon = now + timedelta(days=7)
        active = self._notice_store.get_active_notices(
            self._region, from_dt=now, to_dt=horizon
        )
        lor_notices = [n for n in active if n.notice_type == "LOR"]
        msl_notices = [n for n in active if n.notice_type == "MSL"]
        max_lor = max((n.level for n in lor_notices), default=None)
        max_msl = max((n.level for n in msl_notices), default=None)
        next_from = min((n.period_from for n in active), default=None)

        return {
            "region": self._region,
            "active_count": len(active),
            "lor_active": len(lor_notices),
            "msl_active": len(msl_notices),
            "max_lor_level": max_lor,
            "max_msl_level": max_msl,
            "next_notice_from": next_from.isoformat() if next_from else None,
            "notices": [n.to_dict() for n in active],
            "last_fetched": self._notice_store.last_fetched_at.isoformat()
                if hasattr(self._notice_store, "last_fetched_at") and self._notice_store.last_fetched_at
                else None,
        }
