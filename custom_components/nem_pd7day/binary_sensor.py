"""NEM PD7DAY binary sensor platform — market intervention flag + grid stress."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify as ha_slugify

from .const import (
    ATTR_ATTRIBUTION,
    DEVICE_CONFIGURATION_URL,
    DEVICE_MANUFACTURER,
    DEVICE_MODEL,
    ATTR_LAST_CHANGED,
    ATTR_RUN_DATETIME,
    ATTR_SOURCE_FILE,
    COORDINATOR_KEY,
    DOMAIN,
    get_region,
)
from .coordinator import PD7DayCoordinator

if TYPE_CHECKING:
    from .notice_store import GridNoticeStore

_LOGGER = logging.getLogger(__name__)


def _safe_slug(value: str) -> str:
    """Return a robust slug even when HA helpers are stubbed in tests."""
    try:
        slug = ha_slugify(value)
        if isinstance(slug, str) and slug:
            return slug
    except Exception:  # noqa: BLE001
        pass
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")



async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PD7DayCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR_KEY]
    region: str = get_region(entry)
    entities = [PD7DayInterventionSensor(coordinator, entry, region)]
    entities.append(
        NemPd7dayGridStressBinarySensor(coordinator, entry, region, coordinator.notice_store)
    )
    async_add_entities(entities, update_before_add=True)


class PD7DayInterventionSensor(CoordinatorEntity[PD7DayCoordinator], BinarySensorEntity):
    """
    Market intervention flag from CASESOLUTION.

    State : ON  = intervention pricing is in effect
            OFF = normal market pricing

    When ON, the AEMO has issued a direction to one or more generators and
    the Regional Reference Price (RRP) no longer reflects normal supply/demand
    dispatch. Price forecast sensors will still show values but they should be
    treated as unreliable for optimisation decisions (EV charging schedules,
    battery dispatch targets, EMHASS planning).

    Recommended automation: when this sensor is ON, suppress any automation
    that acts on pd7day price forecasts until it returns OFF.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: PD7DayCoordinator, entry: ConfigEntry, region: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._region = region
        region_slug = _safe_slug(region)
        self._attr_unique_id = f"{entry.entry_id}_{region_slug}_intervention"
        self._attr_name = "Market Intervention"
        self._attr_attribution = ATTR_ATTRIBUTION
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def _data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.case

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self._data is not None

    @property
    def is_on(self) -> bool | None:
        d = self._data
        return d.intervention if d else None

    @property
    def icon(self) -> str:
        return "mdi:alert-circle" if self.is_on else "mdi:check-circle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self._data
        if d is None:
            return {}
        return {
            "region": self._region,
            ATTR_RUN_DATETIME: d.run_datetime,
            ATTR_LAST_CHANGED: d.last_changed,
            ATTR_SOURCE_FILE: (
                self.coordinator.data.source_file if self.coordinator.data else None
            ),
        }


class NemPd7dayGridStressBinarySensor(CoordinatorEntity[PD7DayCoordinator], BinarySensorEntity):
    """
    Binary sensor: ON when any active LOR2+ or MSL2+ notice exists for the
    region within the next 48 hours.

    Attributes expose details of the highest-level active notice.
    """

    _attr_has_entity_name = True
    _attr_name = "Grid Stress"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:transmission-tower-off"
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        entry: ConfigEntry,
        region: str,
        notice_store: "GridNoticeStore",
    ) -> None:
        super().__init__(coordinator)
        self._region = region
        self._notice_store = notice_store
        self._entry = entry
        region_slug = _safe_slug(region)
        self._attr_unique_id = f"nem_pd7day_{region_slug}_grid_stress"
        self._attr_attribution = ATTR_ATTRIBUTION
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
            manufacturer=DEVICE_MANUFACTURER,
            model=DEVICE_MODEL,
            configuration_url=DEVICE_CONFIGURATION_URL,
        )

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Sensor is available only when the notice store is initialised."""
        return self._notice_store is not None and self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        if self._notice_store is None:
            return False
        return self._notice_store.has_active_stress(self._region, horizon_hours=48)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._notice_store is None:
            return {"region": self._region}
        now = datetime.now(timezone(timedelta(hours=10)))
        upcoming = self._notice_store.get_upcoming_stress(self._region, horizon_hours=48)
        all_active = self._notice_store.get_active_notices(
            self._region,
            from_dt=now,
            to_dt=now + timedelta(hours=168),
        )
        highest = max(upcoming, key=lambda n: n.level, default=None)
        return {
            "region": self._region,
            "stress_level": highest.level if highest else None,
            "stress_type": highest.notice_type if highest else None,
            "stress_from": highest.period_from.isoformat() if highest else None,
            "stress_to": highest.period_to.isoformat() if highest else None,
            "notice_id": highest.notice_id if highest else None,
            "active_notices_7d": [n.to_dict() for n in all_active],
            "lor1_count_7d": sum(1 for n in all_active if n.notice_type == "LOR" and n.level >= 1),
            "msl1_count_7d": sum(1 for n in all_active if n.notice_type == "MSL" and n.level >= 1),
        }
