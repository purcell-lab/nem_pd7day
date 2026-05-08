"""Camera platform for NEM PD7DAY — serves chart cameras per region."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CAL_CALIBRATED,
    ATTR_CAL_MAE,
    ATTR_CAL_N_OBS,
    ATTR_CAL_P10,
    ATTR_CAL_P50,
    ATTR_CAL_P90,
    ATTR_CAL_SOURCE,
    DOMAIN,
)
from .coordinator import PD7DayCoordinator
from .nem_time import parse_iso, to_nem_iso

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
        NemPd7dayIsoChartCamera(coordinator, region, entry),
        NemPd7dayForecastChartCamera(coordinator, region, entry),
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
            name=f"NEM PD7DAY {self._region}",
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
            name=f"NEM PD7DAY {self._region}",
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


class NemPd7dayIsoChartCamera(CoordinatorEntity[PD7DayCoordinator], Camera):
    """
    Camera entity that serves the isotonic calibration goodness dashboard.

    Replaces the OLS bias chart. Rendered from live CalibrationResult after
    each coordinator refresh. Shows compression ratio heatmap, iso_mae bars,
    PAV complexity scatter, and compression_ratio drift time-series.
    """

    _attr_has_entity_name = True
    _attr_name = "Calibration Chart"
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
        self._attr_unique_id = f"nem_pd7day_{region.lower()}_iso_chart"

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_image()

    def _handle_coordinator_update(self) -> None:
        self._refresh_image()
        self.async_write_ha_state()

    def _refresh_image(self) -> None:
        """Re-render the iso chart from the coordinator's calibration result."""
        store = getattr(self.coordinator, "_store", None)
        if store is None:
            return
        cal = store.calibration
        if cal is None:
            return
        from . import iso_chart as _ic
        try:
            self._image_bytes = _ic.render_iso_chart(
                cal,
                iso_history=store.iso_history.get(self._region, []),
                obs_count=store.observation_count,
                region=self._region,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Iso chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        return self._image_bytes if self._image_bytes else None


def _horizon_hours(run_at_str: str | None, interval_time_str: str) -> float:
    """Compute forecast horizon in hours between run_at and interval_time."""
    if not run_at_str:
        return 0.0
    try:
        run_at = parse_iso(run_at_str)
        interval = parse_iso(interval_time_str)
        return max(0.0, (interval - run_at).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0


class NemPd7dayForecastChartCamera(CoordinatorEntity[PD7DayCoordinator], Camera):
    """
    Camera entity that serves the 7-day price forecast chart.

    Renders raw vs calibrated forecast as a time-series chart with p10/p90
    confidence band, ToD background shading, and passthrough_high annotations.
    PNG is rendered in-memory after each coordinator refresh and cached.
    """

    _attr_has_entity_name = True
    _attr_name = "7-Day Forecast Chart"
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
        self._attr_unique_id = f"{entry.entry_id}_{region}_forecast_chart"

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry.entry_id}_{self._region}")},
            name=f"NEM PD7DAY {self._region}",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._refresh_image()

    def _handle_coordinator_update(self) -> None:
        self._refresh_image()
        self.async_write_ha_state()

    def _build_forecast_data(self) -> list[dict]:
        """Build enriched forecast dicts from coordinator data + calibration store."""
        if not self.coordinator.data:
            return []
        price_data = self.coordinator.data.prices.get(self._region)
        if price_data is None:
            return []

        store = getattr(self.coordinator, "_store", None)
        run_at = price_data.forecast_generated_at
        result = []

        for period in price_data.forecast:
            h = _horizon_hours(run_at, period.time)
            try:
                hour = parse_iso(period.time).hour
            except (ValueError, TypeError):
                hour = 0

            entry = {
                "nemtime": to_nem_iso(parse_iso(period.nemtime)),
                "time": to_nem_iso(parse_iso(period.time)),
                "raw_value": period.value,
                "horizon_hours": round(h, 1),
            }

            if store:
                cal = store.apply_to_price(period.value, h, hour)
                entry.update({
                    ATTR_CAL_CALIBRATED: cal["calibrated"],
                    ATTR_CAL_P10: cal["p10"],
                    ATTR_CAL_P50: cal["p50"],
                    ATTR_CAL_P90: cal["p90"],
                    ATTR_CAL_MAE: cal.get("ols_mae"),
                    ATTR_CAL_SOURCE: cal["calibrated_source"],
                    ATTR_CAL_N_OBS: cal["n_obs"],
                })
            else:
                entry["value"] = period.value

            result.append(entry)

        return result

    def _refresh_image(self) -> None:
        """Re-render the forecast chart from coordinator data."""
        forecast_data = self._build_forecast_data()
        if not forecast_data:
            return
        from . import forecast_chart as _fc
        try:
            self._image_bytes = _fc.render_forecast_chart(
                forecast_data, region=self._region,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Forecast chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        return self._image_bytes if self._image_bytes else None
