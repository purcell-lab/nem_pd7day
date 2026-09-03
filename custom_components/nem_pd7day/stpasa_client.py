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
import functools
import logging
import re
import zipfile
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin

import aiohttp

from .const import NEMWEB_HEADERS
from .executor import ExecutorJob, run_in_executor
from .nem_time import parse_nem_csv, to_nem_iso
from .nemweb_retry import NemwebFetchError, classify_status, fetch_with_retry

_LOGGER = logging.getLogger(__name__)

# aiohttp transport failures are retryable, but nemweb_retry stays free of the
# aiohttp import so it can be unit tested, so the class is handed to it from
# here. Resolved defensively because unit tests stub the aiohttp module out
# with a MagicMock, where ClientError is not an exception class at all.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = tuple(
    candidate
    for candidate in (getattr(aiohttp, "ClientError", None),)
    if isinstance(candidate, type) and issubclass(candidate, BaseException)
)

STPASA_CURRENT_URL = "https://www.nemweb.com.au/Reports/CURRENT/Short_Term_PASA_Reports/"
STPASA_FILE_PATTERN = re.compile(r"PUBLIC_STPASA_.*\.ZIP$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class StpasaInterval:
    interval_datetime: str   # ISO-8601 NEM +10:00 (interval END, AEMO convention)
    run_datetime: str        # ISO-8601 NEM +10:00
    # None means the field was absent or unparseable in the source row, not
    # 0 MW. Zero is a meaningful reading for every one of these, so a missing
    # value must stay distinguishable from a real zero all the way through to
    # the sensor attribute and the calibration fit. See issue #43.
    demand10: float | None          # MW
    demand50: float | None          # MW
    demand90: float | None          # MW
    surpluscapacity: float | None   # MW
    ss_solar_uigf: float | None     # MW
    ss_wind_uigf: float | None      # MW


@dataclass
class StpasaResult:
    region: str
    run_datetime: str        # ISO-8601 NEM +10:00 of the run (from first row)
    intervals: list[StpasaInterval] = field(default_factory=list)
    fetched_at: str = ""     # ISO-8601 UTC when fetched
    is_stale: bool = False   # True when served beyond STPASA_CACHE_TTL (up to STPASA_STALE_TTL)


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


def _flt_opt(s: str) -> float | None:
    """Parse a numeric STPASA field, returning None when it is not a number.

    Used instead of ``_flt`` for the MW fields, where defaulting an absent or
    blank value to 0.0 would be indistinguishable from a genuine zero reading
    and would feed that zero into the calibration fit. See issue #43.
    """
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


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


def _extract_and_parse_all_regions(
    raw: bytes, now: datetime | None = None
) -> dict[str, StpasaResult]:
    """Unwrap the nested STPASA archive and parse every region.

    Runs in the executor as a single unit — see executor.py. *now* is the
    aware UTC instant stamped into fetched_at; defaults to the wall clock.
    """
    return _parse_all_regions(_extract_csv_bytes(raw), now=now)


def _parse_regionsolution(
    raw_csv: bytes, region: str, now: datetime | None = None
) -> StpasaResult | None:
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
                demand10=_flt_opt(_get("DEMAND10")),
                demand50=_flt_opt(_get("DEMAND50")),
                demand90=_flt_opt(_get("DEMAND90")),
                surpluscapacity=_flt_opt(_get("SURPLUSCAPACITY")),
                ss_solar_uigf=_flt_opt(_get("SS_SOLAR_UIGF")),
                ss_wind_uigf=_flt_opt(_get("SS_WIND_UIGF")),
            )
        )

    if not intervals:
        return None

    intervals.sort(key=lambda p: p.interval_datetime)
    return StpasaResult(
        region=region,
        run_datetime=run_dt_iso or "",
        intervals=intervals,
        fetched_at=(now or datetime.now(timezone.utc)).isoformat(),
    )


def _parse_all_regions(
    raw_csv: bytes, now: datetime | None = None
) -> dict[str, StpasaResult]:
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
                demand10=_flt_opt(_get("DEMAND10")),
                demand50=_flt_opt(_get("DEMAND50")),
                demand90=_flt_opt(_get("DEMAND90")),
                surpluscapacity=_flt_opt(_get("SURPLUSCAPACITY")),
                ss_solar_uigf=_flt_opt(_get("SS_SOLAR_UIGF")),
                ss_wind_uigf=_flt_opt(_get("SS_WIND_UIGF")),
            )
        )

    fetched_at = (now or datetime.now(timezone.utc)).isoformat()
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
        semaphore: Any | None = None,
        executor_job: ExecutorJob | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        # Injected clock for fetched_at, the pattern market_notice_client uses;
        # this module holds no hass reference so it cannot use dt_util (#109).
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # hass.async_add_executor_job — see executor.py.
        self._executor_job = executor_job
        # Shared across all region coordinators to cap concurrent NEMWEB
        # requests. nullcontext when absent (e.g. unit tests).
        self._semaphore = semaphore

    def _gate(self) -> AbstractAsyncContextManager[Any]:
        if self._semaphore is None:
            return contextlib.nullcontext()
        return self._semaphore

    async def _get_listing_html(self) -> str:
        """One attempt at the STPASA directory listing. Raises on a bad status."""
        async with self._session.get(
            STPASA_CURRENT_URL,
            headers=NEMWEB_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            classify_status(
                resp.status,
                url=STPASA_CURRENT_URL,
                headers=getattr(resp, "headers", None),
                # The CURRENT directory always exists, so a 404 on it means the
                # report path moved rather than that a file is unpublished.
                not_published_statuses=(),
            )
            return await resp.text(errors="ignore")

    async def _list_files(self) -> list[dict[str, str]]:
        # A single 403 here used to drop the whole STPASA cycle for every
        # region, since fetch_all_regions is best-effort and returns {} on any
        # error. NEMWEB's 403s are scattered rather than sustained, so a short
        # retry absorbs them. See issue #22.
        html = await fetch_with_retry(
            self._get_listing_html,
            url=STPASA_CURRENT_URL,
            label="STPASA directory listing",
            logger=_LOGGER,
            semaphore=self._semaphore,
            retryable_exceptions=_TRANSPORT_ERRORS,
        )
        if html is None:
            # fetch_with_retry has already logged the status, URL and exception,
            # so this carries no detail of its own. Raised rather than returned
            # empty so the caller does not mistake a failed fetch for a
            # directory that genuinely holds no STPASA files.
            raise NemwebFetchError(
                "STPASA directory listing unavailable after retry",
                retryable=False,
            )

        parser = _LinkExtractor()
        parser.feed(html)

        files: list[dict[str, str]] = []
        for href in parser.links:
            name = href.split("/")[-1]
            if STPASA_FILE_PATTERN.search(name):
                files.append({"name": name, "url": urljoin(STPASA_CURRENT_URL, href)})
        return files

    async def _get_bytes_once(self, url: str) -> bytes:
        """One attempt at a single STPASA ZIP. Raises on a bad status."""
        async with self._session.get(
            url,
            headers=NEMWEB_HEADERS,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            classify_status(
                resp.status,
                url=url,
                headers=getattr(resp, "headers", None),
                # The filename came from the listing moments earlier, so a 404
                # means it was rotated out mid-cycle. Not retryable, and the
                # next cycle picks up the new newest file.
                not_published_statuses=(404,),
            )
            return await resp.read()

    async def _fetch_bytes(self, url: str) -> bytes:
        raw = await fetch_with_retry(
            lambda: self._get_bytes_once(url),
            url=url,
            label="STPASA ZIP",
            logger=_LOGGER,
            semaphore=self._semaphore,
            retryable_exceptions=_TRANSPORT_ERRORS,
        )
        if raw is None:
            raise NemwebFetchError(
                "STPASA ZIP unavailable after retry", retryable=False
            )
        return raw

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
            # Nested-ZIP extraction plus a ~5.4 MB CSV parse across all five
            # regions is ~350 ms of CPU. Both steps go to the executor in one
            # hand-off so the loop is never held.
            results = await run_in_executor(
                self._executor_job,
                functools.partial(_extract_and_parse_all_regions, raw, now=self._clock()),
            )
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
