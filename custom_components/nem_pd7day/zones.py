"""City and zone price proxies: what stands for a place (#288).

A zone is a centre, a preset city of the entry's region or Home Assistant's
own location, and a radius.

- **Zone MLF**: the place's own load connection points, from AEMO's annual
  MLF report (load_points.py). A preset city lists its metropolitan bulk
  supply points; Home takes the one chosen in the options. Their mean MLF is
  the zone MLF (``zone_mlf_source: load_tni``). Where no load point is known,
  the inverse-distance-weighted mean of the members' MLFs stands in
  (``nearby_generation_idw``).
- **Adjustment**: one vote per station, the median over the stations (#314):
  a station with several connection points (Wivenhoe's four) takes one
  vote, its members' median, not one per TNI. Below three stations the
  median is no better than a mean, so the nearest station's vote stands
  alone (#315). The members are the region's catalogue nodes with
  coordinates within the radius, nearest first; where none is that close,
  the single nearest node, flagged ``nearest_only``. They are generation and storage connection
  points: city load is not a variable in NEMDE's constraint equations, so it
  has no local price adjustment of its own, and nearby units are a geographic
  proxy, never a validated household node. One constrained station does not
  move the median, most of them do; the members carry no capacity, so there
  is no MW weighting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Any, Optional, Sequence

from .duid_catalogue import Catalogue, NodeInfo
from .load_points import LoadPoint, LoadPoints

ZONE_HOME = "home"
DEFAULT_RADIUS_KM = 100
MIN_RADIUS_KM = 10
MAX_RADIUS_KM = 500
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
    key: str
    name: str
    latitude: float
    longitude: float
    radius_km: float
    members: tuple[Member, ...]
    nearest_only: bool
    load_points: tuple[LoadPoint, ...] = ()
    nearest_city: Optional[CityReference] = None

    @property
    def stations(self) -> tuple[str, ...]:
        """The members' stations, nearest first, each once (#314)."""
        return tuple(dict.fromkeys(station_key(m.node) for m in self.members))

    @property
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


def zone_choices(region: str) -> list[tuple[str, str]]:
    """(key, name) of the region's preset cities, then Home."""
    return [(city[0], city[1]) for city in CITIES.get(region, ())] + [(ZONE_HOME, "Home")]


def parse_zones(text: Any, region: str) -> list[str]:
    """The zone keys of the region in a comma separated string or a list; others are dropped."""
    known = {key for key, _name in zone_choices(region)}
    items = text if isinstance(text, (list, tuple)) else str(text or "").split(",")
    keys: list[str] = []
    for item in items:
        key = str(item).strip().lower()
        if key in known and key not in keys:
            keys.append(key)
    return keys


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


def _members(catalogue: Catalogue, region: str, lat: float, lon: float, radius_km: float) -> tuple[tuple, bool]:
    located = sorted(
        (Member(node, round(distance_km(lat, lon, node.latitude, node.longitude), 1))
         for node in catalogue.region_nodes(region)
         if node.latitude is not None and node.longitude is not None),
        key=lambda m: (m.distance_km, m.node.tni),
    )
    inside = tuple(m for m in located if m.distance_km <= radius_km)
    return (inside, False) if inside else (tuple(located[:1]), True)


def build_zone(
    catalogue: Catalogue, region: str, key: str, radius_km: float,
    home: Optional[tuple[float, float]] = None, loads: Optional[LoadPoints] = None,
    home_load: Optional[str] = None,
) -> Optional[Zone]:
    """The zone for a key, or None when the key is unknown or Home has no location."""
    tnis: tuple[str, ...]
    if key == ZONE_HOME:
        if home is None:
            return None
        name, (lat, lon), tnis = "Home", home, ((home_load,) if home_load else ())
    else:
        found = [c for c in CITIES.get(region, ()) if c[0] == key]
        if not found:
            return None
        _key, name, lat, lon, tnis = found[0]
    points = tuple(p for p in (loads.get(t) for t in tnis) if p is not None and p.region == region) if loads else ()
    members, nearest_only = _members(catalogue, region, lat, lon, radius_km)
    reference = nearest_city(region, lat, lon, loads) if key == ZONE_HOME else None
    return Zone(key, name, lat, lon, radius_km, members, nearest_only, points, reference)


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
    for member, value in zip(zone.members, values):
        known = votes.setdefault(station_key(member.node), [])
        if value is not None:
            known.append(value)
    station_votes = [median(v) for v in votes.values() if v]
    if not station_votes:
        return None
    if zone.aggregate == AGGREGATE_NEAREST:
        return round(station_votes[0], 4)
    return round(median(station_votes), 4)
