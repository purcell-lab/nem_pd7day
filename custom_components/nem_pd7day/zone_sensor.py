"""One city or zone price proxy sensor per chosen zone (#288).

``<City> Zone Price Proxy``, for each zone chosen in the options (zones.py):

- **State**: ROP x zone MLF + the median of the members' local price
  adjustments, in $/kWh, for the latest dispatch interval, raw. A member
  whose DUIDs disagree is left out of the median.
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
    STATUS_EXPIRED,
    STATUS_NO_DATA,
    STATUS_PUBLISHED,
    STATUS_STALE,
    STATUS_TABLE_MISSING,
    STATUS_ZERO,
    DispatchLocalPrices,
    P5LocalPriceRun,
)
from .local_price_sensor import MAX_AGE, UNIT, _iso, device_info, entity_id_for, listen_to_forecasts
from .nem_time import now_nem
from .node_price_sensor import STATUS_NO_ROP, STATUS_UNKNOWN_NODE, node_adjustment, p5_adjustment
from .predispatch_local_price import PredispatchAdjustments, predispatch_adjustments, predispatch_attrs
from .zones import Zone, zone_adjustment

SUFFIX = "zone_price_proxy"
_UNKNOWN = (STATUS_STALE, STATUS_NO_DATA, STATUS_TABLE_MISSING)


@dataclass
class ZoneDispatch:
    status: str
    rop_mwh: Optional[float] = None
    static_mwh: Optional[float] = None
    adjustment_mwh: Optional[float] = None
    members: list[dict[str, Any]] = field(default_factory=list)

    @property
    def price_mwh(self) -> Optional[float]:
        if self.static_mwh is None or self.adjustment_mwh is None:
            return None
        return round(self.static_mwh + self.adjustment_mwh, 4)


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
    members = []
    for member in zone.members:
        status, adjustment = node_adjustment(prices, member.node.duids, now)
        if status in _UNKNOWN:
            return ZoneDispatch(status, rop)
        members.append({
            "tni": member.node.tni, "stations": list(member.node.stations), "distance_km": member.distance_km,
            "mlf": member.node.mlf, "adjustment_mwh": adjustment, "adjustment_status": status,
        })
    adjustment = zone_adjustment([m["adjustment_mwh"] for m in members])  # type: ignore[misc]
    status = STATUS_CONFLICT if adjustment is None else STATUS_PUBLISHED
    return ZoneDispatch(status, rop, round(rop * mlf, 4), adjustment, members)


def zone_p5_forecast(zone: Zone, region: str, run: Optional[P5LocalPriceRun], now: datetime) -> tuple[str, list]:
    """(status, points): ROP x zone MLF + the members' median P5MIN adjustment per interval still to end."""
    mlf = zone.mlf
    if mlf is None:
        return STATUS_UNKNOWN_NODE, []
    if run is None:
        return STATUS_NO_DATA, []
    if now - run.run_at > MAX_AGE:
        return STATUS_STALE, []
    if not run.table_present:
        return STATUS_TABLE_MISSING, []
    series = run.rop.get(region)
    if not series:
        return STATUS_NO_ROP, []
    ahead = sorted(end for end in series if end > now)
    if not ahead:
        return STATUS_EXPIRED, []
    points: list[dict[str, Any]] = []
    for end in ahead:
        adjustment = zone_adjustment([p5_adjustment(run, m.node.duids, end) for m in zone.members])
        if adjustment is None:
            return STATUS_CONFLICT, points
        points.append({"time": (end - INTERVAL).isoformat(), "interval_end": end.isoformat(),
                       "value": round((series[end] * mlf + adjustment) / 1000.0, 6)})
    return STATUS_PUBLISHED, points


def zone_predispatch(zone: Zone, snapshot: Any, now: datetime) -> PredispatchAdjustments:
    """The members' median pre-dispatch adjustment per period; ``published`` counts the
    periods where at least one member has one."""
    series = [predispatch_adjustments(snapshot, m.node.duids, now) for m in zone.members]
    usable = [s for s in series if s.status in (STATUS_PUBLISHED, STATUS_ZERO, STATUS_CONFLICT)]
    if not usable:
        return PredispatchAdjustments(series[0].status if series else STATUS_UNKNOWN_NODE)
    by_end: dict[datetime, list[float]] = {}
    for s in usable:
        for end, value in s.periods:
            by_end.setdefault(end, []).append(value)
    periods = [(end, zone_adjustment(values) or 0.0) for end, values in sorted(by_end.items())]
    published = sum(1 for _end, values in sorted(by_end.items()) if any(values))
    status = STATUS_PUBLISHED if published else STATUS_ZERO
    return PredispatchAdjustments(status, periods, published, len(periods) - published)


class NemPd7dayZonePriceSensor(CoordinatorEntity, SensorEntity):
    """State: ROP x zone MLF + the members' median adjustment, in $/kWh; a proxy."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:city-variant-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UNIT
    _attr_suggested_display_precision = 4
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"members", "load_points", "forecast", "forecast_interval_ends", "predispatch_forecast"}
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
        snapshot = self.coordinator.tracker.latest
        prices = snapshot.local_prices if snapshot is not None else None
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
            "zone_mlf": zone.mlf, "zone_mlf_source": zone.mlf_source,
            "load_points": [{"tni": p.tni, "name": p.name, "kv": p.kv, "mlf": p.mlf} for p in zone.load_points],
            "adjustment_aggregate": "median",
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
        }

    def _forecast_attrs(self, now: datetime) -> dict[str, Any]:
        run = self._p5min.latest if self._p5min is not None else None
        status, points = zone_p5_forecast(self._zone, self._region, run, now)
        snapshot = getattr(self._predispatch, "latest", None)
        rrp = snapshot.prices.get(self._region, {}) if snapshot is not None else {}
        mlf = self._zone.mlf

        def value(end: datetime, adjustment: float) -> Optional[float]:
            price = rrp.get(end)
            return (price * mlf + adjustment) / 1000.0 if price is not None and mlf is not None else None

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
