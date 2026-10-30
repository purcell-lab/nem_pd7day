#!/usr/bin/env python3
"""Build custom_components/nem_pd7day/duid_catalogue.json: every NEM DUID with
its region, station, fuel and coordinates, and each region's DUIDs most often
locally constrained.

Standard library only; never run by the test suite or by Home Assistant. Run
it before a release to refresh the bundled table:

    python scripts/build_duid_catalogue.py [--days 14] [--month 2026_09]

Sources, all public:

- AEMO MMSDM ``DUDETAILSUMMARY``: DUID, region, station and dispatch type,
  the registration in force at the end of the month. The authority for a
  DUID's region, which the dispatch and P5MIN local price rows do not carry.
- AEMO MMSDM ``STATION``: the station's registered name and state.
- Open Electricity's facility data (``opennem/opennem`` stations.json, and
  the current ``au_facilities.json`` by station code): fuel and coordinates.
  The live Open Electricity API needs a key, so its public files are read;
  a DUID they do not list has no coordinates rather than guessed ones.
- AEMO DispatchIS daily archives for the last ``--days`` days: the
  ``DISPATCH,LOCAL_PRICE`` rows per DUID. Each region's DUIDs are ranked by
  the sum of |adjustment| over the window, how often times how much: a
  region-wide constraint gives dozens of units the same count of intervals.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import re
import sys
import urllib.request
import zipfile
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "custom_components" / "nem_pd7day" / "duid_catalogue.json"
MMSDM = "https://nemweb.com.au/Data_Archive/Wholesale_Electricity/MMSDM/{y}/MMSDM_{y}_{m}/MMSDM_Historical_Data_SQLLoader/DATA/"
ARCHIVE = "https://nemweb.com.au/Reports/Archive/DispatchIS_Reports/"
OE_STATIONS = "https://raw.githubusercontent.com/opennem/opennem/master/opennem/data/stations.json"
OE_GEO = "https://data.openelectricity.org.au/v3/geo/au_facilities.json"
REGIONS = ("QLD1", "NSW1", "VIC1", "SA1", "TAS1")
TOP_N = 5
HEADERS = {"User-Agent": "nem_pd7day duid catalogue builder"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=120) as resp:
        return resp.read()


def csv_rows(text: str, table: str) -> list[dict[str, str]]:
    """The D rows of one MMS table, keyed by the file's own I header."""
    header: list[str] | None = None
    rows = []
    for parts in csv.reader(io.StringIO(text)):
        if len(parts) < 4 or parts[2] != table:
            continue
        if parts[0] == "I":
            header = parts
        elif parts[0] == "D" and header is not None:
            rows.append(dict(zip(header[4:], parts[4:])))
    return rows


def mmsdm_table(month: str, table: str) -> list[dict[str, str]]:
    y, m = month.split("_")
    name = f"PUBLIC_ARCHIVE%23{table}%23FILE01%23{y}{m}010000.zip"
    with zipfile.ZipFile(io.BytesIO(fetch(MMSDM.format(y=y, m=m) + name))) as zf:
        return csv_rows(zf.read(zf.namelist()[0]).decode("utf-8", "replace"), table)


def registrations(month: str) -> dict[str, dict[str, str]]:
    """Each DUID's registration in force at the end of the month's archive."""
    current: dict[str, dict[str, str]] = {}
    for row in mmsdm_table(month, "DUDETAILSUMMARY"):
        if row.get("REGIONID") not in REGIONS:
            continue
        held = current.get(row["DUID"])
        if held is None or row["START_DATE"] > held["START_DATE"]:
            current[row["DUID"]] = row
    cutoff = dt.datetime.now().strftime("%Y/%m/%d")
    return {d: r for d, r in current.items() if r["END_DATE"][:10] >= cutoff}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def open_electricity() -> tuple[dict[str, dict], dict[str, tuple[float, float]]]:
    """(per DUID: fuel, name, lat, lon) from stations.json, and (station code or name: lat, lon) from the geo file."""
    units: dict[str, dict] = {}
    for station in json.loads(fetch(OE_STATIONS)):
        loc = station.get("location") or {}
        for fac in station.get("facilities") or []:
            if fac.get("network_id") == "NEM" and fac.get("code"):
                units[fac["code"]] = {
                    "name": station.get("name"), "fuel": fac.get("fueltech_id"),
                    "lat": loc.get("lat"), "lon": loc.get("lng"),
                }
    geo: dict[str, tuple[float, float]] = {}
    for feature in json.loads(fetch(OE_GEO)).get("features", []):
        code = (feature.get("properties") or {}).get("station_code")
        coords = (feature.get("geometry") or {}).get("coordinates")
        if code and coords and len(coords) == 2 and None not in coords:
            geo[code] = (coords[1], coords[0])
            geo.setdefault("name:" + _norm(feature["properties"].get("name") or ""), (coords[1], coords[0]))
    return units, geo


def constrained_counts(days: int) -> tuple[Counter, Counter, list[str]]:
    """Per DUID over the last days archived: dispatch intervals with a published
    local price adjustment, and the sum of |adjustment| in $/MWh."""
    listing = fetch(ARCHIVE).decode("utf-8", "replace")
    names = sorted(set(re.findall(r"PUBLIC_DISPATCHIS_(\d{8})\.zip", listing)))[-days:]
    counts: Counter = Counter()
    magnitude: Counter = Counter()
    for day in names:
        with zipfile.ZipFile(io.BytesIO(fetch(f"{ARCHIVE}PUBLIC_DISPATCHIS_{day}.zip"))) as outer:
            for member in outer.namelist():
                with zipfile.ZipFile(io.BytesIO(outer.read(member))) as inner:
                    text = inner.read(inner.namelist()[0]).decode("utf-8", "replace")
                lines = "\n".join(line for line in text.splitlines() if ",LOCAL_PRICE," in line)
                for row in csv_rows(lines, "LOCAL_PRICE"):
                    counts[row["DUID"]] += 1
                    magnitude[row["DUID"]] += abs(float(row.get("LOCAL_PRICE_ADJUSTMENT") or 0))
        print(f"  {day}: {sum(counts.values())} rows so far", file=sys.stderr)
    return counts, magnitude, names


def _station_name(aemo: str, oe: str | None) -> str:
    """Open Electricity's name when it has one; AEMO's registered names are mostly capitals."""
    if oe:
        return oe.strip()
    return aemo.title() if aemo.isupper() else aemo


def _coords(value: float | None) -> float | None:
    return round(float(value), 5) if isinstance(value, (int, float)) else None


def build(month: str, days: int) -> dict:
    regs = registrations(month)
    stations = {r["STATIONID"]: r for r in mmsdm_table(month, "STATION")}
    units, geo = open_electricity()
    counts, magnitude, window = constrained_counts(days)
    duids: dict[str, dict] = {}
    for duid, reg in sorted(regs.items()):
        oe = units.get(duid, {})
        station = stations.get(reg["STATIONID"], {})
        lat, lon = oe.get("lat"), oe.get("lon")
        name = _station_name((station.get("STATIONNAME") or reg["STATIONID"]).strip(), oe.get("name"))
        if lat is None or lon is None:
            lat, lon = geo.get(reg["STATIONID"]) or geo.get(duid.rstrip("0123456789")) or geo.get("name:" + _norm(name), (None, None))
        duids[duid] = {
            "region": reg["REGIONID"],
            "station": name,
            "type": reg.get("DISPATCHTYPE") or None,
            "fuel": oe.get("fuel"),
            "lat": _coords(lat), "lon": _coords(lon),
            "constrained_intervals": counts.get(duid, 0),
            "adjustment_mwh_sum": round(magnitude.get(duid, 0.0)),
        }
    top = {
        region: [d for d, _ in sorted(
            ((d, i["adjustment_mwh_sum"]) for d, i in duids.items() if i["region"] == region and i["adjustment_mwh_sum"]),
            key=lambda x: (-x[1], x[0]),
        )[:TOP_N]]
        for region in REGIONS
    }
    return {
        "generated": dt.date.today().isoformat(),
        "registration_month": month,
        "constrained_window": [f"{window[0][:4]}-{window[0][4:6]}-{window[0][6:]}", f"{window[-1][:4]}-{window[-1][4:6]}-{window[-1][6:]}"] if window else [],
        "sources": ["AEMO MMSDM DUDETAILSUMMARY", "AEMO MMSDM STATION", "Open Electricity facilities", "AEMO DispatchIS LOCAL_PRICE"],
        "top": top,
        "duids": duids,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--month", help="MMSDM month as YYYY_MM; default the newest published")
    args = parser.parse_args()
    month = args.month
    if month is None:
        listing = fetch(MMSDM.split("MMSDM_{y}")[0].format(y=dt.date.today().year)).decode("utf-8", "replace")
        month = sorted(set(re.findall(r"MMSDM_(\d{4}_\d{2})", listing)))[-1]
    catalogue = build(month, args.days)
    OUT.write_text(json.dumps(catalogue, indent=1, sort_keys=False) + "\n")
    located = sum(1 for d in catalogue["duids"].values() if d["lat"] is not None)
    print(f"{len(catalogue['duids'])} DUIDs, {located} with coordinates; top: {catalogue['top']}", file=sys.stderr)


if __name__ == "__main__":
    main()
