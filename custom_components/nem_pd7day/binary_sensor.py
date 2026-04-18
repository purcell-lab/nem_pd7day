"""NEM PD7DAY binary sensor platform — market intervention flag."""
from __future__ import annotations

import logging
import re
from typing import Any

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
    CONF_REGIONS,
    DEFAULT_REGIONS,
    ATTR_LAST_CHANGED,
    ATTR_RUN_DATETIME,
    ATTR_SOURCE_FILE,
    COORDINATOR_KEY,
    DOMAIN,
)
from .coordinator import PD7DayCoordinator

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
    regions: list[str] = entry.options.get(
        CONF_REGIONS,
        entry.data.get(CONF_REGIONS, DEFAULT_REGIONS),
    )
    entities = [
        PD7DayInterventionSensor(coordinator, entry, region)
        for region in regions
    ]
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
        self._attr_name = f"{_safe_slug(region).upper()} PD7DAY Market Intervention"
        self.entity_id = f"binary_sensor.nem_pd7day_{region_slug}_intervention"
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
