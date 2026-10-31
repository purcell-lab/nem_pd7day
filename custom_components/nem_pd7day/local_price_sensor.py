"""One DUID local price sensor per DUID, opt in per config entry (#275).

``<station> (<DUID>) Local Price Adjustment``, for each DUID chosen in the options:

- **State**: the latest dispatch interval's LOCAL_PRICE_ADJUSTMENT in $/kWh,
  from the DispatchIS file the binding constraints already read. AEMO
  publishes only non-zero rows, so no row in a fresh file with the
  LOCAL_PRICE table is 0 (``row_status: zero_by_omission``); a stale file, or
  one without the table, leaves it unknown.
- **Local price**: ROP + adjustment for the DUID's region, raw (never clipped
  to the market floor or cap), as attributes; a diagnostic of the dispatch run, not a regional, retail or
  settlement price.
- **Forecast**: the newest P5MIN run's adjustments as ``forecast``, for the
  intervals still to end, an interval without a row being 0 (listed in
  ``inferred_zero_times``), up to a conflicting interval, with its own
  ``forecast_status``: a stale forecast never hides a current dispatch value,
  nor the reverse.

The region's DUIDs most often locally constrained, from the bundled catalogue,
are added as well, disabled; enabling one is all it takes. P5MIN is fetched
only while at least one of these sensors is enabled. A DUID in the catalogue
publishes its station, fuel, region and coordinates, so it shows on a map.

Until 3.33 there were three sensors per DUID; the forecast and local price
ones are removed from the registry (``remove_retired``).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_CONFIGURATION_URL, DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN
from .duid_catalogue import Catalogue, DuidInfo
from .local_price import (
    INTERVAL,
    STATUS_PUBLISHED,
    STATUS_ZERO,
    DispatchLocalPrices,
    dispatch_status,
    forecast_series,
    local_price_mwh,
    market_price_limits,
)
from .nem_time import NEM_TZ, now_nem
from .predispatch_local_price import predispatch_adjustments, predispatch_attrs

# Three missed intervals: older than this, a file no longer describes now.
MAX_AGE = timedelta(minutes=15)
UNIT = "$/kWh"
SUFFIX = "local_price_adjustment"
# The two sensors per DUID that 3.32 published beside this one.
RETIRED_SUFFIXES = ("_local_price_adjustment_forecast", "_local_price")


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(NEM_TZ).isoformat() if value is not None else None


def entity_name(code: str, names: tuple[str, ...], kind: str) -> str:
    """``Gladstone (GSTONE1) Local Price Adjustment``: the catalogue's names where
    they say more than the code, the code, and the kind of sensor."""
    named = [n for n in names if n and n != code]
    return f"{', '.join(named)} ({code}) {kind}" if named else f"{code} {kind}"


def entity_id_for(region: str, code: str, suffix: str) -> str:
    """The entity id the code alone gives, as before names carried the station:
    a new sensor's id does not depend on the catalogue, and an existing one
    keeps its registry entry either way."""
    slug = re.sub(r"[^a-z0-9]+", "_", f"nem_pd7day_{region}_{code}_{suffix}".lower()).strip("_")
    return f"sensor.{slug}"


def device_info(entry: Any, region: str) -> DeviceInfo:
    """The region's device, which the region's price sensors share."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{region}")},
        name=f"NEM PD7DAY {region}",
        manufacturer=DEVICE_MANUFACTURER,
        model=DEVICE_MODEL,
        configuration_url=DEVICE_CONFIGURATION_URL,
    )


class NemPd7dayDuidLocalPriceSensor(CoordinatorEntity, SensorEntity):
    """State: the DUID's dispatch LOCAL_PRICE_ADJUSTMENT in $/kWh; unknown without a row.

    Follows the shared ConstraintCoordinator (dispatch) as its coordinator,
    and the shared P5MinCoordinator (forecast) as a second listener.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:map-marker-radius"
    # A DUID's signal describes one unit's connection point, not the user's
    # prices, so it sits with the diagnostics rather than the region's prices.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UNIT
    _attr_suggested_display_precision = 4
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The lists grow with the horizon; the scalar state is recorded.
    _unrecorded_attributes = frozenset(
        {"forecast", "forecast_interval_ends", "later_observations", "inferred_zero_times", "predispatch_forecast"}
    )

    def __init__(
        self, constraints: Any, p5min: Any, entry: Any, region: str, duid: str,
        info: Optional[DuidInfo] = None, preset: bool = False, predispatch: Any = None,
    ) -> None:
        super().__init__(constraints)
        self._p5min = p5min
        self._predispatch = predispatch
        self._region = region
        self._duid = duid
        self._info = info
        # A preset (one of the region's most constrained DUIDs, not chosen) starts disabled.
        self._attr_entity_registry_enabled_default = not preset
        self._attr_name = entity_name(duid, (info.station,) if info is not None else (), "Local Price Adjustment")
        self._attr_unique_id = f"{entry.entry_id}_{region}_{duid}_{SUFFIX}"
        self.entity_id = entity_id_for(region, duid, SUFFIX)
        self._attr_device_info = device_info(entry, region)

    async def async_added_to_hass(self) -> None:
        await listen_to_forecasts(self, super().async_added_to_hass)

    @property
    def _duid_region(self) -> str:
        """The DUID's own region from the catalogue, else the entry's."""
        return self._info.region if self._info is not None else self._region

    def _prices(self) -> Optional[DispatchLocalPrices]:
        snapshot = self.coordinator.tracker.latest
        return snapshot.local_prices if snapshot is not None else None

    def _adjustment_mwh(self, prices: Optional[DispatchLocalPrices], now: datetime) -> Optional[float]:
        """The published adjustment; 0 for no row in a fresh file with the table; else unknown."""
        status = dispatch_status(prices, self._duid, now, MAX_AGE)
        if status == STATUS_PUBLISHED:
            return prices.rows[self._duid].adjustment_mwh  # type: ignore[union-attr]
        return 0.0 if status == STATUS_ZERO else None

    def _local_price_mwh(self, prices: Optional[DispatchLocalPrices], now: datetime) -> Optional[float]:
        """ROP, not RRP, of the same file, so of one interval and run; a missing ROP is unknown, never RRP."""
        adjustment = self._adjustment_mwh(prices, now)
        rop = prices.rop.get(self._duid_region) if prices is not None else None
        if adjustment is None or rop is None:
            return None
        return local_price_mwh(rop, adjustment)

    @property
    def native_value(self) -> Optional[float]:
        adjustment = self._adjustment_mwh(self._prices(), now_nem())
        return round(adjustment / 1000.0, 6) if adjustment is not None else None

    def _identity(self) -> dict[str, Any]:
        base: dict[str, Any] = {"region": self._region, "duid": self._duid}
        info = self._info
        if info is not None:
            base.update({
                "station": info.station, "fuel": info.fuel, "duid_region": info.region,
                "tni": info.tni, "mlf": info.mlf,
                "latitude": info.latitude, "longitude": info.longitude,
            })
        return base

    def _dispatch_attrs(self, prices: Optional[DispatchLocalPrices], now: datetime) -> dict[str, Any]:
        row = prices.rows.get(self._duid) if prices is not None else None
        attrs: dict[str, Any] = {
            "source": "DISPATCH_LOCAL_PRICE",
            "row_status": dispatch_status(prices, self._duid, now, MAX_AGE),
            "adjustment_mwh": self._adjustment_mwh(prices, now),
            "locally_constrained": row.locally_constrained if row is not None else None,
        }
        if prices is None:
            return attrs
        value = self._local_price_mwh(prices, now)
        floor, cap = market_price_limits(prices.interval_end)
        return {
            **attrs,
            # interval_start is the interval's start, interval_end the SETTLEMENTDATE.
            "interval_start": _iso(prices.interval_end - INTERVAL),
            "interval_end": _iso(prices.interval_end),
            "source_file": prices.source,
            "retrieved_at": _iso(prices.retrieved_at),
            # From the interval's start: AEMO publishes the file while the
            # interval runs, so its end is still ahead.
            "age_seconds": round((now - prices.interval_end + INTERVAL).total_seconds()),
            "rop_region": self._duid_region,
            "rop_mwh": prices.rop.get(self._duid_region),
            "local_price": round(value / 1000.0, 6) if value is not None else None,
            "local_price_mwh": value,
            # The interval's market limits, for reference: not applied.
            "price_floor_mwh": floor,
            "price_cap_mwh": cap,
        }

    def _forecast_attrs(self, now: datetime) -> dict[str, Any]:
        run = self._p5min.latest if self._p5min is not None else None
        series = forecast_series(run, self._duid, now, MAX_AGE)
        return {
            "forecast_source": "P5MIN_LOCAL_PRICE",
            "forecast_status": series.status,
            "forecast_run_at": _iso(run.run_at) if run else None,
            "forecast_source_file": run.source if run else None,
            "forecast_retrieved_at": _iso(run.retrieved_at) if run else None,
            # From the start of the run's first interval, as for dispatch.
            "forecast_age_seconds": round((now - run.run_at + INTERVAL).total_seconds()) if run else None,
            "interval_minutes": 5,
            "interpolation_mode": "previous",
            "forecast_end": series.forecast_end,
            "horizon_end": series.horizon_end,
            "intervals_published": series.published,
            "intervals_inferred_zero": len(series.inferred),
            "intervals_ahead": series.intervals,
            "forecast": series.points,
            "forecast_interval_ends": series.interval_ends,
            "later_observations": series.later,
            "inferred_zero_times": series.inferred,
        }

    def _predispatch_attrs(self, now: datetime) -> dict[str, Any]:
        snapshot = getattr(self._predispatch, "latest", None)
        series = predispatch_adjustments(snapshot, [self._duid], now)
        return predispatch_attrs(series, snapshot, lambda _end, adjustment: adjustment / 1000.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = now_nem()
        return {
            **self._identity(), **self._dispatch_attrs(self._prices(), now),
            **self._forecast_attrs(now), **self._predispatch_attrs(now),
        }


async def listen_to_forecasts(sensor: Any, added: Any) -> None:
    """Listen to P5MIN and pre-dispatch too, after the coordinator's own ``added``.

    P5MIN is fetched only while one of these listens, so ask for a run now;
    pre-dispatch is fetched every half hour regardless.
    """
    await added()
    for source in (sensor._p5min, sensor._predispatch):
        if source is not None:
            sensor.async_on_remove(source.async_add_listener(lambda: sensor.async_write_ha_state()))
    if sensor._p5min is not None and sensor._p5min.latest is None:
        sensor._p5min.request_refresh()


def enable_chosen(hass: Any, entry: Any, region: str, duids: list[str]) -> int:
    """Enable a chosen DUID's sensor that was created disabled as a preset.

    Only the integration's own disabling is undone; a sensor the user
    disabled stays disabled. Returns how many were enabled.
    """
    registry = er.async_get(hass)
    enabled = 0
    for duid in duids:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{region}_{duid}_{SUFFIX}")
        if entity_id is None:
            continue
        reg = registry.async_get(entity_id)
        if reg is not None and reg.disabled_by == er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity_id, disabled_by=None)
            enabled += 1
    return enabled


def remove_retired(hass: Any, entry: Any) -> int:
    """Remove the entry's 3.32 forecast and local price sensors, now attributes of the one sensor."""
    registry = er.async_get(hass)
    prefix = f"{entry.entry_id}_"
    retired = [
        e.entity_id for e in list(registry.entities.values())
        if getattr(e, "config_entry_id", None) == entry.entry_id
        and e.unique_id.startswith(prefix) and e.unique_id.endswith(RETIRED_SUFFIXES)
    ]
    for entity_id in retired:
        registry.async_remove(entity_id)
    return len(retired)


def local_price_entities(
    constraints: Any, p5min: Any, entry: Any, region: str, duids: list[str],
    catalogue: Optional[Catalogue] = None, predispatch: Any = None,
) -> list[SensorEntity]:
    """The chosen DUIDs' sensors, enabled, then the region's most constrained others, disabled."""
    presets = [d for d in (catalogue.top_for(region) if catalogue else ()) if d not in duids]
    return [
        NemPd7dayDuidLocalPriceSensor(
            constraints, p5min, entry, region, duid, catalogue.info(duid) if catalogue else None, preset,
            predispatch,
        )
        for duid, preset in [(d, False) for d in duids] + [(d, True) for d in presets]
    ]
