"""Per-region pre-dispatch sensors, read from the shared PredispatchCoordinator.

NemPd7dayPredispatchInterconnectorSensor: an interconnector's flow, limits and
whether it is held at a limit, for each 30-minute period of the latest run,
with the price at each end. A link at a limit separates the two regions'
prices; one that is not lets a price spike on one side reach the other.

NemPd7dayPredispatchConstraintsSensor: the network constraints forecast to bind
in the region, period by period, with the largest marginal value. Binding
constraints whose ID names no region, such as AEMO's control-room constraint
automation (CA_), are listed beside them as unassigned, as the dispatch sensor
lists them, rather than given a guessed region (#255).

Both are unavailable before the first run arrives and when the run held was
solved more than 75 minutes ago (two runs missed).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

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
# rhs_vs_dispatch compares a forecast constraint with the one dispatch is
# applying now, so a dispatch interval older than this is not compared with.
DISPATCH_RHS_MAX_AGE = timedelta(minutes=15)


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


def _unassigned_constraints(constraints: tuple[BindingConstraint, ...]) -> list[BindingConstraint]:
    """Binding constraints whose ID names no region (CA_, NC_ and the like).

    The same set the dispatch sensor publishes as unassigned_constraints. On 15
    and 16 Oct 2026 a CA_BRIS constraint, applied at about half its dispatch
    RHS, priced QLD1 at the cap hours ahead while no region's list showed it.
    """
    found = []
    for c in constraints:
        info = describe_constraint(c.constraint_id)
        if info.category != "data_snapshot" and not info.regions:
            found.append(c)
    return found


def _priced(constraints: list[BindingConstraint]) -> list[BindingConstraint]:
    return [c for c in constraints if abs(c.marginal_value) < ARTEFACT_MARGINAL_VALUE]


def _max_marginal(constraints: list[BindingConstraint]) -> float:
    return max((abs(c.marginal_value) for c in _priced(constraints)), default=0.0)


def _rhs_vs_dispatch(c: BindingConstraint, dispatch_rhs: Mapping[str, float]) -> Optional[float]:
    """This period's RHS over dispatch's RHS for the same constraint, or None.

    None unless the constraint is binding in the latest dispatch interval too.
    Near 0.5 was the early sign, on both days in #255, that pre-dispatch had
    applied a constraint far tighter than dispatch and its cap price was an
    artefact.
    """
    base = dispatch_rhs.get(c.constraint_id)
    if c.rhs is None or not base:
        return None
    return round(c.rhs / base, 3)


def _constraint_row(c: BindingConstraint, dispatch_rhs: Mapping[str, float]) -> dict[str, Any]:
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
        "rhs_vs_dispatch": _rhs_vs_dispatch(c, dispatch_rhs),
    }


def _unassigned_brief(c: BindingConstraint, dispatch_rhs: Mapping[str, float]) -> dict[str, Any]:
    return {
        "constraint_id": c.constraint_id,
        "marginal_value_mwh": c.marginal_value,
        "rhs": c.rhs,
        "violation_degree": c.violation_degree,
        "rhs_vs_dispatch": _rhs_vs_dispatch(c, dispatch_rhs),
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

    def __init__(self, coordinator: PredispatchCoordinator, entry: Any, region: str, heartbeat: Any = None) -> None:
        super().__init__(coordinator)
        self._region = region
        # The constraints coordinator, which notifies after every dispatch poll: the
        # sensors step to the next period and age out on it, as the pre-dispatch
        # coordinator now notifies of a new run only (#334).
        self._heartbeat = heartbeat
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{region}")},
            name=f"NEM PD7DAY {region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._heartbeat is not None:
            self.async_on_remove(self._heartbeat.async_add_listener(self.async_write_ha_state))

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

    def __init__(
        self, coordinator: PredispatchCoordinator, entry: Any, region: str, ic_id: str, heartbeat: Any = None,
    ) -> None:
        super().__init__(coordinator, entry, region, heartbeat)
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
    dispatch Binding Constraints sensor, and neither are unassigned ones,
    which are listed beside them.
    """

    _attr_native_unit_of_measurement = "constraints"
    _attr_icon = "mdi:transmission-tower-off"
    _unrecorded_attributes = frozenset({"constraints", "unassigned_constraints", "forecast"})

    def __init__(
        self, coordinator: PredispatchCoordinator, entry: Any, region: str, dispatch_constraints: Any = None,
    ) -> None:
        super().__init__(coordinator, entry, region, dispatch_constraints)
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_predispatch_binding_constraints"
        self._attr_name = "Predispatch Binding Constraints"
        # The shared dispatch ConstraintCoordinator, read for rhs_vs_dispatch only.
        self._dispatch_constraints = dispatch_constraints

    def _dispatch_rhs(self, now: datetime) -> dict[str, float]:
        """RHS by constraint ID in the latest dispatch interval, if it is recent."""
        tracker = getattr(self._dispatch_constraints, "tracker", None)
        latest = getattr(tracker, "latest", None)
        if latest is None or now - latest.interval_end > DISPATCH_RHS_MAX_AGE:
            return {}
        return {c.constraint_id: c.rhs for c in latest.constraints if c.rhs}

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

    def _period_row(
        self, snapshot: PredispatchSnapshot, end: datetime, dispatch_rhs: Mapping[str, float],
    ) -> dict[str, Any]:
        constraints = snapshot.constraints.get(end, ())
        found = _region_constraints(constraints, self._region)
        unassigned = _unassigned_constraints(constraints)
        return {
            "time": _iso(end - PERIOD),
            "nemtime": _iso(end),
            "count": len(found),
            "max_marginal_value_mwh": _max_marginal(found),
            "top": [c.constraint_id for c in _priced(found)[:TOP_PER_PERIOD]],
            "max_unassigned_marginal_value_mwh": _max_marginal(unassigned),
            "unassigned": [_unassigned_brief(c, dispatch_rhs) for c in unassigned[:TOP_PER_PERIOD]],
            "rrp": snapshot.prices.get(self._region, {}).get(end),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot, end = self._current()
        attrs = self._run_attrs(snapshot)
        if snapshot is None or end is None:
            return attrs
        now = now_nem()
        dispatch_rhs = self._dispatch_rhs(now)
        rows = [self._period_row(snapshot, e, dispatch_rhs) for e in self._ends(snapshot, now)]
        constraints = snapshot.constraints.get(end, ())
        return {
            **attrs,
            **{k: v for k, v in rows[0].items() if k not in ("top", "unassigned")},
            "binding_nem": len(constraints),
            "constraints": [_constraint_row(c, dispatch_rhs) for c in _region_constraints(constraints, self._region)],
            "unassigned_constraints": [_constraint_row(c, dispatch_rhs) for c in _unassigned_constraints(constraints)],
            "forecast": rows,
        }
