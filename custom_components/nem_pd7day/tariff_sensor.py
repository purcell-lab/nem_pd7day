"""
NEM PD7DAY tariff forecast sensors.

One sensor per (distributor, tariff_code) for each NEM region.
State: current interval tariff price in $/kWh.
Attributes: full 7-day tariff forecast as a list of {interval_time, tariff_$/kwh} dicts.
"""
from __future__ import annotations

import logging
from typing import Any

import datetime

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DEFAULT_ENABLED_TARIFFS, DISTRIBUTOR_DISPLAY_NAMES, DOMAIN, TARIFF_NAMES
from .coordinator import PD7DayCoordinator
from .nem_time import now_nem, parse_iso

_LOGGER = logging.getLogger(__name__)

try:
    from aemo_to_tariff import get_daily_fee, get_periods, spot_to_tariff
except ImportError:
    spot_to_tariff = None  # type: ignore[assignment]
    get_periods = None  # type: ignore[assignment]
    get_daily_fee = None  # type: ignore[assignment]
    _LOGGER.warning("aemo_to_tariff not installed — tariff sensors will be unavailable")

# Default loss factors used by aemo_to_tariff library (Energex defaults)
_DEFAULT_DLF = 1.05905
_DEFAULT_MLF = 1.0154
_DEFAULT_MARKET = 1.0154


class NemPd7dayTariffSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """One sensor per (distributor, tariff_code) for the regional device."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_icon = "mdi:currency-usd"
    _attr_has_entity_name = True
    _attr_should_poll = False

    # Small delay after interval boundary to allow coordinator data to settle
    _BOUNDARY_DELAY = datetime.timedelta(seconds=5)

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
            f"{DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())} {tariff_code} {tariff_name} Tariff"
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates and schedule NEM boundary refresh."""
        await super().async_added_to_hass()
        self._schedule_next_boundary()

    def _next_nem_boundary(self) -> datetime.datetime:
        """Return the next :00 or :30 boundary in NEM time (UTC+10), plus a small delay."""
        now = dt_util.now()
        # Truncate to current minute then find next :00 or :30
        minute = now.minute
        if minute < 30:
            next_boundary = now.replace(minute=30, second=0, microsecond=0)
        else:
            next_boundary = (now + datetime.timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
        return next_boundary + self._BOUNDARY_DELAY

    def _schedule_next_boundary(self) -> None:
        """Schedule a one-shot callback at the next NEM interval boundary."""
        self.async_on_remove(
            async_track_point_in_time(
                self.hass,
                self._handle_interval_tick,
                self._next_nem_boundary(),
            )
        )

    async def _handle_interval_tick(self, _now: datetime.datetime) -> None:
        """Called at each NEM interval boundary to update state and reschedule."""
        self.async_write_ha_state()
        self._schedule_next_boundary()

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

    def _get_tariff_periods(self) -> list[dict[str, Any]]:
        """Return tariff period structure with rates converted to $/kWh."""
        if get_periods is None:
            return []
        try:
            periods = []
            for name, start, end, rate_c in get_periods(self._distributor, self._tariff_code):
                periods.append({
                    "period": name,
                    "start": start.strftime("%H:%M"),
                    "end": end.strftime("%H:%M"),
                    "network_rate_$/kwh": round(rate_c / 100, 6),
                })
            return periods
        except Exception:
            _LOGGER.debug(
                "get_periods failed for %s/%s", self._distributor, self._tariff_code,
                exc_info=True,
            )
            return []

    def _get_daily_supply_charge(self) -> float | None:
        """Return daily supply charge in $/day, or None."""
        if get_daily_fee is None:
            return None
        try:
            return get_daily_fee(self._distributor, self._tariff_code)
        except Exception:
            return None

    @staticmethod
    def _build_forecast_description(
        distributor_display: str, tariff_name: str,
        dlf: float, mlf: float, combined: float,
    ) -> str:
        return (
            f"This sensor shows a forecast all-in electricity tariff price in $/kWh, "
            f"calculated by combining the calibrated AEMO PD7DAY 7-day pre-dispatch spot "
            f"price forecast with the applicable network tariff time-of-use component for "
            f"the {distributor_display} {tariff_name} tariff. "
            f"The spot component is the isotonic-calibrated AEMO forecast adjusted for "
            f"distribution loss factor (DLF={dlf}), metering loss factor (MLF={mlf}), "
            f"and market factors (combined multiplier={combined}\u00d7), which increase the "
            f"effective cost of spot energy at the meter relative to the wholesale price. "
            f"The network component ($/kWh) varies by time of day per the tariff period "
            f"structure above and is sourced from AER-approved distributor pricing. "
            f"IMPORTANT: This is a forecast only and should not be relied upon as an "
            f"accurate prediction of actual electricity costs. Spot prices are inherently "
            f"volatile and can differ significantly from forecasts, particularly beyond "
            f"24 hours. Actual tariff costs will also depend on your specific retail "
            f"contract, metering configuration, and any applicable controlled load, "
            f"demand, or feed-in components not captured here."
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Full 7-day tariff forecast with rich metadata."""
        d = self._price_data
        forecast_list: list[dict[str, Any]] = []
        if d is not None:
            for period in d.forecast:
                tariff_val = self._compute_tariff(period)
                forecast_list.append({
                    "time": period.time,
                    "value": tariff_val,
                })

        distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(
            self._distributor, self._distributor.title(),
        )
        tariff_name = TARIFF_NAMES.get(self._distributor, {}).get(
            self._tariff_code, self._tariff_code,
        )

        combined = round(_DEFAULT_DLF * _DEFAULT_MLF * _DEFAULT_MARKET, 6)

        return {
            # Tariff identity
            "tariff_code": self._tariff_code,
            "tariff_name": tariff_name,
            "distributor": distributor_display,
            "region": self._region,
            "network": self._distributor,
            # Tariff period structure
            "tariff_periods": self._get_tariff_periods(),
            # Standing charges
            "daily_supply_charge_$": self._get_daily_supply_charge(),
            "demand_charge": None,
            # Loss factors
            "distribution_loss_factor_dlf": _DEFAULT_DLF,
            "metering_loss_factor_mlf": _DEFAULT_MLF,
            "market_loss_factor": _DEFAULT_MARKET,
            "combined_loss_multiplier": combined,
            # Description
            "forecast_description": self._build_forecast_description(
                distributor_display, tariff_name,
                _DEFAULT_DLF, _DEFAULT_MLF, combined,
            ),
            # Forecast time-series
            "forecast": forecast_list,
        }
