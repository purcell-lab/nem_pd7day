"""
City and zone price proxies (#288): which catalogue nodes stand for a place,
the zone MLF and median adjustment, and the zone sensor's state, forecasts
and attributes.

Run with:  python -m pytest tests/test_zones.py -v
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_const, _cat, _lp, _cc, _pdc, _pdl, _coord, _lps, _node, _zones, _zs = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "predispatch_client",
    "predispatch_local_price", "local_price_coordinator", "local_price_sensor", "node_price_sensor",
    "zones", "zone_sensor",
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
DISPATCH = (_DIR / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()
PD = (_DIR / "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.CSV").read_text()
ENTRY = SimpleNamespace(entry_id="e1")
NOW = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)


def _node_info(tni, duids, lat, lon, mlf=1.0, region="NSW1", stations=("S",)):
    return _cat.NodeInfo(tni, region, mlf, tuple(duids), stations, lat, lon)


def _catalogue(*nodes):
    return replace(_cat.EMPTY, nodes={n.tni: n for n in nodes})


# Around Sydney (-33.87, 151.21): two close, one 60 km out, one far.
NEAR = _node_info("NNEAR1", ["GUTHEGA"], -33.90, 151.20, mlf=1.0)
CLOSE = _node_info("NCLOSE1", ["NOROWS1"], -33.80, 151.10, mlf=0.98)
OUT = _node_info("NOUT1", ["HUMENSW"], -34.40, 151.00, mlf=0.95)
FAR = _node_info("NFAR1", ["FARAWAY1"], -30.00, 150.00, mlf=0.90)
CAT = _catalogue(NEAR, CLOSE, OUT, FAR)


def _dispatch():
    return _lp.parse_dispatch_local_prices(DISPATCH, "d.zip", datetime(2026, 10, 30, 21, 25, 40, tzinfo=NEM_TZ))


# ── Zones ────────────────────────────────────────────────────────────────────

def test_distance_is_great_circle():
    assert _zones.distance_km(-27.4698, 153.0251, -33.8688, 151.2093) == pytest.approx(732, abs=5)
    assert _zones.distance_km(-33.0, 151.0, -33.0, 151.0) == 0.0


def test_each_region_has_its_cities_and_home():
    for region in ("QLD1", "NSW1", "VIC1", "SA1", "TAS1"):
        choices = _zones.zone_choices(region)
        assert choices[-1] == ("home", "Home") and len(choices) >= 4
    assert ("canberra", "Canberra") in _zones.zone_choices("NSW1")
    assert _zones.parse_zones("brisbane, Townsville, sydney, brisbane, home", "QLD1") == ["brisbane", "townsville", "home"]
    assert _zones.parse_zones(["cairns"], "QLD1") == ["cairns"] and _zones.parse_zones("", "QLD1") == []


def test_members_are_the_nodes_within_the_radius_nearest_first():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 50)
    assert [m.node.tni for m in zone.members] == ["NNEAR1", "NCLOSE1"] and not zone.nearest_only
    assert zone.members[0].distance_km < zone.members[1].distance_km < 50
    wide = _zones.build_zone(CAT, "NSW1", "sydney", 100)
    assert [m.node.tni for m in wide.members] == ["NNEAR1", "NCLOSE1", "NOUT1"]


def test_with_no_node_in_reach_the_nearest_stands_in():
    zone = _zones.build_zone(CAT, "NSW1", "dubbo", 10)
    assert zone.nearest_only and len(zone.members) == 1


def test_home_needs_a_location_and_unknown_keys_are_none():
    assert _zones.build_zone(CAT, "NSW1", "home", 100) is None
    home = _zones.build_zone(CAT, "NSW1", "home", 100, home=(-33.90, 151.20))
    assert home.name == "Home" and home.members[0].node.tni == "NNEAR1"
    assert _zones.build_zone(CAT, "NSW1", "brisbane", 100) is None  # another region's city
    assert _zones.build_zone(CAT, "NSW1", "atlantis", 100) is None


def test_zone_mlf_weighs_the_nearer_nodes_more():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)
    weights = [1 / max(m.distance_km, 5.0) for m in zone.members]
    mlfs = [m.node.mlf for m in zone.members]
    assert zone.mlf == round(sum(w * m for w, m in zip(weights, mlfs)) / sum(weights), 4)
    assert 0.95 < zone.mlf < 1.0


def test_the_adjustment_is_the_median_of_the_known():
    assert _zones.zone_adjustment([0.0, 0.0, -385.47]) == 0.0
    assert _zones.zone_adjustment([88.27, -385.47, 0.0, None]) == 0.0
    assert _zones.zone_adjustment([88.27, -385.47]) == pytest.approx(-148.6)
    assert _zones.zone_adjustment([None]) is None and _zones.zone_adjustment([]) is None


def test_the_bundled_catalogue_has_members_near_every_capital():
    _cat.load_catalogue.cache_clear()
    catalogue = _cat.load_catalogue()
    for region, city in (("QLD1", "brisbane"), ("NSW1", "sydney"), ("VIC1", "melbourne"),
                         ("SA1", "adelaide"), ("TAS1", "hobart")):
        zone = _zones.build_zone(catalogue, region, city, 100)
        assert zone.members and not zone.nearest_only, city
        assert 0.85 < zone.mlf < 1.1, (city, zone.mlf)
        assert all(m.node.region == region for m in zone.members)


# ── Dispatch ─────────────────────────────────────────────────────────────────

def test_one_constrained_member_does_not_move_the_median():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)  # GUTHEGA +88.27, none, HUMENSW -385.47
    result = _zs.dispatch_zone(zone, "NSW1", _dispatch(), NOW)
    assert result.status == "published" and result.adjustment_mwh == 0.0
    assert result.rop_mwh == 97.62 and result.static_mwh == round(97.62 * zone.mlf, 4)
    assert result.price_mwh == result.static_mwh
    assert [m["adjustment_mwh"] for m in result.members] == [88.27, 0.0, -385.47]


def test_most_members_constrained_moves_it():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 50)  # GUTHEGA +88.27, NOROWS1 0
    two = replace(zone, members=(zone.members[0], _zones.Member(OUT, 60.0)))
    result = _zs.dispatch_zone(two, "NSW1", _dispatch(), NOW)
    assert result.adjustment_mwh == pytest.approx((88.27 - 385.47) / 2)
    assert result.price_mwh == pytest.approx(97.62 * two.mlf + (88.27 - 385.47) / 2, abs=1e-3)


def test_stale_no_data_no_rop_and_no_mlf_leave_it_unknown():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)
    assert _zs.dispatch_zone(zone, "NSW1", None, NOW).status == "no_data"
    late = datetime(2026, 10, 30, 21, 50, tzinfo=NEM_TZ)
    assert _zs.dispatch_zone(zone, "NSW1", _dispatch(), late).price_mwh is None
    assert _zs.dispatch_zone(zone, "NSW1", replace(_dispatch(), rop={}), NOW).status == "no_rop"
    no_table = replace(_dispatch(), table_present=False)
    assert _zs.dispatch_zone(zone, "NSW1", no_table, NOW).status == "table_missing"
    unknown = replace(zone, members=(_zones.Member(replace(NEAR, mlf=None), 1.0),))
    assert _zs.dispatch_zone(unknown, "NSW1", _dispatch(), NOW).status == "unknown_node"


def test_a_member_whose_duids_disagree_is_left_out():
    split = _node_info("NSPLIT1", ["GUTHEGA", "HUMENSW"], -33.85, 151.2)
    zone = replace(_zones.build_zone(CAT, "NSW1", "sydney", 50), members=(
        _zones.Member(split, 2.0), _zones.Member(CLOSE, 14.0),
    ))
    result = _zs.dispatch_zone(zone, "NSW1", _dispatch(), NOW)
    assert result.adjustment_mwh == 0.0 and result.members[0]["adjustment_status"] == "conflicting_rows"


# ── Forecasts ────────────────────────────────────────────────────────────────

def test_the_p5min_forecast_takes_the_median_per_interval():
    run = _lp.parse_p5min_local_prices(P5, "p.zip", datetime(2026, 10, 30, 21, 25, 45, tzinfo=NEM_TZ))
    cathrock = _node_info("SCRK", ["CATHROCK"], -34.9, 138.6, mlf=0.9417, region="SA1")
    zone = _zones.Zone("adelaide", "Adelaide", -34.93, 138.6, 100, (_zones.Member(cathrock, 3.0),), False)
    now = datetime(2026, 10, 30, 21, 27, tzinfo=NEM_TZ)
    status, points = _zs.zone_p5_forecast(zone, "SA1", run, now)
    first = min(e for e in run.rop["SA1"] if e > now)
    expected = (run.rop["SA1"][first] * zone.mlf + run.rows["CATHROCK"][first].adjustment_mwh) / 1000
    assert status == "published" and points[0]["value"] == round(expected, 6)
    assert _zs.zone_p5_forecast(zone, "SA1", None, now) == ("no_data", [])
    assert _zs.zone_p5_forecast(zone, "SA1", replace(run, table_present=False), now) == ("table_missing", [])


def test_the_predispatch_forecast_takes_the_median_per_period():
    snapshot = _pdc.parse_predispatch(PD, "pd.zip")
    now = datetime(2026, 10, 31, 13, 10, tzinfo=NEM_TZ)
    uranquinty = _node_info("NURQ4U", ["URANQ14"], -35.2, 147.2)
    quiet = _node_info("NQUIET1", ["NOROWS1"], -35.1, 147.3)
    zone = _zones.Zone("wagga_wagga", "Wagga Wagga", -35.1, 147.36, 100,
                       (_zones.Member(uranquinty, 10.0), _zones.Member(quiet, 5.0)), False)
    series = _zs.zone_predispatch(zone, snapshot, now)
    first_end, first_value = series.periods[0]
    assert first_value == pytest.approx(-1017.04 / 2)  # the median of -1017.04 and 0
    assert series.status == "published" and len(series.periods) == 78
    assert series.published == len(snapshot.local_prices["URANQ14"])
    assert _zs.zone_predispatch(zone, None, now).status == "no_data"


# ── Sensor ───────────────────────────────────────────────────────────────────

def _constraints(prices):
    return SimpleNamespace(tracker=SimpleNamespace(latest=SimpleNamespace(local_prices=prices)))


def test_the_sensor_is_a_labelled_proxy(monkeypatch):
    monkeypatch.setattr(_zs, "now_nem", lambda: NOW)
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)
    sensor = _zs.NemPd7dayZonePriceSensor(_constraints(_dispatch()), None, ENTRY, "NSW1", zone)
    assert sensor._attr_name == "Sydney Zone Price Proxy"
    assert sensor.entity_id == "sensor.nem_pd7day_nsw1_sydney_zone_price_proxy"
    assert sensor._attr_unique_id == "e1_NSW1_sydney_zone_price_proxy"
    assert sensor.native_value == pytest.approx(97.62 * zone.mlf / 1000, abs=1e-6)
    attrs = sensor.extra_state_attributes
    assert attrs["proxy"] is True and attrs["zone_mlf_source"] == "nearby_generation_idw"
    assert attrs["adjustment_aggregate"] == "median" and attrs["member_count"] == 3
    assert attrs["members_constrained"] == 2
    assert (attrs["adjustment_min_mwh"], attrs["adjustment_max_mwh"]) == (-385.47, 88.27)
    assert [m["tni"] for m in attrs["members"]] == ["NNEAR1", "NCLOSE1", "NOUT1"]
    assert attrs["forecast_status"] == "no_data" and attrs["predispatch_status"] == "no_data"
    assert "members" in sensor._unrecorded_attributes and "predispatch_forecast" in sensor._unrecorded_attributes
