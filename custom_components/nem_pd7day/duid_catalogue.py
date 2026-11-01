"""The bundled DUID catalogue: each NEM DUID's region, station, fuel and coordinates.

``duid_catalogue.json`` is built before a release by
``scripts/build_duid_catalogue.py`` from AEMO's registration data
(DUDETAILSUMMARY and STATION), Open Electricity's public facility files and
the last fortnight of DispatchIS LOCAL_PRICE rows. Nothing is fetched at
run time. A DUID registered after the build is not listed, and can still be
entered by hand.

It gives the local price sensors what the feeds do not carry: the region a
DUID is in, so its local price adds the adjustment to that region's ROP; the
connection point (TNI) it is registered at and that node's marginal loss
factor (MLF), for the node price sensors (#286); a name for the options list,
coordinates for the map, and each region's DUIDs most often locally
constrained, offered as disabled sensors.
"""
from __future__ import annotations

import functools
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

PATH = pathlib.Path(__file__).with_name("duid_catalogue.json")
# Registrations with no plant behind them (#311): each region's dummy
# generator and reserve trader, VPP and demand response aggregations, and
# market ancillary service aggregations. None has a location, and none can
# carry a meaningful local price adjustment.
_NON_PHYSICAL_DUID = re.compile(r"^(DG_|RT_|AS_)")
_NON_PHYSICAL_STATION = re.compile(
    r"dummy|reserve trader|\bvpp\b|aggregat|\bm[as]{2}p\b|^(as|dr) ", re.IGNORECASE,
)


@dataclass(frozen=True)
class DuidInfo:
    duid: str
    region: str
    station: str
    type: Optional[str]
    fuel: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    constrained_intervals: int
    tni: Optional[str] = None      # the connection point (transmission node) the unit is registered at
    mlf: Optional[float] = None    # its marginal loss factor, from the registration in force

    @property
    def non_physical(self) -> bool:
        """A registration with no plant behind it: no location, and a dummy, reserve trader or aggregation."""
        if self.latitude is not None:
            return False
        return bool(
            _NON_PHYSICAL_DUID.match(self.duid) or self.fuel == "aggregator_vpp"
            or _NON_PHYSICAL_STATION.search(self.station or "")
        )

    @property
    def label(self) -> str:
        """``GSTONE1 · Gladstone (coal black)`` for the options list; the DUID alone when it is the only name."""
        fuel = f" ({self.fuel.replace('_', ' ')})" if self.fuel else ""
        station = "" if self.station == self.duid else f" · {self.station}"
        return f"{self.duid}{station}{fuel}"


@dataclass(frozen=True)
class NodeInfo:
    """A connection point (TNI) with registered units: its region, MLF, units and their mean position."""

    tni: str
    region: str
    mlf: Optional[float]
    duids: tuple[str, ...]
    stations: tuple[str, ...]
    latitude: Optional[float]
    longitude: Optional[float]

    @property
    def label(self) -> str:
        """``QWIV1 · Wivenhoe (W/HOE#1, W/HOE#2)`` for the options list."""
        station = f" · {', '.join(self.stations)}" if self.stations else ""
        return f"{self.tni}{station} ({', '.join(self.duids)})"


@dataclass(frozen=True)
class Catalogue:
    generated: Optional[str]
    constrained_window: tuple[str, ...]
    duids: dict[str, DuidInfo]
    top: dict[str, tuple[str, ...]]
    nodes: dict[str, NodeInfo] = field(default_factory=dict)

    def info(self, duid: str) -> Optional[DuidInfo]:
        return self.duids.get(duid)

    def node(self, tni: str) -> Optional[NodeInfo]:
        return self.nodes.get(tni)

    def region_nodes(self, region: str, include_non_physical: bool = False) -> list[NodeInfo]:
        """The region's connection points, by station and TNI; without those whose units are all non-physical."""
        found = [n for n in self.nodes.values() if n.region == region
                 and (include_non_physical or not self._all_non_physical(n))]
        return sorted(found, key=lambda n: ((n.stations[0] if n.stations else n.tni).lower(), n.tni))

    def _all_non_physical(self, node: NodeInfo) -> bool:
        infos = [self.duids.get(d) for d in node.duids]
        return bool(infos) and all(i is not None and i.non_physical for i in infos)

    def region_duids(self, region: str, include_non_physical: bool = False) -> list[DuidInfo]:
        """The region's DUIDs, most often constrained first, then by station and DUID; non-physical ones
        (#311) only when asked."""
        found = [i for i in self.duids.values() if i.region == region and (include_non_physical or not i.non_physical)]
        return sorted(found, key=lambda i: (-i.constrained_intervals, i.station.lower(), i.duid))

    def top_for(self, region: str) -> tuple[str, ...]:
        return self.top.get(region, ())


EMPTY = Catalogue(None, (), {}, {})


def _float(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_catalogue(data: Any) -> Catalogue:
    """A Catalogue from the JSON's shape; anything malformed is skipped, not raised."""
    if not isinstance(data, dict):
        return EMPTY
    duids: dict[str, DuidInfo] = {}
    for duid, row in (data.get("duids") or {}).items():
        if not isinstance(row, dict) or not isinstance(row.get("region"), str):
            continue
        duids[duid] = DuidInfo(
            duid=duid,
            region=row["region"],
            station=str(row.get("station") or duid),
            type=row.get("type"),
            fuel=row.get("fuel"),
            latitude=_float(row.get("lat")),
            longitude=_float(row.get("lon")),
            constrained_intervals=int(row.get("constrained_intervals") or 0),
            tni=row.get("tni") if isinstance(row.get("tni"), str) else None,
            mlf=_float(row.get("mlf")),
        )
    top = {
        region: tuple(d for d in listed if d in duids)
        for region, listed in (data.get("top") or {}).items()
        if isinstance(listed, list)
    }
    return Catalogue(
        data.get("generated"), tuple(data.get("constrained_window") or ()), duids, top, _parse_nodes(data.get("nodes")),
    )


def _parse_nodes(raw: Any) -> dict[str, NodeInfo]:
    nodes: dict[str, NodeInfo] = {}
    for tni, row in (raw or {}).items() if isinstance(raw, dict) else ():
        if not isinstance(row, dict) or not isinstance(row.get("region"), str):
            continue
        nodes[tni] = NodeInfo(
            tni=tni,
            region=row["region"],
            mlf=_float(row.get("mlf")),
            duids=tuple(d for d in row.get("duids") or () if isinstance(d, str)),
            stations=tuple(n for n in row.get("stations") or () if isinstance(n, str)),
            latitude=_float(row.get("lat")),
            longitude=_float(row.get("lon")),
        )
    return nodes


@functools.lru_cache(maxsize=1)
def load_catalogue(path: pathlib.Path = PATH) -> Catalogue:
    """Read and parse the bundled file once. Blocking: call it in the executor."""
    try:
        return parse_catalogue(json.loads(path.read_text()))
    except (OSError, ValueError):
        return EMPTY
