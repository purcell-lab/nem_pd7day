"""DUID local price adjustments from NEMWEB: dispatch and the 5-minute pre-dispatch (#275).

Where a constraint separates a unit from its region, NEMDE publishes a local
price adjustment for its DUID: the unit's local price is the region's original
price (ROP) plus the adjustment, between the market floor and the cap (AEMO,
Guide to mis-pricing information). Two public feeds carry it:

- D,DISPATCH,LOCAL_PRICE in each DispatchIS file, the dispatch interval just
  run: SETTLEMENTDATE, DUID, LOCAL_PRICE_ADJUSTMENT, LOCALLY_CONSTRAINED.
  The constraint client already reads that file every five minutes, so the
  rows ride on that fetch (constraint_client.ConstraintClient).
- D,P5MIN,LOCAL_PRICE in each P5MIN file, the next twelve five-minute
  intervals of the run: RUN_DATETIME, DUID, INTERVAL_DATETIME, the same two
  values. The region's ROP for the same run and interval is in
  D,P5MIN,REGIONSOLUTION.

AEMO publishes a row only where the adjustment is non-zero, so an absent row
usually is a zero. That is not established for every row of every feed, so an
absent row is kept as absent, never filled with 0. A file whose LOCAL_PRICE
table is missing altogether says nothing about any DUID. Nothing is carried
across a gap or past the run's last interval.

All times are NEM time, UTC+10 without daylight saving; INTERVAL_DATETIME and
SETTLEMENTDATE are interval ends, and a forecast point's ``time`` is the
interval start, as every other forecast of this integration.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from .const import MARKET_PRICE_CAP_MWH_BY_FY, MARKET_PRICE_FLOOR_MWH
from .nem_time import NEM_TZ

INTERVAL = timedelta(minutes=5)
_P5_FILE_RE = re.compile(r"(PUBLIC_P5MIN_(\d{12})_(\d{14})\.zip)", re.IGNORECASE)
# A DUID is up to ten characters of upper-case letters, digits and underscores
# in the MMS data model.
DUID_RE = re.compile(r"^[A-Z0-9_]{1,10}$")

STATUS_PUBLISHED = "published"
STATUS_ABSENT = "absent"                 # file read, LOCAL_PRICE table present, no row for the DUID
STATUS_TABLE_MISSING = "table_missing"   # the file has no LOCAL_PRICE table at all
STATUS_CONFLICT = "conflicting_rows"     # two different rows for one DUID and interval
STATUS_NO_DATA = "no_data"               # no file read yet
STATUS_STALE = "stale"                   # the newest file is too old to stand for now
STATUS_EXPIRED = "expired"               # every interval of the run has ended


@dataclass(frozen=True)
class LocalPriceRow:
    adjustment_mwh: float
    locally_constrained: Optional[int]


@dataclass(frozen=True)
class DispatchLocalPrices:
    """One DispatchIS interval: the DUIDs with a published adjustment, and each region's ROP."""

    interval_end: datetime
    source: str
    table_present: bool
    rows: dict[str, LocalPriceRow]
    rop: dict[str, float]
    retrieved_at: Optional[datetime] = None


@dataclass(frozen=True)
class P5LocalPriceRun:
    """One P5MIN run: its interval ends, the published rows and each region's ROP."""

    run_at: datetime
    source: str
    table_present: bool
    interval_ends: tuple[datetime, ...]
    rows: dict[str, dict[datetime, LocalPriceRow]]
    rop: dict[str, dict[datetime, float]]
    conflicts: frozenset[tuple[str, datetime]] = field(default_factory=frozenset)
    retrieved_at: Optional[datetime] = None


def parse_duids(text: Any) -> tuple[list[str], list[str]]:
    """(valid, invalid) DUIDs from a comma, space or newline separated string or a list."""
    items = text if isinstance(text, (list, tuple)) else re.split(r"[\s,;]+", str(text or ""))
    valid: list[str] = []
    invalid: list[str] = []
    for item in items:
        duid = str(item).strip().upper()
        if not duid:
            continue
        target = valid if DUID_RE.match(duid) else invalid
        if duid not in target:
            target.append(duid)
    return valid, invalid


def _nem(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S").replace(tzinfo=NEM_TZ)
    except (AttributeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row(record: dict[str, str]) -> Optional[LocalPriceRow]:
    adjustment = _float(record.get("LOCAL_PRICE_ADJUSTMENT"))
    if adjustment is None:
        return None
    constrained = _float(record.get("LOCALLY_CONSTRAINED"))
    return LocalPriceRow(adjustment, int(constrained) if constrained is not None else None)


def _records(content: str, report: str, tables: tuple[str, ...]) -> dict[str, list[dict[str, str]]]:
    """Each wanted table's D rows as dicts, keyed by the file's own I header.

    Lines of other tables are skipped before the CSV parse: a P5MIN file holds
    about 13,000 CONSTRAINTSOLUTION rows this module does not read.
    """
    wanted = tuple(f"{kind},{report},{table}," for kind in ("I", "D") for table in tables)
    headers: dict[str, list[str]] = {}
    out: dict[str, list[dict[str, str]]] = {}
    for line in content.splitlines():
        if not line.startswith(wanted):
            continue
        parts = next(csv.reader([line]))
        table = parts[2]
        if parts[0] == "I":
            headers[table] = parts
            out.setdefault(table, [])
        elif table in headers:
            out[table].append(dict(zip(headers[table][4:], parts[4:])))
    return out


def parse_dispatch_local_prices(
    content: str, source: str = "", retrieved_at: Optional[datetime] = None
) -> Optional[DispatchLocalPrices]:
    """The LOCAL_PRICE rows and pricing-run ROPs of one DispatchIS CSV, or None without a PRICE table."""
    tables = _records(content, "DISPATCH", ("LOCAL_PRICE", "PRICE"))
    interval_end: Optional[datetime] = None
    rop: dict[str, float] = {}
    for record in tables.get("PRICE", []):
        if record.get("INTERVENTION", "0").strip() != "0":
            continue
        interval_end = interval_end or _nem(record.get("SETTLEMENTDATE", ""))
        value = _float(record.get("ROP"))
        if value is not None:
            rop[record.get("REGIONID", "").strip()] = value
    if interval_end is None:
        return None
    rows: dict[str, LocalPriceRow] = {}
    for record in tables.get("LOCAL_PRICE", []):
        if _nem(record.get("SETTLEMENTDATE", "")) != interval_end:
            continue
        row = _row(record)
        if row is not None:
            rows[record.get("DUID", "").strip().upper()] = row
    return DispatchLocalPrices(interval_end, source, "LOCAL_PRICE" in tables, rows, rop, retrieved_at)


def parse_p5min_local_prices(
    content: str, source: str = "", retrieved_at: Optional[datetime] = None
) -> Optional[P5LocalPriceRun]:
    """One P5MIN run's LOCAL_PRICE rows and pricing-run ROPs, or None without a REGIONSOLUTION table.

    Two rows for one DUID and interval that disagree are a conflict: neither
    is used, and the pair is reported.
    """
    tables = _records(content, "P5MIN", ("LOCAL_PRICE", "REGIONSOLUTION"))
    run_at: Optional[datetime] = None
    rop: dict[str, dict[datetime, float]] = {}
    for record in tables.get("REGIONSOLUTION", []):
        if record.get("INTERVENTION", "0").strip() != "0":
            continue
        run_at = run_at or _nem(record.get("RUN_DATETIME", ""))
        end, value = _nem(record.get("INTERVAL_DATETIME", "")), _float(record.get("ROP"))
        if end is not None and value is not None:
            rop.setdefault(record.get("REGIONID", "").strip(), {})[end] = value
    if run_at is None:
        return None
    ends = tuple(sorted({end for series in rop.values() for end in series}))
    rows: dict[str, dict[datetime, LocalPriceRow]] = {}
    conflicts: set[tuple[str, datetime]] = set()
    for record in tables.get("LOCAL_PRICE", []):
        end, row = _nem(record.get("INTERVAL_DATETIME", "")), _row(record)
        if end is None or row is None or _nem(record.get("RUN_DATETIME", "")) != run_at:
            continue
        duid = record.get("DUID", "").strip().upper()
        held = rows.setdefault(duid, {}).get(end)
        if held is not None and held != row:
            conflicts.add((duid, end))
        rows[duid][end] = row
    for duid, end in conflicts:
        rows[duid].pop(end, None)
    return P5LocalPriceRun(
        run_at, source, "LOCAL_PRICE" in tables, ends, rows, rop, frozenset(conflicts), retrieved_at
    )


def market_price_limits(interval_end: datetime) -> tuple[float, float]:
    """(floor, cap) in $/MWh for the financial year of ``interval_end``.

    The cap is set per financial year (1 July); a year not in the table takes
    the nearest one listed, so a stale table caps at the last known value
    rather than not at all.
    """
    local = interval_end.astimezone(NEM_TZ) - timedelta(minutes=5)
    fy = local.year + (1 if local.month >= 7 else 0)
    years = sorted(MARKET_PRICE_CAP_MWH_BY_FY)
    nearest = min(years, key=lambda y: (abs(y - fy), -y))
    return MARKET_PRICE_FLOOR_MWH, MARKET_PRICE_CAP_MWH_BY_FY[nearest]


def local_price_mwh(rop: float, adjustment: float, interval_end: datetime) -> float:
    """clip(ROP + adjustment, floor, cap): the adjustment itself is never clipped."""
    floor, cap = market_price_limits(interval_end)
    return min(max(rop + adjustment, floor), cap)


def dispatch_status(prices: Optional[DispatchLocalPrices], duid: str, now: datetime, max_age: timedelta) -> str:
    if prices is None:
        return STATUS_NO_DATA
    if now - prices.interval_end > max_age:
        return STATUS_STALE
    if not prices.table_present:
        return STATUS_TABLE_MISSING
    return STATUS_PUBLISHED if duid in prices.rows else STATUS_ABSENT


@dataclass
class ForecastSeries:
    """A DUID's P5MIN forecast: only the published intervals from the run's first, no gap crossed."""

    status: str
    points: list[dict[str, Any]]          # {time: interval start ISO, value: $/kWh}
    interval_ends: list[str]               # the source interval ends, parallel to points
    later: list[dict[str, Any]]            # published rows after the first gap, diagnostics only
    published: int                         # intervals ahead with a row, gap or not
    intervals: int                         # intervals of the run still to end
    forecast_end: Optional[str]            # the end of the last point: the forecast stops here
    horizon_end: Optional[str] = None      # the end of the run's last interval


def forecast_series(run: Optional[P5LocalPriceRun], duid: str, now: datetime, max_age: timedelta) -> ForecastSeries:
    """The run's intervals still to end, in order, up to the first without a row.

    An interval that has ended is dropped, not a gap; once every interval has
    ended the run says nothing more (STATUS_EXPIRED).
    """
    if run is None:
        return ForecastSeries(STATUS_NO_DATA, [], [], [], 0, 0, None)
    if now - run.run_at > max_age:
        return ForecastSeries(STATUS_STALE, [], [], [], 0, len(run.interval_ends), None)
    if not run.table_present:
        return ForecastSeries(STATUS_TABLE_MISSING, [], [], [], 0, len(run.interval_ends), None)
    ahead = [end for end in run.interval_ends if end > now]
    if not ahead:
        return ForecastSeries(STATUS_EXPIRED, [], [], [], 0, 0, None)
    rows = run.rows.get(duid, {})
    points: list[dict[str, Any]] = []
    ends: list[str] = []
    later: list[dict[str, Any]] = []
    gap = False
    for end in ahead:
        row = rows.get(end)
        if row is None:
            gap = True
            continue
        point = {"time": (end - INTERVAL).isoformat(), "value": round(row.adjustment_mwh / 1000.0, 6)}
        if gap:
            later.append(point)
        else:
            points.append(point)
            ends.append(end.isoformat())
    if any(d == duid for d, _ in run.conflicts):
        status = STATUS_CONFLICT
    else:
        status = STATUS_PUBLISHED if points else STATUS_ABSENT
    return ForecastSeries(status, points, ends, later, len(points) + len(later), len(ahead),
                          ends[-1] if ends else None, ahead[-1].isoformat())


def latest_p5_file(html: str) -> Optional[str]:
    """The newest P5MIN file in a directory listing."""
    names = {m.group(1): (m.group(2), m.group(3)) for m in _P5_FILE_RE.finditer(html)}
    return max(names, key=lambda n: (names[n], n)) if names else None
