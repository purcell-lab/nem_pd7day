"""
The bundled DUID catalogue (duid_catalogue.py and duid_catalogue.json) and
what the local price sensors take from it: the DUID's region, station, fuel
and coordinates, and each region's most constrained DUIDs as disabled presets.

The JSON is built by scripts/build_duid_catalogue.py from AEMO's registration
data, Open Electricity's facility files and a fortnight of DispatchIS
LOCAL_PRICE rows; these tests check its shape, not its contents of the day.

Run with:  python -m pytest tests/test_duid_catalogue.py -v
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const, _cat, _lp, _cc, _coord, _sensor = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "local_price_coordinator", "local_price_sensor"
)

REGIONS = ("QLD1", "NSW1", "VIC1", "SA1", "TAS1")
DISPATCH = (Path(__file__).parent / "fixtures" / "local_price" / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
ENTRY = SimpleNamespace(entry_id="e1")


# ── The bundled file ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bundled():
    _cat.load_catalogue.cache_clear()
    return _cat.load_catalogue()


def test_the_bundled_catalogue_covers_every_region(bundled):
    assert len(bundled.duids) > 500
    assert {i.region for i in bundled.duids.values()} == set(REGIONS)
    assert all(_lp.DUID_RE.match(d) for d in bundled.duids), "every registered DUID passes the options check"


def test_each_region_has_up_to_five_presets_of_its_own(bundled):
    for region in REGIONS:
        top = bundled.top_for(region)
        assert 0 < len(top) <= 5 and len(set(top)) == len(top)
        assert all(bundled.info(d).region == region for d in top)


def test_coordinates_are_in_australia_or_absent(bundled):
    located = [i for i in bundled.duids.values() if i.latitude is not None]
    assert len(located) > 400
    assert all(-44 < i.latitude < -10 and 112 < i.longitude < 154 for i in located)
    assert all(i.longitude is None for i in bundled.duids.values() if i.latitude is None)


def test_known_duids(bundled):
    assert bundled.info("GSTONE1").region == "QLD1"
    assert bundled.info("CATHROCK").region == "SA1"
    assert bundled.info("BARRON-1").region == "QLD1"  # a hyphen, as AEMO registers it


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_malformed_rows_are_skipped_not_raised():
    catalogue = _cat.parse_catalogue({
        "duids": {
            "OK1": {"region": "QLD1", "station": "Ok", "lat": -27.5, "lon": 153.0, "constrained_intervals": 4},
            "NOREGION": {"station": "x"},
            "BADROW": "nope",
            "NOCOORD": {"region": "QLD1", "lat": "?", "lon": True},
        },
        "top": {"QLD1": ["OK1", "MISSING"], "NSW1": "nope"},
    })
    assert set(catalogue.duids) == {"OK1", "NOCOORD"}
    assert catalogue.info("NOCOORD").latitude is None and catalogue.info("NOCOORD").longitude is None
    assert catalogue.info("NOCOORD").station == "NOCOORD"
    assert catalogue.top_for("QLD1") == ("OK1",) and catalogue.top_for("NSW1") == ()
    assert _cat.parse_catalogue(None) is _cat.EMPTY


def test_a_missing_or_corrupt_file_is_an_empty_catalogue(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{not json")
    assert _cat.load_catalogue.__wrapped__(bad) is _cat.EMPTY
    assert _cat.load_catalogue.__wrapped__(tmp_path / "missing.json") is _cat.EMPTY


def test_region_order_is_most_constrained_first_and_labels_name_the_station():
    catalogue = _cat.parse_catalogue({"duids": {
        "B1": {"region": "QLD1", "station": "Bravo", "fuel": "solar_utility", "constrained_intervals": 1},
        "A1": {"region": "QLD1", "station": "Alpha", "constrained_intervals": 1},
        "C1": {"region": "QLD1", "station": "Charlie", "constrained_intervals": 9},
        "N1": {"region": "NSW1", "station": "Nsw"},
    }})
    assert [i.duid for i in catalogue.region_duids("QLD1")] == ["C1", "A1", "B1"]
    assert catalogue.info("B1").label == "B1 · Bravo (solar utility)"
    assert catalogue.info("A1").label == "A1 · Alpha"


def test_non_physical_units_are_left_out_unless_asked():
    """#311: a dummy generator, a reserve trader and a VPP, none located, against
    a located plant and an unlocated new solar farm, both real."""
    catalogue = _cat.parse_catalogue({"duids": {
        "DG_QLD1": {"region": "QLD1", "station": "QLD1 Dummy Generator", "tni": "QDNSG"},
        "RT_QLD1": {"region": "QLD1", "station": "Reserve Trader QLD1", "type": "LOAD", "tni": "QRT1"},
        "VSQHT1V1": {"region": "QLD1", "station": "VPP Ht QLD 1", "type": "LOAD", "tni": "QVPHYDR1"},
        "DRVIOT04": {"region": "QLD1", "station": "Dr Viotas Q SL1", "type": "LOAD"},
        "ASQENC1": {"region": "QLD1", "station": "Enoc Masp", "fuel": "aggregator_vpp"},
        "GSTONE1": {"region": "QLD1", "station": "Gladstone", "lat": -23.85, "lon": 151.22, "tni": "QGLD"},
        "ALDGASF1": {"region": "QLD1", "station": "Aldoga Solar Farm", "tni": "QLCS1A"},
    }, "nodes": {
        "QDNSG": {"region": "QLD1", "duids": ["DG_QLD1"], "stations": ["QLD1 Dummy Generator"]},
        "QGLD": {"region": "QLD1", "duids": ["GSTONE1"], "stations": ["Gladstone"]},
        "QLCS1A": {"region": "QLD1", "duids": ["ALDGASF1"], "stations": ["Aldoga Solar Farm"]},
    }})
    flagged = {d for d, i in catalogue.duids.items() if i.non_physical}
    assert flagged == {"DG_QLD1", "RT_QLD1", "VSQHT1V1", "DRVIOT04", "ASQENC1"}
    assert {i.duid for i in catalogue.region_duids("QLD1")} == {"GSTONE1", "ALDGASF1"}
    assert len(catalogue.region_duids("QLD1", include_non_physical=True)) == 7
    assert [n.tni for n in catalogue.region_nodes("QLD1")] == ["QLCS1A", "QGLD"]
    assert len(catalogue.region_nodes("QLD1", include_non_physical=True)) == 3


def test_the_bundled_catalogue_flags_dummies_and_aggregations_only():
    catalogue = _cat.load_catalogue()
    flagged = {d for d, i in catalogue.duids.items() if i.non_physical}
    assert {"DG_QLD1", "RT_QLD1", "VSQHT1V1", "AS_NSW1"} <= flagged
    assert not flagged & {"ALDGASF1", "BRDDBES1", "GSTONE1", "CATHROCK"}  # real, located or not
    assert all(catalogue.info(d).latitude is None for d in flagged)
    assert not flagged & {d for top in catalogue.top.values() for d in top}


# ── Sensors ──────────────────────────────────────────────────────────────────

CATALOGUE = _cat.parse_catalogue({
    "duids": {
        "CATHROCK": {"region": "SA1", "station": "Cathedral Rocks", "fuel": "wind", "lat": -34.84874, "lon": 135.59265},
        "KEPSF1": {"region": "QLD1", "station": "Kennedy Energy Park", "lat": -20.87279, "lon": 144.41303},
        "KEPWF1": {"region": "QLD1", "station": "Kennedy Energy Park"},
        "GSTONE1": {"region": "QLD1", "station": "Gladstone"},
    },
    "top": {"QLD1": ["KEPSF1", "KEPWF1", "GSTONE1"]},
})


def _constraints():
    prices = _lp.parse_dispatch_local_prices(DISPATCH, "f.zip", datetime(2026, 10, 30, 21, 25, 40, tzinfo=NEM_TZ))
    return SimpleNamespace(tracker=SimpleNamespace(latest=SimpleNamespace(local_prices=prices)))


def _duid(duid, region="QLD1", info=None, p5=None):
    return _sensor.NemPd7dayDuidLocalPriceSensor(
        _constraints(), p5 if p5 is not None else SimpleNamespace(latest=None), ENTRY, region, duid, info,
    )


def test_a_chosen_non_physical_unit_keeps_working_flagged_with_a_notice(caplog):
    """#311: DG_QLD1, already configured, still gets its sensor, marked non_physical."""
    catalogue = _cat.parse_catalogue({"duids": {
        "DG_QLD1": {"region": "QLD1", "station": "QLD1 Dummy Generator", "tni": "QDNSG", "mlf": 1.0},
        "GSTONE1": {"region": "QLD1", "station": "Gladstone", "lat": -23.85, "lon": 151.22},
    }})
    with caplog.at_level("INFO"):
        entities = _sensor.local_price_entities(
            _constraints(), SimpleNamespace(latest=None), ENTRY, "QLD1", ["DG_QLD1", "GSTONE1"], catalogue,
        )
    attrs = {e._duid: e.extra_state_attributes for e in entities}
    assert attrs["DG_QLD1"]["non_physical"] is True and "non_physical" not in attrs["GSTONE1"]
    assert sum("DG_QLD1" in r.getMessage() and "#311" in r.getMessage() for r in caplog.records) == 1


def test_presets_are_disabled_and_a_chosen_preset_is_not_repeated():
    entities = _sensor.local_price_entities(
        _constraints(), SimpleNamespace(latest=None), ENTRY, "QLD1", ["GSTONE1", "CATHROCK"], CATALOGUE
    )
    assert [(e._attr_unique_id, e._attr_entity_registry_enabled_default) for e in entities] == [
        ("e1_QLD1_GSTONE1_local_price_adjustment", True),
        ("e1_QLD1_CATHROCK_local_price_adjustment", True),
        ("e1_QLD1_KEPSF1_local_price_adjustment", False),
        ("e1_QLD1_KEPWF1_local_price_adjustment", False),
    ]


def test_a_catalogued_duid_publishes_its_station_and_coordinates(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ))
    attrs = _duid("CATHROCK", info=CATALOGUE.info("CATHROCK")).extra_state_attributes
    assert (attrs["latitude"], attrs["longitude"]) == (-34.84874, 135.59265)
    assert (attrs["station"], attrs["fuel"], attrs["duid_region"]) == ("Cathedral Rocks", "wind", "SA1")
    assert "latitude" not in _duid("UNKNOWN1").extra_state_attributes


def test_the_local_price_uses_the_duid_regions_rop():
    """Live on 31 Oct a QLD1 entry priced CATHROCK (SA) on QLD1's ROP. With the
    catalogue it takes SA1's: 28.67941 - 33.92 = -5.24 $/MWh."""
    with patch.object(_sensor, "now_nem", lambda: datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)):
        sa = _duid("CATHROCK", info=CATALOGUE.info("CATHROCK")).extra_state_attributes
        unknown = _duid("CATHROCK").extra_state_attributes
    assert sa["local_price"] == pytest.approx(-0.005241)  # rounded to 6 places
    assert sa["rop_region"] == "SA1"
    assert unknown["rop_region"] == "QLD1"  # not in the catalogue: the entry's
    assert unknown["local_price"] == pytest.approx((85.25 - 33.92) / 1000)


def test_local_price_and_node_price_side_by_side():
    """#313: AEMO's local price is at the regional reference node, ROP +
    adjustment; the node price is the same at the DUID's connection point,
    x its MLF (#312). CATHROCK at SCRK, MLF 0.9417."""
    info = replace(CATALOGUE.info("CATHROCK"), tni="SCRK", mlf=0.9417)
    with patch.object(_sensor, "now_nem", lambda: datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)):
        attrs = _duid("CATHROCK", info=info).extra_state_attributes
        unknown = _duid("CATHROCK").extra_state_attributes
    mlf = 0.9417
    assert attrs["local_price_formula"] == "ROP + LOCAL_PRICE_ADJUSTMENT"
    assert attrs["node_price_formula"] == "(ROP + LOCAL_PRICE_ADJUSTMENT) x MLF"
    assert attrs["node_price_mwh"] == pytest.approx(attrs["local_price_mwh"] * mlf, abs=1e-4)
    assert attrs["node_price"] == pytest.approx(attrs["node_price_mwh"] / 1000, abs=1e-6)
    assert unknown["node_price_mwh"] is None and unknown["node_price"] is None  # no MLF without the catalogue


# ── Enabling a chosen preset ─────────────────────────────────────────────────

class _Registry:
    def __init__(self, rows):
        self.rows = rows

    def async_get_entity_id(self, domain, platform, unique_id):
        return next((r.entity_id for r in self.rows.values() if r.unique_id == unique_id), None)

    def async_get(self, entity_id):
        return self.rows.get(entity_id)

    def async_update_entity(self, entity_id, **changes):
        for key, value in changes.items():
            setattr(self.rows[entity_id], key, value)


def test_choosing_a_preset_enables_what_the_integration_disabled():
    integration = _sensor.er.RegistryEntryDisabler.INTEGRATION
    user = _sensor.er.RegistryEntryDisabler.USER
    rows = {
        "sensor.a": SimpleNamespace(entity_id="sensor.a", unique_id="e1_QLD1_KEPSF1_local_price_adjustment", disabled_by=integration),
        "sensor.b": SimpleNamespace(entity_id="sensor.b", unique_id="e1_QLD1_GSTONE1_local_price_adjustment", disabled_by=user),
    }
    with patch.object(_sensor.er, "async_get", return_value=_Registry(rows)):
        assert _sensor.enable_chosen(None, ENTRY, "QLD1", ["KEPSF1", "GSTONE1", "NOTHERE1"]) == 1
    assert rows["sensor.a"].disabled_by is None
    assert rows["sensor.b"].disabled_by == user  # the user's choice stands


def test_the_retired_sensors_are_removed_and_the_kept_one_is_not():
    """3.32 published three sensors per DUID; the forecast and local price ones go."""
    rows = {
        eid: SimpleNamespace(entity_id=eid, unique_id=uid, config_entry_id=uid.split("_")[0])
        for eid, uid in [
            ("sensor.keep", "e1_QLD1_GSTONE1_local_price_adjustment"),
            ("sensor.fc", "e1_QLD1_GSTONE1_local_price_adjustment_forecast"),
            ("sensor.lp", "e1_QLD1_GSTONE1_local_price"),
            ("sensor.tariff", "e1_QLD1_energex_96200_tariff"),
            ("sensor.other_entry", "e2_QLD1_GSTONE1_local_price"),
        ]
    }
    removed = []
    registry = SimpleNamespace(entities=rows, async_remove=removed.append)
    with patch.object(_sensor.er, "async_get", return_value=registry):
        assert _sensor.remove_retired(None, ENTRY) == 2
    assert removed == ["sensor.fc", "sensor.lp"]


# ── P5MIN only while a forecast sensor listens ───────────────────────────────

def test_p5min_is_not_fetched_until_a_duid_sensor_listens():
    coord = _coord.P5MinCoordinator(SimpleNamespace(), None, SimpleNamespace())
    assert coord.always_update is False  # a poll that found no new run wakes no sensor (#334)
    requests = []
    coord._guard = SimpleNamespace(request=lambda: requests.append(1))
    coord._listeners = {}
    coord._on_dispatch()
    assert requests == []
    coord._listeners = {object(): None}
    coord._on_dispatch()
    coord.request_refresh()
    assert requests == [1, 1]


def test_an_added_duid_sensor_listens_to_p5min_and_asks_for_the_first_run():
    asked, listeners = [], []
    p5 = SimpleNamespace(
        latest=None, request_refresh=lambda: asked.append(1),
        async_add_listener=lambda cb: listeners.append(cb) or (lambda: None),
    )
    sensor = _duid("KEPSF1", p5=p5)
    sensor.async_on_remove = lambda unsub: None

    async def _noop(self):
        return None

    with patch.object(_sensor.CoordinatorEntity, "async_added_to_hass", _noop, create=True):
        run_async(sensor.async_added_to_hass())
    assert asked == [1] and len(listeners) == 1


def test_the_json_and_the_builder_agree_on_shape():
    data = json.loads(_cat.PATH.read_text())
    assert {"generated", "constrained_window", "top", "duids"} <= set(data)
    row = next(iter(data["duids"].values()))
    assert {"region", "station", "type", "fuel", "lat", "lon", "constrained_intervals"} <= set(row)


def test_a_duid_that_is_its_only_name_is_labelled_alone():
    catalogue = _cat.parse_catalogue({"duids": {
        "ASGENE01": {"region": "QLD1", "station": "ASGENE01"},
        "ORABESS1": {"region": "NSW1", "station": "Orana BESS", "fuel": "battery_charging"},
    }})
    assert catalogue.info("ASGENE01").label == "ASGENE01"
    assert catalogue.info("ORABESS1").label == "ORABESS1 · Orana BESS (battery charging)"


def test_the_builder_keeps_acronyms_and_drops_internal_labels():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_duid_catalogue", Path(__file__).parents[1] / "scripts" / "build_duid_catalogue.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert builder._station_name("TERANG BESS", None, "TRGBESS1") == "Terang BESS"
    assert builder._station_name("MT PIPER", None, "MP1") == "Mt Piper"
    assert builder._station_name("ASGENE01", None, "ASGENE01") == "ASGENE01"
    assert builder._station_name("GLADSTONE POWER STATION", "Gladstone", "GSTONE1") == "Gladstone"
