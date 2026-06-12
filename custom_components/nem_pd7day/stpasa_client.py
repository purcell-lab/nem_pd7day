"""
NEMWeb STPASA (Short Term PASA) data client.

Fetches, extracts, and parses the latest PUBLIC_STPASA ZIP from NEMWeb,
returning the REGIONSOLUTION rows for a single region.

STPASA provides AEMO's demand (DEMAND10/50/90 — POE bands) and
semi-scheduled renewable generation (SS_SOLAR_UIGF / SS_WIND_UIGF) and
surplus-capacity forecasts on a 30-minute resolution out to ~7 days.
These signals are used as the second-stage (OLS) residual correction on
top of the isotonic PD7DAY calibration at the h22–120 horizon band.

Timezone policy
---------------
All datetime values from the CSV are in NEM time (AEST, UTC+10:00, no DST).
Every timestamp returned by this module is a timezone-aware ISO-8601 string
with an explicit +10:00 suffix.  fetched_at is a UTC ISO-8601 string.

Failure policy
--------------
fetch() is best-effort: on ANY error (network, parse, missing data) it logs
a warning and returns None.  The caller falls through to isotonic-only
calibration silently — STPASA must never fail the coordinator.
"""
from __future__ import annotations

import contextlib
import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import aiohttp

from .nem_time import parse_nem_csv, to_nem_iso

_LOGGER = logging.getLogger(__name__)

STPASA_CURRENT_URL = "https://www.nemweb.com.au/Reports/CURRENT/Short_Term_PASA_Reports/"
STPASA_FILE_PATTERN = re.compile(r"PUBLIC_STPASA_.*\.ZIP$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class StpasaInterval:
    interval_datetime: str   # ISO-8601 NEM +10:00 (interval END, AEMO convention)
    run_datetime: str        # ISO-8601 NEM +10:00
    demand10: float          # MW
    demand50: float          # MW
    demand90: float          # MW
    surpluscapacity: float   # MW
    ss_solar_uigf: float     # MW
    ss_wind_uigf: float      # MW


@dataclass
class StpasaResult:
    region: str
    run_datetime: str        # ISO-8601 NEM +10:00 of the run (from first row)
    intervals: list[StpasaInterval] = field(default_factory=list)
    fetched_at: str = ""     # ISO-8601 UTC when fetched


# ---------------------------------------------------------------------------
# HTML link parser
# ---------------------------------------------------------------------------

class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flt(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _extract_csv_bytes(raw: bytes) -> bytes:
    """
    Extract the innermost CSV from an STPASA download.

    STPASA outer ZIPs contain an inner ZIP which contains the CSV.  Some
    archives nest only one level.  Walk the ZIP layers until a .CSV is found.
    """
    data = raw
    for _ in range(3):  # at most a couple of nesting levels
        if not zipfile.is_zipfile(io.BytesIO(data)):
            break
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            csv_members = [m for m in names if m.upper().endswith(".CSV")]
            if csv_members:
                with zf.open(sorted(csv_members)[0]) as f:
                    return f.read()
            zip_members = [m for m in names if m.upper().endswith(".ZIP")]
            if not zip_members:
                raise FileNotFoundError("No CSV or inner ZIP inside STPASA archive")
            with zf.open(sorted(zip_members)[-1]) as f:
                data = f.read()
    if zipfile.is_zipfile(io.BytesIO(data)):
        raise FileNotFoundError("No CSV found inside nested STPASA archive")
    return data


def _parse_regionsolution(raw_csv: bytes, region: str) -> StpasaResult | None:
    """
    Parse REGIONSOLUTION rows for *region* from an AEMO STPASA CSV.

    The CSV uses AEMO's standard layout: an "I" header row defines column
    names for the "D" data rows that follow.  We build a column-name → index
    map from the matching "I,...,REGIONSOLUTION,..." row so the parse is
    resilient to column re-ordering.
    """
    text = raw_csv.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))

    col_index: dict[str, int] = {}
    intervals: list[StpasaInterval] = []
    run_dt_iso: str | None = None

    for row in reader:
        if not row or len(row) < 3:
            continue
        rectype = row[0].strip()
        table = row[2].strip().upper() if len(row) > 2 else ""

        if table != "REGIONSOLUTION":
            continue

        if rectype == "I":
            # Header row — map column name (upper) → index
            col_index = {name.strip().upper(): i for i, name in enumerate(row)}
            continue

        if rectype != "D" or not col_index:
            continue

        def _get(name: str) -> str:
            idx = col_index.get(name)
            if idx is None or idx >= len(row):
                return ""
            return row[idx]

        if _get("REGIONID").strip() != region:
            continue

        interval_raw = _get("INTERVAL_DATETIME").strip()
        run_raw = _get("RUN_DATETIME").strip()
        if not interval_raw:
            continue

        interval_iso = to_nem_iso(parse_nem_csv(interval_raw))
        run_iso = to_nem_iso(parse_nem_csv(run_raw)) if run_raw else ""
        if run_dt_iso is None and run_iso:
            run_dt_iso = run_iso

        intervals.append(
            StpasaInterval(
                interval_datetime=interval_iso,
                run_datetime=run_iso,
                demand10=_flt(_get("DEMAND10")),
                demand50=_flt(_get("DEMAND50")),
                demand90=_flt(_get("DEMAND90")),
                surpluscapacity=_flt(_get("SURPLUSCAPACITY")),
                ss_solar_uigf=_flt(_get("SS_SOLAR_UIGF")),
                ss_wind_uigf=_flt(_get("SS_WIND_UIGF")),
            )
        )

    if not intervals:
        return None

    intervals.sort(key=lambda p: p.interval_datetime)
    return StpasaResult(
        region=region,
        run_datetime=run_dt_iso or "",
        intervals=intervals,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _parse_all_regions(raw_csv: bytes) -> dict[str, StpasaResult]:
    """
    Single-pass parse of an STPASA CSV → dict[region, StpasaResult].

    Reads every REGIONSOLUTION D-row, buckets by REGIONID, and builds one
    StpasaResult per region found (intervals sorted by interval_datetime).
    This is the multi-region equivalent of _parse_regionsolution: the STPASA
    ZIP holds all NEM regions, so a single pass populates every region store.
    """
    text = raw_csv.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))

    col_index: dict[str, int] = {}
    buckets: dict[str, list[StpasaInterval]] = {}
    run_dt_by_region: dict[str, str] = {}

    for row in reader:
        if not row or len(row) < 3:
            continue
        rectype = row[0].strip()
        table = row[2].strip().upper() if len(row) > 2 else ""

        if table != "REGIONSOLUTION":
            continue

        if rectype == "I":
            col_index = {name.strip().upper(): i for i, name in enumerate(row)}
            continue

        if rectype != "D" or not col_index:
            continue

        def _get(name: str) -> str:
            idx = col_index.get(name)
            if idx is None or idx >= len(row):
                return ""
            return row[idx]

        region = _get("REGIONID").strip()
        if not region:
            continue

        interval_raw = _get("INTERVAL_DATETIME").strip()
        run_raw = _get("RUN_DATETIME").strip()
        if not interval_raw:
            continue

        interval_iso = to_nem_iso(parse_nem_csv(interval_raw))
        run_iso = to_nem_iso(parse_nem_csv(run_raw)) if run_raw else ""
        if run_iso and region not in run_dt_by_region:
            run_dt_by_region[region] = run_iso

        buckets.setdefault(region, []).append(
            StpasaInterval(
                interval_datetime=interval_iso,
                run_datetime=run_iso,
                demand10=_flt(_get("DEMAND10")),
                demand50=_flt(_get("DEMAND50")),
                demand90=_flt(_get("DEMAND90")),
                surpluscapacity=_flt(_get("SURPLUSCAPACITY")),
                ss_solar_uigf=_flt(_get("SS_SOLAR_UIGF")),
                ss_wind_uigf=_flt(_get("SS_WIND_UIGF")),
            )
        )

    fetched_at = datetime.now(timezone.utc).isoformat()
    results: dict[str, StpasaResult] = {}
    for region, intervals in buckets.items():
        if not intervals:
            continue
        intervals.sort(key=lambda p: p.interval_datetime)
        results[region] = StpasaResult(
            region=region,
            run_datetime=run_dt_by_region.get(region, ""),
            intervals=intervals,
            fetched_at=fetched_at,
        )
    return results


# ---------------------------------------------------------------------------
# Async network client
# ---------------------------------------------------------------------------

class StpasaClient:
    """Async client for NEMWeb STPASA REGIONSOLUTION data."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        semaphore: "object | None" = None,
    ) -> None:
        self._session = session
        # Shared across all region coordinators to cap concurrent NEMWEB
        # requests. nullcontext when absent (e.g. unit tests).
        self._semaphore = semaphore

    def _gate(self):
        if self._semaphore is None:
            return contextlib.nullcontext()
        return self._semaphore

    async def _list_files(self) -> list[dict[str, str]]:
        async with self._gate():
            async with self._session.get(
                STPASA_CURRENT_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text(errors="ignore")

        parser = _LinkExtractor()
        parser.feed(html)

        files = []
        for href in parser.links:
            name = href.split("/")[-1]
            if STPASA_FILE_PATTERN.search(name):
                files.append({"name": name, "url": urljoin(STPASA_CURRENT_URL, href)})
        return files

    async def _fetch_bytes(self, url: str) -> bytes:
        async with self._gate():
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                resp.raise_for_status()
                return await resp.read()

    async def fetch_all_regions(self) -> dict[str, StpasaResult]:
        """
        Fetch the latest STPASA ZIP once and parse ALL regions from it.

        Returns dict[region_str, StpasaResult] — one entry per region found in
        the CSV.  Returns an empty dict on any error (best-effort, non-fatal):
        the STPASA ZIP holds every NEM region, so a single download serves all
        region stores.
        """
        try:
            files = await self._list_files()
            if not files:
                _LOGGER.warning("STPASA: no PUBLIC_STPASA ZIP files found at NEMWeb")
                return {}
            newest = sorted(files, key=lambda x: x["name"])[-1]
            raw = await self._fetch_bytes(newest["url"])
            csv_bytes = _extract_csv_bytes(raw)
            results = _parse_all_regions(csv_bytes)
            if not results:
                _LOGGER.warning(
                    "STPASA: no REGIONSOLUTION rows in %s", newest["name"]
                )
            return results
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("STPASA fetch failed (non-fatal): %s", exc)
            return {}

    async def fetch(self, region: str) -> StpasaResult | None:
        """Fetch latest STPASA for *region*.  Returns None on any error."""
        all_results = await self.fetch_all_regions()
        return all_results.get(region)
