"""City and zone price proxies: which catalogue nodes stand for a place (#288).

A zone is a centre, a preset city of the entry's region or Home Assistant's
own location, and a radius. Its members are the region's catalogue nodes with
coordinates within the radius, nearest first; where none is that close, the
single nearest node, flagged ``nearest_only``. The nodes are generation and
storage connection points: city load is not a variable in NEMDE's constraint
equations, and AEMO publishes no names or locations for load connection
points in machine-readable form, so a zone's members are a geographic proxy,
never a validated household node.

- **Zone MLF**: the inverse-distance-weighted mean of the members' MLFs,
  generation MLFs near the city standing in for its load MLF.
- **Adjustment**: the median of the members' local price adjustments; one
  constrained unit does not move it, most of them do. The members carry no
  capacity, so there is no MW weighting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median
from typing import Any, Optional, Sequence

from .duid_catalogue import Catalogue, NodeInfo

ZONE_HOME = "home"
DEFAULT_RADIUS_KM = 100
MIN_RADIUS_KM = 10
MAX_RADIUS_KM = 500
# A member closer than this weighs as if it were this far, so one node at the
# centre does not take the whole MLF.
_MIN_WEIGHT_KM = 5.0

# (key, name, latitude, longitude) per region: the capital and the larger
# regional centres. Canberra is in NSW1.
CITIES: dict[str, tuple[tuple[str, str, float, float], ...]] = {
    "QLD1": (
        ("brisbane", "Brisbane", -27.4698, 153.0251),
        ("gold_coast", "Gold Coast", -28.0167, 153.4000),
        ("sunshine_coast", "Sunshine Coast", -26.6500, 153.0667),
        ("toowoomba", "Toowoomba", -27.5598, 151.9507),
        ("rockhampton", "Rockhampton", -23.3781, 150.5136),
        ("mackay", "Mackay", -21.1411, 149.1861),
        ("townsville", "Townsville", -19.2590, 146.8169),
        ("cairns", "Cairns", -16.9186, 145.7781),
    ),
    "NSW1": (
        ("sydney", "Sydney", -33.8688, 151.2093),
        ("newcastle", "Newcastle", -32.9283, 151.7817),
        ("wollongong", "Wollongong", -34.4278, 150.8931),
        ("canberra", "Canberra", -35.2809, 149.1300),
        ("wagga_wagga", "Wagga Wagga", -35.1082, 147.3598),
        ("tamworth", "Tamworth", -31.0927, 150.9320),
        ("dubbo", "Dubbo", -32.2569, 148.6011),
    ),
    "VIC1": (
        ("melbourne", "Melbourne", -37.8136, 144.9631),
        ("geelong", "Geelong", -38.1499, 144.3617),
        ("ballarat", "Ballarat", -37.5622, 143.8503),
        ("bendigo", "Bendigo", -36.7570, 144.2794),
        ("latrobe_valley", "Latrobe Valley", -38.2353, 146.3949),
    ),
    "SA1": (
        ("adelaide", "Adelaide", -34.9285, 138.6007),
        ("port_augusta", "Port Augusta", -32.4911, 137.7653),
        ("mount_gambier", "Mount Gambier", -37.8284, 140.7804),
    ),
    "TAS1": (
        ("hobart", "Hobart", -42.8821, 147.3272),
        ("launceston", "Launceston", -41.4332, 147.1441),
        ("burnie", "Burnie", -41.0526, 145.9061),
    ),
}


@dataclass(frozen=True)
class Member:
    node: NodeInfo
    distance_km: float


@dataclass(frozen=True)
class Zone:
    key: str
    name: str
    latitude: float
    longitude: float
    radius_km: float
    members: tuple[Member, ...]
    nearest_only: bool

    @property
    def mlf(self) -> Optional[float]:
        """The inverse-distance-weighted mean of the members' MLFs."""
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
    return [(key, name) for key, name, _lat, _lon in CITIES.get(region, ())] + [(ZONE_HOME, "Home")]


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


def build_zone(
    catalogue: Catalogue, region: str, key: str, radius_km: float,
    home: Optional[tuple[float, float]] = None,
) -> Optional[Zone]:
    """The zone for a key, or None when the key is unknown or Home has no location."""
    if key == ZONE_HOME:
        if home is None:
            return None
        name, (lat, lon) = "Home", home
    else:
        found = [c for c in CITIES.get(region, ()) if c[0] == key]
        if not found:
            return None
        _key, name, lat, lon = found[0]
    located = sorted(
        (Member(node, round(distance_km(lat, lon, node.latitude, node.longitude), 1))
         for node in catalogue.region_nodes(region)
         if node.latitude is not None and node.longitude is not None),
        key=lambda m: (m.distance_km, m.node.tni),
    )
    inside = tuple(m for m in located if m.distance_km <= radius_km)
    members, nearest_only = (inside, False) if inside else (tuple(located[:1]), True)
    return Zone(key, name, lat, lon, radius_km, members, nearest_only)


def zone_adjustment(values: Sequence[Optional[float]]) -> Optional[float]:
    """The median of the members' adjustments that are known; None when none is."""
    known = [v for v in values if v is not None]
    return round(median(known), 4) if known else None
