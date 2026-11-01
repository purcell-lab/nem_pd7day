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

_const, _cat, _lp, _cc, _pdc, _pdl, _coord, _lps, _node, _ldp, _zones, _zs = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "predispatch_client",
    "predispatch_local_price", "local_price_coordinator", "local_price_sensor", "node_price_sensor",
    "load_points", "zones", "zone_sensor",
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
DISPATCH = (_DIR / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()
PD = (_DIR / "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.CSV").read_text()
ENTRY = SimpleNamespace(entry_id="e1")
NOW = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)


def _node_info(tni, duids, lat, lon, mlf=1.0, region="NSW1", stations=None):
    """A node, its own station unless one is given (each a vote, #314)."""
    return _cat.NodeInfo(tni, region, mlf, tuple(duids), stations or (f"Station {tni}",), lat, lon)


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
    assert ("canberra", "Canberra") in _zones.zone_choices("NSW1") and ("orange", "Orange") in _zones.zone_choices("NSW1")
    assert _zones.parse_zones("brisbane, Townsville, sydney, brisbane, home", "QLD1") == ["brisbane", "townsville", "home"]
    assert _zones.parse_zones(["cairns"], "QLD1") == ["cairns"] and _zones.parse_zones("", "QLD1") == []


def test_members_are_the_nodes_within_the_radius_nearest_first():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 50)
    assert [m.node.tni for m in zone.members] == ["NNEAR1", "NCLOSE1"] and not zone.nearest_only
    assert zone.members[0].distance_km < zone.members[1].distance_km < 50
    wide = _zones.build_zone(CAT, "NSW1", "sydney", 100)
    assert [m.node.tni for m in wide.members] == ["NNEAR1", "NCLOSE1", "NOUT1"]


def test_with_no_node_in_reach_the_nearest_stands_in():
    zone = _zones.build_zone(CAT, "NSW1", "orange", 10)
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


def _zone_of(*stations):
    """A zone with one member per (station, distance), nearest first."""
    members = tuple(_zones.Member(_node_info(f"N{i}", [f"D{i}"], -33.9, 151.2, stations=(name,)), km)
                    for i, (name, km) in enumerate(stations))
    return _zones.Zone("z", "Z", -33.9, 151.2, 100, members, False)


def test_the_adjustment_is_the_median_of_the_stations_known():
    three = _zone_of(("A", 1), ("B", 2), ("C", 3))
    assert three.aggregate == "station_median" and three.stations == ("A", "B", "C")
    assert _zones.zone_adjustment(three, [0.0, 0.0, -385.47]) == 0.0
    assert _zones.zone_adjustment(three, [88.27, -385.47, None]) == pytest.approx(-148.6)  # two known
    assert _zones.zone_adjustment(three, [None, None, None]) is None
    assert _zones.zone_adjustment(_zone_of(), []) is None


def test_a_station_with_many_tnis_takes_one_vote():
    """#314: the Sunshine Coast zone of 31 Oct 2026, Valdora and Woolooga with
    one TNI each and Wivenhoe with four. Per TNI, Wivenhoe's 4 of 6 votes set
    the median; per station it is one of three."""
    zone = _zone_of(("Valdora", 10.7), ("Woolooga", 88.5),
                    ("Wivenhoe", 91.1), ("Wivenhoe", 91.1), ("Wivenhoe", 91.1), ("Wivenhoe", 91.1))
    assert zone.stations == ("Valdora", "Woolooga", "Wivenhoe") and zone.aggregate == "station_median"
    values = [0.0, 5.0, -40.0, -40.0, -40.0, -40.0]
    assert _zones.zone_adjustment(zone, values) == 0.0  # per TNI it would be -40.0
    assert _zones.zone_adjustment(zone, [0.0, 5.0, -40.0, -38.0, None, -42.0]) == 0.0  # Wivenhoe's own median, -40


def test_below_three_stations_the_nearest_stands_alone():
    """#315: the Home zone of 31 Oct 2026, Valdora at 16.7 km and Woolooga at
    73.4 km. A median of two is their mean, half of Woolooga's adjustment."""
    home = _zone_of(("Valdora", 16.7), ("Woolooga", 73.4))
    assert home.aggregate == "nearest_station"
    assert _zones.zone_adjustment(home, [0.0, 7.6]) == 0.0
    assert _zones.zone_adjustment(home, [None, 7.6]) == 7.6  # the nearest with a known vote
    assert _zones.zone_adjustment(_zone_of(("A", 1)), [-12.5]) == -12.5


def test_the_bundled_catalogue_has_members_near_every_capital():
    _cat.load_catalogue.cache_clear()
    catalogue = _cat.load_catalogue()
    for region, city in (("QLD1", "brisbane"), ("NSW1", "sydney"), ("VIC1", "melbourne"),
                         ("SA1", "adelaide"), ("TAS1", "hobart")):
        zone = _zones.build_zone(catalogue, region, city, 100)
        assert zone.members and not zone.nearest_only, city
        assert 0.85 < zone.mlf < 1.1, (city, zone.mlf)
        assert all(m.node.region == region for m in zone.members)


def test_a_preset_leaves_out_plant_outside_its_electrical_area():
    """#317, interim: Sydney without the Central Coast's Eraring, Vales Point and
    Colongra; Wagga Wagga without Tumut; the Sunshine Coast without Wivenhoe."""
    catalogue = _cat.load_catalogue()
    sydney = _zones.build_zone(catalogue, "NSW1", "sydney", 100)
    assert {m.node.tni[:3] for m in sydney.excluded} == {"NCL", "NVP", "NEP"}
    assert not any(m.node.tni.startswith(("NEP", "NVPP", "NCLG")) for m in sydney.members)
    assert "Tallawarra" in {s for m in sydney.members for s in m.node.stations}  # Illawarra stays
    wagga = _zones.build_zone(catalogue, "NSW1", "wagga_wagga", 100)
    assert [m.node.tni for m in wagga.excluded] == ["NBLW8", "NTU21J"]
    coast = _zones.build_zone(catalogue, "QLD1", "sunshine_coast", 100)
    assert [m.node.tni for m in coast.members] == ["QPWD1S", "QWLS1W"] and len(coast.excluded) == 4
    narrow = _zones.build_zone(catalogue, "NSW1", "sydney", 50)
    assert narrow.excluded == ()  # nothing to leave out within 50 km
    home = _zones.build_zone(catalogue, "QLD1", "home", 100, home=(coast.latitude, coast.longitude))
    assert home.excluded == () and any(m.node.tni.startswith("QWIV") for m in home.members)  # presets only


def test_the_sensor_lists_who_was_left_out_and_why(monkeypatch):
    monkeypatch.setattr(_zs, "now_nem", lambda: NOW)
    coast = _zones.build_zone(_cat.load_catalogue(), "QLD1", "sunshine_coast", 100)
    attrs = _zs.NemPd7dayZonePriceSensor(
        SimpleNamespace(tracker=SimpleNamespace(latest=None)), None, ENTRY, "QLD1", coast,
    ).extra_state_attributes
    assert [m["tni"] for m in attrs["excluded_members"]] == ["QWIP1", "QWIP2", "QWIV1", "QWIV2"]
    assert attrs["exclusion_reason"].startswith("Wivenhoe") and attrs["member_count"] == 2
    brisbane = _zones.build_zone(_cat.load_catalogue(), "QLD1", "brisbane", 100)
    plain = _zs.NemPd7dayZonePriceSensor(
        SimpleNamespace(tracker=SimpleNamespace(latest=None)), None, ENTRY, "QLD1", brisbane,
    ).extra_state_attributes
    assert plain["excluded_members"] == [] and plain["exclusion_reason"] is None


# ── Load connection points ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def loads():
    _ldp.load_load_points.cache_clear()
    return _ldp.load_load_points()


def test_the_bundled_load_points_come_from_the_2026_27_report(loads):
    assert loads.financial_year == "2026-27" and len(loads.points) > 500
    assert {p.region for p in loads.points.values()} == {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}
    assert all(0.8 < p.mlf < 1.1 for p in loads.points.values())
    belmont = loads.get("QBMH")
    assert (belmont.name, belmont.kv, belmont.mlf) == ("Belmont", "110", 1.0128)
    assert belmont.label == "Belmont 110 kV (QBMH) · MLF 1.0128"
    assert loads.get("QPIV").mlf == 0.9974  # the dual-MLF point's load row, not its generation row
    assert loads.get("QINF").mlf == 0.9637  # Innisfail's row effective from 1 July 2026
    names = [p.name for p in loads.region_points("TAS1")]
    assert names == sorted(names)


def test_every_preset_citys_load_points_are_bundled_in_its_region(loads):
    for region, cities in _zones.CITIES.items():
        for key, _name, _lat, _lon, tnis in cities:
            assert tnis, key
            for tni in tnis:
                assert loads.get(tni) is not None and loads.get(tni).region == region, (key, tni)


def test_a_city_takes_its_load_points_mlf(loads):
    _cat.load_catalogue.cache_clear()
    catalogue = _cat.load_catalogue()
    brisbane = _zones.build_zone(catalogue, "QLD1", "brisbane", 100, loads=loads)
    expected = [loads.get(t).mlf for t in _zones.CITIES["QLD1"][0][4]]
    assert brisbane.mlf == round(sum(expected) / len(expected), 4) == 1.012
    assert brisbane.mlf_source == "load_tni" and len(brisbane.load_points) == 15
    townsville = _zones.build_zone(catalogue, "QLD1", "townsville", 100, loads=loads)
    assert townsville.mlf == 0.9998  # nearby generation alone said 0.951
    without = _zones.build_zone(catalogue, "QLD1", "townsville", 100)
    assert without.mlf_source == "nearby_generation_idw" and without.mlf != townsville.mlf


def test_home_takes_its_chosen_load_point(loads):
    home = _zones.build_zone(CAT, "NSW1", "home", 100, home=(-33.9, 151.2), loads=loads, home_load="NSYS")
    assert home.mlf == loads.get("NSYS").mlf and home.mlf_source == "load_tni"
    other = _zones.build_zone(CAT, "NSW1", "home", 100, home=(-33.9, 151.2), loads=loads, home_load="QBMH")
    assert other.load_points == () and other.mlf_source == "nearby_generation_idw"  # another region's point
    unset = _zones.build_zone(CAT, "NSW1", "home", 100, home=(-33.9, 151.2), loads=loads)
    assert unset.mlf_source == "nearby_generation_idw"


def test_home_is_set_against_the_nearest_preset_city(loads):
    """#316: Home in the Sunshine Coast hinterland on Woolooga (0.978) beside
    the Sunshine Coast preset, 25.6 km away, on Palmwoods (1.0132)."""
    catalogue = _cat.load_catalogue()
    home = _zones.build_zone(catalogue, "QLD1", "home", 100, home=(-26.42, 153.08), loads=loads, home_load="QWLG")
    city = home.nearest_city
    assert (city.key, city.distance_km, city.mlf, city.tnis) == ("sunshine_coast", 25.6, 1.0132, ("QPWD",))
    assert home.mlf == 0.978
    assert _zones.build_zone(catalogue, "QLD1", "brisbane", 100, loads=loads).nearest_city is None  # Home only
    assert _zones.nearest_city("XXX1", -26.4, 153.1) is None
    assert _zones.nearest_city("QLD1", -26.42, 153.08).mlf is None  # no load points to hand
    attrs = _zs.NemPd7dayZonePriceSensor(
        SimpleNamespace(tracker=SimpleNamespace(latest=None)), None, ENTRY, "QLD1", home,
    ).extra_state_attributes
    assert (attrs["nearest_city"], attrs["nearest_city_mlf"], attrs["nearest_city_load_points"]) == (
        "Sunshine Coast", 1.0132, ["QPWD"])
    assert attrs["zone_mlf_vs_nearest_city"] == round(0.978 / 1.0132 - 1, 4) == -0.0347


def test_load_points_parse_tolerantly():
    points = _ldp.parse_load_points({"financial_year": "2026-27", "points": {
        "QOK1": {"region": "QLD1", "name": "Ok", "kv": "33", "mlf": 1.0},
        "QBAD": {"region": "QLD1", "name": "Bad", "mlf": "x"},
        "QNO": "row",
    }})
    assert list(points.points) == ["QOK1"]
    assert _ldp.parse_load_points("nonsense") == _ldp.EMPTY


def test_the_builder_keeps_load_rows_and_the_rows_from_1_july():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_load_points", Path(__file__).parents[1] / "scripts" / "build_load_points.py",
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    text = "\n".join([
        "Table 1 Queensland loads",
        "Location Voltage (kV*) TNI code 2026-27 MLF 2025-26 MLF",
        "Belmont 110 QBMH 1.0128 1.0102",
        "Pioneer Valley - Dual MLF (Generation) 66 QPIV 0.8765 0.9",
        "Pioneer Valley - Dual MLF (Load) 66 QPIV 0.9974 0.99",
        "Innisfail (effective prior to 01/07/2026) 22 QINF 0.9921 0.99",
        "Innisfail (effective from 01/07/2026) 22 QINF 0.9637",
        "Biloela 66/11 QBIL 0.9207 0.9313",
        "Table 2 Queensland generation",
        "Gladstone PS 275 GSTONE1 QGLD1 QGLD 0.9299 0.93",
        "Table 7 Australian Capital Territory loads",
        "Civic 132 ACVC 0.9871 0.98",
    ])
    points = builder.parse(text)
    assert set(points) == {"QBMH", "QPIV", "QINF", "QBIL", "ACVC"}
    assert points["QPIV"] == {"region": "QLD1", "name": "Pioneer Valley", "kv": "66", "mlf": 0.9974}
    assert points["QINF"]["mlf"] == 0.9637 and points["QINF"]["name"] == "Innisfail"
    assert points["QBIL"]["kv"] == "66/11" and points["ACVC"]["region"] == "NSW1"


# ── Dispatch ─────────────────────────────────────────────────────────────────

def test_one_constrained_member_does_not_move_the_median():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)  # GUTHEGA +88.27, none, HUMENSW -385.47
    result = _zs.dispatch_zone(zone, "NSW1", _dispatch(), NOW)
    assert result.status == "published" and result.adjustment_mwh == 0.0
    assert result.rop_mwh == 97.62 and result.static_mwh == round(97.62 * zone.mlf, 4)
    assert result.price_mwh == result.static_mwh
    assert [m["adjustment_mwh"] for m in result.members] == [88.27, 0.0, -385.47]


def test_most_stations_constrained_moves_it():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)  # GUTHEGA +88.27, NOROWS1 0, HUMENSW -385.47
    humensw_too = _node_info("NOUT2", ["HUMENSW"], -34.40, 151.00, mlf=0.95)
    constrained = replace(zone, members=(zone.members[0], _zones.Member(OUT, 60.0), _zones.Member(humensw_too, 61.0)))
    result = _zs.dispatch_zone(constrained, "NSW1", _dispatch(), NOW)
    assert result.adjustment_mwh == -385.47  # two of three stations at -385.47
    assert result.price_mwh == pytest.approx((97.62 - 385.47) * constrained.mlf, abs=1e-3)


def test_a_two_station_zone_takes_the_nearest_and_says_so(monkeypatch):
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)
    two = replace(zone, members=(zone.members[0], _zones.Member(OUT, 60.0)))  # GUTHEGA +88.27, HUMENSW -385.47
    assert _zs.dispatch_zone(two, "NSW1", _dispatch(), NOW).adjustment_mwh == 88.27  # not the mean, -148.6
    monkeypatch.setattr(_zs, "now_nem", lambda: NOW)
    attrs = _zs.NemPd7dayZonePriceSensor(_constraints(_dispatch()), None, ENTRY, "NSW1", two).extra_state_attributes
    assert attrs["adjustment_aggregate"] == "nearest_station" and attrs["member_stations"] == ["Station NNEAR1", "Station NOUT1"]
    assert attrs["aggregation_note"].startswith("2 stations in reach, fewer than 3")


def test_stale_no_data_no_rop_and_no_mlf_leave_it_unknown():
    zone = _zones.build_zone(CAT, "NSW1", "sydney", 100)
    assert _zs.dispatch_zone(zone, "NSW1", None, NOW).status == "no_data"
    late = datetime(2026, 10, 30, 21, 50, tzinfo=NEM_TZ)
    assert _zs.dispatch_zone(zone, "NSW1", _dispatch(), late).price_mwh is None
    assert _zs.dispatch_zone(zone, "NSW1", replace(_dispatch(), rop={}), NOW).status == "no_rop"
    no_table = replace(_dispatch(), table_present=False, rows={})
    zero = _zs.dispatch_zone(zone, "NSW1", no_table, NOW)  # AEMO's zero (#309)
    assert zero.status == "dispatch_zero" and zero.adjustment_mwh == 0.0
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
    expected = (run.rop["SA1"][first] + run.rows["CATHROCK"][first].adjustment_mwh) * zone.mlf / 1000
    assert status == "published" and points[0]["value"] == round(expected, 6)
    assert _zs.zone_p5_forecast(zone, "SA1", None, now) == ("no_data", [])
    status, points = _zs.zone_p5_forecast(zone, "SA1", replace(run, table_present=False, rows={}), now)
    assert status == "published" and points[0]["value"] == round(run.rop["SA1"][first] * zone.mlf / 1000, 6)


def test_the_predispatch_forecast_votes_per_station_per_period():
    snapshot = _pdc.parse_predispatch(PD, "pd.zip")
    now = datetime(2026, 10, 31, 13, 10, tzinfo=NEM_TZ)
    uranquinty = _node_info("NURQ4U", ["URANQ14"], -35.2, 147.2)
    quiet = _node_info("NQUIET1", ["NOROWS1"], -35.1, 147.3)
    zone = _zones.Zone("wagga_wagga", "Wagga Wagga", -35.1, 147.36, 100,
                       (_zones.Member(quiet, 5.0), _zones.Member(uranquinty, 10.0)), False)
    series = _zs.zone_predispatch(zone, snapshot, now)
    assert series.periods[0][1] == 0.0  # two stations: the nearest's, not the mean of -1017.04 and 0
    third = replace(zone, members=(*zone.members, _zones.Member(_node_info("NURQ2", ["URANQ14"], -35.2, 147.2), 12.0)))
    first_end, first_value = _zs.zone_predispatch(third, snapshot, now).periods[0]
    assert first_value == -1017.04  # the median of 0, -1017.04 and -1017.04
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
    assert attrs["load_points"] == [] and "load_points" in sensor._unrecorded_attributes
    assert attrs["adjustment_aggregate"] == "station_median" and attrs["member_count"] == 3
    assert len(attrs["member_stations"]) == 3 and "aggregation_note" not in attrs
    assert attrs["members_constrained"] == 2
    assert (attrs["adjustment_min_mwh"], attrs["adjustment_max_mwh"]) == (-385.47, 88.27)
    assert [m["tni"] for m in attrs["members"]] == ["NNEAR1", "NCLOSE1", "NOUT1"]
    assert attrs["forecast_status"] == "no_data" and attrs["predispatch_status"] == "no_data"
    assert "members" in sensor._unrecorded_attributes and "predispatch_forecast" in sensor._unrecorded_attributes
