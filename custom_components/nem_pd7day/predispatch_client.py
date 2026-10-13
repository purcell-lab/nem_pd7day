"""Interconnectors, region prices and binding constraints from NEMWEB PredispatchIS.

AEMO's 30-minute pre-dispatch runs every half hour and covers each trading
period to the end of the next trading day, about 40 to 80 periods. One file
carries every region and interconnector, so it says, period by period:

- each interconnector's flow, its export and import limits, and the constraint
  setting each limit (D,PREDISPATCH,INTERCONNECTOR_SOLN);
- each region's price (D,PREDISPATCH,REGION_PRICES);
- every constraint equation's marginal value (D,PREDISPATCH,CONSTRAINT_SOLUTION),
  about 1,100 a period, of which the non-zero ones are binding.

That is what PD7DAY gives once or twice a day, refreshed every 30 minutes for
the next day and a half, and it is what decides whether a price spike in one
region reaches the next: an interconnector at a limit separates the two prices.

Directory: https://www.nemweb.com.au/Reports/Current/PredispatchIS_Reports/
Files:     PUBLIC_PREDISPATCHIS_YYYYMMDDHHMI_YYYYMMDDHHMISS.zip, the run's first
           period END and the time the file was written, both NEM time.
The zip is about 800 KB and the CSV inside about 8 MB, so it is unzipped and
parsed in the executor.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .const import PREDISPATCHIS_BASE_URL
from .constraint_client import BindingConstraint, ConstraintClient
from .executor import run_in_executor
from .nem_time import NEM_TZ

_LOGGER = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"(PUBLIC_PREDISPATCHIS_(\d{12})_(\d{14})\.zip)", re.IGNORECASE)
PERIOD = timedelta(minutes=30)
_TABLES = ("INTERCONNECTOR_SOLN", "REGION_PRICES", "CONSTRAINT_SOLUTION", "CASE_SOLUTION")
# A flow within this much of a limit is at the limit: max(1 MW, 0.5 % of it).
# Pre-dispatch often reports a zero marginal value for an interconnector held
# at a frequency-control limit, so the flow is compared with the limit instead.
_BOUND_MW = 1.0
_BOUND_FRACTION = 0.005


@dataclass(frozen=True)
class InterconnectorPeriod:
    """One interconnector in one period, in its nominal direction (AEMO's sign)."""

    interval_end: datetime     # NEM time, aware
    mwflow: float
    exportlimit: Optional[float]
    importlimit: Optional[float]
    marginalvalue: Optional[float]
    violationdegree: Optional[float]
    export_constraint: Optional[str]   # EXPORTGENCONID, the equation setting exportlimit
    import_constraint: Optional[str]   # IMPORTGENCONID, the equation setting importlimit

    @property
    def bound(self) -> Optional[str]:
        """"export" or "import" when the flow is at that limit, else None.

        When the two limits meet the flow is fixed, and which case it is
        depends on where they meet:
        - "closed" at 0 MW, so the link carries nothing either way: Project
          EnergyConnect (NSW1-SA1) held by NS_ZERO and SN_ZERO while it is not
          in service;
        - "pinned" anywhere else, so the link carries exactly that flow:
          Murraylink (V-S-MNSP1) held at 71 to 110 MW by V^SML_BUDP_3 (#243).
        """
        if self.exportlimit is not None and self.importlimit is not None:
            if self.exportlimit - self.importlimit <= max(_BOUND_MW, _BOUND_FRACTION * abs(self.exportlimit)):
                at_zero = abs(self.exportlimit) <= _BOUND_MW and abs(self.importlimit) <= _BOUND_MW
                return "closed" if at_zero else "pinned"
        for side, limit in (("export", self.exportlimit), ("import", self.importlimit)):
            if limit is None:
                continue
            tolerance = max(_BOUND_MW, _BOUND_FRACTION * abs(limit))
            at_limit = self.mwflow >= limit - tolerance if side == "export" else self.mwflow <= limit + tolerance
            if at_limit:
                return side
        return None

    @property
    def limit_constraint(self) -> Optional[str]:
        """The constraint holding the flow at its limit, when it is at one.

        For a closed or pinned link, the export one (NS_ZERO for NSW1-SA1,
        V^SML_BUDP_3 for Murraylink); both are published as export_constraint
        and import_constraint.
        """
        side = self.bound
        if side is None:
            return None
        return self.import_constraint if side == "import" else self.export_constraint


@dataclass(frozen=True)
class PredispatchSnapshot:
    run_at: datetime                                    # when AEMO solved it, NEM time
    source: str                                         # the PredispatchIS file name
    interconnectors: dict[str, tuple[InterconnectorPeriod, ...]]
    prices: dict[str, dict[datetime, float]]            # region -> period end -> RRP $/MWh
    constraints: dict[datetime, tuple[BindingConstraint, ...]]   # period end -> binding
    evaluated: int                                      # constraint rows in the pricing run

    @property
    def period_ends(self) -> list[datetime]:
        ends: set[datetime] = set(self.constraints)
        for periods in self.interconnectors.values():
            ends.update(p.interval_end for p in periods)
        return sorted(ends)


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nem_datetime(value: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(value or "", "%Y/%m/%d %H:%M:%S").replace(tzinfo=NEM_TZ)
    except ValueError:
        return None


def _pricing_rows(content: str) -> dict[str, list[dict[str, str]]]:
    """The pricing-run (INTERVENTION 0) rows of each wanted table, by header."""
    headers: dict[str, list[str]] = {}
    rows: dict[str, list[dict[str, str]]] = {table: [] for table in _TABLES}
    for parts in csv.reader(io.StringIO(content)):
        if len(parts) < 4 or parts[1] != "PREDISPATCH" or parts[2] not in rows:
            continue
        if parts[0] == "I":
            headers[parts[2]] = parts[4:]
        elif parts[0] == "D" and parts[2] in headers:
            row = dict(zip(headers[parts[2]], parts[4:]))
            if row.get("INTERVENTION", "0").strip() == "0":
                rows[parts[2]].append(row)
    return rows


def _interconnectors(rows: list[dict[str, str]]) -> dict[str, tuple[InterconnectorPeriod, ...]]:
    found: dict[str, list[InterconnectorPeriod]] = {}
    for row in rows:
        end, flow = _nem_datetime(row.get("DATETIME")), _number(row.get("MWFLOW"))
        if end is None or flow is None:
            continue
        found.setdefault(row.get("INTERCONNECTORID", "").strip(), []).append(
            InterconnectorPeriod(
                interval_end=end,
                mwflow=flow,
                exportlimit=_number(row.get("EXPORTLIMIT")),
                importlimit=_number(row.get("IMPORTLIMIT")),
                marginalvalue=_number(row.get("MARGINALVALUE")),
                violationdegree=_number(row.get("VIOLATIONDEGREE")),
                export_constraint=(row.get("EXPORTGENCONID") or "").strip() or None,
                import_constraint=(row.get("IMPORTGENCONID") or "").strip() or None,
            )
        )
    return {ic: tuple(sorted(periods, key=lambda p: p.interval_end)) for ic, periods in found.items() if ic}


def _prices(rows: list[dict[str, str]]) -> dict[str, dict[datetime, float]]:
    prices: dict[str, dict[datetime, float]] = {}
    for row in rows:
        end, rrp = _nem_datetime(row.get("DATETIME")), _number(row.get("RRP"))
        if end is not None and rrp is not None:
            prices.setdefault(row.get("REGIONID", "").strip(), {})[end] = rrp
    return prices


def _constraints(rows: list[dict[str, str]]) -> dict[datetime, tuple[BindingConstraint, ...]]:
    """Binding (non-zero marginal value) or violated equations per period, largest cost first."""
    found: dict[datetime, list[BindingConstraint]] = {}
    for row in rows:
        end = _nem_datetime(row.get("DATETIME"))
        if end is None:
            continue
        bucket = found.setdefault(end, [])
        marginal = _number(row.get("MARGINALVALUE")) or 0.0
        violation = _number(row.get("VIOLATIONDEGREE")) or 0.0
        if marginal == 0.0 and violation == 0.0:
            continue
        bucket.append(BindingConstraint(
            constraint_id=row.get("CONSTRAINTID", "").strip(),
            rhs=_number(row.get("RHS")),
            lhs=_number(row.get("LHS")),
            marginal_value=marginal,
            violation_degree=violation,
        ))
    return {
        end: tuple(sorted(rows_, key=lambda c: (-abs(c.marginal_value), c.constraint_id)))
        for end, rows_ in found.items()
    }


def _run_at(case_rows: list[dict[str, str]], source: str) -> Optional[datetime]:
    """When the run was solved: CASE_SOLUTION's LASTCHANGED, else the file name."""
    for row in case_rows:
        when = _nem_datetime(row.get("LASTCHANGED"))
        if when is not None:
            return when
    match = _FILENAME_RE.search(source)
    if match:
        return datetime.strptime(match.group(3), "%Y%m%d%H%M%S").replace(tzinfo=NEM_TZ)
    return None


def parse_predispatch(content: str, source: str = "") -> Optional[PredispatchSnapshot]:
    """One PredispatchIS CSV, or None when it has no interconnector rows.

    Columns are read from each table's own I header, and only the pricing run
    (INTERVENTION 0) is kept, as for the dispatch price and constraints.
    """
    rows = _pricing_rows(content)
    interconnectors = _interconnectors(rows["INTERCONNECTOR_SOLN"])
    run_at = _run_at(rows["CASE_SOLUTION"], source)
    if not interconnectors or run_at is None:
        return None
    return PredispatchSnapshot(
        run_at=run_at,
        source=source,
        interconnectors=interconnectors,
        prices=_prices(rows["REGION_PRICES"]),
        constraints=_constraints(rows["CONSTRAINT_SOLUTION"]),
        evaluated=len(rows["CONSTRAINT_SOLUTION"]),
    )


def latest_file(html: str) -> Optional[str]:
    """The newest PredispatchIS file in a directory listing."""
    names = {m.group(1): (m.group(2), m.group(3)) for m in _FILENAME_RE.finditer(html)}
    return max(names, key=lambda n: (names[n], n)) if names else None


class PredispatchClient(ConstraintClient):
    """Fetches the newest PredispatchIS file.

    Same two requests as the DispatchIS client, the directory listing and the
    zip, through the shared NEMWEB gate; the coordinator decides when a new run
    can be expected, so the listing is not read every five minutes.
    """

    BASE_URL = PREDISPATCHIS_BASE_URL
    LABEL = "PredispatchIS"
    NO_ROWS = "no D,PREDISPATCH,INTERCONNECTOR_SOLN rows"

    def _latest(self, html: str) -> Optional[str]:
        return latest_file(html)

    async def _parse_off_loop(self, content: str, name: str) -> Optional[PredispatchSnapshot]:
        return await run_in_executor(self._executor_job, parse_predispatch, content, name)
