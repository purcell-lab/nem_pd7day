"""DUID local price adjustments from NEMWEB: dispatch and the 5-minute pre-dispatch (#275).

The 30-minute pre-dispatch rows are read in predispatch_local_price (#287).

Where a constraint separates a unit from its region, NEMDE publishes a local
price adjustment for its DUID: the unit's local price is the region's original
price (ROP) plus the adjustment (AEMO, Guide to mis-pricing information),
published raw: the market floor and cap apply to the regional price, so they
are not applied here. Two public feeds carry it:

- D,DISPATCH,LOCAL_PRICE in each DispatchIS file, the dispatch interval just
  run: SETTLEMENTDATE, DUID, LOCAL_PRICE_ADJUSTMENT, LOCALLY_CONSTRAINED.
  The constraint client already reads that file every five minutes, so the
  rows ride on that fetch (constraint_client.ConstraintClient).
- D,P5MIN,LOCAL_PRICE in each P5MIN file, the next twelve five-minute
  intervals of the run: RUN_DATETIME, DUID, INTERVAL_DATETIME, the same two
  values. The region's ROP for the same run and interval is in
  D,P5MIN,REGIONSOLUTION.

AEMO publishes a row only where the adjustment is non-zero, so a DUID without
a row in a fresh file that has the LOCAL_PRICE table is taken as 0, with the
status ``zero_by_omission`` so an inferred 0 stays apart from a published one.
A file whose LOCAL_PRICE table is missing altogether, a stale file and a
conflicting interval say nothing, and are never filled. Nothing is carried
past the run's last interval.

All times are NEM time, UTC+10 without daylight saving; INTERVAL_DATETIME and
SETTLEMENTDATE are interval ends, and a forecast point's ``time`` is the
interval start, as every other forecast of this integration.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Optional

from .const import MARKET_PRICE_CAP_MWH_BY_FY, MARKET_PRICE_FLOOR_MWH
from .nem_time import NEM_TZ

INTERVAL = timedelta(minutes=5)
_P5_FILE_RE = re.compile(r"(PUBLIC_P5MIN_(\d{12})_(\d{14})\.zip)", re.IGNORECASE)
# A DUID is up to ten characters in the MMS data model: upper-case letters,
# digits, and in registered DUIDs also _, -, # and / (BARRON-1, W/HOE#1).
DUID_RE = re.compile(r"^[A-Z0-9_#/-]{1,10}$")
# A connection point (TNI) is up to ten letters and digits (QWIV1, NBAY1).
TNI_RE = re.compile(r"^[A-Z0-9_]{2,10}$")

STATUS_PUBLISHED = "published"
# File read, LOCAL_PRICE table present, no row for the DUID: AEMO publishes a
# row only where the adjustment is not zero, so the value is taken as 0.
STATUS_ZERO = "zero_by_omission"
STATUS_TABLE_MISSING = "table_missing"   # the file has no LOCAL_PRICE table at all
STATUS_CONFLICT = "conflicting_rows"     # two different rows for one DUID and interval
STATUS_NO_DATA = "no_data"               # no file read yet
STATUS_STALE = "stale"                   # the newest file is too old to stand for now
STATUS_EXPIRED = "expired"               # every interval of the run has ended
# The DispatchIS file had no LOCAL_PRICE table, a gap in AEMO's publishing (about
# 2% of files, early evening), so the P5MIN run covering the interval stands in.
STATUS_P5_FALLBACK = "p5min_fallback"


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
    # Set when the rows are a P5MIN run's for this interval (with_p5_fallback):
    # its file, and the DUIDs whose rows conflicted there.
    fallback_source: Optional[str] = None
    fallback_conflicts: frozenset[str] = field(default_factory=frozenset)


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


def parse_duids(text: Any, pattern: re.Pattern[str] = DUID_RE) -> tuple[list[str], list[str]]:
    """(valid, invalid) DUIDs from a comma, space or newline separated string or a list."""
    items = text if isinstance(text, (list, tuple)) else re.split(r"[\s,;]+", str(text or ""))
    valid: list[str] = []
    invalid: list[str] = []
    for item in items:
        duid = str(item).strip().upper()
        if not duid:
            continue
        target = valid if pattern.match(duid) else invalid
        if duid not in target:
            target.append(duid)
    return valid, invalid


def parse_tnis(text: Any) -> tuple[list[str], list[str]]:
    """(valid, invalid) transmission node identifiers, as parse_duids."""
    return parse_duids(text, TNI_RE)


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


def local_price_mwh(rop: float, adjustment: float) -> float:
    """ROP + adjustment, raw: never clipped to the market floor or cap, which
    apply to the regional price, so a deep constraint shows its full depth."""
    return round(rop + adjustment, 4)


def with_p5_fallback(
    prices: Optional[DispatchLocalPrices], run: Optional[P5LocalPriceRun],
) -> Optional[DispatchLocalPrices]:
    """The dispatch interval's rows, from the P5MIN run covering it when the file has no LOCAL_PRICE table.

    AEMO now and then publishes a DispatchIS file without the table at all,
    never with an empty one (11 of 576 files over 30 and 31 Oct 2026, all
    early evening), so its absence says nothing about the adjustments. The
    P5MIN run solved for the same interval keeps its table; its adjustments
    stand in, flagged ``p5min_fallback``, while the ROP stays the dispatch
    file's own. Where the file has the table, or no run covers the interval,
    the file is returned as it is.
    """
    if prices is None or prices.table_present or run is None or not run.table_present:
        return prices
    end = prices.interval_end
    if end not in run.interval_ends:
        return prices
    conflicts = frozenset(duid for duid, at in run.conflicts if at == end)
    rows = {duid: series[end] for duid, series in run.rows.items() if end in series and duid not in conflicts}
    return replace(prices, table_present=True, rows=rows, fallback_source=run.source, fallback_conflicts=conflicts)


def dispatch_status(prices: Optional[DispatchLocalPrices], duid: str, now: datetime, max_age: timedelta) -> str:
    if prices is None:
        return STATUS_NO_DATA
    if now - prices.interval_end > max_age:
        return STATUS_STALE
    if not prices.table_present:
        return STATUS_TABLE_MISSING
    if prices.fallback_source is not None:
        return STATUS_CONFLICT if duid in prices.fallback_conflicts else STATUS_P5_FALLBACK
    return STATUS_PUBLISHED if duid in prices.rows else STATUS_ZERO


def dispatch_adjustment(
    prices: Optional[DispatchLocalPrices], duid: str, now: datetime, max_age: timedelta,
) -> tuple[str, Optional[float]]:
    """(status, $/MWh): the row's adjustment, 0 without one where that means 0, else unknown."""
    status = dispatch_status(prices, duid, now, max_age)
    if status in (STATUS_PUBLISHED, STATUS_ZERO, STATUS_P5_FALLBACK):
        row = prices.rows.get(duid)  # type: ignore[union-attr]
        return status, row.adjustment_mwh if row is not None else 0.0
    return status, None


@dataclass
class ForecastSeries:
    """A DUID's P5MIN forecast: the run's intervals still to end, up to a conflict.

    An interval without a row, in a run whose LOCAL_PRICE table is present,
    is 0 and listed in ``inferred``: AEMO publishes only non-zero rows.
    """

    status: str
    points: list[dict[str, Any]]          # {time: interval start ISO, value: $/kWh}
    interval_ends: list[str]               # the source interval ends, parallel to points
    later: list[dict[str, Any]]            # published rows after a conflicting interval, diagnostics only
    published: int                         # intervals ahead with a row
    intervals: int                         # intervals of the run still to end
    forecast_end: Optional[str]            # the end of the last point: the forecast stops here
    horizon_end: Optional[str] = None      # the end of the run's last interval
    inferred: list[str] = field(default_factory=list)  # point times taken as 0 for want of a row


def forecast_series(run: Optional[P5LocalPriceRun], duid: str, now: datetime, max_age: timedelta) -> ForecastSeries:
    """The run's intervals still to end, in order, up to the first conflicting interval.

    An interval that has ended is dropped; once every interval has ended the
    run says nothing more (STATUS_EXPIRED). An interval without a row is 0; a
    stale run, or one without the LOCAL_PRICE table, gives no points at all,
    and nothing is carried past the run's last interval.
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
    inferred: list[str] = []
    gap = False
    for end in ahead:
        if (duid, end) in run.conflicts:
            gap = True
            continue
        row = rows.get(end)
        point: dict[str, Any] = {
            "time": (end - INTERVAL).isoformat(), "value": round(row.adjustment_mwh / 1000.0, 6) if row else 0.0,
        }
        if gap:
            if row is not None:
                later.append(point)
            continue
        points.append(point)
        ends.append(end.isoformat())
        if row is None:
            inferred.append(point["time"])
    published = sum(1 for end in ahead if end in rows)
    if gap:
        status = STATUS_CONFLICT
    else:
        status = STATUS_PUBLISHED if published else STATUS_ZERO
    return ForecastSeries(status, points, ends, later, published, len(ahead),
                          ends[-1] if ends else None, ahead[-1].isoformat(), inferred)


def latest_p5_file(html: str) -> Optional[str]:
    """The newest P5MIN file in a directory listing."""
    names = {m.group(1): (m.group(2), m.group(3)) for m in _P5_FILE_RE.finditer(html)}
    return max(names, key=lambda n: (names[n], n)) if names else None
