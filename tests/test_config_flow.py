"""
Tests for config_flow.py: the user and reconfigure flows, and the options
flow's defaults (from current options, not stale entry data).

config_flow.py needs a ConfigFlow/OptionsFlow base with the real result
helpers and a voluptuous Schema that applies defaults, which the MagicMock tree
in support.install_ha_stubs() cannot provide. The ``flow_env`` fixture installs
SimpleNamespace stubs for exactly those modules, loads config_flow against
them, and restores sys.modules afterwards so no other file sees them.

Run with:  python -m pytest tests/test_config_flow.py -v
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from support import load, run_async

_STUBBED_MODULES = [
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


class _FakeConfigFlow:
    # Populated by tests exercising the reconfigure flow.
    _reconfigure_entry = None
    _current_entries: list = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    async def async_set_unique_id(self, unique_id: str):
        self._unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        return None

    def _get_reconfigure_entry(self):
        return self._reconfigure_entry

    def _async_current_entries(self, include_ignore=True):
        return list(self._current_entries)

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}

    def async_update_reload_and_abort(
        self, entry, *, title=None, data=None, options=None, unique_id=None, **kwargs
    ):
        return {
            "type": "update_reload_and_abort",
            "entry": entry,
            "title": title,
            "data": data,
            "options": options,
            "unique_id": unique_id,
        }

    def async_create_entry(self, *, title, data, options=None, description_placeholders=None):
        return {
            "type": "create_entry",
            "title": title,
            "data": data,
            "options": options,
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
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id, data_schema, errors=None, description_placeholders=None):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders,
        }


class _Marker:
    def __init__(self, key, default=None, description=None):
        self.key = key
        self.default = default
        self.description = description


class _Required(_Marker):
    pass


class _Optional(_Marker):
    pass


class _Schema:
    """Enough of voluptuous.Schema to apply Required/Optional defaults."""

    def __init__(self, spec):
        self.spec = spec

    def __call__(self, data):
        out = {}
        payload = data or {}
        for key_spec, validator in self.spec.items():
            if isinstance(key_spec, _Marker):
                key = key_spec.key
                value = payload.get(key, key_spec.default)
            else:
                key = key_spec
                value = payload.get(key)
            if callable(validator):
                value = validator(value)
            out[key] = value
        return out


@pytest.fixture
def flow_env():
    """Load config_flow against local HA stubs; yields (config_flow_mod, const_mod)."""
    snapshot = {name: sys.modules.get(name) for name in _STUBBED_MODULES}

    ha_config_entries = types.SimpleNamespace(
        ConfigFlow=_FakeConfigFlow,
        OptionsFlow=_FakeOptionsFlow,
        ConfigEntry=object,
        FlowResult=dict,
    )
    ha_helpers_selector = types.SimpleNamespace(selector=lambda _cfg: (lambda value: value))

    sys.modules["aiohttp"] = types.SimpleNamespace(ClientError=Exception)
    sys.modules["voluptuous"] = types.SimpleNamespace(Required=_Required, Optional=_Optional, Schema=_Schema)
    sys.modules["homeassistant"] = types.SimpleNamespace(config_entries=ha_config_entries)
    sys.modules["homeassistant.config_entries"] = ha_config_entries
    sys.modules["homeassistant.core"] = types.SimpleNamespace(callback=lambda f: f)
    sys.modules["homeassistant.helpers"] = types.SimpleNamespace(selector=ha_helpers_selector)
    sys.modules["homeassistant.helpers.aiohttp_client"] = types.SimpleNamespace(
        async_get_clientsession=lambda _hass: MagicMock()
    )

    const_mod = load("const")
    load("nem_time")
    load("pd7day_client")
    config_flow_mod = load("config_flow")
    # Reconfigure state is class-level on the fake; start each test clean.
    _FakeConfigFlow._reconfigure_entry = None
    _FakeConfigFlow._current_entries = []
    try:
        yield config_flow_mod, const_mod
    finally:
        for name, previous in snapshot.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def stub_client(config_flow_mod) -> list:
    """Replace the connectivity probe; returns the list of region lists it was asked for."""
    captured: list = []

    class _ClientStub:
        def __init__(self, _session):
            pass

        async def fetch_all(self, regions):
            captured.append(list(regions))
            return MagicMock()

    config_flow_mod.PD7DayClient = _ClientStub
    config_flow_mod.async_get_clientsession = lambda _hass: MagicMock()
    return captured


def make_entry(const_mod, region="QLD1", options=None, unique_id=None):
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.unique_id = unique_id or f"nem_pd7day_{region}"
    entry.data = {const_mod.CONF_REGION: region}
    entry.options = {} if options is None else options
    return entry


# ── User flow ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "region, mode",
    [("QLD1", "days_1_7"), ("NSW1", "days_2_7")],
)
def test_user_flow_creates_entry_for_selected_region(flow_env, region, mode):
    """Region step advances to forecast_mode; that step creates the entry with the mode in options."""
    config_flow_mod, const_mod = flow_env
    captured = stub_client(config_flow_mod)
    flow = config_flow_mod.PD7DayConfigFlow()
    flow.hass = MagicMock()

    result = run_async(flow.async_step_user({const_mod.CONF_REGION: region}))
    assert result["type"] == "form"
    assert result["step_id"] == "forecast_mode"

    result2 = run_async(flow.async_step_forecast_mode({const_mod.CONF_FORECAST_MODE: mode}))
    assert result2["type"] == "create_entry"
    assert flow._unique_id == f"nem_pd7day_{region}"
    assert result2["title"] == f"NEM PD7DAY {region}"
    assert result2["data"][const_mod.CONF_REGION] == region
    assert result2["options"][const_mod.CONF_FORECAST_MODE] == mode
    # active_tariff is not part of setup; it defaults to empty and is set via Options.
    assert result2["options"][const_mod.CONF_ACTIVE_TARIFF] == ""
    # The connectivity check probes the selected region.
    assert captured == [[region]]


def test_forecast_mode_option_values_are_stable(flow_env):
    """The mode strings are persisted in config entries; renaming them would strand existing installs."""
    _, const_mod = flow_env
    assert const_mod.FORECAST_MODE_FULL == "days_1_7"
    assert const_mod.FORECAST_MODE_DAYS_2_7 == "days_2_7"


# ── Options flow ──────────────────────────────────────────────────────────────

def test_options_flow_defaults_come_from_current_options(flow_env):
    """The form defaults to entry.options' region, and to days_2_7 when the install predates forecast_mode."""
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "NSW1"})

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init())

    assert result["type"] == "form"
    resolved = result["data_schema"]({})  # voluptuous applies Required defaults to an empty payload
    assert resolved[const_mod.CONF_REGION] == "NSW1"
    assert resolved[const_mod.CONF_FORECAST_MODE] == const_mod.FORECAST_MODE_DAYS_2_7


def test_options_flow_migrates_old_list_based_regions(flow_env):
    """An old list-based regions config defaults to its first element."""
    config_flow_mod, const_mod = flow_env
    entry = MagicMock()
    entry.data = {const_mod.CONF_REGIONS: ["NSW1", "VIC1"]}
    entry.options = {}

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init())

    assert result["type"] == "form"
    assert result["data_schema"]({})[const_mod.CONF_REGION] == "NSW1"


def test_options_flow_saves_region_mode_and_active_tariff(flow_env):
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={
        const_mod.CONF_REGION: "NSW1",
        const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_DAYS_2_7,
    })

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init({
        const_mod.CONF_REGION: "VIC1",
        const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_FULL,
        const_mod.CONF_ACTIVE_TARIFF: "energex/6900",
    }))

    assert result["type"] == "create_entry"
    assert result["data"][const_mod.CONF_REGION] == "VIC1"
    assert result["data"][const_mod.CONF_FORECAST_MODE] == const_mod.FORECAST_MODE_FULL
    assert result["data"][const_mod.CONF_ACTIVE_TARIFF] == "energex/6900"


def test_options_flow_saves_the_amber_forecast_entity_and_clears_it_when_omitted(flow_env):
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1"})
    base = {const_mod.CONF_REGION: "QLD1", const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_DAYS_2_7}

    chosen = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(
        {**base, const_mod.CONF_AMBER_FORECAST_ENTITY: "sensor.amber_general_forecast"}))
    cleared = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(dict(base)))

    assert chosen["data"][const_mod.CONF_AMBER_FORECAST_ENTITY] == "sensor.amber_general_forecast"
    assert cleared["data"][const_mod.CONF_AMBER_FORECAST_ENTITY] == ""


def test_the_amber_field_offers_only_amber_sensors_and_suggests_the_current_one(flow_env, monkeypatch):
    """A wrapper template's last point is not an interval start, so only Amber
    Electric's and Amber Express's own sensors are offered (#235)."""
    config_flow_mod, const_mod = flow_env
    configs: list = []
    monkeypatch.setattr(config_flow_mod.selector, "selector", lambda cfg: configs.append(cfg) or (lambda v: v))
    entry = make_entry(const_mod, "QLD1", options={
        const_mod.CONF_REGION: "QLD1",
        const_mod.CONF_AMBER_FORECAST_ENTITY: "sensor.amber_express_amber_general_price",
    })

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init())

    (marker,) = [m for m in result["data_schema"].spec if m.key == const_mod.CONF_AMBER_FORECAST_ENTITY]
    assert marker.description == {"suggested_value": "sensor.amber_express_amber_general_price"}
    (entity_cfg,) = [c["entity"] for c in configs if "entity" in c]
    assert entity_cfg["filter"] == [
        {"integration": "amberelectric", "domain": "sensor"},
        {"integration": "amber_express", "domain": "sensor"},
    ]


def test_options_flow_saves_local_price_duids_upper_cased_and_deduplicated(flow_env):
    """#275: DUIDs are normalised; empty is the default. The 3.32 diagnostic toggle is gone."""
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1"})
    base = {const_mod.CONF_REGION: "QLD1", const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_DAYS_2_7}

    listed = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(
        {**base, const_mod.CONF_LOCAL_PRICE_DUIDS: "gstone1, CATHROCK\nGSTONE1"}))
    omitted = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(dict(base)))

    assert listed["data"][const_mod.CONF_LOCAL_PRICE_DUIDS] == "GSTONE1, CATHROCK"
    assert "local_price_diagnostic" not in listed["data"]
    assert omitted["data"][const_mod.CONF_LOCAL_PRICE_DUIDS] == ""


def test_options_flow_refuses_a_string_that_cannot_be_a_duid(flow_env):
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1"})

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init({
        const_mod.CONF_REGION: "QLD1",
        const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_DAYS_2_7,
        const_mod.CONF_LOCAL_PRICE_DUIDS: "GSTONE1, TOOLONGDUID99, BAD@ID",
    }))

    assert result["type"] == "form"
    assert result["errors"] == {const_mod.CONF_LOCAL_PRICE_DUIDS: "invalid_duid"}


# ── Reconfigure flow ──────────────────────────────────────────────────────────

def test_reconfigure_flow_to_a_different_region_updates_and_reloads(flow_env):
    config_flow_mod, const_mod = flow_env
    captured = stub_client(config_flow_mod)
    entry = make_entry(const_mod, "QLD1", options={
        const_mod.CONF_REGION: "QLD1",
        const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_FULL,
        const_mod.CONF_ACTIVE_TARIFF: "",
    })
    flow = config_flow_mod.PD7DayConfigFlow()
    flow.hass = MagicMock()
    flow._reconfigure_entry = entry
    flow._current_entries = [entry]

    form = run_async(flow.async_step_reconfigure())
    assert form["type"] == "form"
    assert form["step_id"] == "reconfigure"
    assert form["data_schema"]({})[const_mod.CONF_REGION] == "QLD1"  # defaults to the current region

    result = run_async(flow.async_step_reconfigure({const_mod.CONF_REGION: "NSW1"}))
    assert result["type"] == "update_reload_and_abort"
    assert result["entry"] is entry
    assert result["title"] == "NEM PD7DAY NSW1"
    assert result["data"][const_mod.CONF_REGION] == "NSW1"
    assert result["options"][const_mod.CONF_REGION] == "NSW1"
    assert result["options"][const_mod.CONF_FORECAST_MODE] == const_mod.FORECAST_MODE_FULL  # preserved
    assert result["unique_id"] == "nem_pd7day_NSW1"
    assert captured == [["NSW1"]]  # the connectivity check probes the new region


def test_reconfigure_flow_to_the_same_region_does_not_abort(flow_env):
    """The same region is the current entry, so it must not abort as already_configured."""
    config_flow_mod, const_mod = flow_env
    stub_client(config_flow_mod)
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1"})
    flow = config_flow_mod.PD7DayConfigFlow()
    flow.hass = MagicMock()
    flow._reconfigure_entry = entry
    flow._current_entries = [entry]

    result = run_async(flow.async_step_reconfigure({const_mod.CONF_REGION: "QLD1"}))

    assert result["type"] == "update_reload_and_abort"
    assert result["entry"] is entry
    assert result["data"][const_mod.CONF_REGION] == "QLD1"
    assert result["unique_id"] == "nem_pd7day_QLD1"


def test_energex_two_way_trial_is_a_selectable_active_tariff(flow_env):
    """96200 (Residential Two-Way Tariff Trial) can be the active tariff, so a
    days 2-7 sensor exists for it; its full days 1-7 sensor would overlap the
    Amber forecast it is summed with."""
    config_flow_mod, _const_mod = flow_env
    values = [o["value"] for o in config_flow_mod._tariff_options_for_region("QLD1")]
    assert "energex/96200" in values
    assert values[0] == "energex/6900"          # the default stays the first entry
    assert config_flow_mod._default_tariff_for_region("QLD1") == "energex/6900"


def test_the_duid_field_is_a_dropdown_of_the_region_with_typed_values_kept(flow_env, monkeypatch):
    """The options list the region's DUIDs from the bundled catalogue, most
    constrained first; a chosen DUID it lacks stays selectable."""
    config_flow_mod, const_mod = flow_env
    catalogue = config_flow_mod.load_catalogue.__wrapped__()  # the bundled file
    configs: list = []
    monkeypatch.setattr(config_flow_mod.selector, "selector", lambda cfg: configs.append(cfg) or (lambda v: v))
    entry = make_entry(const_mod, "QLD1", options={
        const_mod.CONF_REGION: "QLD1", const_mod.CONF_LOCAL_PRICE_DUIDS: "GSTONE1, NEWDUID1",
    })

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init())

    (marker,) = [m for m in result["data_schema"].spec if m.key == const_mod.CONF_LOCAL_PRICE_DUIDS]
    assert marker.description == {"suggested_value": ["GSTONE1", "NEWDUID1"]}
    select, _nodes = [c["select"] for c in configs if "select" in c and c["select"].get("custom_value")]
    values = [o["value"] for o in select["options"]]
    assert select["multiple"] is True
    assert values[-1] == "NEWDUID1" and "GSTONE1" in values and "CATHROCK" not in values
    assert {catalogue.info(v).region for v in values[:-1]} == {"QLD1"}


def test_the_node_field_lists_the_regions_tnis_with_typed_values_kept(flow_env, monkeypatch):
    """Transmission nodes (#286): the region's TNIs, labelled by station; a typed TNI stays selectable."""
    config_flow_mod, const_mod = flow_env
    configs: list = []
    monkeypatch.setattr(config_flow_mod.selector, "selector", lambda cfg: configs.append(cfg) or (lambda v: v))
    entry = make_entry(const_mod, "QLD1", options={
        const_mod.CONF_REGION: "QLD1", const_mod.CONF_NODES: "QWIV1, QNEW9",
    })

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init())

    (marker,) = [m for m in result["data_schema"].spec if m.key == const_mod.CONF_NODES]
    assert marker.description == {"suggested_value": ["QWIV1", "QNEW9"]}
    _duids, nodes = [c["select"] for c in configs if "select" in c and c["select"].get("custom_value")]
    labels = {o["value"]: o["label"] for o in nodes["options"]}
    assert labels["QWIV1"].startswith("QWIV1 · Wivenhoe") and labels["QNEW9"] == "QNEW9"
    assert "NBAY1" not in labels and nodes["multiple"] is True


def test_nodes_are_saved_comma_separated_and_a_bad_one_is_refused(flow_env):
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1"})
    base = {const_mod.CONF_REGION: "QLD1", const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_DAYS_2_7}

    saved = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(
        {**base, const_mod.CONF_NODES: ["qwiv1", "QGLD1", "QWIV1"]}))
    refused = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(
        {**base, const_mod.CONF_NODES: ["QWIV1", "BAD-1"]}))
    omitted = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(dict(base)))

    assert saved["data"][const_mod.CONF_NODES] == "QWIV1, QGLD1"
    assert refused["errors"] == {const_mod.CONF_NODES: "invalid_tni"}
    assert omitted["data"][const_mod.CONF_NODES] == ""


def test_a_dropdown_choice_is_saved_as_the_comma_separated_string(flow_env):
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1"})

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init({
        const_mod.CONF_REGION: "QLD1",
        const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_DAYS_2_7,
        const_mod.CONF_LOCAL_PRICE_DUIDS: ["KEPSF1", "BARRON-1", "kepsf1"],
    }))

    assert result["data"][const_mod.CONF_LOCAL_PRICE_DUIDS] == "KEPSF1, BARRON-1"


def test_every_number_selector_step_is_one_home_assistant_accepts(flow_env, monkeypatch):
    """HA's NumberSelector takes a step of "any" or at least 0.001; 0.0001 made
    the whole options form fail to open (HTTP 400), the UI's Configure included."""
    config_flow_mod, const_mod = flow_env
    configs: list = []
    monkeypatch.setattr(config_flow_mod.selector, "selector", lambda cfg: configs.append(cfg) or (lambda v: v))
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1", const_mod.CONF_LOSS_FACTOR: 1.0272})

    run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init())

    numbers = [c["number"] for c in configs if "number" in c]
    assert numbers, "the loss factor is a number selector"
    for number in numbers:
        step = number.get("step", 1)
        assert step == "any" or step >= 1e-3, number


def test_options_flow_saves_the_loss_factor_and_clears_it_when_omitted(flow_env):
    config_flow_mod, const_mod = flow_env
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1"})
    base = {const_mod.CONF_REGION: "QLD1", const_mod.CONF_FORECAST_MODE: const_mod.FORECAST_MODE_DAYS_2_7}

    chosen = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(
        {**base, const_mod.CONF_LOSS_FACTOR: 1.0272}))
    cleared = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init(dict(base)))

    assert chosen["data"][const_mod.CONF_LOSS_FACTOR] == 1.0272
    assert cleared["data"][const_mod.CONF_LOSS_FACTOR] is None


def test_options_form_shows_the_library_and_amber_loss_factors(flow_env, monkeypatch):
    config_flow_mod, const_mod = flow_env
    monkeypatch.setattr(config_flow_mod, "amber_loss_factor", lambda hass: 1.0272)
    entry = make_entry(const_mod, "QLD1", options={const_mod.CONF_REGION: "QLD1", const_mod.CONF_LOSS_FACTOR: 1.0272})

    result = run_async(config_flow_mod.PD7DayOptionsFlow(entry).async_step_init())

    placeholders = result["description_placeholders"]
    assert (placeholders["library_loss_factor"], placeholders["amber_loss_factor"]) == ("1.0919", "1.0272")
    (marker,) = [m for m in result["data_schema"].spec if m.key == const_mod.CONF_LOSS_FACTOR]
    assert marker.description == {"suggested_value": 1.0272}
