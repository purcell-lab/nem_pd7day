"""
NEM PD7DAY sensor platform.

Sensors
-------
PD7DayForecastSensor          — regional spot price, calibrated + confidence interval
PD7DayGasForecastSensor       — NEM-wide gas generation pressure (TJ/day)
PD7DayInterconnectorSensor    — interconnector MW flow + constraint forecast
PD7DayCalibrationSensor       — calibration status, observation count, MAE by bucket
"""
from __future__ import annotations

import logging
from typing import Any

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
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
    ATTR_CURRENT_TJ,
    ATTR_EXPORTLIMIT,
    ATTR_FORECAST,
    ATTR_FORECAST_GENERATED_AT,
    ATTR_GAS_FORECAST,
    ATTR_IC_FORECAST,
    ATTR_IMPORTLIMIT,
    ATTR_INTERCONNECTOR_ID,
    ATTR_INTERVAL_MINUTES,
    ATTR_IS_CONSTRAINED,
    ATTR_LAST_CHANGED,
    ATTR_MARGINALVALUE,
    ATTR_MAX_24H,
    ATTR_MAX_7D_TJ,
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
    CONF_CALIBRATION_REGION,
    CONF_REGIONS,
    COORDINATOR_KEY,
    DEFAULT_CALIBRATION_REGION,
    DEFAULT_REGIONS,
    DEVICE_CONFIGURATION_URL,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    interconnectors_for_regions,
    QLD1_INTERCONNECTORS,
    STORE_KEY,
)
from .coordinator import PD7DayCoordinator

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
    regions: list[str] = entry.options.get(
        CONF_REGIONS,
        entry.data.get(CONF_REGIONS, DEFAULT_REGIONS),
    )
    calibration_region: str = entry.options.get(
        CONF_CALIBRATION_REGION,
        entry.data.get(
            CONF_CALIBRATION_REGION,
            regions[0] if regions else DEFAULT_CALIBRATION_REGION,
        ),
    )
    if calibration_region not in regions and regions:
        calibration_region = regions[0]
    selected_interconnector_ids = interconnectors_for_regions(regions)

    entities: list[SensorEntity] = []

    for region in regions:
        entities.append(PD7DayForecastSensor(coordinator, store, entry, region))
        entities.append(PD7DayRegionSourceFileDatetimeSensor(coordinator, entry, region))
        entities.append(PD7DayRegionDataUpdatedDatetimeSensor(coordinator, entry, region))

    entities.append(PD7DayGasForecastSensor(coordinator, entry))

    live_interconnector_ids = set(coordinator.data.interconnectors) if (
        coordinator.data and coordinator.data.interconnectors
    ) else set()
    for region in regions:
        region_interconnector_ids = interconnectors_for_regions([region])
        interconnector_ids = live_interconnector_ids or region_interconnector_ids or QLD1_INTERCONNECTORS
        for ic_id in sorted(interconnector_ids):
            if ic_id in region_interconnector_ids:
                entities.append(PD7DayInterconnectorSensor(coordinator, entry, region, ic_id))

    # Calibration diagnostic sensor bound to configured calibration region.
    entities.append(PD7DayCalibrationSensor(coordinator, store, entry, calibration_region))

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
        self._attr_name = f"{region} PD7DAY Forecast"
        self.entity_id = f"sensor.{slug}_pd7day_forecast"
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
    def native_value(self) -> float | None:
        d = self._price_data
        if d is None:
            return None
        if self._store:
            cal = self._store.apply_to_price(
                d.current_value,
                horizon_hours=0.0,
                hour_of_day=now_nem().hour,
            )
            return cal["calibrated"]
        return d.current_value

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
                ATTR_CAL_MAE: cal.get("mae"),
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
                calibrated_forecast[1]["calibrated"]
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


# ---------------------------------------------------------------------------
# Gas generation pressure sensor
# ---------------------------------------------------------------------------

class PD7DayGasForecastSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """NEM-wide gas-powered generation forecast (TJ/day)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "TJ"
    _attr_icon = "mdi:fire"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = "nem_pd7day_gas_forecast"
        self._attr_name = "NEM PD7DAY Gas Generation Forecast"
        self.entity_id = "sensor.nem_pd7day_gas_forecast"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_gas")},
            name="NEM PD7DAY Gas Generation",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.market_summary

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._data is not None

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.current_tj if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._data
        if d is None:
            return {}
        return {
            ATTR_RUN_DATETIME: d.run_datetime,
            ATTR_CURRENT_TJ: d.current_tj,
            ATTR_MAX_7D_TJ: d.max_7d_tj,
            ATTR_GAS_FORECAST: [
                {
                    "nemtime": to_nem_iso(parse_iso(p.nemtime)),
                    "time": to_nem_iso(parse_iso(p.time)),
                    "value_tj": p.value_tj,
                }
                for p in d.forecast
            ],
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
        self._attr_name = f"{region} PD7DAY Source File Datetime"
        self.entity_id = f"sensor.nem_pd7day_{region_slug}_source_file_datetime"
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
        self._attr_name = f"{region} PD7DAY Data Updated Datetime"
        self.entity_id = f"sensor.nem_pd7day_{region_slug}_data_updated_datetime"
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
        self._attr_name = f"{region} PD7DAY Interconnector {ic_id}"
        self.entity_id = f"sensor.pd7day_{region_slug}_ic_{ic_slug}"
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

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.current_mwflow if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._data
        if d is None:
            return {}
        current = d.forecast[0] if d.forecast else None
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
        self._attr_name = f"{region} NEM PD7DAY Calibration"
        self.entity_id = f"sensor.nem_pd7day_{region_slug}_calibration"
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
        return attrs
