"""Separate shadow-only sensor, using existing dispatch and calibrated forecasts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import staleness_attributes
from .dispatch_client import parse_settlement
from .nem_time import NEM_TZ
from .scarcity_premium import (
    FLOOR_CAP,
    PREMIUM_CAP,
    TRIGGER,
    PremiumResult,
    build_premium,
    finite_price,
    morning_samples,
)

_LOGGER = logging.getLogger(__name__)


class ScarcityPremiumSensor(CoordinatorEntity, SensorEntity):
    """An additive $/kWh forecast. Does not modify any existing price or tariff."""

    _attr_has_entity_name = True
    _attr_name = "NEM Scarcity Premium"
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_should_poll = False
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, coordinator, entry, base_sensor) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._base = base_sensor
        self._dispatch = entry.runtime_data.dispatch
        self._attr_unique_id = f"nem_pd7day_{entry.entry_id}_qld1_scarcity_premium"
        self._attr_device_info = base_sensor._attr_device_info
        self._samples: dict[str, float] = {}
        self._storage = None
        self._lock = asyncio.Lock()
        self._result = PremiumResult("initialising", [], 0, None, None)

    @property
    def available(self) -> bool:
        return self._result.value is not None

    @property
    def native_value(self) -> float | None:
        return self._result.value

    @property
    def extra_state_attributes(self) -> dict:
        result = self._result
        return {
            "forecast": result.forecast,
            "interpolation_mode": "previous",
            "interval_minutes": 30,
            "region": "QLD1",
            "status": result.status,
            "experimental": True,
            "model": "morning_price_floor_v1_unvalidated",
            "base_entity": self._base.entity_id,
            "base_value_field": "forecast.value",
            "morning_samples": result.count,
            "required_samples": 36,
            "morning_mean_mwh": round(result.morning_mean * 1000, 3) if result.morning_mean is not None else None,
            "target_floor_mwh": round(result.target_floor * 1000, 3) if result.target_floor is not None else None,
            "trigger_mwh": TRIGGER * 1000,
            "floor_cap_mwh": FLOOR_CAP * 1000,
            "premium_cap_mwh": PREMIUM_CAP * 1000,
            "signal_window_nem": "07:00-10:00",
            "application_window_nem": "10:00-14:00",
            "future_day_policy": "zero_pending_that_days_morning_observations",
        }

    async def async_added_to_hass(self) -> None:
        self._storage = Store(self.hass, 1, f"{self._attr_unique_id}_samples")
        restored = await self._storage.async_load()
        if isinstance(restored, dict):
            self._samples = morning_samples(restored, datetime.now(timezone.utc))
        await super().async_added_to_hass()
        if self._dispatch is not None:
            self.async_on_remove(self._dispatch.async_add_listener(self._queue_refresh))
        self.async_on_remove(
            async_track_time_change(self.hass, self._on_clock, second=45)
        )
        await self._async_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._storage is not None:
            await self._storage.async_save(dict(self._samples))
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._queue_refresh()

    @callback
    def _queue_refresh(self) -> None:
        self._entry.async_create_background_task(
            self.hass, self._async_refresh(), "scarcity_premium_refresh"
        )

    async def _on_clock(self, _now) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        async with self._lock:
            now = datetime.now(timezone.utc)
            samples = morning_samples(self._samples, now)
            price = self._dispatch.prices.get("QLD1") if self._dispatch is not None else None
            if price is not None:
                try:
                    stamp = parse_settlement(price.interval_datetime).replace(tzinfo=NEM_TZ)
                    # Reject future or stale snapshots; never count a forecast as an actual.
                    if 0 <= (now - stamp).total_seconds() <= 600:
                        samples[stamp.astimezone(timezone.utc).isoformat()] = finite_price(price.rrp)
                except (TypeError, ValueError):
                    pass
            samples = morning_samples(samples, now)
            if samples != self._samples:
                self._samples = samples
                if self._storage is not None:
                    self._storage.async_delay_save(lambda: dict(self._samples), 5)
            base_rows = []
            fresh = False
            # Before the signal is ready there is no need to warm a forecast.
            if 10 <= now.astimezone(NEM_TZ).hour < 14 and len(samples) == 36:
                try:
                    await self._base._async_warm_calibrated_forecast()
                    data = self._base._price_data
                    if data is not None:
                        base_rows = self._base._cached_calibrated_forecast(
                            self._base._calibrated_forecast_key(data)
                        ) or []
                    fresh = (
                        bool(self.coordinator.last_update_success)
                        and not staleness_attributes(self.coordinator).get("is_stale", True)
                    )
                except Exception:
                    _LOGGER.exception("Scarcity premium base forecast unavailable")
            self._result = build_premium(now, samples, base_rows, base_fresh=fresh)
            self.async_write_ha_state()
