"""
Static node price sensors (#286): ROP x MLF of a chosen transmission node,
for the latest dispatch interval and the P5MIN intervals still to end; the
catalogue's nodes (TNI, MLF, DUIDs, stations, coordinates); the options field.

Run with:  python -m pytest tests/test_node_price.py -v
"""
from __future__ import annotations

import importlib.util
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_const, _cat, _lp, _cc, _coord, _lps, _node = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "local_price_coordinator",
    "local_price_sensor", "node_price_sensor",
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
DISPATCH = (_DIR / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()
ENTRY = SimpleNamespace(entry_id="e1")
NODE = _cat.NodeInfo("QWIV1", "QLD1", 0.9932, ("W/HOE#1",), ("Wivenhoe",), -27.37, 152.63)


def _at(hour, minute=0, second=0):
    return datetime(2026, 10, 30, hour, minute, second, tzinfo=NEM_TZ)


def _constraints(prices):
    return SimpleNamespace(tracker=SimpleNamespace(latest=SimpleNamespace(local_prices=prices)))


def _sensor(node=NODE, tni="QWIV1", p5=None, prices="fresh"):
    if prices == "fresh":
        prices = _lp.parse_dispatch_local_prices(DISPATCH, "d.zip", _at(21, 25, 40))
    run = SimpleNamespace(latest=p5)
    return _node.NemPd7dayNodePriceSensor(_constraints(prices), run, ENTRY, "QLD1", tni, node)


def _p5():
    return _lp.parse_p5min_local_prices(P5, "p.zip", _at(21, 25, 45))


# ── Sensor ───────────────────────────────────────────────────────────────────

def test_the_node_price_is_rop_times_mlf(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: _at(21, 26))
    sensor = _sensor()
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == pytest.approx(85.25 * 0.9932 / 1000, abs=1e-6)
    assert attrs["rop_mwh"] == 85.25 and attrs["node_price_mwh"] == pytest.approx(84.6703)
    assert (attrs["tni"], attrs["mlf"], attrs["row_status"]) == ("QWIV1", 0.9932, "published")
    assert attrs["stations"] == ["Wivenhoe"] and attrs["duids"] == ["W/HOE#1"]
    assert (attrs["latitude"], attrs["longitude"]) == (-27.37, 152.63)
    assert sensor._attr_unique_id == "e1_QLD1_QWIV1_node_price"
    assert sensor._attr_name == "Wivenhoe (QWIV1) Node Price"
    assert sensor.entity_id == "sensor.nem_pd7day_qld1_qwiv1_node_price"
    assert sensor._attr_entity_category == _lps.EntityCategory.DIAGNOSTIC


def test_the_nodes_own_region_prices_it(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: _at(21, 26))
    bayswater = _cat.NodeInfo("NBAY1", "NSW1", 0.9623, ("BW01",), ("Bayswater",), None, None)
    sensor = _sensor(bayswater, "NBAY1")
    assert sensor.native_value == pytest.approx(97.62 * 0.9623 / 1000, abs=1e-6)
    assert sensor.extra_state_attributes["node_region"] == "NSW1"


def test_unknown_node_stale_no_data_and_no_rop_stay_unknown(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: _at(21, 26))
    unknown = _sensor(None, "QNEW1")
    assert unknown.native_value is None and unknown.extra_state_attributes["row_status"] == "unknown_node"
    assert unknown.extra_state_attributes["forecast_status"] == "unknown_node"
    empty = _sensor(prices=None)
    assert empty.native_value is None and empty.extra_state_attributes["row_status"] == "no_data"
    no_rop = _sensor(prices=_lp.parse_dispatch_local_prices(DISPATCH.replace(",QLD1,", ",XXX1,"), "d.zip", _at(21, 25)))
    assert no_rop.native_value is None and no_rop.extra_state_attributes["row_status"] == "no_rop"
    monkeypatch.setattr(_node, "now_nem", lambda: _at(21, 45))
    stale = _sensor()
    assert stale.native_value is None and stale.extra_state_attributes["row_status"] == "stale"


def test_the_forecast_is_the_p5min_rop_times_mlf_for_intervals_to_end(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: _at(21, 27))
    attrs = _sensor(p5=_p5()).extra_state_attributes
    assert attrs["forecast_status"] == "published" and attrs["intervals_ahead"] == 11
    assert attrs["forecast"][0] == {
        "time": "2026-10-30T21:25:00+10:00", "interval_end": "2026-10-30T21:30:00+10:00",
        "value": round(76.94187 * 0.9932 / 1000, 6),
    }
    assert attrs["forecast_interval_ends"][0] == "2026-10-30T21:30:00+10:00"


def test_a_stale_or_expired_forecast_gives_no_points():
    run = _p5()
    assert _node.node_forecast(run, "QLD1", 0.99, _at(21, 45)) == ("stale", [])
    assert _node.node_forecast(run, "QLD1", 0.99, _at(22, 30))[0] == "stale"
    assert _node.node_forecast(None, "QLD1", 0.99, _at(21, 26)) == ("no_data", [])
    assert _node.node_forecast(run, "XXX1", 0.99, _at(21, 26)) == ("no_rop", [])
    assert _node.node_forecast(run, "QLD1", None, _at(21, 26)) == ("unknown_node", [])


def test_entities_are_built_for_the_chosen_nodes_only():
    catalogue = replace(_cat.EMPTY, nodes={"QWIV1": NODE})
    sensors = _node.node_price_entities(_constraints(None), None, ENTRY, "QLD1", ["QWIV1", "QNEW1"], catalogue)
    assert [s._tni for s in sensors] == ["QWIV1", "QNEW1"]
    assert sensors[0]._node is NODE and sensors[1]._node is None
    assert all(getattr(s, "_attr_entity_registry_enabled_default", True) for s in sensors)


# ── Catalogue ────────────────────────────────────────────────────────────────

def test_the_bundled_catalogue_has_nodes_with_mlfs():
    _cat.load_catalogue.cache_clear()
    catalogue = _cat.load_catalogue()
    assert len(catalogue.nodes) > 500
    assert all(_lp.TNI_RE.match(t) for t in catalogue.nodes)
    mlfs = [n.mlf for n in catalogue.nodes.values() if n.mlf is not None]
    assert len(mlfs) > 500 and all(0.7 < m < 1.2 for m in mlfs)
    gladstone = catalogue.info("GSTONE1")
    assert gladstone.tni and catalogue.node(gladstone.tni).region == "QLD1"
    assert "GSTONE1" in catalogue.node(gladstone.tni).duids
    assert {n.region for n in catalogue.region_nodes("NSW1")} == {"NSW1"}


def test_nodes_parse_tolerantly_and_are_labelled_by_station():
    catalogue = _cat.parse_catalogue({"nodes": {
        "QWIV1": {"region": "QLD1", "mlf": 0.9932, "duids": ["W/HOE#1", "W/HOE#2"], "stations": ["Wivenhoe"],
                  "lat": -27.37, "lon": 152.63},
        "QBAD": "not a row",
        "QNOR1": {"mlf": 1.0},
        "QZZZ1": {"region": "QLD1", "mlf": "x", "duids": "y"},
    }})
    assert catalogue.node("QWIV1").label == "QWIV1 · Wivenhoe (W/HOE#1, W/HOE#2)"
    assert "QBAD" not in catalogue.nodes and "QNOR1" not in catalogue.nodes
    assert catalogue.node("QZZZ1") is None or catalogue.node("QZZZ1").mlf is None
    assert catalogue.node("NOPE") is None


def test_the_builder_groups_duids_by_their_connection_point():
    spec = importlib.util.spec_from_file_location(
        "build_duid_catalogue", Path(__file__).parents[1] / "scripts" / "build_duid_catalogue.py",
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    nodes = builder._nodes({
        "A1": {"region": "QLD1", "station": "Alpha", "tni": "QAAA1", "mlf": 0.98, "lat": -27.0, "lon": 152.0},
        "A2": {"region": "QLD1", "station": "Alpha", "tni": "QAAA1", "mlf": 0.98, "lat": -27.2, "lon": 152.2},
        "B1": {"region": "QLD1", "station": "Beta", "tni": "QBBB1", "mlf": None, "lat": None, "lon": None},
        "C1": {"region": "QLD1", "station": "Gamma", "tni": None, "mlf": 1.0},
    })
    assert set(nodes) == {"QAAA1", "QBBB1"}
    assert nodes["QAAA1"]["duids"] == ["A1", "A2"] and nodes["QAAA1"]["stations"] == ["Alpha"]
    assert nodes["QAAA1"]["mlf"] == 0.98
    assert nodes["QAAA1"]["lat"] == pytest.approx(-27.1) and nodes["QAAA1"]["lon"] == pytest.approx(152.1)
    assert nodes["QBBB1"]["lat"] is None and nodes["QBBB1"]["mlf"] is None
    assert builder._mlf("0.98765") == 0.9877 and builder._mlf("") is None and builder._mlf(None) is None


def test_names_carry_the_station_and_ids_stay_the_codes():
    """The dropdown's names stay on the entity: Gladstone (GSTONE1), Wivenhoe (QWIV1).
    The entity id is the code's alone, so it does not move with the catalogue."""
    gstone = _cat.parse_catalogue({"duids": {
        "GSTONE1": {"region": "QLD1", "station": "Gladstone"},
        "ASGENE01": {"region": "QLD1", "station": "ASGENE01"},
        "W/HOE#1": {"region": "QLD1", "station": "Wivenhoe"},
    }})
    duid = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(None), None, ENTRY, "QLD1", "GSTONE1", gstone.info("GSTONE1"))
    assert duid._attr_name == "Gladstone (GSTONE1) Local Price Adjustment"
    assert duid.entity_id == "sensor.nem_pd7day_qld1_gstone1_local_price_adjustment"
    internal = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(None), None, ENTRY, "QLD1", "ASGENE01", gstone.info("ASGENE01"))
    assert internal._attr_name == "ASGENE01 Local Price Adjustment"  # the station is only the code
    typed = _lps.NemPd7dayDuidLocalPriceSensor(_constraints(None), None, ENTRY, "QLD1", "NEWDUID1")
    assert typed._attr_name == "NEWDUID1 Local Price Adjustment"
    hoe = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(None), None, ENTRY, "QLD1", "W/HOE#1", gstone.info("W/HOE#1"))
    assert hoe._attr_name == "Wivenhoe (W/HOE#1) Local Price Adjustment"
    assert hoe.entity_id == "sensor.nem_pd7day_qld1_w_hoe_1_local_price_adjustment"  # as HA slugified the name
    two = _cat.NodeInfo("QTWO1", "QLD1", 1.0, ("A1", "B1"), ("Alpha", "Beta"), None, None)
    node = _node.NemPd7dayNodePriceSensor(_constraints(None), None, ENTRY, "QLD1", "QTWO1", two)
    assert node._attr_name == "Alpha, Beta (QTWO1) Node Price"
    unknown = _node.NemPd7dayNodePriceSensor(_constraints(None), None, ENTRY, "QLD1", "QNEW1", None)
    assert unknown._attr_name == "QNEW1 Node Price" and unknown.entity_id == "sensor.nem_pd7day_qld1_qnew1_node_price"


def test_parse_tnis():
    assert _lp.parse_tnis("qwiv1, NBAY1\nqwiv1, BAD-1") == (["QWIV1", "NBAY1"], ["BAD-1"])
