"""AEMO's IPRR congestion zones, bundled: which zone each TNI is in (#387).

AEMO manages transmission congestion by zone, and the Integrating
Price-Responsive Resources (IPRR) rule groups the NEM into 19 congestion zones.
Its draft Congestion Zone Mapping Tool maps every TNI to one. Every TNI has a
default zone, except the virtual TNI SJP1 (South Australia), where an NMI's
postcode can place it in another zone than SJP1's default.

congestion_zones.json is built from the tool by
scripts/build_congestion_zones.py, with the tool's date and draft status, so
it can be refreshed when AEMO finalises it. Nothing is fetched at run time.

A catalogue node's TNI is the first four characters of its connection point
ID. Some connection points (aggregations, non-standard IDs) have no TNI the
tool knows, and a few TNIs map to a zone of another region than the node's;
both are reported as unmapped rather than guessed.

Domain layer: no Home Assistant imports.
"""
from __future__ import annotations

import functools
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Optional

PATH = pathlib.Path(__file__).with_name("congestion_zones.json")


@dataclass(frozen=True)
class CongestionZones:
    """The tool's mapping: zone to region, TNI to zone, and SJP1's postcodes."""

    zones: dict[str, str] = field(default_factory=dict)
    tnis: dict[str, str] = field(default_factory=dict)
    postcodes: dict[str, dict[str, str]] = field(default_factory=dict)
    source_date: str = ""
    status: str = ""

    @property
    def source(self) -> str:
        """How the mapping is cited on a sensor: the tool and its date, and draft where it is one."""
        draft = " (draft)" if "DRAFT" in self.status.upper() else ""
        return f"AEMO IPRR Congestion Zone Mapping Tool, {self.source_date}{draft}".strip()

    def zone_for(self, tni: Optional[str], postcode: Any = None) -> Optional[str]:
        """The zone of a TNI (four characters; a longer connection point ID is cut to them), or None."""
        if not tni:
            return None
        code = str(tni).strip().upper()[:4]
        by_postcode = self.postcodes.get(code)
        if by_postcode and postcode not in (None, ""):
            try:
                found = by_postcode.get(str(int(str(postcode).strip())))
            except ValueError:
                found = None
            if found:
                return found
        return self.tnis.get(code)

    def region_of(self, zone: Optional[str]) -> Optional[str]:
        return self.zones.get(zone) if zone else None

    def region_zones(self, region: str) -> list[str]:
        """The region's zones, by code."""
        return sorted(zone for zone, zone_region in self.zones.items() if zone_region == region)

    def node_zone(self, tni: Optional[str], region: str) -> Optional[str]:
        """The zone of a node in ``region``: None where the tool has no zone for it, or one of another region."""
        zone = self.zone_for(tni)
        return zone if zone is not None and self.zones.get(zone) == region else None


EMPTY = CongestionZones()


def parse_congestion_zones(data: Any) -> CongestionZones:
    """The mapping from the JSON payload; anything malformed is dropped, not raised."""
    if not isinstance(data, dict):
        return EMPTY

    def table(key: str) -> dict[str, Any]:
        value = data.get(key)
        return value if isinstance(value, dict) else {}

    zones = {str(z).upper(): str(r) for z, r in table("zones").items()}
    tnis = {str(t).upper(): str(z).upper() for t, z in table("tnis").items() if str(z).upper() in zones}
    postcodes = {
        str(t).upper(): {str(p): str(z).upper() for p, z in by.items() if str(z).upper() in zones}
        for t, by in table("postcodes").items() if isinstance(by, dict)
    }
    return CongestionZones(zones, tnis, postcodes, str(data.get("source_date") or ""), str(data.get("status") or ""))


@functools.lru_cache(maxsize=1)
def load_congestion_zones(path: pathlib.Path = PATH) -> CongestionZones:
    """The bundled mapping, read once; EMPTY when the file is missing or unreadable."""
    try:
        return parse_congestion_zones(json.loads(path.read_text()))
    except (OSError, ValueError):
        return EMPTY
