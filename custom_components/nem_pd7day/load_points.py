"""Load connection points and their MLFs, bundled (#288).

load_points.json is built by scripts/build_load_points.py from the load
tables of AEMO's annual report, "Marginal Loss Factors: Financial Year
YYYY-YY": each load TNI's region, location, voltage and MLF. NEMWEB and
MMSDM carry the codes and MLFs but not the names, which a city's or a home's
connection point is chosen by. Nothing is fetched at run time.
"""
from __future__ import annotations

import functools
import json
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

PATH = pathlib.Path(__file__).with_name("load_points.json")


@dataclass(frozen=True)
class LoadPoint:
    tni: str
    region: str
    name: str
    kv: str
    mlf: float

    @property
    def label(self) -> str:
        """``Belmont 110 kV (QBMH) · MLF 1.0128``"""
        return f"{self.name} {self.kv} kV ({self.tni}) · MLF {self.mlf:.4f}"


@dataclass(frozen=True)
class LoadPoints:
    financial_year: Optional[str]
    points: dict[str, LoadPoint]

    def get(self, tni: str) -> Optional[LoadPoint]:
        return self.points.get(tni)

    def region_points(self, region: str) -> list[LoadPoint]:
        """The region's load points, by name."""
        return sorted((p for p in self.points.values() if p.region == region), key=lambda p: (p.name, p.kv, p.tni))


EMPTY = LoadPoints(None, {})


def parse_load_points(data: Any) -> LoadPoints:
    """Tolerant of malformed rows: a row that cannot be read is skipped."""
    if not isinstance(data, dict):
        return EMPTY
    points: dict[str, LoadPoint] = {}
    rows = data.get("points")
    for tni, row in (rows.items() if isinstance(rows, dict) else ()):
        try:
            points[str(tni)] = LoadPoint(
                str(tni), str(row["region"]), str(row["name"]), str(row.get("kv", "")), float(row["mlf"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
    fy = data.get("financial_year")
    return LoadPoints(fy if isinstance(fy, str) else None, points)


@functools.lru_cache(maxsize=1)
def load_load_points(path: pathlib.Path = PATH) -> LoadPoints:
    """Read and parse the bundled file once. Blocking: call it in the executor."""
    try:
        return parse_load_points(json.loads(path.read_text()))
    except (OSError, ValueError):
        return EMPTY
