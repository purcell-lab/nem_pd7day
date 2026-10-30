"""The active tariff from Amber, and the tariff sensors it enables (#260).

A VIC1 user on Amber Express asked for the tariff Amber reports to pick the
active tariff and the unused VIC1 tariffs to be disabled. The site sensor
below is the live shape from a QLD1 install on 17 Oct 2026 (NMI and site id
removed): the general channel carries the import tariff, and the feedIn
channel reports the same import code, not 96200X.

Run with:  python -m pytest tests/test_active_tariff.py -v
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from support import install_ha_stubs, load_chain

install_ha_stubs()

_const, _catalogue, _mod = load_chain("const", "tariff_catalogue", "active_tariff")

INTEGRATION = _mod.er.RegistryEntryDisabler.INTEGRATION
DOMAIN = _const.DOMAIN

LIVE_SITE = {
    "id": "<site id>",
    "nmi": "<redacted>",
    "network": "Energex",
    "status": "active",
    "interval_length": 5,
    "channels": [
        {"identifier": "E1", "type": "general", "tariff": "96200"},
        {"identifier": "B1", "type": "feedIn", "tariff": "96200"},
    ],
}


# ── Amber's network to a distributor ─────────────────────────────────────────

@pytest.mark.parametrize("network, region, expected", [
    ("Energex", "QLD1", "energex"),
    ("energex", "QLD1", "energex"),
    ("Ergon Energy", "QLD1", "ergon"),
    ("Endeavour", "NSW1", "endeavour"),
    ("Endeavour Energy", "NSW1", "endeavour"),
    ("Ausgrid", "NSW1", "ausgrid"),
    ("Jemena", "VIC1", "jemena"),
    ("United Energy", "VIC1", "united"),
    ("United", "VIC1", "united"),
    ("AusNet Services", "VIC1", "ausnet"),
    ("Powercor", "VIC1", "powercor"),
    ("SA Power Networks", "SA1", "sapn"),
    ("SAPN", "SA1", "sapn"),
    ("TasNetworks", "TAS1", "tasnetworks"),
    ("CitiPower", "VIC1", None),         # no CitiPower tariffs in the library
    ("Energex", "VIC1", None),            # not a distributor of the region
    ("", "QLD1", None),
])
def test_network_maps_to_a_distributor_of_the_region(network, region, expected):
    assert _mod.network_distributor(network, region) == expected


def test_the_live_site_reports_energex_96200():
    assert _mod.amber_site_tariff(LIVE_SITE, "QLD1") == ("energex", "96200")


def test_the_export_partner_comes_from_the_catalogue_not_the_feed_in_channel():
    active = _mod.ActiveTariff("energex", "96200", _mod.SOURCE_AMBER)
    assert _mod.export_partner(active) == "96200X"


@pytest.mark.parametrize("change, reason", [
    ({"network": None}, "no network attribute"),
    ({"network": "CitiPower"}, "not a distributor of QLD1"),
    ({"channels": [{"type": "feedIn", "tariff": "96200"}]}, "no tariff on the general channel"),
    ({"channels": [{"type": "general", "tariff": "NOPE1"}]}, "not in the installed tariff library"),
])
def test_an_unusable_site_says_why(change, reason):
    found = _mod.amber_site_tariff({**LIVE_SITE, **change}, "QLD1")
    assert isinstance(found, str) and reason in found


# ── Which tariff is active ────────────────────────────────────────────────────

class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


def _hass(states=None):
    return types.SimpleNamespace(
        states=_States({
            eid: types.SimpleNamespace(state=state, attributes=attrs)
            for eid, (state, attrs) in (states or {}).items()
        }),
        config_entries=MagicMock(),
    )


class FakeRegistry:
    """The parts of the entity registry the module reads and writes."""

    def __init__(self, *rows):
        self.entities = {r.entity_id: r for r in rows}

    def async_update_entity(self, entity_id, **changes):
        for key, value in changes.items():
            setattr(self.entities[entity_id], key, value)

    def async_update_entity_options(self, entity_id, domain, options):
        row = self.entities[entity_id]
        row.options = {**(row.options or {}), domain: {**((row.options or {}).get(domain) or {}), **options}}

    def async_remove(self, entity_id):
        del self.entities[entity_id]


def _row(entity_id, unique_id=None, platform=DOMAIN, entry_id="E", disabled_by=None, options=None):
    return types.SimpleNamespace(
        entity_id=entity_id, unique_id=unique_id or entity_id, config_entry_id=entry_id,
        platform=platform, domain=entity_id.split(".")[0], disabled_by=disabled_by, options=options or {},
    )


SITE_ROW = _row("sensor.amber_express_amber_site", platform="amber_express", entry_id="A")


def _entry(**options):
    return types.SimpleNamespace(entry_id="E", title="NEM PD7DAY QLD1", options=options)


def _resolve(entry, states=None, registry=None):
    with patch.object(_mod.er, "async_get", return_value=registry or FakeRegistry(SITE_ROW)):
        return _mod.resolve_active_tariff(_hass(states), entry, "QLD1")


SITE_STATE = {"sensor.amber_express_amber_site": ("Energex", LIVE_SITE)}


def test_amber_sets_the_tariff_when_none_is_chosen():
    for chosen in ({}, {"active_tariff": ""}, {"active_tariff": "auto"}):
        active = _resolve(_entry(**chosen), SITE_STATE)
        assert (active.key, active.source, active.detail) == (
            "energex/96200", "amber", "sensor.amber_express_amber_site"
        ), chosen


def test_a_chosen_tariff_wins_over_amber():
    active = _resolve(_entry(active_tariff="energex/6900"), SITE_STATE)
    assert (active.key, active.source) == ("energex/6900", "option")


def test_without_amber_the_region_default_stands_in():
    active = _resolve(_entry(), {}, FakeRegistry())
    assert (active.key, active.source) == ("energex/6900", "default")


def test_an_unavailable_site_is_not_read():
    active = _resolve(_entry(), {"sensor.amber_express_amber_site": ("unavailable", LIVE_SITE)})
    assert active.source == "default"


def test_the_site_paired_with_the_configured_amber_forecast_comes_first():
    other = _row("sensor.amber_express_home2_site", platform="amber_express", entry_id="B")
    forecast = _row("sensor.amber_express_home2_general_price", platform="amber_express", entry_id="B")
    registry = FakeRegistry(SITE_ROW, other, forecast)
    states = {
        "sensor.amber_express_amber_site": ("Energex", LIVE_SITE),
        "sensor.amber_express_home2_site": ("Energex", {**LIVE_SITE, "channels": [
            {"type": "general", "tariff": "6900"}]}),
    }
    entry = _entry(amber_forecast_entity="sensor.amber_express_home2_general_price")
    assert _resolve(entry, states, registry).key == "energex/6900"
    assert _resolve(_entry(), states, registry).key == "energex/96200"


# ── Syncing the registry ──────────────────────────────────────────────────────

def _tariff_rows():
    """QLD1 tariff sensors as a fresh install registers them."""
    def tariff(code, kind="tariff", default=True):
        return _row(f"sensor.qld1_{code}_{kind}", f"E_QLD1_energex_{code}_{kind}",
                    disabled_by=None if default else INTEGRATION)
    return [
        tariff("6900"), tariff("6900", "export_tariff"),
        tariff("8900"),
        tariff("96200"), tariff("96200", "export_tariff"),
        tariff("8400", default=False),
        tariff("6800", "export_tariff", default=False),
        _row("sensor.qld1_day27_6900", "nem_pd7day_QLD1_energex_6900_days27"),
        _row("sensor.qld1_day27_6900x", "nem_pd7day_QLD1_energex_6900_export_days27"),
        _row("sensor.qld1_day27_96200", "nem_pd7day_QLD1_energex_96200_days27"),
        _row("sensor.other_entry", "E_QLD1_energex_6900_tariff", entry_id="OTHER"),
    ]


def _sync(registry, active, entry=None, days27=True):
    with patch.object(_mod.er, "async_get", return_value=registry):
        return _mod.sync_tariff_entities(_hass(), entry or _entry(), "QLD1", active, days27_active=days27)


AMBER_96200 = _mod.ActiveTariff("energex", "96200", "amber")


def _state(registry):
    return {eid: (r.disabled_by is None) for eid, r in registry.entities.items()}


def test_only_the_active_tariff_and_its_export_partner_stay_enabled():
    registry = FakeRegistry(*_tariff_rows())
    result = _sync(registry, AMBER_96200)
    assert sorted(result.disabled) == [
        "sensor.qld1_6900_export_tariff", "sensor.qld1_6900_tariff", "sensor.qld1_8900_tariff",
    ]
    state = _state(registry)
    assert state["sensor.qld1_96200_tariff"] and state["sensor.qld1_96200_export_tariff"]
    assert not state["sensor.qld1_8400_tariff"]          # was never enabled
    assert state["sensor.other_entry"]                    # another entry's sensor
    # The stale day 2-7 sensors of 6900 go; the active one stays.
    assert sorted(result.removed) == ["sensor.qld1_day27_6900", "sensor.qld1_day27_6900x"]
    assert "sensor.qld1_day27_96200" in registry.entities


def test_a_default_disabled_active_tariff_is_enabled():
    registry = FakeRegistry(*_tariff_rows())
    result = _sync(registry, _mod.ActiveTariff("energex", "8400", "amber"))
    assert result.enabled == ["sensor.qld1_8400_tariff"]
    assert registry.entities["sensor.qld1_8400_tariff"].disabled_by is None


def test_a_sensor_the_user_enabled_is_left_alone():
    rows = _tariff_rows()
    registry = FakeRegistry(*rows)
    registry.entities["sensor.qld1_8400_tariff"].disabled_by = None   # the user enabled it
    registry.entities["sensor.qld1_8900_tariff"].disabled_by = "user"
    _sync(registry, AMBER_96200)
    assert registry.entities["sensor.qld1_8400_tariff"].disabled_by is None
    assert registry.entities["sensor.qld1_8900_tariff"].disabled_by == "user"


def test_each_sensor_is_disabled_once_and_a_re_enable_sticks():
    registry = FakeRegistry(*_tariff_rows())
    _sync(registry, AMBER_96200)
    registry.entities["sensor.qld1_6900_tariff"].disabled_by = None   # the user turns it back on
    again = _sync(registry, AMBER_96200)
    assert again.disabled == []
    assert registry.entities["sensor.qld1_6900_tariff"].disabled_by is None


def test_switching_the_option_off_restores_what_was_disabled():
    registry = FakeRegistry(*_tariff_rows())
    _sync(registry, AMBER_96200)
    result = _sync(registry, AMBER_96200, entry=_entry(only_active_tariff=False))
    assert sorted(result.enabled) == [
        "sensor.qld1_6900_export_tariff", "sensor.qld1_6900_tariff", "sensor.qld1_8900_tariff",
    ]
    assert registry.entities["sensor.qld1_8400_tariff"].disabled_by is not None   # still default-off


def test_a_new_active_tariff_brings_its_sensors_back():
    registry = FakeRegistry(*_tariff_rows())
    _sync(registry, AMBER_96200)
    result = _sync(registry, _mod.ActiveTariff("energex", "6900", "option"))
    assert sorted(result.enabled) == ["sensor.qld1_6900_export_tariff", "sensor.qld1_6900_tariff"]
    assert sorted(result.disabled) == ["sensor.qld1_96200_export_tariff", "sensor.qld1_96200_tariff"]


def test_nothing_is_disabled_on_a_default_guess():
    registry = FakeRegistry(*_tariff_rows())
    result = _sync(registry, _mod.ActiveTariff("energex", "6900", "default"))
    assert result.disabled == []


def test_full_mode_removes_every_day_2_7_sensor():
    registry = FakeRegistry(*_tariff_rows())
    result = _sync(registry, AMBER_96200, days27=False)
    assert len(result.removed) == 3


def test_tariff_codes_with_underscores_parse():
    assert _mod._tariff_of("E_VIC1_victoria_VICR_TOU_tariff", "E_VIC1_") == ("victoria", "VICR_TOU")
    assert _mod._tariff_of("E_VIC1_victoria_VICR_TOU_export_tariff", "E_VIC1_") == ("victoria", "VICR_TOU")
    assert _mod._days27_tariff_of("nem_pd7day_VIC1_victoria_VICR_TOU_days27", "VIC1") == ("victoria", "VICR_TOU")


# ── Amber arriving after setup ────────────────────────────────────────────────

def test_a_site_reporting_a_different_tariff_reloads_the_entry():
    hass = _hass({"sensor.amber_express_amber_site": ("Energex", LIVE_SITE)})
    captured = {}

    def _track(_hass, entity_ids, callback):
        captured["ids"], captured["cb"] = entity_ids, callback
        return MagicMock()

    with patch.object(_mod.er, "async_get", return_value=FakeRegistry(SITE_ROW)), \
            patch.object(_mod, "callback", lambda f: f), \
            patch.object(_mod, "async_track_state_change_event", _track):
        unsub = _mod.watch_amber_site(hass, _entry(), "QLD1", _mod.ActiveTariff("energex", "6900", "default"))
        assert unsub is not None and captured["ids"] == ["sensor.amber_express_amber_site"]
        captured["cb"](None)
    hass.config_entries.async_schedule_reload.assert_called_once_with("E")


def test_a_chosen_tariff_is_not_watched():
    with patch.object(_mod.er, "async_get", return_value=FakeRegistry(SITE_ROW)):
        assert _mod.watch_amber_site(_hass(), _entry(), "QLD1", _mod.ActiveTariff("energex", "6900", "option")) is None
