"""Zone price proxies by AEMO congestion zone: what stands for a place (#288, #387).

Each zone is one of AEMO's IPRR congestion zones in the entry's region, the
19 zones NEMDE's constraint equations and AEMO's congestion management are
written against (congestion_zones.py). Until v3.52.0 a zone was a preset city
and a radius, which crossed electrical areas (#317).

- **Members**: the region's catalogue nodes whose TNI the tool maps to the
  zone, nearest the zone's city first. They are generation and storage
  connection points: city load is not a variable in NEMDE's constraint
  equations, so it has no local price adjustment of its own, and a zone's
  units are a network proxy, never a validated household node. A node the
  tool does not map, or maps to another region's zone, is listed as unmapped,
  not guessed. A zone with no located member takes the region's nearest node,
  flagged ``nearest_only``.
- **Name**: the preset city of the region with the most load points in the
  zone (``Sydney`` for N_SYDNEY, which also holds Newcastle and Wollongong);
  ``cities`` lists every preset city whose load points are in it. Entity and
  unique IDs carry the zone code.
- **Zone MLF**: the mean MLF of the region's load connection points the tool
  maps to the zone (load_points.py, ``zone_mlf_source: load_tni``); without
  one, the inverse-distance-weighted mean of the members' MLFs
  (``nearby_generation_idw``).
- **Adjustment**: one vote per station, the median over the stations (#314):
  a station with several connection points (Wivenhoe's four) takes one vote,
  its members' median, not one per TNI. Below three stations the median is no
  better than a mean, so the nearest station's vote stands alone (#315). One
  constrained station does not move the median, most of them do; the members
  carry no capacity, so there is no MW weighting.
- **Home** is Home Assistant's own location, in the zone of the load
  connection point chosen for it (#316) through the tool, else the zone of its
  nearest mapped node. Its MLF is the chosen load point's, else the zone's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cached_property
from statistics import median
from typing import Optional, Sequence

from .congestion_zones import CongestionZones
from .duid_catalogue import Catalogue, NodeInfo
from .load_points import LoadPoint, LoadPoints

ZONE_HOME = "home"
# A member closer than this weighs as if it were this far, so one node at the
# centre does not take the whole MLF.
_MIN_WEIGHT_KM = 5.0
SOURCE_LOAD = "load_tni"
SOURCE_GENERATION = "nearby_generation_idw"
# Fewer stations than this and the median is no better than a mean (#315).
MIN_MEDIAN_STATIONS = 3
AGGREGATE_MEDIAN = "station_median"
AGGREGATE_NEAREST = "nearest_station"

# (key, name, latitude, longitude, load TNIs) per region: the capital and the
# larger regional centres, each with the bulk supply points that serve it, as
# AEMO's 2026-27 MLF report names them. Canberra is in NSW1.
CITIES: dict[str, tuple[tuple[str, str, float, float, tuple[str, ...]], ...]] = {
    "QLD1": (
        ("brisbane", "Brisbane", -27.4698, 153.0251, (
            "QALG", "QAGW", "QCBW", "QBBS", "QBMH", "QGDA", "QLGH", "QLGL", "QMRE", "QRLD",
            "QRLE", "QRBS", "QSPN", "QSUM", "QTNS",
        )),
        ("gold_coast", "Gold Coast", -28.0167, 153.4000, ("QMAL", "QMAR", "QMGB", "QMGL")),
        ("sunshine_coast", "Sunshine Coast", -26.6500, 153.0667, ("QPWD",)),
        ("toowoomba", "Toowoomba", -27.5598, 151.9507, ("QMRG", "QMRX")),
        ("rockhampton", "Rockhampton", -23.3781, 150.5136, ("QEGN", "QROC")),
        ("mackay", "Mackay", -21.1411, 149.1861, ("QALC", "QMKA", "QPIV")),
        ("townsville", "Townsville", -19.2590, 146.8169, ("QASF", "QDGL", "QGAR", "QTVE", "QTVS")),
        ("cairns", "Cairns", -16.9186, 145.7781, ("QEMT", "QKAM", "QWRE")),
    ),
    "NSW1": (
        ("sydney", "Sydney", -33.8688, 151.2093, (
            "NALX", "NBFN", "NBFS", "NBM1", "NBMP", "NBG1", "NBG3", "NCBS", "NCS1", "NCTB",
            "NCAR", "NCR1", "NGSQ", "NGB1", "NHYM", "NHLD", "NHLX", "NHBB", "NHVN", "NING",
            "NKOG", "NKN1", "NKNL", "NLCV", "NLP1", "NMQP", "NMQS", "NMKV", "NMPK", "NMBK",
            "NPHT", "NPHL", "NPO1", "NRGV", "NRKD", "NRWR", "NRSB", "NSPT", "NSFS", "NSE2",
            "NSN1", "NSN2", "NSYS", "NSW1", "NSW2", "NTPR", "NVYD", "NWAV",
        )),
        ("newcastle", "Newcastle", -32.9283, 151.7817, ("NBHL", "NBHX", "NNEW", "NNEX", "NTMC", "NTME", "NWR1")),
        ("wollongong", "Wollongong", -34.4278, 150.8931, ("NDT1", "NDT2")),
        ("canberra", "Canberra", -35.2809, 149.1300, (
            "AAVT", "ABCN", "ACTE", "ACVC", "AELK", "AGCK", "AGLM", "AHRM", "ALTM", "AMLG",
            "AQB1", "AQB2", "ATDR", "ATLP", "AWDN", "AWSA",
        )),
        ("wagga_wagga", "Wagga Wagga", -35.1082, 147.3598, ("NWG2", "NWG6", "NWGN")),
        ("tamworth", "Tamworth", -31.0927, 150.9320, ("NTA2",)),
        ("orange", "Orange", -33.2835, 149.1013, ("NONO", "NRGE")),
    ),
    "VIC1": (
        ("melbourne", "Melbourne", -37.8136, 144.9631, (
            "VATS", "VBL1", "VBL2", "VBL3", "VBL6", "VBL7", "VBT2", "VBT6", "VBTS", "VCB2",
            "VCB5", "VCBT", "VDPT", "VER2", "VERT", "VFB2", "VFBT", "VHTS", "VKT2", "VKTS",
            "VMT2", "VMT6", "VMT7", "VRT2", "VRT6", "VRT7", "VRW2", "VRW3", "VRW6", "VRW7",
            "VSM6", "VSMT", "VSV2", "VSVT", "VTS2", "VTS3", "VTS4", "VTST", "VTT2", "VTTS",
            "VWM2", "VWM6", "VWM7",
        )),
        ("geelong", "Geelong", -38.1499, 144.3617, ("VGT6",)),
        ("ballarat", "Ballarat", -37.5622, 143.8503, ("VBAT",)),
        ("bendigo", "Bendigo", -36.7570, 144.2794, ("VBE2", "VBE6")),
        ("latrobe_valley", "Latrobe Valley", -38.2353, 146.3949, ("VLY6", "VMWT")),
    ),
    "SA1": (
        ("adelaide", "Adelaide", -34.9285, 138.6007, (
            "SACR", "SETC", "SHVA", "SKLB", "SLFE", "SMAG", "SMUP", "SMVE", "SNBN", "SNFD",
            "SPAR", "SPGW", "STSY",
        )),
        ("port_augusta", "Port Augusta", -32.4911, 137.7653, ("SDAV", "SDAW")),
        ("mount_gambier", "Mount Gambier", -37.8284, 140.7804, ("SBL1", "SBLA", "SMGA")),
        # The Riverland's own congestion zone has no other city (#387).
        ("renmark", "Renmark", -34.1748, 140.7468, ("SBER", "SBE1", "SNWB")),
    ),
    "TAS1": (
        ("hobart", "Hobart", -42.8821, 147.3272, (
            "TBW2", "TCR2", "TCS3", "TK13", "TKI2", "TLF2", "TMT2", "TNH2", "TRI3", "TRI4", "TRK2",
        )),
        ("launceston", "Launceston", -41.4332, 147.1441, ("THA3", "TMY2", "TNW2", "TSL2", "TTR2")),
        ("burnie", "Burnie", -41.0526, 145.9061, ("TBU3", "TEB2", "TEB3")),
    ),
}


@dataclass(frozen=True)
class Member:
    node: NodeInfo
    distance_km: float


@dataclass(frozen=True)
class CityReference:
    """The preset city nearest Home, and its own load points' MLF, to set Home's choice against (#316)."""

    key: str
    name: str
    distance_km: float
    mlf: Optional[float]
    tnis: tuple[str, ...]


@dataclass(frozen=True)
class Zone:
    """One zone price proxy: a congestion zone of the region, or Home."""

    key: str                                  # the congestion zone's code, lower case, or "home"
    name: str                                 # the zone's city, or "Home"
    latitude: float
    longitude: float
    congestion_zone: Optional[str]            # AEMO's code, e.g. N_SYDNEY
    members: tuple[Member, ...]
    nearest_only: bool
    load_points: tuple[LoadPoint, ...] = ()
    nearest_city: Optional[CityReference] = None
    cities: tuple[str, ...] = ()              # every preset city whose load points are in the zone
    city_keys: tuple[str, ...] = ()           # their keys, the named city first (the registry migration)
    unmapped: tuple[str, ...] = ()            # the region's nodes the tool does not map
    source: str = ""                          # the mapping's citation

    # Worked out once per zone, not on every update: a zone is rebuilt only at setup, and the
    # largest (Adelaide, 46 members) otherwise spent half its update time here.
    @cached_property
    def member_keys(self) -> tuple[str, ...]:
        """Each member's station, in member order."""
        return tuple(station_key(m.node) for m in self.members)

    @cached_property
    def stations(self) -> tuple[str, ...]:
        """The members' stations, nearest first, each once (#314)."""
        return tuple(dict.fromkeys(self.member_keys))

    @cached_property
    def aggregate(self) -> str:
        """How the members' adjustments make the zone's: the stations' median, or the nearest station's."""
        return AGGREGATE_MEDIAN if len(self.stations) >= MIN_MEDIAN_STATIONS else AGGREGATE_NEAREST

    @property
    def mlf_source(self) -> Optional[str]:
        if self.load_points:
            return SOURCE_LOAD
        return SOURCE_GENERATION if any(m.node.mlf is not None for m in self.members) else None

    @property
    def mlf(self) -> Optional[float]:
        """The mean MLF of the place's load points; else the members' inverse-distance-weighted mean."""
        if self.load_points:
            return round(sum(p.mlf for p in self.load_points) / len(self.load_points), 4)
        weighted = [(m.node.mlf, 1.0 / max(m.distance_km, _MIN_WEIGHT_KM)) for m in self.members
                    if m.node.mlf is not None]
        if not weighted:
            return None
        return round(sum(mlf * w for mlf, w in weighted) / sum(w for _mlf, w in weighted), 4)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, haversine on a 6371 km sphere."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def home_region(catalogue: Catalogue, lat: float, lon: float) -> Optional[str]:
    """The region whose grid serves a location: that of the nearest located catalogue node.

    Home has one zone, in that region's entry, not one in every entry. Without
    a located node, the region of the nearest preset city stands in.
    """
    located = [n for n in catalogue.nodes.values() if n.latitude is not None and n.longitude is not None]
    if located:
        return min(located, key=lambda n: distance_km(lat, lon, n.latitude, n.longitude)).region  # type: ignore[arg-type]
    cities = [(region, c) for region, cs in CITIES.items() for c in cs]
    if not cities:
        return None
    return min(cities, key=lambda rc: distance_km(lat, lon, rc[1][2], rc[1][3]))[0]


def nearest_city(
    region: str, lat: float, lon: float, loads: Optional[LoadPoints] = None,
) -> Optional[CityReference]:
    """The region's preset city nearest a point, with the mean MLF of its load points, where known.

    AEMO publishes no coordinates for load connection points, so the Home
    load point cannot be found by distance; the nearest preset city's own
    choice is the closest thing to a check on it.
    """
    cities = CITIES.get(region, ())
    if not cities:
        return None
    key, name, clat, clon, tnis = min(cities, key=lambda c: distance_km(lat, lon, c[2], c[3]))
    points = [p for p in (loads.get(t) for t in tnis) if p is not None and p.region == region] if loads else []
    mlf = round(sum(p.mlf for p in points) / len(points), 4) if points else None
    return CityReference(key, name, round(distance_km(lat, lon, clat, clon), 1), mlf, tuple(p.tni for p in points))


def _located(catalogue: Catalogue, region: str) -> list[NodeInfo]:
    return [n for n in catalogue.region_nodes(region) if n.latitude is not None and n.longitude is not None]


def _by_distance(nodes: Sequence[NodeInfo], lat: float, lon: float) -> tuple[Member, ...]:
    """The nodes as members, nearest first."""
    return tuple(sorted(
        (Member(n, round(distance_km(lat, lon, n.latitude, n.longitude), 1)) for n in nodes),  # type: ignore[arg-type]
        key=lambda m: (m.distance_km, m.node.tni),
    ))


def _members(
    catalogue: Catalogue, region: str, zones: CongestionZones, zone: Optional[str], lat: float, lon: float,
) -> tuple[tuple[Member, ...], bool]:
    """(members, nearest_only): the zone's located nodes nearest first; the region's nearest where it has none."""
    located = _located(catalogue, region)
    inside = _by_distance([n for n in located if zone and zones.node_zone(n.tni, region) == zone], lat, lon)
    if inside:
        return inside, False
    return _by_distance(located, lat, lon)[:1], True


def _zone_loads(region: str, zones: CongestionZones, zone: Optional[str],
                loads: Optional[LoadPoints]) -> tuple[LoadPoint, ...]:
    """The region's load connection points the tool maps to the zone."""
    if loads is None or zone is None:
        return ()
    return tuple(p for p in loads.region_points(region) if zones.zone_for(p.tni) == zone)


def zone_cities(region: str, zones: CongestionZones) -> dict[str, list[tuple[str, str, float, float, tuple[str, ...]]]]:
    """Each zone's preset cities, the one with the most load points in the zone first.

    A city is in the zone most of its load points map to; a tie goes to the
    order of CITIES. A city none of whose load points the tool maps is in no zone.
    """
    found: dict[str, list[tuple[int, int, tuple[str, str, float, float, tuple[str, ...]]]]] = {}
    for order, city in enumerate(CITIES.get(region, ())):
        counts: dict[str, int] = {}
        for tni in city[4]:
            zone = zones.zone_for(tni)
            if zone is not None and zones.region_of(zone) == region:
                counts[zone] = counts.get(zone, 0) + 1
        if counts:
            zone = max(counts, key=lambda z: counts[z])
            found.setdefault(zone, []).append((-counts[zone], order, city))
    return {zone: [city for *_rank, city in sorted(rows)] for zone, rows in found.items()}


def unmapped_nodes(catalogue: Catalogue, region: str, zones: CongestionZones) -> tuple[str, ...]:
    """The region's catalogue nodes the tool maps to no zone of the region, by connection point."""
    return tuple(sorted(n.tni for n in catalogue.region_nodes(region) if zones.node_zone(n.tni, region) is None))


def build_zones(
    catalogue: Catalogue, region: str, zones: CongestionZones, loads: Optional[LoadPoints] = None,
) -> list[Zone]:
    """One zone per congestion zone of the region, by code, named for its city (#387)."""
    by_zone = zone_cities(region, zones)
    unmapped = unmapped_nodes(catalogue, region, zones)
    built: list[Zone] = []
    for code in zones.region_zones(region):
        cities = by_zone.get(code, [])
        located = [n for n in _located(catalogue, region) if zones.node_zone(n.tni, region) == code]
        if cities:
            _key, name, lat, lon, _tnis = cities[0]
        elif located:  # no preset city: the members' centre, named by the zone's code
            name = code
            lat = sum(n.latitude for n in located) / len(located)  # type: ignore[misc]
            lon = sum(n.longitude for n in located) / len(located)  # type: ignore[misc]
        else:
            continue
        members, nearest_only = _members(catalogue, region, zones, code, lat, lon)
        built.append(Zone(
            code.lower(), name, lat, lon, code, members, nearest_only, _zone_loads(region, zones, code, loads),
            cities=tuple(c[1] for c in cities), city_keys=tuple(c[0] for c in cities),
            unmapped=unmapped, source=zones.source,
        ))
    return built


def home_zone(
    catalogue: Catalogue, region: str, zones: CongestionZones, home: Optional[tuple[float, float]],
    loads: Optional[LoadPoints] = None, home_load: Optional[str] = None,
) -> Optional[Zone]:
    """Home's zone: that of its chosen load point through the tool, else of its nearest mapped node; None
    without a location."""
    if home is None:
        return None
    lat, lon = home
    code = zones.node_zone(home_load, region) if home_load else None
    if code is None:
        mapped = [n for n in _located(catalogue, region) if zones.node_zone(n.tni, region) is not None]
        nearest = _by_distance(mapped, lat, lon)[:1]
        code = zones.node_zone(nearest[0].node.tni, region) if nearest else None
    members, nearest_only = _members(catalogue, region, zones, code, lat, lon)
    chosen = loads.get(home_load) if loads is not None and home_load else None
    points = (chosen,) if chosen is not None and chosen.region == region else _zone_loads(region, zones, code, loads)
    cities = zone_cities(region, zones).get(code, []) if code else []
    return Zone(
        ZONE_HOME, "Home", lat, lon, code, members, nearest_only, points,
        nearest_city=nearest_city(region, lat, lon, loads), cities=tuple(c[1] for c in cities),
        unmapped=unmapped_nodes(catalogue, region, zones), source=zones.source,
    )


def station_key(node: NodeInfo) -> str:
    """The station a node votes for: its first station, else its TNI."""
    return node.stations[0] if node.stations else node.tni


def zone_adjustment(zone: Zone, values: Sequence[Optional[float]]) -> Optional[float]:
    """One vote per station from the members' adjustments, in member order; None when none is known.

    A station's vote is the median of its members' known adjustments. The
    zone's is the median of the votes, or, below three stations, the
    nearest station's with a known vote.
    """
    votes: dict[str, list[float]] = {}
    for key, value in zip(zone.member_keys, values):
        known = votes.setdefault(key, [])
        if value is not None:
            known.append(value)
    station_votes = [median(v) for v in votes.values() if v]
    if not station_votes:
        return None
    if zone.aggregate == AGGREGATE_NEAREST:
        return round(station_votes[0], 4)
    return round(median(station_votes), 4)
