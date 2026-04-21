"""Camera platform for NEM PD7DAY — serves the time-of-day price chart and duck-curve bias chart."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PD7DayCoordinator

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

_PLACEHOLDER = b""  # returned before first render


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ToD chart camera and bias chart camera from a config entry."""
    coordinator: PD7DayCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    region = entry.data.get("region", "QLD1").upper()
    async_add_entities([
        NemPd7dayTodCamera(coordinator, region, entry),
        NemPd7dayBiasChartCamera(coordinator, region, entry),
    ])


class NemPd7dayTodCamera(CoordinatorEntity[PD7DayCoordinator], Camera):
    """
    Camera entity that serves the time-of-day actual price chart.

    The PNG is rendered in-memory by tod_stats.render_chart() after each
    coordinator refresh and cached as bytes.  async_camera_image() simply
    returns the cached buffer — no network I/O on snapshot requests.
    """

    _attr_has_entity_name = True
    _attr_name = "Price ToD Chart"
    _attr_content_type = "image/png"
    _attr_supported_features = CameraEntityFeature(0)
    _attr_is_streaming = False
    _attr_brand = "AEMO NEM"
    _attr_model = "NEM PD7DAY"

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        region: str,
        entry: ConfigEntry,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._region = region
        self._entry = entry
        self._image_bytes: bytes = _PLACEHOLDER
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_tod_chart"

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Render immediately if stats are already available
        self._refresh_image()

    def _handle_coordinator_update(self) -> None:
        """Called by CoordinatorEntity on every coordinator refresh."""
        self._refresh_image()
        self.async_write_ha_state()

    def _refresh_image(self) -> None:
        """Re-render the chart from the coordinator's cached tod_stats."""
        tod_stats = getattr(self.coordinator, "tod_stats", None)
        if tod_stats is None or not tod_stats.slots:
            return
        from . import tod_stats as _ts
        try:
            self._image_bytes = _ts.render_chart(tod_stats, region=self._region)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("ToD chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return the latest rendered PNG."""
        return self._image_bytes if self._image_bytes else None


class NemPd7dayBiasChartCamera(CoordinatorEntity[PD7DayCoordinator], Camera):
    """
    Camera entity that serves the duck-curve forecast bias chart.

    Rendered from live CalibrationResult coefficients after each coordinator
    refresh.  Shows heatmap of OLS slope a per horizon × ToD bucket, bar chart
    of top bias patterns, and the stylised duck curve panel.
    """

    _attr_has_entity_name = True
    _attr_name = "Bias Chart"
    _attr_content_type = "image/png"
    _attr_supported_features = CameraEntityFeature(0)
    _attr_is_streaming = False
    _attr_brand = "AEMO NEM"
    _attr_model = "NEM PD7DAY"

    def __init__(
        self,
        coordinator: PD7DayCoordinator,
        region: str,
        entry: ConfigEntry,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._region = region
        self._entry = entry
        self._image_bytes: bytes = _PLACEHOLDER
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_bias_chart"

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_image()

    def _handle_coordinator_update(self) -> None:
        self._refresh_image()
        self.async_write_ha_state()

    def _refresh_image(self) -> None:
        """Re-render the bias chart from the coordinator's calibration result."""
        store = getattr(self.coordinator, "_store", None)
        if store is None:
            return
        cal = store.calibration
        if cal is None:
            return
        from . import bias_chart as _bc
        try:
            self._image_bytes = _bc.render_chart(
                cal,
                obs_count=store.observation_count,
                region=self._region,
                tod_stats=self.coordinator.tod_stats,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Bias chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        return self._image_bytes if self._image_bytes else None
