"""
Tests for config_flow.py — region selection handling and options defaults.

Covers regression-prone behavior around multi-select regions and ensures
OptionsFlow uses current options (not stale entry data).

Run with: python -m pytest tests/test_config_flow.py -v
"""
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


def _load_config_flow_under_test():
    """Load config_flow with local HA stubs and return (module, const_module, restore_fn)."""
    module_names = [
        "aiohttp",
        "voluptuous",
        "homeassistant",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.aiohttp_client",
        "custom_components.nem_pd7day.const",
        "custom_components.nem_pd7day.nem_time",
        "custom_components.nem_pd7day.pd7day_client",
        "custom_components.nem_pd7day.config_flow",
    ]
    snapshot = {name: sys.modules.get(name) for name in module_names}

    class _FakeConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

        async def async_set_unique_id(self, unique_id: str):
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            return None

        def async_create_entry(self, *, title, data, description_placeholders=None):
            return {
                "type": "create_entry",
                "title": title,
                "data": data,
                "description_placeholders": description_placeholders,
            }

        def async_show_form(self, *, step_id, data_schema, errors=None, description_placeholders=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors or {},
                "description_placeholders": description_placeholders,
            }

    class _FakeOptionsFlow:
        def async_create_entry(self, *, title, data):
            return {
                "type": "create_entry",
                "title": title,
                "data": data,
            }

        def async_show_form(self, *, step_id, data_schema, description_placeholders=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "description_placeholders": description_placeholders,
            }

    ha_config_entries = types.SimpleNamespace(
        ConfigFlow=_FakeConfigFlow,
        OptionsFlow=_FakeOptionsFlow,
        ConfigEntry=object,
        FlowResult=dict,
    )

    ha_helpers_selector = types.SimpleNamespace(selector=lambda _cfg: (lambda value: value))
    ha_helpers = types.SimpleNamespace(selector=ha_helpers_selector)
    ha_aiohttp_client = types.SimpleNamespace(async_get_clientsession=lambda _hass: MagicMock())
    ha_core = types.SimpleNamespace(callback=lambda f: f)

    class _Required:
        def __init__(self, key, default=None):
            self.key = key
            self.default = default

    class _Optional:
        def __init__(self, key, default=None):
            self.key = key
            self.default = default

    class _Schema:
        def __init__(self, spec):
            self.spec = spec

        def __call__(self, data):
            out = {}
            payload = data or {}
            for key_spec, validator in self.spec.items():
                if isinstance(key_spec, (_Required, _Optional)):
                    key = key_spec.key
                    value = payload.get(key, key_spec.default)
                else:
                    key = key_spec
                    value = payload.get(key)

                if callable(validator):
                    value = validator(value)
                out[key] = value
            return out

    vol_mod = types.SimpleNamespace(Required=_Required, Optional=_Optional, Schema=_Schema)

    sys.modules["aiohttp"] = types.SimpleNamespace(ClientError=Exception)
    sys.modules["voluptuous"] = vol_mod
    sys.modules["homeassistant"] = types.SimpleNamespace(config_entries=ha_config_entries)
    sys.modules["homeassistant.config_entries"] = ha_config_entries
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = ha_aiohttp_client

    const_mod = _load(
        "custom_components.nem_pd7day.const",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
    )
    _load(
        "custom_components.nem_pd7day.nem_time",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
    )
    _load(
        "custom_components.nem_pd7day.pd7day_client",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
    )
    config_flow_mod = _load(
        "custom_components.nem_pd7day.config_flow",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "config_flow.py"),
    )

    def _restore():
        for name, previous in snapshot.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    return config_flow_mod, const_mod, _restore


def test_user_step_creates_entry_with_all_selected_regions():
    """Submitting regions should create entry directly in single step."""
    config_flow_mod, const_mod, restore = _load_config_flow_under_test()
    captured_fetch_args = []

    class _ClientStub:
        def __init__(self, _session):
            pass

        async def fetch_all(self, regions):
            captured_fetch_args.append(list(regions))
            return MagicMock()

    config_flow_mod.PD7DayClient = _ClientStub
    config_flow_mod.async_get_clientsession = lambda _hass: MagicMock()

    try:
        flow = config_flow_mod.PD7DayConfigFlow()
        flow.hass = MagicMock()

        result = run_async(
            flow.async_step_user({const_mod.CONF_REGIONS: ["QLD1", "NSW1", "VIC1"]})
        )

        assert result["type"] == "create_entry"
        assert flow._unique_id == "nem_pd7day"
        assert result["title"] == "NEM PD7DAY"
        assert result["data"][const_mod.CONF_REGIONS] == ["QLD1", "NSW1", "VIC1"]
        assert result["data"][const_mod.CONF_CALIBRATION_REGION] == "QLD1"
        # Connectivity check only probes one region by design.
        assert captured_fetch_args == [["QLD1"]]
    finally:
        restore()


def test_user_step_normalizes_single_string_region_to_list():
    """Single region selection must persist as list and create entry directly."""
    config_flow_mod, const_mod, restore = _load_config_flow_under_test()

    class _ClientStub:
        def __init__(self, _session):
            pass

        async def fetch_all(self, _regions):
            return MagicMock()

    config_flow_mod.PD7DayClient = _ClientStub
    config_flow_mod.async_get_clientsession = lambda _hass: MagicMock()

    try:
        flow = config_flow_mod.PD7DayConfigFlow()
        flow.hass = MagicMock()

        result = run_async(flow.async_step_user({const_mod.CONF_REGIONS: "QLD1"}))

        assert result["type"] == "create_entry"
        assert result["data"][const_mod.CONF_REGIONS] == ["QLD1"]
        assert result["data"][const_mod.CONF_CALIBRATION_REGION] == "QLD1"
    finally:
        restore()


def test_user_step_empty_region_selection_returns_form_error():
    """Empty region selection must not create an entry and must return a required-field error."""
    config_flow_mod, const_mod, restore = _load_config_flow_under_test()
    try:
        flow = config_flow_mod.PD7DayConfigFlow()
        flow.hass = MagicMock()

        result = run_async(flow.async_step_user({const_mod.CONF_REGIONS: []}))

        assert result["type"] == "form"
        assert result["errors"].get(const_mod.CONF_REGIONS) == "required"
    finally:
        restore()


def test_options_flow_defaults_to_current_options_not_entry_data():
    """Options init defaults must reflect entry.options regions when present."""
    config_flow_mod, const_mod, restore = _load_config_flow_under_test()
    entry = MagicMock()
    entry.data = {const_mod.CONF_REGIONS: ["QLD1"]}
    entry.options = {const_mod.CONF_REGIONS: ["NSW1", "VIC1"]}

    try:
        flow = config_flow_mod.PD7DayOptionsFlow(entry)
        result = run_async(flow.async_step_init())

        assert result["type"] == "form"
        # Voluptuous applies Required defaults when schema is called with empty dict.
        resolved = result["data_schema"]({})
        assert resolved[const_mod.CONF_REGIONS] == ["NSW1", "VIC1"]
    finally:
        restore()


def test_options_flow_creates_entry_with_regions_and_calibration_region():
    """Options flow must save regions + calibration_region in single step."""
    config_flow_mod, const_mod, restore = _load_config_flow_under_test()
    entry = MagicMock()
    entry.data = {
        const_mod.CONF_REGIONS: ["QLD1"],
        const_mod.CONF_CALIBRATION_REGION: "QLD1",
    }
    entry.options = {
        const_mod.CONF_REGIONS: ["NSW1", "VIC1"],
        const_mod.CONF_CALIBRATION_REGION: "NSW1",
    }

    try:
        flow = config_flow_mod.PD7DayOptionsFlow(entry)
        result = run_async(flow.async_step_init({const_mod.CONF_REGIONS: ["NSW1", "VIC1"]}))

        assert result["type"] == "create_entry"
        assert result["data"][const_mod.CONF_REGIONS] == ["NSW1", "VIC1"]
        # Existing calibration region NSW1 is still in updated regions, so keep it
        assert result["data"][const_mod.CONF_CALIBRATION_REGION] == "NSW1"
    finally:
        restore()


def test_options_flow_resets_calibration_region_when_removed():
    """If current calibration region is removed from regions, default to first."""
    config_flow_mod, const_mod, restore = _load_config_flow_under_test()
    entry = MagicMock()
    entry.data = {
        const_mod.CONF_REGIONS: ["QLD1"],
        const_mod.CONF_CALIBRATION_REGION: "QLD1",
    }
    entry.options = {
        const_mod.CONF_REGIONS: ["QLD1", "NSW1"],
        const_mod.CONF_CALIBRATION_REGION: "QLD1",
    }

    try:
        flow = config_flow_mod.PD7DayOptionsFlow(entry)
        # Remove QLD1 from regions — calibration region should reset to SA1
        result = run_async(flow.async_step_init({const_mod.CONF_REGIONS: ["SA1", "VIC1"]}))

        assert result["type"] == "create_entry"
        assert result["data"][const_mod.CONF_REGIONS] == ["SA1", "VIC1"]
        assert result["data"][const_mod.CONF_CALIBRATION_REGION] == "SA1"
    finally:
        restore()
