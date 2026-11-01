"""One city or zone price proxy sensor per chosen zone (#288).

``<City> Zone Price Proxy``, for each zone chosen in the options (zones.py):

- **State**: (ROP + the zone's local price adjustment) x zone MLF, in
  $/kWh, for the latest dispatch interval, raw (local_price.node_price_mwh,
  #312). The zone's adjustment takes one vote per station, the median of
  the stations', or the nearest station's below three (zones.py, #314,
  #315). A member whose DUIDs disagree is left out.
- **Forecast**: the same per P5MIN interval still to end, and per 30-minute
  pre-dispatch period to the end of the next trading day (RRP there, as
  pre-dispatch publishes no ROP).

The zone MLF is the place's own load connection points' where they are known
(zones.py). Always a proxy: the adjustment comes from generation and storage
connection points near the place, not its load, and no zone price is a
settlement or retail price. ``proxy: true`` and the members, with their distances and
adjustments, say so on every sensor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .local_price import (
    INTERVAL,
    STATUS_CONFLICT,
    STATUS_DISPATCH_ZERO,
    STATUS_EXPIRED,
    STATUS_NO_DATA,
    STATUS_PUBLISHED,
    STATUS_STALE,
    STATUS_ZERO,
    DispatchLocalPrices,
    P5LocalPriceRun,
    node_price_mwh,
)
from .local_price_sensor import MAX_AGE, UNIT, _iso, device_info, dispatch_prices, entity_id_for, listen_to_forecasts
from .nem_time import now_nem
from .node_price_sensor import STATUS_NO_ROP, STATUS_UNKNOWN_NODE, node_adjustment, p5_adjustment
from .predispatch_local_price import PredispatchAdjustments, predispatch_adjustments, predispatch_attrs
from .zones import AGGREGATE_NEAREST, MIN_MEDIAN_STATIONS, OUTSIDE, Zone, station_key, zone_adjustment

SUFFIX = "zone_price_proxy"


@dataclass
class ZoneDispatch:
    status: str
    rop_mwh: Optional[float] = None
    static_mwh: Optional[float] = None
    adjustment_mwh: Optional[float] = None
    members: list[dict[str, Any]] = field(default_factory=list)
    mlf: Optional[float] = None

    @property
    def price_mwh(self) -> Optional[float]:
        if self.rop_mwh is None or self.mlf is None or self.static_mwh is None or self.adjustment_mwh is None:
            return None
        return node_price_mwh(self.rop_mwh, self.adjustment_mwh, self.mlf)


def dispatch_zone(zone: Zone, region: str, prices: Optional[DispatchLocalPrices], now: datetime) -> ZoneDispatch:
    """The zone's price for the latest dispatch interval, and each member's part in it."""
    mlf = zone.mlf
    if mlf is None:
        return ZoneDispatch(STATUS_UNKNOWN_NODE)
    if prices is None:
        return ZoneDispatch(STATUS_NO_DATA)
    if now - prices.interval_end > MAX_AGE:
        return ZoneDispatch(STATUS_STALE)
    rop = prices.rop.get(region)
    if rop is None:
        return ZoneDispatch(STATUS_NO_ROP)
    members: list[dict[str, Any]] = []
    values: list[Optional[float]] = []
    for member in zone.members:
        status, adjustment = node_adjustment(prices, member.node.duids, now)
        values.append(adjustment)
        members.append({
            "tni": member.node.tni, "stations": list(member.node.stations), "distance_km": member.distance_km,
            "station": station_key(member.node),
            "mlf": member.node.mlf, "adjustment_mwh": adjustment, "adjustment_status": status,
        })
    adjustment = zone_adjustment(zone, values)
    if adjustment is None:
        status = STATUS_CONFLICT
    else:
        status = prices.fallback_status or (STATUS_PUBLISHED if prices.table_present else STATUS_DISPATCH_ZERO)
    return ZoneDispatch(status, rop, round(rop * mlf, 4), adjustment, members, mlf)


def zone_p5_forecast(zone: Zone, region: str, run: Optional[P5LocalPriceRun], now: datetime) -> tuple[str, list]:
    """(status, points): (ROP + the zone's P5MIN adjustment) x zone MLF per interval still to end."""
    mlf = zone.mlf
    if mlf is None:
        return STATUS_UNKNOWN_NODE, []
    if run is None:
        return STATUS_NO_DATA, []
    if now - run.run_at > MAX_AGE:
        return STATUS_STALE, []
    series = run.rop.get(region)
    if not series:
        return STATUS_NO_ROP, []
    ahead = sorted(end for end in series if end > now)
    if not ahead:
        return STATUS_EXPIRED, []
    points: list[dict[str, Any]] = []
    for end in ahead:
        adjustment = zone_adjustment(zone, [p5_adjustment(run, m.node.duids, end) for m in zone.members])
        if adjustment is None:
            return STATUS_CONFLICT, points
        points.append({"time": (end - INTERVAL).isoformat(), "interval_end": end.isoformat(),
                       "value": round(node_price_mwh(series[end], adjustment, mlf) / 1000.0, 6)})
    return STATUS_PUBLISHED, points


def zone_predispatch(zone: Zone, snapshot: Any, now: datetime) -> PredispatchAdjustments:
    """The zone's pre-dispatch adjustment per period, one vote per station; ``published``
    counts the periods where at least one member has one."""
    series = [predispatch_adjustments(snapshot, m.node.duids, now) for m in zone.members]
    usable = [s.status in (STATUS_PUBLISHED, STATUS_ZERO, STATUS_CONFLICT) for s in series]
    if not any(usable):
        return PredispatchAdjustments(series[0].status if series else STATUS_UNKNOWN_NODE)
    lookups = [dict(s.periods) if ok else {} for s, ok in zip(series, usable)]
    ends = sorted({end for lookup in lookups for end in lookup})
    periods, published = [], 0
    for end in ends:
        values = [lookup.get(end) for lookup in lookups]
        periods.append((end, zone_adjustment(zone, values) or 0.0))
        published += any(values)
    status = STATUS_PUBLISHED if published else STATUS_ZERO
    return PredispatchAdjustments(status, periods, published, len(periods) - published)


def _nearest_city_attrs(zone: Zone) -> dict[str, Any]:
    """Home only: the nearest preset city and its load points' MLF, beside Home's own (#316)."""
    city = zone.nearest_city
    if city is None:
        return {}
    return {
        "nearest_city": city.name, "nearest_city_distance_km": city.distance_km,
        "nearest_city_mlf": city.mlf, "nearest_city_load_points": list(city.tnis),
        "zone_mlf_vs_nearest_city": round(zone.mlf / city.mlf - 1, 4) if zone.mlf and city.mlf else None,
    }


def _aggregation_note(zone: Zone) -> dict[str, str]:
    """Why a zone of fewer than three stations takes its nearest station's adjustment (#315)."""
    if zone.aggregate != AGGREGATE_NEAREST:
        return {}
    count = len(zone.stations)
    return {"aggregation_note": (
        f"{count} station{'s' if count != 1 else ''} in reach, fewer than {MIN_MEDIAN_STATIONS}: the nearest "
        "station's adjustment stands alone, as a median of so few would move with any one unit"
    )}


class NemPd7dayZonePriceSensor(CoordinatorEntity, SensorEntity):
    """State: (ROP + the zone's adjustment, one vote per station) x zone MLF, in $/kWh; a proxy."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:city-variant-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UNIT
    _attr_suggested_display_precision = 4
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"members", "excluded_members", "load_points", "forecast", "forecast_interval_ends", "predispatch_forecast"}
    )

    def __init__(
        self, constraints: Any, p5min: Any, entry: Any, region: str, zone: Zone, predispatch: Any = None,
    ) -> None:
        super().__init__(constraints)
        self._p5min = p5min
        self._predispatch = predispatch
        self._region = region
        self._zone = zone
        self._attr_name = f"{zone.name} Zone Price Proxy"
        self._attr_unique_id = f"{entry.entry_id}_{region}_{zone.key}_{SUFFIX}"
        self.entity_id = entity_id_for(region, zone.key, SUFFIX)
        self._attr_device_info = device_info(entry, region)

    async def async_added_to_hass(self) -> None:
        await listen_to_forecasts(self, super().async_added_to_hass)

    def _dispatch(self, now: datetime) -> tuple[Optional[DispatchLocalPrices], ZoneDispatch]:
        prices = dispatch_prices(self, now)
        return prices, dispatch_zone(self._zone, self._region, prices, now)

    @property
    def native_value(self) -> Optional[float]:
        price = self._dispatch(now_nem())[1].price_mwh
        return round(price / 1000.0, 6) if price is not None else None

    def _zone_attrs(self, prices: Optional[DispatchLocalPrices], zone_now: ZoneDispatch) -> dict[str, Any]:
        zone = self._zone
        adjustments = [m["adjustment_mwh"] for m in zone_now.members if m["adjustment_mwh"] is not None]
        return {
            "proxy": True,
            "zone": zone.key, "region": self._region,
            "latitude": zone.latitude, "longitude": zone.longitude, "radius_km": zone.radius_km,
            "nearest_only": zone.nearest_only, "member_count": len(zone.members),
            "member_stations": list(zone.stations),
            "excluded_members": [{"tni": m.node.tni, "stations": list(m.node.stations), "distance_km": m.distance_km}
                                 for m in zone.excluded],
            "exclusion_reason": OUTSIDE[zone.key][1] if zone.excluded else None,
            "zone_mlf": zone.mlf, "zone_mlf_source": zone.mlf_source,
            "load_points": [{"tni": p.tni, "name": p.name, "kv": p.kv, "mlf": p.mlf} for p in zone.load_points],
            **_nearest_city_attrs(zone),
            "adjustment_aggregate": zone.aggregate,
            **_aggregation_note(zone),
            "formula": "(ROP + zone LOCAL_PRICE_ADJUSTMENT) x zone MLF",
            "row_status": zone_now.status,
            "price_mwh": zone_now.price_mwh, "static_price_mwh": zone_now.static_mwh,
            "rop_mwh": zone_now.rop_mwh, "adjustment_mwh": zone_now.adjustment_mwh,
            "members_constrained": sum(1 for a in adjustments if a),
            "adjustment_min_mwh": min(adjustments) if adjustments else None,
            "adjustment_max_mwh": max(adjustments) if adjustments else None,
            "members": zone_now.members or [
                {"tni": m.node.tni, "stations": list(m.node.stations), "distance_km": m.distance_km}
                for m in zone.members
            ],
            "interval_end": _iso(prices.interval_end) if prices is not None else None,
            "source_file": prices.source if prices is not None else None,
            "fallback_source_file": prices.fallback_source if prices is not None else None,
        }

    def _forecast_attrs(self, now: datetime) -> dict[str, Any]:
        run = self._p5min.latest if self._p5min is not None else None
        status, points = zone_p5_forecast(self._zone, self._region, run, now)
        snapshot = getattr(self._predispatch, "latest", None)
        rrp = snapshot.prices.get(self._region, {}) if snapshot is not None else {}
        mlf = self._zone.mlf

        def value(end: datetime, adjustment: float) -> Optional[float]:
            price = rrp.get(end)
            return node_price_mwh(price, adjustment, mlf) / 1000.0 if price is not None and mlf is not None else None

        return {
            "forecast_source": "P5MIN_REGIONSOLUTION_ROP_LOCAL_PRICE",
            "forecast_status": status,
            "forecast_run_at": _iso(run.run_at) if run else None,
            "forecast": points,
            "forecast_interval_ends": [p["interval_end"] for p in points],
            **predispatch_attrs(zone_predispatch(self._zone, snapshot, now), snapshot, value),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = now_nem()
        prices, zone_now = self._dispatch(now)
        return {**self._zone_attrs(prices, zone_now), **self._forecast_attrs(now)}


def zone_price_entities(
    constraints: Any, p5min: Any, entry: Any, region: str, zones: list[Zone], predispatch: Any = None,
) -> list[SensorEntity]:
    """The chosen zones' sensors, enabled; there are no presets."""
    return [NemPd7dayZonePriceSensor(constraints, p5min, entry, region, zone, predispatch) for zone in zones]
