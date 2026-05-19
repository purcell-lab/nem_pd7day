"""
NEM PD7DAY tariff forecast sensors.

One sensor per (distributor, tariff_code) for each NEM region.
State: current interval tariff price in $/kWh.
Attributes: full 7-day tariff forecast as a list of {interval_time, tariff_$/kwh} dicts.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_ENABLED_TARIFFS, DOMAIN, TARIFF_NAMES
from .coordinator import PD7DayCoordinator
from .nem_time import now_nem, parse_iso

_LOGGER = logging.getLogger(__name__)

try:
    from aemo_to_tariff import spot_to_tariff
except ImportError:
    spot_to_tariff = None  # type: ignore[assignment]
    _LOGGER.warning("aemo_to_tariff not installed — tariff sensors will be unavailable")


class NemPd7dayTariffSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """One sensor per (distributor, tariff_code) for the regional device."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_icon = "mdi:currency-usd"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
        distributor: str,
        tariff_code: str,
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._distributor = distributor
        self._tariff_code = tariff_code
        self._entry = entry
        self._attr_unique_id = (
            f"{entry.entry_id}_{region}_{distributor}_{tariff_code}_tariff"
        )
        tariff_name = TARIFF_NAMES.get(distributor, {}).get(tariff_code, tariff_code)
        self._attr_name = (
            f"{distributor.title()} {tariff_code} {tariff_name} Tariff"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
        )

    @property
    def entity_registry_enabled_default(self) -> bool:
        return (self._distributor, self._tariff_code) in DEFAULT_ENABLED_TARIFFS

    @property
    def _price_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.prices.get(self._region)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self._price_data is not None
            and spot_to_tariff is not None
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
        return forecast[0] if forecast else None

    def _compute_tariff(self, period) -> float | None:
        """Compute tariff price in $/kWh for a single forecast period."""
        if spot_to_tariff is None:
            return None
        try:
            interval_dt = parse_iso(period.time)
            rrp_mwh = period.value * 1000  # $/kWh → $/MWh
            result_c_kwh = spot_to_tariff(
                interval_dt, self._distributor, self._tariff_code, rrp_mwh,
            )
            return round(result_c_kwh / 100, 6)  # c/kWh → $/kWh
        except Exception:
            _LOGGER.debug(
                "spot_to_tariff failed for %s/%s at %s",
                self._distributor, self._tariff_code, period.time,
                exc_info=True,
            )
            return None

    @property
    def native_value(self) -> float | None:
        """Current interval tariff price in $/kWh."""
        d = self._price_data
        if d is None:
            return None
        period = self._current_period(d.forecast)
        if period is None:
            return None
        return self._compute_tariff(period)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Full 7-day tariff forecast."""
        d = self._price_data
        forecast_list = []
        if d is not None:
            for period in d.forecast:
                tariff_val = self._compute_tariff(period)
                forecast_list.append({
                    "interval_time": period.time,
                    "tariff_$/kwh": tariff_val,
                })
        return {
            "distributor": self._distributor,
            "tariff_code": self._tariff_code,
            "region": self._region,
            "forecast": forecast_list,
        }
