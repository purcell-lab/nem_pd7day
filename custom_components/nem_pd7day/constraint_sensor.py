"""Per-region binding constraints sensor, read from the shared ConstraintCoordinator."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .constraint_client import INTERVAL, ConstraintSnapshot, describe_constraint
from .constraint_coordinator import ConstraintCoordinator
from .const import DEVICE_CONFIGURATION_URL, DEVICE_MANUFACTURER, DEVICE_MODEL, DOMAIN
from .nem_time import NEM_TZ, now_nem

# Three missed intervals: older than this, the list no longer describes the market now.
MAX_AGE = timedelta(minutes=15)
# Lists that grow with the market stay out of the recorder; the count is recorded.
_LISTS = ("constraints", "fcas_constraints", "unassigned_constraints")
# What the state counts, stated on the sensor (#318).
FILTER = (
    "binding network, interconnector and negative residue constraints whose ID names this region; "
    "FCAS constraints and IDs naming no region are listed, not counted"
)


class NemPd7dayBindingConstraintsSensor(CoordinatorEntity[ConstraintCoordinator], SensorEntity):
    """
    State: binding network constraints touching the region in the latest
    dispatch interval. FCAS constraints and constraints whose ID names no
    region are listed separately and not counted.
    """

    _attr_has_entity_name = True
    _attr_name = "Binding Constraints"
    _attr_icon = "mdi:transmission-tower-off"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "constraints"
    _unrecorded_attributes = frozenset((*_LISTS, "binding_nem_network_ids"))

    def __init__(self, coordinator: ConstraintCoordinator, entry: Any, region: str) -> None:
        super().__init__(coordinator)
        self._region = region
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_binding_constraints"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{region}")},
            name=f"NEM PD7DAY {region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    def _snapshot(self, now: Optional[datetime] = None) -> Optional[ConstraintSnapshot]:
        """The latest interval, or None when there is none or it is too old."""
        snapshot = self.coordinator.tracker.latest
        if snapshot is None or (now or now_nem()) - snapshot.interval_end > MAX_AGE:
            return None
        return snapshot

    @property
    def available(self) -> bool:  # type: ignore[override]
        return self._snapshot() is not None

    def _split(self, snapshot: ConstraintSnapshot) -> dict[str, list[dict]]:
        lists: dict[str, list[dict]] = {name: [] for name in _LISTS}
        for c in snapshot.constraints:
            info = describe_constraint(c.constraint_id)
            if info.category == "data_snapshot":
                continue
            if not info.regions:
                lists["unassigned_constraints"].append(self.coordinator.tracker.row(c))
            elif self._region in info.regions:
                key = "fcas_constraints" if info.category == "fcas" else "constraints"
                lists[key].append(self.coordinator.tracker.row(c))
        return lists

    @property
    def native_value(self) -> Optional[int]:
        snapshot = self._snapshot()
        return len(self._split(snapshot)["constraints"]) if snapshot is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        tracked = self.coordinator.tracker.tracked_since
        attrs: dict[str, Any] = {
            "region": self._region,
            "source": "NEMWEB DispatchIS",
            "tracked_since": tracked.astimezone(NEM_TZ).isoformat() if tracked else None,
        }
        if snapshot is None:
            return attrs
        return {
            **attrs,
            # time is the interval START, nemtime its END, as in the forecasts.
            "time": (snapshot.interval_end - INTERVAL).astimezone(NEM_TZ).isoformat(),
            "nemtime": snapshot.interval_end.astimezone(NEM_TZ).isoformat(),
            "file": snapshot.source,
            "filter": FILTER,
            "binding_nem": len(snapshot.constraints),
            **_nem_network(snapshot),
            "evaluated_nem": snapshot.evaluated,
            **self._split(snapshot),
        }


def _nem_network(snapshot: ConstraintSnapshot) -> dict[str, Any]:
    """The NEM's binding constraints other than FCAS, whatever region their ID names, or none (#318).

    So a constraint the regions do not count still shows: at 18:45 on 31 Oct
    2026 every region read 0 while L_PEC_X_6C_6G_6H_XST, an interconnector
    limit naming no region, bound alone, and DispatchIS carried no
    LOCAL_PRICE table (#309).
    """
    ids = sorted(
        c.constraint_id for c in snapshot.constraints
        if describe_constraint(c.constraint_id).category not in ("fcas", "data_snapshot")
    )
    return {"binding_nem_network": len(ids), "binding_nem_network_ids": ids}
