"""DUID local price adjustment sensors, opt in per config entry (#275).

For each DUID listed in the entry's options:

- ``<DUID> Local Price Adjustment``: the latest dispatch interval's
  adjustment, from the DispatchIS file the binding constraints already read.
- ``<DUID> Local Price Adjustment Forecast``: the newest P5MIN run's
  adjustments as a ``forecast`` list, for the intervals still to end and only
  up to the first interval without a row.
- ``<DUID> Local Price`` (the diagnostic option): clip(ROP + adjustment,
  floor, cap) for the entry's region, a diagnostic of the dispatch run, not a
  regional, retail or settlement price.

A state is unknown, never 0, when the DUID has no row: ``row_status`` says why.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_CONFIGURATION_URL, DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN
from .local_price import (
    INTERVAL,
    STATUS_PUBLISHED,
    DispatchLocalPrices,
    dispatch_status,
    forecast_series,
    local_price_mwh,
    market_price_limits,
)
from .nem_time import NEM_TZ, now_nem

# Three missed intervals: older than this, a file no longer describes now.
MAX_AGE = timedelta(minutes=15)
UNIT = "$/kWh"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(NEM_TZ).isoformat() if value is not None else None


class _LocalPriceSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:map-marker-radius"
    _attr_native_unit_of_measurement = UNIT
    _attr_suggested_display_precision = 4
    _attr_state_class = SensorStateClass.MEASUREMENT
    _suffix = ""
    _label = ""

    def __init__(self, coordinator: Any, entry: Any, region: str, duid: str) -> None:
        super().__init__(coordinator)
        self._region = region
        self._duid = duid
        self._attr_name = f"{duid} {self._label}"
        self._attr_unique_id = f"{entry.entry_id}_{region}_{duid}_{self._suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{region}")},
            name=f"NEM PD7DAY {region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    def _base(self, source: str) -> dict[str, Any]:
        return {"region": self._region, "duid": self._duid, "source": source}


class _DispatchLocalPriceSensor(_LocalPriceSensor):
    """Reads the local prices the shared ConstraintCoordinator's file carried."""

    def _prices(self) -> Optional[DispatchLocalPrices]:
        snapshot = self.coordinator.tracker.latest
        return snapshot.local_prices if snapshot is not None else None

    def _dispatch_attrs(self, prices: Optional[DispatchLocalPrices], now: datetime) -> dict[str, Any]:
        row = prices.rows.get(self._duid) if prices is not None else None
        attrs = {
            **self._base("DISPATCH_LOCAL_PRICE"),
            "row_status": dispatch_status(prices, self._duid, now, MAX_AGE),
            "adjustment_mwh": row.adjustment_mwh if row is not None else None,
            "locally_constrained": row.locally_constrained if row is not None else None,
        }
        if prices is None:
            return attrs
        return {
            **attrs,
            # interval_start is the interval's start, interval_end the SETTLEMENTDATE.
            "interval_start": _iso(prices.interval_end - INTERVAL),
            "interval_end": _iso(prices.interval_end),
            "source_file": prices.source,
            "retrieved_at": _iso(prices.retrieved_at),
            "age_seconds": round((now - prices.interval_end).total_seconds()),
        }


class NemPd7dayLocalPriceAdjustmentSensor(_DispatchLocalPriceSensor):
    """State: the DUID's dispatch LOCAL_PRICE_ADJUSTMENT in $/kWh; unknown without a row."""

    _suffix = "local_price_adjustment"
    _label = "Local Price Adjustment"

    @property
    def native_value(self) -> Optional[float]:
        prices = self._prices()
        if dispatch_status(prices, self._duid, now_nem(), MAX_AGE) != STATUS_PUBLISHED:
            return None
        return round(prices.rows[self._duid].adjustment_mwh / 1000.0, 6)  # type: ignore[union-attr]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._dispatch_attrs(self._prices(), now_nem())


class NemPd7dayLocalPriceSensor(_DispatchLocalPriceSensor):
    """State: clip(ROP + adjustment, floor, cap) in $/kWh for the entry's region.

    ROP, not RRP: the original price before any intervention pricing. The
    region's ROP is the same file's, so the two are of one interval and run;
    a missing ROP leaves the state unknown rather than substituting RRP.
    """

    _suffix = "local_price"
    _label = "Local Price"
    _attr_icon = "mdi:cash-marker"

    def _value_mwh(self, prices: Optional[DispatchLocalPrices]) -> Optional[float]:
        if dispatch_status(prices, self._duid, now_nem(), MAX_AGE) != STATUS_PUBLISHED:
            return None
        rop = prices.rop.get(self._region)  # type: ignore[union-attr]
        if rop is None:
            return None
        return local_price_mwh(rop, prices.rows[self._duid].adjustment_mwh, prices.interval_end)  # type: ignore[union-attr]

    @property
    def native_value(self) -> Optional[float]:
        value = self._value_mwh(self._prices())
        return round(value / 1000.0, 6) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        prices = self._prices()
        attrs = self._dispatch_attrs(prices, now_nem())
        if prices is None:
            return attrs
        floor, cap = market_price_limits(prices.interval_end)
        return {
            **attrs,
            "rop_mwh": prices.rop.get(self._region),
            "local_price_mwh": self._value_mwh(prices),
            "price_floor_mwh": floor,
            "price_cap_mwh": cap,
            "note": "diagnostic: not the regional, retail or settlement price",
        }


class NemPd7dayLocalPriceAdjustmentForecastSensor(_LocalPriceSensor):
    """State: the adjustment for the interval now running, from the newest P5MIN run.

    ``forecast`` holds only intervals still to end, from the first one, and
    stops at the first interval without a row; later rows are listed apart as
    ``later_observations``, for diagnosis only.
    """

    _suffix = "local_price_adjustment_forecast"
    _label = "Local Price Adjustment Forecast"
    _attr_icon = "mdi:chart-timeline-variant"
    # The lists grow with the horizon; the scalar state is recorded.
    _unrecorded_attributes = frozenset({"forecast", "interval_ends", "later_observations"})

    def _series(self, now: datetime):
        return forecast_series(self.coordinator.latest, self._duid, now, MAX_AGE)

    @property
    def native_value(self) -> Optional[float]:
        now = now_nem()
        series = self._series(now)
        if not series.points or datetime.fromisoformat(series.points[0]["time"]) > now:
            return None
        return series.points[0]["value"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = now_nem()
        run = self.coordinator.latest
        series = self._series(now)
        return {
            **self._base("P5MIN_LOCAL_PRICE"),
            "row_status": series.status,
            "run_at": _iso(run.run_at) if run else None,
            "source_file": run.source if run else None,
            "retrieved_at": _iso(run.retrieved_at) if run else None,
            "age_seconds": round((now - run.run_at).total_seconds()) if run else None,
            "interval_minutes": 5,
            "interpolation_mode": "previous",
            "forecast_end": series.forecast_end,
            "horizon_end": series.horizon_end,
            "intervals_published": series.published,
            "intervals_ahead": series.intervals,
            "forecast": series.points,
            "interval_ends": series.interval_ends,
            "later_observations": series.later,
        }


def local_price_entities(
    constraints: Any, p5min: Any, entry: Any, region: str, duids: list[str], diagnostic: bool
) -> list[SensorEntity]:
    entities: list[SensorEntity] = []
    for duid in duids:
        entities.append(NemPd7dayLocalPriceAdjustmentSensor(constraints, entry, region, duid))
        entities.append(NemPd7dayLocalPriceAdjustmentForecastSensor(p5min, entry, region, duid))
        if diagnostic:
            entities.append(NemPd7dayLocalPriceSensor(constraints, entry, region, duid))
    return entities
