"""Per-region pre-dispatch sensors, read from the shared PredispatchCoordinator.

NemPd7dayPredispatchInterconnectorSensor: an interconnector's flow, limits and
whether it is held at a limit, for each 30-minute period of the latest run,
with the price at each end. A link at a limit separates the two regions'
prices; one that is not lets a price spike on one side reach the other.

NemPd7dayPredispatchConstraintsSensor: the network constraints forecast to bind
in the region, period by period, with the largest marginal value.

Both are unavailable before the first run arrives and when the run held was
solved more than 75 minutes ago (two runs missed).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_CONFIGURATION_URL,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    DOMAIN,
    INTERCONNECTOR_ENDS,
)
from .constraint_client import BindingConstraint, describe_constraint
from .nem_time import NEM_TZ, now_nem
from .predispatch_client import PERIOD, InterconnectorPeriod, PredispatchSnapshot
from .predispatch_coordinator import PredispatchCoordinator

MAX_AGE = timedelta(minutes=75)
# A marginal value this large is a constraint violation penalty, not a price:
# SN_ZERO and NS_ZERO, the zero-flow equations, show $8.35M/MWh. Such a
# constraint is still listed and counted, but left out of max_marginal_value_mwh.
ARTEFACT_MARGINAL_VALUE = 1_000_000.0
TOP_PER_PERIOD = 5


def _iso(dt: datetime) -> str:
    return dt.astimezone(NEM_TZ).isoformat()


def _region_constraints(constraints: tuple[BindingConstraint, ...], region: str) -> list[BindingConstraint]:
    """Binding network, interconnector and negative residue constraints touching the region."""
    found = []
    for c in constraints:
        info = describe_constraint(c.constraint_id)
        if info.category not in ("fcas", "data_snapshot") and region in info.regions:
            found.append(c)
    return found


def _priced(constraints: list[BindingConstraint]) -> list[BindingConstraint]:
    return [c for c in constraints if abs(c.marginal_value) < ARTEFACT_MARGINAL_VALUE]


def _constraint_row(c: BindingConstraint) -> dict[str, Any]:
    info = describe_constraint(c.constraint_id)
    return {
        "constraint_id": c.constraint_id,
        "regions": list(info.regions),
        "category": info.category,
        "cause": info.cause,
        "rhs": c.rhs,
        "lhs": c.lhs,
        "marginal_value_mwh": c.marginal_value,
        "violation_degree": c.violation_degree,
    }


def _ic_row(p: InterconnectorPeriod, prices: dict[str, dict[datetime, float]], ends: tuple[str, str]) -> dict:
    return {
        "time": _iso(p.interval_end - PERIOD),
        "nemtime": _iso(p.interval_end),
        "mwflow": p.mwflow,
        "exportlimit": p.exportlimit,
        "importlimit": p.importlimit,
        "marginalvalue": p.marginalvalue,
        "violationdegree": p.violationdegree,
        "bound": p.bound,
        "limit_constraint": p.limit_constraint,
        "rrp_from": prices.get(ends[0], {}).get(p.interval_end),
        "rrp_to": prices.get(ends[1], {}).get(p.interval_end),
    }


class _PredispatchSensor(CoordinatorEntity[PredispatchCoordinator], SensorEntity):
    """Device, freshness and current-period lookup shared by both sensors."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: PredispatchCoordinator, entry: Any, region: str) -> None:
        super().__init__(coordinator)
        self._region = region
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{region}")},
            name=f"NEM PD7DAY {region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    def _snapshot(self, now: Optional[datetime] = None) -> Optional[PredispatchSnapshot]:
        snapshot = self.coordinator.latest
        if snapshot is None or (now or now_nem()) - snapshot.run_at > MAX_AGE:
            return None
        return snapshot

    def _run_attrs(self, snapshot: Optional[PredispatchSnapshot]) -> dict[str, Any]:
        attrs: dict[str, Any] = {"region": self._region, "source": "NEMWEB PredispatchIS"}
        if snapshot is not None:
            attrs.update({"run_at": _iso(snapshot.run_at), "file": snapshot.source})
        return attrs


class NemPd7dayPredispatchInterconnectorSensor(_PredispatchSensor):
    """
    State: the interconnector's forecast MW flow for the current 30-minute
    period, in its nominal direction (from_region to to_region positive).
    """

    _attr_native_unit_of_measurement = "MW"
    _attr_icon = "mdi:transmission-tower-export"
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, coordinator: PredispatchCoordinator, entry: Any, region: str, ic_id: str) -> None:
        super().__init__(coordinator, entry, region)
        self._ic_id = ic_id
        self._ends = INTERCONNECTOR_ENDS[ic_id]
        slug = ic_id.lower().replace("-", "_")
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_predispatch_ic_{slug}"
        self._attr_name = f"Predispatch Interconnector {ic_id}"

    def _periods(self, now: Optional[datetime] = None) -> list[InterconnectorPeriod]:
        """The periods not yet over, current first."""
        now = now or now_nem()
        snapshot = self._snapshot(now)
        if snapshot is None:
            return []
        return [p for p in snapshot.interconnectors.get(self._ic_id, ()) if p.interval_end > now]

    @property
    def available(self) -> bool:  # type: ignore[override]
        return bool(self._periods())

    @property
    def native_value(self) -> Optional[float]:
        periods = self._periods()
        return periods[0].mwflow if periods else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        attrs = {
            **self._run_attrs(snapshot),
            "interconnector_id": self._ic_id,
            "from_region": self._ends[0],
            "to_region": self._ends[1],
        }
        periods = self._periods()
        if snapshot is None or not periods:
            return attrs
        rows = [_ic_row(p, snapshot.prices, self._ends) for p in periods]
        current = periods[0]
        return {
            **attrs,
            **rows[0],
            "export_constraint": current.export_constraint,
            "import_constraint": current.import_constraint,
            "forecast": rows,
        }


class NemPd7dayPredispatchConstraintsSensor(_PredispatchSensor):
    """
    State: binding network constraints touching the region forecast for the
    current 30-minute period. FCAS constraints are not counted, as in the
    dispatch Binding Constraints sensor.
    """

    _attr_native_unit_of_measurement = "constraints"
    _attr_icon = "mdi:transmission-tower-off"
    _unrecorded_attributes = frozenset({"constraints", "forecast"})

    def __init__(self, coordinator: PredispatchCoordinator, entry: Any, region: str) -> None:
        super().__init__(coordinator, entry, region)
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_predispatch_binding_constraints"
        self._attr_name = "Predispatch Binding Constraints"

    def _ends(self, snapshot: PredispatchSnapshot, now: datetime) -> list[datetime]:
        return [end for end in snapshot.period_ends if end > now]

    def _current(self) -> tuple[Optional[PredispatchSnapshot], Optional[datetime]]:
        now = now_nem()
        snapshot = self._snapshot(now)
        ends = self._ends(snapshot, now) if snapshot is not None else []
        return (snapshot, ends[0]) if ends else (None, None)

    @property
    def available(self) -> bool:  # type: ignore[override]
        return self._current()[1] is not None

    @property
    def native_value(self) -> Optional[int]:
        snapshot, end = self._current()
        if snapshot is None or end is None:
            return None
        return len(_region_constraints(snapshot.constraints.get(end, ()), self._region))

    def _period_row(self, snapshot: PredispatchSnapshot, end: datetime) -> dict[str, Any]:
        found = _region_constraints(snapshot.constraints.get(end, ()), self._region)
        priced = _priced(found)
        return {
            "time": _iso(end - PERIOD),
            "nemtime": _iso(end),
            "count": len(found),
            "max_marginal_value_mwh": max((abs(c.marginal_value) for c in priced), default=0.0),
            "top": [c.constraint_id for c in priced[:TOP_PER_PERIOD]],
            "rrp": snapshot.prices.get(self._region, {}).get(end),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot, end = self._current()
        attrs = self._run_attrs(snapshot)
        if snapshot is None or end is None:
            return attrs
        rows = [self._period_row(snapshot, e) for e in self._ends(snapshot, now_nem())]
        found = _region_constraints(snapshot.constraints.get(end, ()), self._region)
        return {
            **attrs,
            **{k: v for k, v in rows[0].items() if k != "top"},
            "binding_nem": len(snapshot.constraints.get(end, ())),
            "constraints": [_constraint_row(c) for c in found],
            "forecast": rows,
        }
