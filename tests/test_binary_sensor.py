"""
Tests for binary_sensor.py: the region-scoped intervention and grid-stress
entities.

Run with:  python -m pytest tests/test_binary_sensor.py -v
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

from support import install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const_mod, _bs_mod = load_chain("const", "binary_sensor")

CONF_REGION = _const_mod.CONF_REGION
DOMAIN = _const_mod.DOMAIN


def test_setup_entry_creates_intervention_and_grid_stress_sensors_for_the_region():
    coordinator = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.data = {CONF_REGION: "QLD1"}
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(
        coordinator=coordinator, store=MagicMock(), dispatch=None,
    )
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    created: list = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(_bs_mod.async_setup_entry(hass, entry, _add_entities))

    assert [type(e) for e in created] == [
        _bs_mod.PD7DayInterventionSensor,
        _bs_mod.NemPd7dayGridStressBinarySensor,
    ]
    assert created[0]._region == "QLD1"


def test_intervention_sensor_uses_slugified_ids_and_region_device():
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = MagicMock(
        case=MagicMock(
            intervention=False,
            run_datetime="2026-04-15T07:25:07+10:00",
            last_changed="2026-04-15T07:25:07+10:00",
        ),
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
    )
    entry = MagicMock()
    entry.entry_id = "entry_1"

    entity = _bs_mod.PD7DayInterventionSensor(coordinator, entry, "NSW1")

    assert entity._attr_unique_id == "entry_1_nsw1_intervention"
    assert entity._attr_name == "Market Intervention"
    assert entity._attr_device_info["identifiers"] == {(DOMAIN, "entry_1_NSW1")}
