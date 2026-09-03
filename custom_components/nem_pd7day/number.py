"""
NEM PD7DAY number entities.

One RestoreNumber per region for "Additional Usage Fees ($/kWh)".
Lives on the regional device so it is grouped alongside the tariff sensors.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DEFAULT_ADDITIONAL_FEE, DOMAIN, get_region

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

DEFAULT_MIN = 0.0
DEFAULT_MAX = 1.0
DEFAULT_STEP = 0.0001


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:
    """Register one AdditionalFeeNumber per configured region."""
    region = get_region(entry)
    async_add_entities([AdditionalFeeNumber(entry, region)], True)


class AdditionalFeeNumber(RestoreNumber):
    """Configurable additional usage fee for a NEM region, in $/kWh."""

    _attr_has_entity_name = True
    _attr_name = "Additional Usage Fees"
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_native_min_value = DEFAULT_MIN
    _attr_native_max_value = DEFAULT_MAX
    _attr_native_step = DEFAULT_STEP
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:currency-usd"

    def __init__(self, entry: ConfigEntry, region: str) -> None:
        self._entry = entry
        self._region = region
        self._attr_unique_id = f"nem_pd7day_{region}_additional_usage_fee"
        self._attr_native_value = DEFAULT_ADDITIONAL_FEE

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous value on startup."""
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data is not None and last_data.native_value is not None:
            self._attr_native_value = last_data.native_value

    async def async_set_native_value(self, value: float) -> None:
        """Handle value change from UI or service call."""
        self._attr_native_value = value
        self.async_write_ha_state()
