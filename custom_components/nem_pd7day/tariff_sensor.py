"""
NEM PD7DAY tariff forecast sensors.

One sensor per (distributor, tariff_code) for each NEM region.
State: current interval tariff price in $/kWh.
Attributes: full 7-day tariff forecast as a list of {interval_time, tariff_$/kwh} dicts.
"""
from __future__ import annotations

import contextlib
import io
import logging
import sys
from typing import Any

import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACTIVE_TARIFF,
    CONF_FORECAST_MODE,
    DEFAULT_ADDITIONAL_FEE,
    DEFAULT_ENABLED_TARIFFS,
    DISPATCH_KEY,
    DISTRIBUTOR_DISPLAY_NAMES,
    DOMAIN,
    EXPORT_TARIFF_NAMES,
    EXPORT_TARIFF_PROGRAMS,
    FORECAST_MODE_DAYS_2_7,
    FORECAST_MODE_FULL,
    TARIFF_NAMES,
    additional_fee_entity_id,
)
from .coordinator import PD7DayCoordinator
from .nem_time import _amber_express_cutoff, now_nem, parse_iso

_LOGGER = logging.getLogger(__name__)


def _horizon_hours(run_at_str: str | None, interval_time_str: str) -> float:
    """Compute forecast horizon in hours between run_at and interval_time."""
    if not run_at_str:
        return 0.0
    try:
        run_at = parse_iso(run_at_str)
        interval = parse_iso(interval_time_str)
        return max(0.0, (interval - run_at).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0


@contextlib.contextmanager
def _suppress_stdout():
    """Suppress stdout to silence debug print() calls in aemo_to_tariff library."""
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = old_stdout


try:
    from aemo_to_tariff import get_daily_fee, get_periods, spot_to_tariff, spot_to_feed_in_tariff
    import aemo_to_tariff as _att
except ImportError:
    spot_to_tariff = None  # type: ignore[assignment]
    spot_to_feed_in_tariff = None  # type: ignore[assignment]
    get_periods = None  # type: ignore[assignment]
    get_daily_fee = None  # type: ignore[assignment]
    _att = None  # type: ignore[assignment]
    _LOGGER.warning("aemo_to_tariff not installed — tariff sensors will be unavailable")

# Default loss factors used by aemo_to_tariff library (Energex defaults)
_DEFAULT_DLF = 1.05905
_DEFAULT_MLF = 1.0154
_DEFAULT_MARKET = 1.0154

# Map const.py distributor keys to aemo_to_tariff module names
_DISTRIBUTOR_LIB_MAP = {
    "sapn": "sapower",
}


def get_tariff_name(distributor_key: str, tariff_code: str) -> str:
    """Look up human-readable tariff name from the aemo_to_tariff library."""
    if _att is None:
        return TARIFF_NAMES.get(distributor_key, {}).get(tariff_code, tariff_code)
    lib_key = _DISTRIBUTOR_LIB_MAP.get(distributor_key, distributor_key)
    module = getattr(_att, lib_key, None)
    if module and hasattr(module, "tariffs"):
        tariff_data = module.tariffs.get(tariff_code, {})
        name = tariff_data.get("name", "")
        if name:
            return name
    # Fallback to TARIFF_NAMES const then raw code
    return TARIFF_NAMES.get(distributor_key, {}).get(tariff_code, tariff_code)


def get_export_tariff_name(distributor_key: str, export_code: str) -> str:
    """Look up human-readable export tariff name."""
    if _att is not None:
        lib_key = _DISTRIBUTOR_LIB_MAP.get(distributor_key, distributor_key)
        module = getattr(_att, lib_key, None)
        if module and hasattr(module, "tariffs"):
            tariff_data = module.tariffs.get(export_code, {})
            name = tariff_data.get("name", "")
            if name:
                return name
    return EXPORT_TARIFF_NAMES.get(export_code, export_code)


class NemPd7dayTariffSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """One sensor per (distributor, tariff_code) for the regional device."""

    _attr_state_class = None
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_suggested_display_precision = 4
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
        store=None,
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._distributor = distributor
        self._tariff_code = tariff_code
        self._entry = entry
        self._store = store
        self._attr_unique_id = (
            f"{entry.entry_id}_{region}_{distributor}_{tariff_code}_tariff"
        )
        distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
        tariff_name = get_tariff_name(distributor, tariff_code)
        self._attr_name = f"{distributor_display} {tariff_name} Tariff ({tariff_code})"
        # Per-instance single-entry caches: (cache_key_tuple, result_float)
        self._tariff_cache: tuple[tuple, float] | None = None
        self._period_tariff_cache: tuple[tuple, float] | None = None
        # Static tariff structure — computed once at construction, never changes at runtime
        self._cached_tariff_periods: list[dict[str, Any]] = self._get_tariff_periods()
        self._cached_daily_supply_charge: float | None = self._get_daily_supply_charge()

    async def async_added_to_hass(self) -> None:
        """Subscribe to PD7Day coordinator, DispatchCoordinator, and NEM boundary refresh."""
        await super().async_added_to_hass()
        self._schedule_next_boundary()

        # Subscribe to DispatchCoordinator so state refreshes every 5 minutes
        dispatch_coordinator = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get(DISPATCH_KEY)
        )
        if dispatch_coordinator is not None:
            self.async_on_remove(
                dispatch_coordinator.async_add_listener(
                    lambda: self.async_write_ha_state()
                )
            )

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
        """Base tariff sensors always use DEFAULT_ENABLED_TARIFFS for visibility."""
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

    def _get_additional_fee(self) -> float:
        """Read additional usage fee from native number entity for this region."""
        from .const import DEFAULT_ADDITIONAL_FEE, additional_fee_entity_id
        try:
            entity_id = additional_fee_entity_id(self._region)
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                return float(state.state)
        except (ValueError, AttributeError):
            pass
        return DEFAULT_ADDITIONAL_FEE

    def _calibrated_value(self, period) -> float:
        """Return calibrated price $/kWh for a forecast period, raw if no store."""
        if not self._store:
            return period.value
        d = self._price_data
        run_at = d.forecast_generated_at if d else None
        h = _horizon_hours(run_at, period.time)
        try:
            hour = parse_iso(period.time).hour
        except (ValueError, TypeError):
            hour = 0
        cal = self._store.apply_to_price(period.value, h, hour)
        return cal["calibrated"]

    def _compute_tariff(self, period) -> float | None:
        """Compute tariff price in $/kWh for a single forecast period.

        The library expects AEMO nemtime (interval END) and subtracts 5 min
        internally for period lookup. Passing interval START would place a
        16:00-start interval at 15:55 -> Day rate instead of Evening rate.
        """
        if spot_to_tariff is None:
            return None
        try:
            # Pass nemtime (interval END) — library subtracts 5 min for ToU lookup
            rrp_mwh = self._calibrated_value(period) * 1000  # calibrated spot $/kWh -> $/MWh
            cache_key = (period.nemtime, round(rrp_mwh, 4))
            cache = getattr(self, "_period_tariff_cache", None)
            if cache is not None and cache[0] == cache_key:
                return cache[1]
            interval_dt = parse_iso(period.nemtime)
            # aemo_to_tariff/sapower.py contains debug print() calls; suppress to avoid HA log noise
            with _suppress_stdout():
                result_c_kwh = spot_to_tariff(
                    interval_dt, self._distributor, self._tariff_code, rrp_mwh,
                )
            fee = self._get_additional_fee()
            result = round((result_c_kwh / 100 + fee) * 1.1, 6)
            self._period_tariff_cache = (cache_key, result)
            return result
        except Exception:
            _LOGGER.debug(
                "spot_to_tariff failed for %s/%s at %s",
                self._distributor, self._tariff_code, period.nemtime,
                exc_info=True,
            )
            return None

    def _apply_tariff_to_spot(self, rrp_kwh: float, now_nem_dt: datetime.datetime) -> float | None:
        """Apply tariff to a raw spot price for the current dispatch interval."""
        if spot_to_tariff is None:
            return None
        try:
            # Construct a nemtime (interval END) from current NEM time
            # Round to nearest 5-min boundary + 5min for interval END
            minute = now_nem_dt.minute
            rounded_min = (minute // 5) * 5 + 5
            if rounded_min >= 60:
                nemtime_dt = (now_nem_dt + datetime.timedelta(hours=1)).replace(
                    minute=rounded_min - 60, second=0, microsecond=0
                )
            else:
                nemtime_dt = now_nem_dt.replace(minute=rounded_min, second=0, microsecond=0)
            rrp_mwh = rrp_kwh * 1000
            cache_key = (nemtime_dt.isoformat(), round(rrp_mwh, 4))
            cache = getattr(self, "_tariff_cache", None)
            if cache is not None and cache[0] == cache_key:
                return cache[1]
            # aemo_to_tariff/sapower.py contains debug print() calls; suppress to avoid HA log noise
            with _suppress_stdout():
                result_c_kwh = spot_to_tariff(
                    nemtime_dt, self._distributor, self._tariff_code, rrp_mwh,
                )
            fee = self._get_additional_fee()
            result = round((result_c_kwh / 100 + fee) * 1.1, 6)
            self._tariff_cache = (cache_key, result)
            return result
        except Exception:
            _LOGGER.debug(
                "spot_to_tariff (dispatch) failed for %s/%s",
                self._distributor, self._tariff_code,
                exc_info=True,
            )
            return None

    @property
    def native_value(self) -> float | None:
        """Current interval tariff price in $/kWh."""
        # Try 5-minute dispatch price first
        dispatch = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get(DISPATCH_KEY)
        if dispatch and dispatch.prices.get(self._region):
            rrp_kwh = dispatch.prices[self._region].rrp
            tariff_val = self._apply_tariff_to_spot(rrp_kwh, now_nem())
            if tariff_val is not None:
                return round(tariff_val, 6)

        # Fallback: current interval from PD7DAY forecast
        _LOGGER.debug(
            "%s/%s: no dispatch price for %s — falling back to PD7DAY forecast",
            self._distributor,
            self._tariff_code,
            self._region,
        )
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
            # aemo_to_tariff/sapower.py contains debug print() calls; suppress to avoid HA log noise
            with _suppress_stdout():
                raw_periods = list(get_periods(self._distributor, self._tariff_code))
            periods = []
            for row in raw_periods:
                # aemo_to_tariff returns 4-tuples for most networks but
                # 5-tuples for SAPN: (name, start, end, condition, rate_c)
                # Use positional unpacking: first 3 fixed, rate_c always last.
                if len(row) < 4:
                    continue
                name, start, end = row[0], row[1], row[2]
                rate_c = row[-1]
                if rate_c is None:
                    continue
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
            # aemo_to_tariff/sapower.py contains debug print() calls; suppress to avoid HA log noise
            with _suppress_stdout():
                return get_daily_fee(self._distributor, self._tariff_code)
        except Exception:
            return None

    @staticmethod
    def _build_forecast_description(
        distributor_display: str, tariff_name: str,
        dlf: float, mlf: float, combined: float,
        fee: float = DEFAULT_ADDITIONAL_FEE,
        region: str = "",
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
            f"The final price includes a 10% GST component and an additional usage fee "
            f"(currently {fee:.4f} $/kWh, configurable via the number entity "
            f"'nem_pd7day_{region.lower()}_additional_usage_fee') added before GST. "
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
            # Base tariff sensor always provides full day 1-7 forecast
            for period in d.forecast:
                tariff_val = self._compute_tariff(period)
                forecast_list.append({
                    "time": period.time,           # interval START (nemtime - 30 min)
                    "nemtime": period.nemtime,     # interval END (AEMO convention)
                    "spot": round(self._calibrated_value(period), 6),  # calibrated spot price $/kWh
                    "value": tariff_val,           # spot + network ToU component $/kWh
                })

        distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(
            self._distributor, self._distributor.title(),
        )
        tariff_name = get_tariff_name(self._distributor, self._tariff_code)

        combined = round(_DEFAULT_DLF * _DEFAULT_MLF * _DEFAULT_MARKET, 6)

        fee = self._get_additional_fee()

        return {
            # Tariff identity
            "tariff_code": self._tariff_code,
            "tariff_name": tariff_name,
            "distributor": distributor_display,
            "region": self._region,
            "network": self._distributor,
            # Tariff period structure
            "tariff_periods": getattr(self, "_cached_tariff_periods", None) or self._get_tariff_periods(),
            # Standing charges
            "daily_supply_charge_$": getattr(self, "_cached_daily_supply_charge", self._get_daily_supply_charge()),
            "demand_charge": None,
            # Loss factors
            "distribution_loss_factor_dlf": _DEFAULT_DLF,
            "metering_loss_factor_mlf": _DEFAULT_MLF,
            "market_loss_factor": _DEFAULT_MARKET,
            "combined_loss_multiplier": combined,
            # Additional usage fee & GST
            "additional_usage_fee_$/kwh": fee,
            "gst_multiplier": 1.1,
            # Description
            "forecast_description": self._build_forecast_description(
                distributor_display, tariff_name,
                _DEFAULT_DLF, _DEFAULT_MLF, combined, fee, self._region,
            ),
            # Forecast time-series
            "forecast": forecast_list,
        }


class TariffForecastDays27Sensor(NemPd7dayTariffSensor):
    """Day 2-7 tariff sensor — only registered for the active tariff in days_2_7 mode."""

    _attr_state_class = None
    _attr_suggested_display_precision = 4
    _attr_entity_registry_enabled_default = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
        distributor: str,
        tariff_code: str,
        store=None,
    ) -> None:
        super().__init__(coordinator, entry, region, distributor, tariff_code, store=store)
        distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
        tariff_name = get_tariff_name(distributor, tariff_code)
        self._attr_name = f"Day 2-7 {distributor_display} {tariff_name} Tariff ({tariff_code})"
        self._attr_unique_id = (
            f"nem_pd7day_{region}_{distributor}_{tariff_code}_days27"
        )

    @property
    def entity_registry_enabled_default(self) -> bool:
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Day 2-7 tariff forecast with amber_express_cutoff trim."""
        d = self._price_data
        forecast_list: list[dict[str, Any]] = []
        if d is not None:
            cutoff_dt = _amber_express_cutoff()
            filtered_periods = [
                p for p in d.forecast if parse_iso(p.time) > cutoff_dt
            ]
            for period in filtered_periods:
                tariff_val = self._compute_tariff(period)
                forecast_list.append({
                    "time": period.time,
                    "nemtime": period.nemtime,
                    "spot": round(self._calibrated_value(period), 6),
                    "value": tariff_val,
                })

        distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(
            self._distributor, self._distributor.title(),
        )
        tariff_name = get_tariff_name(self._distributor, self._tariff_code)

        combined = round(_DEFAULT_DLF * _DEFAULT_MLF * _DEFAULT_MARKET, 6)

        fee = self._get_additional_fee()

        return {
            "tariff_code": self._tariff_code,
            "tariff_name": tariff_name,
            "distributor": distributor_display,
            "region": self._region,
            "network": self._distributor,
            "tariff_periods": getattr(self, "_cached_tariff_periods", None) or self._get_tariff_periods(),
            "daily_supply_charge_$": getattr(self, "_cached_daily_supply_charge", self._get_daily_supply_charge()),
            "demand_charge": None,
            "distribution_loss_factor_dlf": _DEFAULT_DLF,
            "metering_loss_factor_mlf": _DEFAULT_MLF,
            "market_loss_factor": _DEFAULT_MARKET,
            "combined_loss_multiplier": combined,
            "additional_usage_fee_$/kwh": fee,
            "gst_multiplier": 1.1,
            "forecast_description": self._build_forecast_description(
                distributor_display, tariff_name,
                _DEFAULT_DLF, _DEFAULT_MLF, combined, fee, self._region,
            ),
            "forecast": forecast_list,
        }


class NemPd7dayExportTariffSensor(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Export tariff sensor — uses spot_to_feed_in_tariff instead of spot_to_tariff."""

    _attr_state_class = None
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_suggested_display_precision = 4
    _attr_icon = "mdi:currency-usd"
    _attr_has_entity_name = True
    _attr_should_poll = False

    _BOUNDARY_DELAY = datetime.timedelta(seconds=5)

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
        distributor: str,
        import_code: str,
        export_code: str,
        store=None,
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._distributor = distributor
        self._import_code = import_code
        self._export_code = export_code
        self._entry = entry
        self._store = store
        self._attr_unique_id = (
            f"{entry.entry_id}_{region}_{distributor}_{import_code}_export_tariff"
        )
        # Per-instance single-entry caches: (cache_key_tuple, result_float)
        self._export_tariff_cache: tuple[tuple, float] | None = None
        self._period_export_tariff_cache: tuple[tuple, float] | None = None
        distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(distributor, distributor.title())
        export_name = get_export_tariff_name(distributor, export_code)
        # Avoid "... Export Export Tariff" when name already contains "Export"
        if "Export" in export_name:
            self._attr_name = f"{distributor_display} {export_name} Tariff ({export_code})"
        else:
            self._attr_name = f"{distributor_display} {export_name} Export Tariff ({export_code})"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_next_boundary()

        dispatch_coordinator = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get(DISPATCH_KEY)
        )
        if dispatch_coordinator is not None:
            self.async_on_remove(
                dispatch_coordinator.async_add_listener(
                    lambda: self.async_write_ha_state()
                )
            )

    def _next_nem_boundary(self) -> datetime.datetime:
        now = dt_util.now()
        minute = now.minute
        if minute < 30:
            next_boundary = now.replace(minute=30, second=0, microsecond=0)
        else:
            next_boundary = (now + datetime.timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
        return next_boundary + self._BOUNDARY_DELAY

    def _schedule_next_boundary(self) -> None:
        self.async_on_remove(
            async_track_point_in_time(
                self.hass,
                self._handle_interval_tick,
                self._next_nem_boundary(),
            )
        )

    async def _handle_interval_tick(self, _now: datetime.datetime) -> None:
        self.async_write_ha_state()
        self._schedule_next_boundary()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
        )

    @property
    def entity_registry_enabled_default(self) -> bool:
        return (self._distributor, self._import_code) in DEFAULT_ENABLED_TARIFFS

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
            and spot_to_feed_in_tariff is not None
        )

    def _current_period(self, forecast: list):
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

    def _get_additional_fee(self) -> float:
        try:
            entity_id = additional_fee_entity_id(self._region)
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                return float(state.state)
        except (ValueError, AttributeError):
            pass
        return DEFAULT_ADDITIONAL_FEE

    def _calibrated_value(self, period) -> float:
        """Return calibrated price $/kWh for a forecast period, raw if no store."""
        if not self._store:
            return period.value
        d = self._price_data
        run_at = d.forecast_generated_at if d else None
        h = _horizon_hours(run_at, period.time)
        try:
            hour = parse_iso(period.time).hour
        except (ValueError, TypeError):
            hour = 0
        cal = self._store.apply_to_price(period.value, h, hour)
        return cal["calibrated"]

    def _compute_export_tariff(self, period) -> float | None:
        if spot_to_feed_in_tariff is None:
            return None
        try:
            rrp_mwh = self._calibrated_value(period) * 1000  # calibrated spot $/kWh -> $/MWh
            cache_key = (period.nemtime, round(rrp_mwh, 4))
            cache = getattr(self, "_period_export_tariff_cache", None)
            if cache is not None and cache[0] == cache_key:
                return cache[1]
            interval_dt = parse_iso(period.nemtime)
            with _suppress_stdout():
                result_c_kwh = spot_to_feed_in_tariff(
                    interval_dt, self._distributor, self._export_code, rrp_mwh,
                )
            result = round(result_c_kwh / 100, 6)
            self._period_export_tariff_cache = (cache_key, result)
            return result
        except Exception:
            _LOGGER.debug(
                "spot_to_feed_in_tariff failed for %s/%s at %s",
                self._distributor, self._export_code, period.nemtime,
                exc_info=True,
            )
            return None

    def _apply_export_tariff_to_spot(self, rrp_kwh: float, now_nem_dt: datetime.datetime) -> float | None:
        if spot_to_feed_in_tariff is None:
            return None
        try:
            minute = now_nem_dt.minute
            rounded_min = (minute // 5) * 5 + 5
            if rounded_min >= 60:
                nemtime_dt = (now_nem_dt + datetime.timedelta(hours=1)).replace(
                    minute=rounded_min - 60, second=0, microsecond=0
                )
            else:
                nemtime_dt = now_nem_dt.replace(minute=rounded_min, second=0, microsecond=0)
            rrp_mwh = rrp_kwh * 1000
            cache_key = (nemtime_dt.isoformat(), round(rrp_mwh, 4))
            cache = getattr(self, "_export_tariff_cache", None)
            if cache is not None and cache[0] == cache_key:
                return cache[1]
            with _suppress_stdout():
                result_c_kwh = spot_to_feed_in_tariff(
                    nemtime_dt, self._distributor, self._export_code, rrp_mwh,
                )
            result = round(result_c_kwh / 100, 6)
            self._export_tariff_cache = (cache_key, result)
            return result
        except Exception:
            _LOGGER.debug(
                "spot_to_feed_in_tariff (dispatch) failed for %s/%s",
                self._distributor, self._export_code,
                exc_info=True,
            )
            return None

    @property
    def native_value(self) -> float | None:
        dispatch = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get(DISPATCH_KEY)
        if dispatch and dispatch.prices.get(self._region):
            rrp_kwh = dispatch.prices[self._region].rrp
            tariff_val = self._apply_export_tariff_to_spot(rrp_kwh, now_nem())
            if tariff_val is not None:
                return round(tariff_val, 6)

        _LOGGER.debug(
            "%s/%s: no dispatch price for %s — falling back to PD7DAY forecast",
            self._distributor,
            self._export_code,
            self._region,
        )
        d = self._price_data
        if d is None:
            return None
        period = self._current_period(d.forecast)
        if period is None:
            return None
        return self._compute_export_tariff(period)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._price_data
        forecast_list: list[dict[str, Any]] = []
        if d is not None:
            for period in d.forecast:
                tariff_val = self._compute_export_tariff(period)
                forecast_list.append({
                    "time": period.time,
                    "nemtime": period.nemtime,
                    "spot": round(self._calibrated_value(period), 6),  # calibrated spot price $/kWh
                    "value": tariff_val,
                })

        distributor_display = DISTRIBUTOR_DISPLAY_NAMES.get(
            self._distributor, self._distributor.title(),
        )
        export_name = get_export_tariff_name(self._distributor, self._export_code)

        combined = round(_DEFAULT_DLF * _DEFAULT_MLF * _DEFAULT_MARKET, 6)
        fee = self._get_additional_fee()

        return {
            "tariff_code": self._export_code,
            "import_tariff_code": self._import_code,
            "tariff_name": export_name,
            "distributor": distributor_display,
            "region": self._region,
            "network": self._distributor,
            "distribution_loss_factor_dlf": _DEFAULT_DLF,
            "metering_loss_factor_mlf": _DEFAULT_MLF,
            "market_loss_factor": _DEFAULT_MARKET,
            "combined_loss_multiplier": combined,
            "additional_usage_fee_$/kwh": fee,
            "gst_multiplier": 1.1,
            "forecast": forecast_list,
        }
