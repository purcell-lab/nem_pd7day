#!/usr/bin/env python3
"""Build custom_components/nem_pd7day/load_points.json from AEMO's annual MLF report.

AEMO publishes each financial year's marginal loss factors as a PDF, whose
load tables name every load connection point (TNI): location, voltage, TNI
code and MLF. NEMWEB's daily MLF report and MMSDM's TRANSMISSIONLOSSFACTOR
carry the same codes and MLFs but no names, and aemo.com.au refuses automated
downloads, so the PDF is downloaded by hand and passed here:

    python -m venv /tmp/pdf && /tmp/pdf/bin/pip install pypdf
    /tmp/pdf/bin/python scripts/build_load_points.py marginal-loss-factors-for-the-2026-27-financial-year.pdf

Where a TNI has two rows, the load one is kept ("Dual MLF (Load)"), and of
rows effective before and from 1 July, the one from 1 July.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "custom_components" / "nem_pd7day" / "load_points.json"

REGIONS = {
    "Queensland": "QLD1", "New South Wales": "NSW1", "Australian Capital Territory": "NSW1",
    "Victoria": "VIC1", "South Australia": "SA1", "Tasmania": "TAS1",
}
_TABLE = re.compile(r"^Table \d+ (?P<state>.+?) loads\s*$")
_OTHER_TABLE = re.compile(r"^Table \d+ ")
_ROW = re.compile(
    r"^(?P<loc>.+?)\s+(?P<kv>\d[\d.]*(?:/\d[\d.]*)*)\s+(?P<tni>[A-Z][A-Z0-9]{2,7})\s+(?P<mlf>\d\.\d{4})\b"
)
_FY = re.compile(r"Financial Year (\d{4}-\d{2})")


def _clean(location: str) -> tuple[str, int]:
    """The name without dual-MLF and effective-date notes, and how much the row is preferred."""
    preference = 1
    if "Dual MLF (Generation)" in location or "effective prior to" in location:
        preference = 0
    if "Dual MLF (Load)" in location or "effective from" in location:
        preference = 2
    name = re.sub(r"\s*-\s*Dual MLF \((Generation|Load)\)", "", location)
    name = re.sub(r"\s*\(effective (prior to|from) [^)]*\)", "", name)
    return re.sub(r"\s+", " ", name).strip(" -"), preference


def parse(text: str) -> dict[str, dict]:
    """TNI -> {region, name, kv, mlf} from the report's load tables."""
    points: dict[str, tuple[int, dict]] = {}
    region = None
    for line in text.splitlines():
        table = _TABLE.match(line)
        if table:
            region = REGIONS.get(table.group("state").strip())
            continue
        if _OTHER_TABLE.match(line):
            region = None
            continue
        row = _ROW.match(line) if region else None
        if row is None:
            continue
        name, preference = _clean(row.group("loc"))
        point = {"region": region, "name": name, "kv": row.group("kv"), "mlf": float(row.group("mlf"))}
        current = points.get(row.group("tni"))
        if current is None or preference > current[0]:
            points[row.group("tni")] = (preference, point)
    return {tni: point for tni, (_p, point) in sorted(points.items())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdf", help="AEMO's 'Marginal Loss Factors: Financial Year YYYY-YY' report")
    args = parser.parse_args()
    from pypdf import PdfReader  # a build-time dependency only

    text = "\n".join(page.extract_text() or "" for page in PdfReader(args.pdf).pages)
    points = parse(text)
    fy = _FY.search(text)
    data = {
        "financial_year": fy.group(1) if fy else None,
        "source": "AEMO, Marginal Loss Factors: Financial Year report, load tables",
        "points": points,
    }
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    by_region: dict[str, int] = {}
    for point in points.values():
        by_region[point["region"]] = by_region.get(point["region"], 0) + 1
    print(f"{len(points)} load connection points ({data['financial_year']}): {by_region}")


if __name__ == "__main__":
    main()
