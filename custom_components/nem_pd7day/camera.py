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
from .calibration_inputs import (
    calibrate_interval,
    horizon_hours as _horizon_hours,
    interval_key_for_period,
)
from .coordinator import PD7DayCoordinator
from .nem_time import parse_iso, to_nem_iso

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

_PLACEHOLDER = b""  # returned before first render


class _InitialRenderMixin:
    """Schedules the first chart render without blocking platform setup.

    ``async_added_to_hass`` runs inside ``async_add_entities``, so awaiting the
    first matplotlib render there holds up the whole camera platform. With 15
    camera entities across five regions contending for the executor pool during
    startup, that reliably exceeded Home Assistant's 10 second platform warning
    threshold:

        Setup of camera platform nem_pd7day is taking over 10 seconds.

    Nothing needs the image to exist at setup time. ``async_camera_image``
    already returns ``None`` while ``_image_bytes`` is the ``_PLACEHOLDER``
    empty bytes, and ``_handle_coordinator_update`` has always fired the
    refresh as a task rather than awaiting it. So the first render is scheduled
    the same way, and setup returns immediately.
    """

    def _schedule_initial_render(self) -> None:
        task = self.hass.async_create_background_task(
            self._async_refresh_image(),
            name=f"nem_pd7day initial chart render {self.entity_id}",
        )
        # Cancel an in-flight render if the entity is removed first. Cancelling
        # an already-finished task is a no-op.
        self.async_on_remove(task.cancel)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ToD chart camera and bias chart camera from a config entry."""
    coordinator: PD7DayCoordinator = entry.runtime_data.coordinator
    region = entry.data.get("region", "QLD1").upper()
    async_add_entities([
        NemPd7dayTodCamera(coordinator, region, entry),
        NemPd7dayIsoChartCamera(coordinator, region, entry),
        NemPd7dayForecastChartCamera(coordinator, region, entry),
    ])


class NemPd7dayTodCamera(
    _InitialRenderMixin, CoordinatorEntity[PD7DayCoordinator], Camera
):
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
        self._schedule_initial_render()

    def _handle_coordinator_update(self) -> None:
        """Called by CoordinatorEntity on every coordinator refresh."""
        self.hass.async_create_task(self._async_refresh_image())
        self.async_write_ha_state()

    def _render(self) -> bytes:
        """Blocking render — called in executor thread."""
        tod_stats = getattr(self.coordinator, "tod_stats", None)
        if tod_stats is None or not tod_stats.slots:
            return self._image_bytes
        from . import tod_stats as _ts
        return _ts.render_chart(tod_stats, region=self._region)

    async def _async_refresh_image(self) -> None:
        try:
            result = await self.hass.async_add_executor_job(self._render)
            if result:
                self._image_bytes = result
        except Exception:  # noqa: BLE001
            _LOGGER.exception("ToD chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return the latest rendered PNG."""
        return self._image_bytes if self._image_bytes else None


class NemPd7dayBiasChartCamera(
    _InitialRenderMixin, CoordinatorEntity[PD7DayCoordinator], Camera
):
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
        self._schedule_initial_render()

    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._async_refresh_image())
        self.async_write_ha_state()

    def _render(self) -> bytes:
        """Blocking render — called in executor thread."""
        store = getattr(self.coordinator, "_store", None)
        if store is None:
            return self._image_bytes
        cal = store.calibration
        if cal is None:
            return self._image_bytes
        from . import bias_chart as _bc
        return _bc.render_chart(
            cal,
            obs_count=store.observation_count,
            region=self._region,
            tod_stats=self.coordinator.tod_stats,
        )

    async def _async_refresh_image(self) -> None:
        try:
            result = await self.hass.async_add_executor_job(self._render)
            if result:
                self._image_bytes = result
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Bias chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        return self._image_bytes if self._image_bytes else None


class NemPd7dayIsoChartCamera(
    _InitialRenderMixin, CoordinatorEntity[PD7DayCoordinator], Camera
):
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
        self._schedule_initial_render()

    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._async_refresh_image())
        self.async_write_ha_state()

    def _render(self) -> bytes:
        """Blocking render — called in executor thread."""
        store = getattr(self.coordinator, "_store", None)
        if store is None:
            return self._image_bytes
        cal = store.calibration
        if cal is None:
            return self._image_bytes
        from . import iso_chart as _ic
        return _ic.render_iso_chart(
            cal,
            iso_history=store.iso_history,
            obs_count=store.observation_count,
            region=self._region,
        )

    async def _async_refresh_image(self) -> None:
        try:
            result = await self.hass.async_add_executor_job(self._render)
            if result:
                self._image_bytes = result
        except ModuleNotFoundError as exc:
            _LOGGER.warning("Iso chart render skipped: %s", exc)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Iso chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        return self._image_bytes if self._image_bytes else None


class NemPd7dayForecastChartCamera(
    _InitialRenderMixin, CoordinatorEntity[PD7DayCoordinator], Camera
):
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
        self._schedule_initial_render()

    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._async_refresh_image())
        self.async_write_ha_state()

    def _build_forecast_data(self) -> list[dict]:
        """Build enriched forecast dicts from coordinator data + calibration store.

        Applies the gas+QNI covariate gate (Rec 2): spike forecasts above
        SPIKE_COVARIATE_RAW_FLOOR are only treated as passthrough_high when
        gas>150TJ AND qni<-300MW (or horizon<12h).  Otherwise the displayed
        value is capped at SPIKE_COVARIATE_CAP.

        Also passes forecast_run_at and per-interval spike_first_run info
        to enable horizon-gated callouts (Rec 1) and persistence scoring
        (Rec 4) in the chart renderer.
        """
        if not self.coordinator.data:
            return []
        price_data = self.coordinator.data.prices.get(self._region)
        if price_data is None:
            return []

        store = getattr(self.coordinator, "_store", None)
        run_at = price_data.forecast_generated_at

        # Rec 4 (simplified): build set of intervals that were spike in prior run
        prior_spike_intervals = self._prior_spike_intervals()

        result = []

        for period in price_data.forecast:
            h = _horizon_hours(run_at, period.time)
            try:
                hour = parse_iso(period.time).hour
            except (ValueError, TypeError):
                hour = 0

            interval_key = interval_key_for_period(period)

            entry = {
                "nemtime": to_nem_iso(parse_iso(period.nemtime)),
                "time": interval_key,
                "raw_value": period.value,
                "horizon_hours": round(h, 1),
                "forecast_run_at": run_at,
                "spike_first_run": interval_key not in prior_spike_intervals,
            }

            # Same shared entry point as sensor.py and tariff_sensor.py, so the
            # line drawn on the chart, the band shaded around it and the source
            # label are the ones the forecast sensor already published for this
            # interval. This used to call store.apply_to_price directly with the
            # gas and QNI covariates only, no stpasa_features and no
            # run_features, and the stage-2 gate needs both, so the chart could
            # never render isotonic+stpasa while the sensor routinely did. See
            # issue #80, and #66 for why there is one assembly and not four.
            #
            # WHY this is safe to call from the executor: _render runs this in
            # a worker thread, but calibrate_interval only reads. It touches no
            # Home Assistant API and writes no entity state. Its one write is
            # the STPASA index cache inside coordinator.stpasa_index, which is
            # already reached from both the loop and the executor by the
            # sensor's calibration warm and publishes its run key last for that
            # reason. sensor._calibrated_forecast_values calls this same
            # function for every interval of the run from
            # async_add_executor_job today.
            cal = calibrate_interval(
                store, self.coordinator, period.value, interval_key, h, hour,
                run_at_iso=run_at,
            )
            if cal is not None:
                cal_update = {
                    ATTR_CAL_CALIBRATED: cal["calibrated"],
                    ATTR_CAL_P10: cal["p10"],
                    ATTR_CAL_P50: cal["p50"],
                    ATTR_CAL_P90: cal["p90"],
                    ATTR_CAL_MAE: cal.get("ols_mae"),
                    ATTR_CAL_SOURCE: cal["calibrated_source"],
                    ATTR_CAL_N_OBS: cal["n_obs"],
                }
                # spike_credible, issue #84. Until this line the camera never
                # wrote the key, so the set _save_spike_intervals builds below
                # was always empty, spike_first_run was therefore always True,
                # and forecast_chart.py drops any interval whose
                # spike_credible is not True before it reaches the callout
                # eligibility check. The chart's spike callouts had never once
                # been drawn. The sensor has carried the field all along, so
                # this is the same camera and sensor divergence as #66 and #80.
                #
                # The key is copied only when apply_to_price actually set it,
                # rather than with cal.get(), because the field is a tri-state
                # and the three states are not the same fact. True means the
                # gas and QNI covariates both support the spike. None means at
                # least one of them was missing, so the question was not
                # answered. Absent means the raw price is below
                # SPIKE_THRESHOLD, so the question was never asked. Only True
                # draws a callout, and the other two draw nothing, which is the
                # conservative direction. What must not happen is a default of
                # False, which would record an unanswered question as a
                # confirmed negative.
                if "spike_credible" in cal:
                    cal_update["spike_credible"] = cal["spike_credible"]
                entry.update(cal_update)
            else:
                # No calibration store, or no raw price. Carry the raw value
                # through rather than inventing a calibrated 0.
                entry["value"] = period.value

            result.append(entry)

        # Save current spike intervals for next-run persistence check (Rec 4)
        self._save_spike_intervals(result)

        return result

    def _prior_spike_intervals(self) -> set[str]:
        """Return interval keys that were spike in the previous forecast run."""
        return getattr(self, "_last_spike_intervals", set())

    def _save_spike_intervals(self, forecast_data: list[dict]) -> None:
        """Persist current spike interval set for next-run comparison (Rec 4)."""
        from .calibration_engine import SPIKE_THRESHOLD
        # An absent raw price is not a spike and is not a zero. The old
        # default of 0 compared fine only because the direct apply_to_price
        # call above raised on a None price before ever reaching here; the
        # shared entry point returns None instead, so this now has to say what
        # a missing price means rather than substitute a number for it.
        self._last_spike_intervals: set[str] = {
            entry["time"]
            for entry in forecast_data
            if entry.get("raw_value") is not None
            and entry["raw_value"] >= SPIKE_THRESHOLD
            and entry.get("spike_credible") is True
        }

    def _render(self) -> bytes:
        """Blocking render — called in executor thread."""
        forecast_data = self._build_forecast_data()
        if not forecast_data:
            return self._image_bytes
        from . import forecast_chart as _fc

        # Collect grid stress annotations overlapping the chart window
        annotations = None
        notice_store = getattr(self.coordinator, "notice_store", None)
        if notice_store is not None:
            try:
                chart_start = parse_iso(forecast_data[0]["nemtime"])
                chart_end = parse_iso(forecast_data[-1]["nemtime"])
                annotations = notice_store.get_active_notices(
                    self._region,
                    from_dt=chart_start,
                    to_dt=chart_end,
                )
            except (KeyError, ValueError, TypeError):
                pass

        return _fc.render_forecast_chart(
            forecast_data, region=self._region, annotations=annotations,
        )

    async def _async_refresh_image(self) -> None:
        try:
            result = await self.hass.async_add_executor_job(self._render)
            if result:
                self._image_bytes = result
        except ModuleNotFoundError as exc:
            _LOGGER.warning("Forecast chart render skipped: %s", exc)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Forecast chart render failed")

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        return self._image_bytes if self._image_bytes else None
