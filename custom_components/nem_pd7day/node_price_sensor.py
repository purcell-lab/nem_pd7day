"""One node price sensor per chosen transmission node (#286, #287).

``<stations> (<TNI>) Node Price``, for each connection point (TNI) chosen in the options:

- **State**: the node price in $/kWh for the latest dispatch interval,
  ROP x MLF + adjustment: the node's region original price, from the
  DispatchIS file the binding constraints already read, scaled by the node's
  marginal loss factor from the bundled catalogue, plus the node's local
  price adjustment, the published DISPATCH_LOCAL_PRICE of its DUIDs (0 when a
  fresh file has no row for them). Raw, never clipped: the floor and cap apply
  to the region's price. ``static_price_mwh`` (ROP x MLF) and
  ``adjustment_mwh`` are its two parts.
- **Forecast**: the newest P5MIN run's ROP x MLF + adjustment for the
  intervals still to end, with its own ``forecast_status``.
- **Pre-dispatch forecast**: RRP x MLF + adjustment from the 30-minute
  pre-dispatch, to the end of the next trading day; pre-dispatch publishes
  RRP, not ROP.

A diagnostic, not a retail or settlement price. A TNI not in the catalogue
has no MLF or DUIDs, so it stays unknown (``row_status: unknown_node``).
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
    STATUS_CONFLICT,
    STATUS_EXPIRED,
    STATUS_NO_DATA,
    STATUS_P5_FALLBACK,
    STATUS_PUBLISHED,
    STATUS_STALE,
    STATUS_TABLE_MISSING,
    STATUS_ZERO,
    DispatchLocalPrices,
    P5LocalPriceRun,
    dispatch_status,
)
from .local_price_sensor import (
    MAX_AGE,
    UNIT,
    _iso,
    device_info,
    dispatch_prices,
    entity_id_for,
    entity_name,
    listen_to_forecasts,
)
from .nem_time import now_nem
from .predispatch_local_price import PredispatchAdjustments, predispatch_adjustments, predispatch_attrs

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
    _unrecorded_attributes = frozenset(
        {"forecast", "forecast_interval_ends", "duids", "stations", "predispatch_forecast"}
    )

    def __init__(
        self, constraints: Any, p5min: Any, entry: Any, region: str, tni: str, node: Optional[NodeInfo] = None,
        predispatch: Any = None,
    ) -> None:
        super().__init__(constraints)
        self._p5min = p5min
        self._predispatch = predispatch
        self._region = region
        self._tni = tni
        self._node = node
        self._duids: tuple[str, ...] = node.duids if node is not None else ()
        self._attr_name = entity_name(tni, node.stations if node is not None else (), "Node Price")
        self._attr_unique_id = f"{entry.entry_id}_{region}_{tni}_{SUFFIX}"
        self.entity_id = entity_id_for(region, tni, SUFFIX)
        self._attr_device_info = device_info(entry, region)

    async def async_added_to_hass(self) -> None:
        await listen_to_forecasts(self, super().async_added_to_hass)

    @property
    def _node_region(self) -> str:
        return self._node.region if self._node is not None else self._region

    @property
    def _mlf(self) -> Optional[float]:
        return self._node.mlf if self._node is not None else None

    def _prices(self) -> Optional[DispatchLocalPrices]:
        return dispatch_prices(self)

    def _status(self, prices: Optional[DispatchLocalPrices], now: datetime) -> str:
        if self._mlf is None:
            return STATUS_UNKNOWN_NODE
        if prices is None:
            return STATUS_NO_DATA
        if now - prices.interval_end > MAX_AGE:
            return STATUS_STALE
        return STATUS_PUBLISHED if self._node_region in prices.rop else STATUS_NO_ROP

    def _static_mwh(self, prices: Optional[DispatchLocalPrices], now: datetime) -> Optional[float]:
        """ROP x MLF: the static node price, before any local price adjustment."""
        if self._status(prices, now) != STATUS_PUBLISHED:
            return None
        return round(prices.rop[self._node_region] * self._mlf, 4)  # type: ignore[union-attr,operator]

    def _node_price_mwh(self, prices: Optional[DispatchLocalPrices], now: datetime) -> Optional[float]:
        """ROP x MLF + adjustment, raw: the state, in $/MWh; unknown when either part is."""
        static, (_status, adjustment) = self._static_mwh(prices, now), self._adjustment(prices, now)
        return round(static + adjustment, 4) if static is not None and adjustment is not None else None

    def _adjustment(self, prices: Optional[DispatchLocalPrices], now: datetime) -> tuple[str, Optional[float]]:
        return node_adjustment(prices, self._duids, now)

    @property
    def native_value(self) -> Optional[float]:
        value = self._node_price_mwh(self._prices(), now_nem())
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
        adjustment_status, adjustment = self._adjustment(prices, now)
        attrs.update({
            "node_price_mwh": self._node_price_mwh(prices, now),
            "static_price_mwh": self._static_mwh(prices, now),
            "adjustment_status": adjustment_status,
            "adjustment_mwh": adjustment,
        })
        if prices is None:
            return attrs
        return {
            **attrs,
            "interval_start": _iso(prices.interval_end - INTERVAL),
            "interval_end": _iso(prices.interval_end),
            "source_file": prices.source,
            "fallback_source_file": prices.fallback_source,
            "retrieved_at": _iso(prices.retrieved_at),
            "age_seconds": round((now - prices.interval_end + INTERVAL).total_seconds()),
            "rop_mwh": prices.rop.get(self._node_region),
        }

    def _forecast_attrs(self, now: datetime) -> dict[str, Any]:
        run = self._p5min.latest if self._p5min is not None else None
        status, points = node_forecast(run, self._node_region, self._mlf, self._duids, now)
        return {
            "forecast_source": "P5MIN_REGIONSOLUTION_ROP_LOCAL_PRICE",
            "forecast_status": status,
            "forecast_run_at": _iso(run.run_at) if run else None,
            "forecast_source_file": run.source if run else None,
            "interval_minutes": 5,
            "interpolation_mode": "previous",
            "intervals_ahead": len(points),
            "forecast": points,
            "forecast_interval_ends": [p["interval_end"] for p in points],
        }

    def _predispatch_attrs(self, now: datetime) -> dict[str, Any]:
        snapshot = getattr(self._predispatch, "latest", None)
        mlf = self._mlf
        if mlf is None:
            return predispatch_attrs(PredispatchAdjustments(STATUS_UNKNOWN_NODE), snapshot, lambda *_: None)
        rrp = snapshot.prices.get(self._node_region, {}) if snapshot is not None else {}

        def value(end: datetime, adjustment: float) -> Optional[float]:
            price = rrp.get(end)
            return (price * mlf + adjustment) / 1000.0 if price is not None else None

        return predispatch_attrs(predispatch_adjustments(snapshot, self._duids, now), snapshot, value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = now_nem()
        return {
            **self._identity(), **self._dispatch_attrs(self._prices(), now),
            **self._forecast_attrs(now), **self._predispatch_attrs(now),
        }


def node_adjustment(
    prices: Optional[DispatchLocalPrices], duids: tuple[str, ...], now: datetime,
) -> tuple[str, Optional[float]]:
    """(status, $/MWh): a node's DUIDs' published adjustment, 0 for no row in a fresh file."""
    if not duids:
        return STATUS_UNKNOWN_NODE, None
    statuses = {dispatch_status(prices, duid, now, MAX_AGE) for duid in duids}
    known = {STATUS_PUBLISHED, STATUS_ZERO, STATUS_P5_FALLBACK}
    if statuses - known:
        return sorted(statuses - known)[0], None
    values = {prices.rows[d].adjustment_mwh for d in duids if d in prices.rows}  # type: ignore[union-attr]
    if len(values) > 1:
        return STATUS_CONFLICT, None
    if STATUS_P5_FALLBACK in statuses:
        return STATUS_P5_FALLBACK, values.pop() if values else 0.0
    return (STATUS_PUBLISHED, values.pop()) if values else (STATUS_ZERO, 0.0)


def p5_adjustment(run: P5LocalPriceRun, duids: tuple[str, ...], end: datetime) -> Optional[float]:
    """The DUIDs' common adjustment for one P5MIN interval, 0 without a row; None where they conflict."""
    if any((duid, end) in run.conflicts for duid in duids):
        return None
    values = {run.rows[d][end].adjustment_mwh for d in duids if end in run.rows.get(d, {})}
    if len(values) > 1:
        return None
    return values.pop() if values else 0.0


def node_forecast(
    run: Optional[P5LocalPriceRun], region: str, mlf: Optional[float], duids: tuple[str, ...], now: datetime,
) -> tuple[str, list[dict[str, Any]]]:
    """(status, points): the run's ROP x MLF + the DUIDs' adjustment for the intervals still to end,
    in $/kWh, raw, up to the first interval where the DUIDs conflict."""
    if mlf is None or not duids:
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
        adjustment = p5_adjustment(run, duids, end)
        if adjustment is None:
            return STATUS_CONFLICT, points
        points.append({"time": (end - INTERVAL).isoformat(), "interval_end": end.isoformat(),
                       "value": round((series[end] * mlf + adjustment) / 1000.0, 6)})
    return STATUS_PUBLISHED, points


def node_price_entities(
    constraints: Any, p5min: Any, entry: Any, region: str, tnis: list[str], catalogue: Optional[Catalogue] = None,
    predispatch: Any = None,
) -> list[SensorEntity]:
    """The chosen nodes' sensors, enabled; there are no presets."""
    return [
        NemPd7dayNodePriceSensor(
            constraints, p5min, entry, region, tni, catalogue.node(tni) if catalogue else None, predispatch,
        )
        for tni in tnis
    ]
