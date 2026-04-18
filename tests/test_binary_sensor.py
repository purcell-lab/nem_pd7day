"""Tests for binary_sensor.py region-scoped intervention entities."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _load_binary_sensor_under_test():
    module_names = [
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.binary_sensor",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.util",
        "custom_components.nem_pd7day.const",
        "custom_components.nem_pd7day.coordinator",
        "custom_components.nem_pd7day.binary_sensor",
    ]
    snapshot = {name: sys.modules.get(name) for name in module_names}

    ha_binary_sensor = types.SimpleNamespace(
        BinarySensorDeviceClass=types.SimpleNamespace(PROBLEM="problem"),
        BinarySensorEntity=object,
    )
    ha_device_registry = types.SimpleNamespace(DeviceInfo=dict)
    ha_entity_platform = types.SimpleNamespace(AddEntitiesCallback=object)

    class _FakeCoordinatorEntity:
        def __init__(self, coordinator=None, **kwargs):
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls

    ha_update_coordinator = types.SimpleNamespace(CoordinatorEntity=_FakeCoordinatorEntity)

    sys.modules["homeassistant"] = types.SimpleNamespace()
    sys.modules["homeassistant.components"] = types.SimpleNamespace(binary_sensor=ha_binary_sensor)
    sys.modules["homeassistant.components.binary_sensor"] = ha_binary_sensor
    sys.modules["homeassistant.config_entries"] = types.SimpleNamespace(ConfigEntry=object)
    sys.modules["homeassistant.core"] = types.SimpleNamespace(HomeAssistant=object)
    sys.modules["homeassistant.helpers"] = types.SimpleNamespace()
    sys.modules["homeassistant.helpers.device_registry"] = ha_device_registry
    sys.modules["homeassistant.helpers.entity_platform"] = ha_entity_platform
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update_coordinator
    sys.modules["homeassistant.util"] = types.SimpleNamespace(
        slugify=lambda value: value.lower().replace("-", "_").replace(" ", "_")
    )

    const_mod = _load(
        "custom_components.nem_pd7day.const",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
    )
    sys.modules["custom_components.nem_pd7day.coordinator"] = types.SimpleNamespace(
        PD7DayCoordinator=object
    )
    binary_sensor_mod = _load(
        "custom_components.nem_pd7day.binary_sensor",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "binary_sensor.py"),
    )

    def _restore():
        for name, previous in snapshot.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    return binary_sensor_mod, const_mod, _restore


def test_async_setup_entry_creates_one_binary_sensor_per_selected_region():
    binary_sensor_mod, const_mod, restore = _load_binary_sensor_under_test()
    try:
        coordinator = MagicMock()
        entry = MagicMock()
        entry.entry_id = "entry_1"
        entry.data = {const_mod.CONF_REGIONS: ["QLD1"]}
        entry.options = {const_mod.CONF_REGIONS: ["QLD1", "NSW1"]}

        hass = MagicMock()
        hass.data = {
            const_mod.DOMAIN: {
                entry.entry_id: {
                    const_mod.COORDINATOR_KEY: coordinator,
                }
            }
        }

        created = []

        def _add_entities(entities, update_before_add=False):
            created.extend(entities)

        run_async(binary_sensor_mod.async_setup_entry(hass, entry, _add_entities))

        assert len(created) == 2
        entity_ids = sorted(ent.entity_id for ent in created)
        assert entity_ids == [
            "binary_sensor.nem_pd7day_nsw1_intervention",
            "binary_sensor.nem_pd7day_qld1_intervention",
        ]
    finally:
        restore()


def test_intervention_sensor_uses_slugified_ids_and_region_device():
    binary_sensor_mod, _, restore = _load_binary_sensor_under_test()
    try:
        coordinator = MagicMock()
        coordinator.last_update_success = True
        coordinator.data = MagicMock(
            case=MagicMock(intervention=False, run_datetime="2026-04-15T07:25:07+10:00", last_changed="2026-04-15T07:25:07+10:00"),
            source_file="PUBLIC_PD7DAY_20260415.ZIP",
        )

        entry = MagicMock()
        entry.entry_id = "entry_1"

        entity = binary_sensor_mod.PD7DayInterventionSensor(coordinator, entry, "NSW1")

        assert entity._attr_unique_id == "entry_1_nsw1_intervention"
        assert entity.entity_id == "binary_sensor.nem_pd7day_nsw1_intervention"
        assert entity._attr_name == "NSW1 PD7DAY Market Intervention"
        assert entity._attr_device_info["identifiers"] == {
            ("nem_pd7day", "entry_1_NSW1")
        }
    finally:
        restore()
