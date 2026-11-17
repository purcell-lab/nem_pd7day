#!/usr/bin/env python3
"""Build custom_components/nem_pd7day/congestion_zones.json from AEMO's IPRR Congestion Zone Mapping Tool (#387).

AEMO's Integrating Price-Responsive Resources (IPRR) rule groups the NEM into
19 congestion zones and maps every TNI to one. The draft tool, published with
the IPRR trial participation guidelines consultation, is a workbook whose
``lookuplist`` sheet holds one row per TNI (``zone_name, tnicode, condition,
postcode``). Every TNI has a ``default`` zone, except the virtual TNI SJP1
(South Australia), whose rows also carry a postcode: an NMI on SJP1 is in the
zone of its postcode, else in SJP1's default. aemo.com.au refuses automated
downloads, so the workbook is downloaded by hand and passed here:

    python -m venv /tmp/xl && /tmp/xl/bin/pip install openpyxl
    /tmp/xl/bin/python scripts/build_congestion_zones.py congestion_zone_mapping_tool_final.xlsx

The workbook's "Date Produced" and status are recorded with the mapping, so it
can be refreshed when AEMO finalises it.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "custom_components" / "nem_pd7day" / "congestion_zones.json"
URL = (
    "https://www.aemo.com.au/-/media/files/stakeholder_consultation/consultations/nem-consultations/2026/"
    "iprr-trial-participation-guidelines-consultation/congestion_zone_mapping_tool_final.xlsx"
)
REGION_BY_PREFIX = {"Q": "QLD1", "N": "NSW1", "V": "VIC1", "S": "SA1", "T": "TAS1"}


def _cell(value: object) -> str:
    return str(value).strip() if value is not None else ""


def build(rows: list[tuple], produced: str | None, status: str | None) -> dict:
    """The JSON payload from the lookup rows ``(zone_name, tnicode, condition, postcode)``."""
    tnis: dict[str, str] = {}
    postcodes: dict[str, dict[str, str]] = {}
    for zone_name, tnicode, condition, postcode in rows:
        zone, tni = _cell(zone_name).upper(), _cell(tnicode).upper()
        if not zone or not tni:
            continue
        if _cell(condition).lower() == "default":
            if tnis.get(tni, zone) != zone:
                raise ValueError(f"{tni} has two default zones: {tnis[tni]} and {zone}")
            tnis[tni] = zone
        elif postcode not in (None, ""):
            postcodes.setdefault(tni, {})[str(int(float(str(postcode))))] = zone
    for tni in postcodes:
        if tni not in tnis:
            raise ValueError(f"{tni} has postcode rows but no default zone")
    zones = sorted(set(tnis.values()) | {z for by in postcodes.values() for z in by.values()})
    return {
        "source": "AEMO, IPRR Congestion Zone Mapping Tool (lookuplist)",
        "status": status or "",
        "source_date": produced or "",
        "url": URL,
        "zones": {zone: REGION_BY_PREFIX[zone[0]] for zone in zones},
        "tnis": dict(sorted(tnis.items())),
        "postcodes": {tni: dict(sorted(by.items(), key=lambda kv: int(kv[0]))) for tni, by in sorted(postcodes.items())},
    }


def read_workbook(path: Path) -> tuple[list[tuple], str | None, str | None]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = [tuple(r[:4]) for r in wb["lookuplist"].iter_rows(min_row=2, values_only=True)]
    produced = status = None
    for row in wb["Instruction"].iter_rows(values_only=True):
        cells = [c for c in row if c is not None]
        for i, cell in enumerate(cells):
            if isinstance(cell, str) and cell.strip().startswith("Date Produced") and i + 1 < len(cells):
                value = cells[i + 1]
                produced = value.date().isoformat() if isinstance(value, _dt.datetime) else _cell(value)
            if isinstance(cell, str) and "DRAFT" in cell.upper() and status is None:
                status = cell.split(":")[0].strip()
    return rows, produced, status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    rows, produced, status = read_workbook(args.workbook)
    data = build(rows, produced, status)
    OUT.write_text(json.dumps(data, indent=1) + "\n")
    print(f"{OUT.name}: {len(data['tnis'])} TNIs in {len(data['zones'])} zones, "
          f"{sum(map(len, data['postcodes'].values()))} postcode rows, produced {data['source_date']}")


if __name__ == "__main__":
    main()
