#!/usr/bin/env python3
"""Harvest NEMWEB market files for the golden master, and extract scenario inputs.

Spec 000 Part D. Standard library only; never run by the test suite.

    python scripts/nemweb_harvest.py harvest <archive_dir> [pd7day stpasa tradingis notices]
    python scripts/nemweb_harvest.py extract <archive_dir> <scenario> <region> <pd7day_run_stamp>

``harvest`` saves every file NEMWEB still lists, keeping only the tables the
integration reads, and appends one manifest row per source file (URL, size,
SHA-256, rows kept). It resumes from the manifest, so it can be rerun.

``extract`` copies one PD7DAY run, the STPASA run published at or before it,
the region's TradingIS prices for the run's seven days and the market notices
created in the 48 hours before it into ``tests/golden/nemweb/<scenario>/``, with the
matching manifest rows.

Retention, checked 27 September 2026: PD7DAY, STPASA and market notices stay
about 60 days in the current directories and PD7DAY has no archive, so a run
not harvested within about 60 days is lost. TradingIS and STPASA also have
archives.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://nemweb.com.au/Reports/"
FEEDS = {
    "pd7day": (
        BASE + "CURRENT/PD7Day/",
        r"(PUBLIC_PD7DAY_\d{14}_\d+\.zip)",
        {"CASESOLUTION", "MARKET_SUMMARY", "INTERCONNECTORSOLUTION", "PRICESOLUTION"},
    ),
    "stpasa": (
        BASE + "CURRENT/Short_Term_PASA_Reports/",
        r"(PUBLIC_STPASA_\d{12}_\d+\.zip)",
        {"REGIONSOLUTION"},
    ),
    "tradingis": (
        BASE + "ARCHIVE/TradingIS_Reports/",
        r"(PUBLIC_TRADINGIS_\d{8}_\d{8}\.zip)",
        {"PRICE"},
    ),
}
NOTICES = BASE + "CURRENT/Market_Notice/"
HEADERS = {"User-Agent": "nem_pd7day golden-master harvest"}
REPO = Path(__file__).resolve().parent.parent


def _get(url: str) -> bytes:
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001 - retried, then raised
            print("retry", url, err, flush=True)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(url)


def _keep(line: str, tables: set[str]) -> bool:
    parts = line.split(",", 3)
    return parts[0] == "C" or (len(parts) > 2 and parts[2] in tables)


def _trim(raw: bytes, tables: set[str]) -> list[str]:
    """Keep C rows and the I and D rows of the named tables, unchanged.

    TradingIS weekly archives hold one inner zip per interval; each is opened.
    """
    rows: list[str] = []
    outer = zipfile.ZipFile(io.BytesIO(raw))
    for name in sorted(outer.namelist()):
        data = outer.read(name)
        if name.lower().endswith(".zip"):
            inner = zipfile.ZipFile(io.BytesIO(data))
            data = inner.read(inner.namelist()[0])
        text = data.decode("ascii")
        rows.extend(line for line in text.splitlines(keepends=True) if _keep(line, tables))
    return rows


def _manifest(out: Path) -> tuple[Path, set[str]]:
    man = out / "manifest.jsonl"
    done = {json.loads(row)["file"] for row in man.read_text().splitlines()} if man.exists() else set()
    return man, done


def harvest_feed(archive: Path, feed: str) -> None:
    listing_url, pattern, tables = FEEDS[feed]
    out = archive / feed
    out.mkdir(parents=True, exist_ok=True)
    man, done = _manifest(out)
    names = sorted(set(re.findall(pattern, _get(listing_url).decode(), re.IGNORECASE)))
    print(feed, "listed", len(names), flush=True)
    with man.open("a") as m:
        for name in names:
            if name in done:
                continue
            raw = _get(listing_url + name)
            rows = _trim(raw, tables)
            with gzip.open(out / (name[:-4] + ".trimmed.csv.gz"), "wt", newline="") as f:
                f.writelines(rows)
            m.write(json.dumps({
                "file": name, "url": listing_url + name,
                "zip_sha256": hashlib.sha256(raw).hexdigest(),
                "zip_bytes": len(raw), "rows": len(rows),
            }) + "\n")
            m.flush()
            time.sleep(0.3)
    print(feed, "DONE", flush=True)


def harvest_notices(archive: Path) -> None:
    out = archive / "market_notice"
    out.mkdir(parents=True, exist_ok=True)
    man, done = _manifest(out)
    names = sorted(set(re.findall(r"(NEMITWEB1_MKTNOTICE_\d{8}\.R\d+)", _get(NOTICES).decode())))
    print("notices listed", len(names), flush=True)
    with man.open("a") as m:
        for name in names:
            if name in done:
                continue
            raw = _get(NOTICES + name)
            (out / name).write_bytes(raw)
            m.write(json.dumps({
                "file": name, "url": NOTICES + name,
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            }) + "\n")
            m.flush()
            time.sleep(0.2)
    print("notices DONE", flush=True)


def _rows(path: Path) -> list[str]:
    with gzip.open(path, "rt", newline="") as f:
        return f.readlines()


def _manifest_row(folder: Path, name: str) -> dict:
    for line in (folder / "manifest.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row["file"] == name:
            return row
    raise KeyError(name)


def extract(archive: Path, scenario: str, region: str, stamp: str) -> None:
    dest = REPO / "tests" / "golden" / "nemweb" / scenario
    dest.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []

    (pd_path,) = sorted((archive / "pd7day").glob(f"PUBLIC_PD7DAY_{stamp}_*.trimmed.csv.gz"))
    (dest / pd_path.name).write_bytes(pd_path.read_bytes())
    pd_zip = pd_path.name.replace(".trimmed.csv.gz", ".zip")
    manifest.append({"feed": "pd7day", **_manifest_row(archive / "pd7day", pd_zip)})
    run_file_time = dt.datetime.strptime(stamp[:12], "%Y%m%d%H%M")

    # STPASA published at or before the PD7DAY file time.
    best = None
    for path in sorted((archive / "stpasa").glob("PUBLIC_STPASA_*.csv.gz")):
        when = dt.datetime.strptime(path.name.split("_")[2], "%Y%m%d%H%M")
        if when <= run_file_time:
            best = path
    assert best is not None, "no STPASA run before the PD7DAY run"
    (dest / best.name).write_bytes(best.read_bytes())
    st_zip = re.sub(r"\.(regionsolution|trimmed)\.csv\.gz$", ".zip", best.name)
    manifest.append({"feed": "stpasa", **_manifest_row(archive / "stpasa", st_zip)})

    # TradingIS prices for the region over the run's seven days.
    start = run_file_time.replace(hour=0, minute=0)
    end = start + dt.timedelta(days=8)
    kept: list[str] = []
    header_done = False
    for path in sorted((archive / "tradingis").glob("PUBLIC_TRADINGIS_*.csv.gz")):
        first, last = (
            dt.datetime.strptime(x, "%Y%m%d")
            for x in re.match(r"PUBLIC_TRADINGIS_(\d{8})_(\d{8})", path.name).groups()
        )
        if last + dt.timedelta(days=1) < start or first > end:
            continue
        for line in _rows(path):
            parts = line.split(",")
            if parts[0] == "I" and not header_done:
                kept.append(line)
                header_done = True
            elif parts[0] == "D" and parts[6] == region:
                when = dt.datetime.strptime(parts[4].strip('"'), "%Y/%m/%d %H:%M:%S")
                if start < when <= end:
                    kept.append(line)
        tz = path.name.replace(".price.csv.gz", ".zip").replace(".trimmed.csv.gz", ".zip")
        manifest.append({"feed": "tradingis", **_manifest_row(archive / "tradingis", tz)})
    name = f"TRADINGIS_{region}_{start:%Y%m%d}_{end:%Y%m%d}.price.csv.gz"
    with gzip.GzipFile(dest / name, "wb", mtime=0) as f:
        f.write("".join(kept).encode("ascii"))

    # Market notices created in the 48 hours up to the PD7DAY file time, so a
    # scenario never sees a notice from after its own instant.
    for path in sorted((archive / "market_notice").glob("NEMITWEB1_MKTNOTICE_*.R*")):
        found = re.search(rb"Creation Date :\s+(\d\d/\d\d/\d{4})\s+(\d\d:\d\d:\d\d)", path.read_bytes())
        if not found:
            continue
        created = dt.datetime.strptime(b" ".join(found.groups()).decode(), "%d/%m/%Y %H:%M:%S")
        if run_file_time - dt.timedelta(hours=48) <= created <= run_file_time:
            (dest / path.name).write_bytes(path.read_bytes())
            manifest.append({"feed": "market_notice", **_manifest_row(archive / "market_notice", path.name)})

    (dest / "manifest.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in manifest))
    size = sum(p.stat().st_size for p in dest.iterdir())
    print(scenario, region, stamp, len(manifest), "sources", size, "bytes", flush=True)


def main(argv: list[str]) -> None:
    if len(argv) >= 2 and argv[0] == "harvest":
        archive = Path(argv[1])
        for feed in argv[2:] or ["pd7day", "stpasa", "tradingis", "notices"]:
            harvest_notices(archive) if feed == "notices" else harvest_feed(archive, feed)
    elif len(argv) == 5 and argv[0] == "extract":
        extract(Path(argv[1]), argv[2], argv[3], argv[4])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
