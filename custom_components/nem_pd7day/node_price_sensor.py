"""One static node price sensor per chosen transmission node (#286).

``<TNI> Node Price``, for each connection point (TNI) chosen in the options:

- **State**: ROP x MLF in $/kWh for the latest dispatch interval: the node's
  region original price, from the DispatchIS file the binding constraints
  already read, scaled by the node's marginal loss factor from the bundled
  catalogue. Not clipped: the floor and cap apply to the region's price.
- **Forecast**: the newest P5MIN run's ROP x MLF for the intervals still to
  end, with its own ``forecast_status``.

It is the static node price, the price a generator at the node is settled at
before any local price adjustment; a diagnostic, not a retail or settlement
price. A TNI not in the catalogue has no MLF, so it stays unknown
(``row_status: unknown_node``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .duid_catalogue import Catalogue, NodeInfo
from .local_price import (
    INTERVAL,
    STATUS_EXPIRED,
    STATUS_NO_DATA,
    STATUS_PUBLISHED,
    STATUS_STALE,
    DispatchLocalPrices,
    P5LocalPriceRun,
)
from .local_price_sensor import MAX_AGE, UNIT, _iso, device_info
from .nem_time import now_nem

SUFFIX = "node_price"
STATUS_UNKNOWN_NODE = "unknown_node"   # no MLF: the TNI is not in the catalogue
STATUS_NO_ROP = "no_rop"               # the file has no price for the node's region


class NemPd7dayNodePriceSensor(CoordinatorEntity, SensorEntity):
    """State: ROP x MLF of the node in $/kWh; unknown without a fresh ROP or an MLF.

    Follows the shared ConstraintCoordinator (dispatch) as its coordinator,
    and the shared P5MinCoordinator (forecast) as a second listener.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:transmission-tower"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UNIT
    _attr_suggested_display_precision = 4
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"forecast", "forecast_interval_ends", "duids", "stations"})

    def __init__(
        self, constraints: Any, p5min: Any, entry: Any, region: str, tni: str, node: Optional[NodeInfo] = None,
    ) -> None:
        super().__init__(constraints)
        self._p5min = p5min
        self._region = region
        self._tni = tni
        self._node = node
        self._attr_name = f"{tni} Node Price"
        self._attr_unique_id = f"{entry.entry_id}_{region}_{tni}_{SUFFIX}"
        self._attr_device_info = device_info(entry, region)

    async def async_added_to_hass(self) -> None:
        """Listen to P5MIN too; it is fetched only while one of these listens, so ask for a run now."""
        await super().async_added_to_hass()
        if self._p5min is None:
            return
        self.async_on_remove(self._p5min.async_add_listener(lambda: self.async_write_ha_state()))
        if self._p5min.latest is None:
            self._p5min.request_refresh()

    @property
    def _node_region(self) -> str:
        return self._node.region if self._node is not None else self._region

    @property
    def _mlf(self) -> Optional[float]:
        return self._node.mlf if self._node is not None else None

    def _prices(self) -> Optional[DispatchLocalPrices]:
        snapshot = self.coordinator.tracker.latest
        return snapshot.local_prices if snapshot is not None else None

    def _status(self, prices: Optional[DispatchLocalPrices], now: datetime) -> str:
        if self._mlf is None:
            return STATUS_UNKNOWN_NODE
        if prices is None:
            return STATUS_NO_DATA
        if now - prices.interval_end > MAX_AGE:
            return STATUS_STALE
        return STATUS_PUBLISHED if self._node_region in prices.rop else STATUS_NO_ROP

    def _price_mwh(self, prices: Optional[DispatchLocalPrices], now: datetime) -> Optional[float]:
        if self._status(prices, now) != STATUS_PUBLISHED:
            return None
        return round(prices.rop[self._node_region] * self._mlf, 4)  # type: ignore[union-attr,operator]

    @property
    def native_value(self) -> Optional[float]:
        value = self._price_mwh(self._prices(), now_nem())
        return round(value / 1000.0, 6) if value is not None else None

    def _identity(self) -> dict[str, Any]:
        node = self._node
        base: dict[str, Any] = {"region": self._region, "tni": self._tni, "mlf": self._mlf}
        if node is not None:
            base.update({
                "node_region": node.region, "duids": list(node.duids), "stations": list(node.stations),
                "latitude": node.latitude, "longitude": node.longitude,
            })
        return base

    def _dispatch_attrs(self, prices: Optional[DispatchLocalPrices], now: datetime) -> dict[str, Any]:
        attrs: dict[str, Any] = {"source": "DISPATCH_PRICE_ROP", "row_status": self._status(prices, now)}
        if prices is None:
            return attrs
        return {
            **attrs,
            "interval_start": _iso(prices.interval_end - INTERVAL),
            "interval_end": _iso(prices.interval_end),
            "source_file": prices.source,
            "retrieved_at": _iso(prices.retrieved_at),
            "age_seconds": round((now - prices.interval_end + INTERVAL).total_seconds()),
            "rop_mwh": prices.rop.get(self._node_region),
            "node_price_mwh": self._price_mwh(prices, now),
        }

    def _forecast_attrs(self, now: datetime) -> dict[str, Any]:
        run = self._p5min.latest if self._p5min is not None else None
        status, points = node_forecast(run, self._node_region, self._mlf, now)
        return {
            "forecast_source": "P5MIN_REGIONSOLUTION_ROP",
            "forecast_status": status,
            "forecast_run_at": _iso(run.run_at) if run else None,
            "forecast_source_file": run.source if run else None,
            "interval_minutes": 5,
            "interpolation_mode": "previous",
            "intervals_ahead": len(points),
            "forecast": points,
            "forecast_interval_ends": [p["interval_end"] for p in points],
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = now_nem()
        return {**self._identity(), **self._dispatch_attrs(self._prices(), now), **self._forecast_attrs(now)}


def node_forecast(
    run: Optional[P5LocalPriceRun], region: str, mlf: Optional[float], now: datetime,
) -> tuple[str, list[dict[str, Any]]]:
    """(status, points): the run's ROP x MLF for the intervals still to end, in $/kWh."""
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
    points = [
        {"time": (end - INTERVAL).isoformat(), "interval_end": end.isoformat(),
         "value": round(series[end] * mlf / 1000.0, 6)}
        for end in ahead
    ]
    return STATUS_PUBLISHED, points


def node_price_entities(
    constraints: Any, p5min: Any, entry: Any, region: str, tnis: list[str], catalogue: Optional[Catalogue] = None,
) -> list[SensorEntity]:
    """The chosen nodes' sensors, enabled; there are no presets."""
    return [
        NemPd7dayNodePriceSensor(constraints, p5min, entry, region, tni, catalogue.node(tni) if catalogue else None)
        for tni in tnis
    ]
