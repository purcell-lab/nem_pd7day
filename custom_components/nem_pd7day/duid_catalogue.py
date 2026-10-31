"""The bundled DUID catalogue: each NEM DUID's region, station, fuel and coordinates.

``duid_catalogue.json`` is built before a release by
``scripts/build_duid_catalogue.py`` from AEMO's registration data
(DUDETAILSUMMARY and STATION), Open Electricity's public facility files and
the last fortnight of DispatchIS LOCAL_PRICE rows. Nothing is fetched at
run time. A DUID registered after the build is not listed, and can still be
entered by hand.

It gives the local price sensors what the feeds do not carry: the region a
DUID is in, so the local price diagnostic adds the adjustment to that
region's ROP, a name for the options list, coordinates for the map, and each
region's DUIDs most often locally constrained, offered as disabled sensors.
"""
from __future__ import annotations

import functools
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

PATH = pathlib.Path(__file__).with_name("duid_catalogue.json")


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

    @property
    def label(self) -> str:
        """``GSTONE1 · Gladstone (coal black)`` for the options list; the DUID alone when it is the only name."""
        fuel = f" ({self.fuel.replace('_', ' ')})" if self.fuel else ""
        station = "" if self.station == self.duid else f" · {self.station}"
        return f"{self.duid}{station}{fuel}"


@dataclass(frozen=True)
class Catalogue:
    generated: Optional[str]
    constrained_window: tuple[str, ...]
    duids: dict[str, DuidInfo]
    top: dict[str, tuple[str, ...]]

    def info(self, duid: str) -> Optional[DuidInfo]:
        return self.duids.get(duid)

    def region_duids(self, region: str) -> list[DuidInfo]:
        """The region's DUIDs, most often constrained first, then by station and DUID."""
        found = [i for i in self.duids.values() if i.region == region]
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
        )
    top = {
        region: tuple(d for d in listed if d in duids)
        for region, listed in (data.get("top") or {}).items()
        if isinstance(listed, list)
    }
    return Catalogue(data.get("generated"), tuple(data.get("constrained_window") or ()), duids, top)


@functools.lru_cache(maxsize=1)
def load_catalogue(path: pathlib.Path = PATH) -> Catalogue:
    """Read and parse the bundled file once. Blocking: call it in the executor."""
    try:
        return parse_catalogue(json.loads(path.read_text()))
    except (OSError, ValueError):
        return EMPTY
