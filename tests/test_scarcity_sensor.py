"""Adapter behaviour using this repository's established HA stubs."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Installs the existing sensor/coordinator test stubs.
import test_sensor
from custom_components.nem_pd7day.scarcity_premium import NEM


@pytest.fixture
def adapter(monkeypatch):
    # Load a separate copy so the callback mock used by older tests cannot
    # turn our real callback methods into MagicMocks.
    monkeypatch.setattr(sys.modules["homeassistant.core"], "callback", lambda fn: fn)
    class AdapterCoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        async def async_added_to_hass(self):
            pass

        async def async_will_remove_from_hass(self):
            pass

    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.update_coordinator"],
        "CoordinatorEntity", AdapterCoordinatorEntity,
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.components.sensor"], "SensorEntity", object
    )
    name = "custom_components.nem_pd7day._scarcity_adapter_test"
    path = Path(__file__).parents[1] / "custom_components/nem_pd7day/scarcity_sensor.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fixed = datetime(2026, 9, 23, 10, 5, tzinfo=NEM)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz or timezone.utc)

    monkeypatch.setattr(mod, "datetime", Clock)
    monkeypatch.setattr(mod, "staleness_attributes", lambda _: {"is_stale": False})
    return mod


def make_sensor(adapter):
    from test_scarcity_premium import base
    dispatch = SimpleNamespace(prices={})
    entry = MagicMock()
    entry.entry_id = "test"
    entry.runtime_data = SimpleNamespace(dispatch=dispatch)
    base_sensor = SimpleNamespace(
        _attr_device_info={"identifiers": {("nem_pd7day", "test_QLD1")}},
        entity_id="sensor.qld1_price",
        _price_data=object(),
        _async_warm_calibrated_forecast=AsyncMock(),
        _calibrated_forecast_key=lambda _: ("key",),
        _cached_calibrated_forecast=lambda _: base(),
    )
    sensor = adapter.ScarcityPremiumSensor(
        SimpleNamespace(last_update_success=True), entry, base_sensor
    )
    sensor.async_write_ha_state = MagicMock()
    sensor.hass = MagicMock()
    sensor.async_on_remove = MagicMock()
    return sensor


async def test_live_refresh_duplicate_does_not_inflate_sample_count(adapter):
    from test_scarcity_premium import observations
    sensor = make_sensor(adapter)
    sensor._samples = observations()
    sensor._storage = MagicMock()
    sensor._dispatch.prices["QLD1"] = SimpleNamespace(
        interval_datetime="2026-09-23T10:00:00", rrp=.041
    )
    await sensor._async_refresh()
    await sensor._async_refresh()
    assert sensor.available
    assert sensor.native_value == .031
    assert len(sensor._samples) == 36
    attrs = sensor.extra_state_attributes
    assert attrs["base_entity"] == "sensor.qld1_price"
    assert attrs["interpolation_mode"] == "previous"
    assert len(attrs["forecast"]) == 337
    # Only initial normalisation changes persisted keys.
    assert sensor._storage.async_delay_save.call_count == 1


async def test_dispatch_update_cannot_fill_missing_interval_with_stale_data(adapter):
    from test_scarcity_premium import observations
    sensor = make_sensor(adapter)
    sensor._samples = observations()
    del sensor._samples["2026-09-23T07:05:00+10:00"]
    sensor._dispatch.prices["QLD1"] = SimpleNamespace(
        interval_datetime="2026-09-23T07:05:00", rrp=.041
    )
    await sensor._async_refresh()
    assert not sensor.available
    assert sensor.extra_state_attributes["status"] == "incomplete_morning"
    assert sensor.extra_state_attributes["forecast"] == []


@pytest.mark.parametrize("settlement", ["not a timestamp", None], ids=["malformed", "missing"])
async def test_unparseable_dispatch_settlement_is_ignored_not_raised(adapter, settlement):
    """A dispatch snapshot whose SETTLEMENTDATE cannot be read adds no sample
    and does not break the refresh; the stored morning is served unchanged."""
    from test_scarcity_premium import observations
    control = make_sensor(adapter)
    control._samples = observations()
    await control._async_refresh()  # the same refresh with no dispatch price
    sensor = make_sensor(adapter)
    sensor._samples = observations()
    sensor._dispatch.prices["QLD1"] = SimpleNamespace(interval_datetime=settlement, rrp=.041)
    await sensor._async_refresh()
    assert sensor._samples == control._samples
    assert sensor.available
    assert sensor.native_value == .031


async def test_warm_failure_and_stale_coordinator_clear_forecast(adapter, monkeypatch):
    from test_scarcity_premium import observations
    sensor = make_sensor(adapter)
    sensor._samples = observations()
    await sensor._async_refresh()
    assert sensor.available
    monkeypatch.setattr(adapter, "staleness_attributes", lambda _: {"is_stale": True})
    await sensor._async_refresh()
    assert not sensor.available
    assert sensor.extra_state_attributes["forecast"] == []
    sensor._base._async_warm_calibrated_forecast.side_effect = RuntimeError("test")
    await sensor._async_refresh()
    assert not sensor.available


async def test_startup_restores_samples_and_registers_both_listeners(adapter, monkeypatch):
    from test_scarcity_premium import observations
    sensor = make_sensor(adapter)
    storage = MagicMock()
    storage.async_load = AsyncMock(return_value=observations())
    monkeypatch.setattr(adapter, "Store", lambda *args: storage)
    sensor._dispatch.async_add_listener = MagicMock(return_value=lambda: None)
    await sensor.async_added_to_hass()
    assert sensor.available
    assert sensor.extra_state_attributes["morning_samples"] == 36
    assert sensor._dispatch.async_add_listener.call_count == 1
    assert sensor.async_on_remove.call_count == 2


async def test_unload_persists_samples(adapter):
    from test_scarcity_premium import observations
    sensor = make_sensor(adapter)
    sensor._samples = observations()
    storage = MagicMock()
    storage.async_save = AsyncMock()
    sensor._storage = storage
    await sensor.async_will_remove_from_hass()
    storage.async_save.assert_awaited_once_with(sensor._samples)


@pytest.mark.parametrize("region,expected", [("QLD1", 1), ("NSW1", 0)])
async def test_registration_only_in_qld(region, expected):
    coordinator = MagicMock()
    coordinator.data = None
    entry = MagicMock()
    entry.entry_id = "registration"
    entry.data = {"region": region}
    entry.options = {}
    entry.runtime_data = SimpleNamespace(coordinator=coordinator, store=MagicMock(), dispatch=None)
    result = []
    await test_sensor.sensor_async_setup_entry(
        MagicMock(), entry, lambda entities, **kwargs: result.extend(entities)
    )
    assert sum(type(e).__name__ == "ScarcityPremiumSensor" for e in result) == expected
