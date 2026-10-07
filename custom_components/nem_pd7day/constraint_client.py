"""Binding network constraints from NEMWEB DispatchIS.

Each DispatchIS file carries one D,DISPATCH,CONSTRAINT row per constraint
equation NEMDE evaluated for the interval, about 1,100 of them. A constraint is
binding when its marginal value is non-zero: relieving it by 1 MW would change
the cost of dispatch by that many dollars per MWh. A violated constraint is
included too.

Directory: https://www.nemweb.com.au/Reports/Current/DispatchIS_Reports/
Files:     PUBLIC_DISPATCHIS_YYYYMMDDHHMI_<seq>.zip, YYYYMMDDHHMI the interval END.

The region a constraint belongs to is read from its ID, following AEMO's
Constraint Naming Guidelines (SC_CM_04): the first character is the region
(Q, N, V, S, T), or I for interconnector plant, F_ marks FCAS and NRM_ negative
residue management. AEMO publishes no region with the equation in DispatchIS,
so the mapping is a naming convention, not data; an ID that follows no
convention is published as unassigned rather than guessed.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from .const import DISPATCHIS_BASE_URL, NEMWEB_HEADERS
from .executor import ExecutorJob, run_in_executor
from .nem_time import NEM_TZ
from .nemweb_retry import classify_status, fetch_with_retry

_LOGGER = logging.getLogger(__name__)

# See tradingis_client.py: resolved defensively because unit tests stub aiohttp.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = tuple(
    candidate
    for candidate in (getattr(aiohttp, "ClientError", None),)
    if isinstance(candidate, type) and issubclass(candidate, BaseException)
)

_FILENAME_RE = re.compile(r"(PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip)", re.IGNORECASE)
INTERVAL = timedelta(minutes=5)

REGIONS = ("QLD1", "NSW1", "VIC1", "SA1", "TAS1")
_LETTER = {"Q": "QLD1", "N": "NSW1", "V": "VIC1", "S": "SA1", "T": "TAS1"}
_MAINLAND = ("QLD1", "NSW1", "VIC1", "SA1")
# FCAS region abbreviations from the naming guideline, longest first.
_FCAS_AREAS = (
    ("MAIN", _MAINLAND),
    ("ESTN", ("QLD1", "NSW1", "VIC1")),
    ("EST", ("QLD1", "NSW1", "VIC1")),
    ("STHN", ("NSW1", "VIC1", "SA1")),
    ("STH", ("NSW1", "VIC1", "SA1")),
)
_CAUSES = {">": "thermal", ":": "stability", "^": "voltage_stability", "+": "frequency_control"}
# Reserved first characters (naming guideline, "Forbidden and reserved").
_RESERVED = {"#": "quick", "$": "bid", "@": "control_room", "~": "mandatory_restriction"}
_REGION_ID = re.compile(r"(QLD1|NSW1|VIC1|SA1|TAS1)")
# Two region letters naming a flow, optionally the link: VSML (Murraylink),
# TVBL (Basslink), NQTE (Terranora), or bare as in SV_470_DYN.
_PAIR = re.compile(r"^([QNVST])([QNVST])(?:ML|BL|TE)?(?=[_>:^+])")
# Region, underscore, region: V_T_NIL_FCSPS, a Victoria to Tasmania equation.
_UNDERSCORE_PAIR = re.compile(r"^([QNVST])_([QNVST])_")
_SINGLE = re.compile(r"^([QNVST])([>:^+_]{1,2})")


@dataclass(frozen=True)
class ConstraintInfo:
    """What a constraint ID says about itself."""

    regions: tuple[str, ...]
    category: str              # network, interconnector, fcas, negative_residue, ...
    cause: Optional[str]       # thermal, stability, voltage_stability, frequency_control
    co_optimised: bool         # a doubled cause character
    system_normal: bool        # NIL: no outage


def _cause(chars: str) -> tuple[Optional[str], bool]:
    if not chars or chars[0] not in _CAUSES:
        return None, False
    return _CAUSES[chars[0]], len(chars) > 1 and chars[1] == chars[0]


def describe_constraint(constraint_id: str) -> ConstraintInfo:
    """Region, category and cause read from an AEMO constraint ID."""
    cid = constraint_id.strip()
    nil = "NIL" in re.split(r"[_>:^+\-]", cid)

    def info(regions: tuple[str, ...], category: str, cause_chars: str = "") -> ConstraintInfo:
        cause, co_opt = _cause(cause_chars)
        return ConstraintInfo(tuple(r for r in REGIONS if r in regions), category, cause, co_opt, nil)

    if cid.startswith(("DSNAP_", "DATASNAP_", "DATA_")):
        return info((), "data_snapshot")
    if cid.startswith("NRM_"):
        return info(tuple(_REGION_ID.findall(cid)), "negative_residue")
    if cid.startswith("F_"):
        rest = cid[2:]
        cause_chars = re.match(r"[A-Z]*([>:^+_]*)", rest).group(1)  # type: ignore[union-attr]
        for code, regions in _FCAS_AREAS:
            if rest.startswith(code):
                return info(regions, "fcas", cause_chars)
        if rest[:1] == "I" and rest[1:2] in "+_":
            return info(REGIONS, "fcas", cause_chars)
        return info((_LETTER[rest[0]],) if rest[:1] in _LETTER else (), "fcas", cause_chars)
    if cid[:1] in _RESERVED:
        return info(tuple(_REGION_ID.findall(cid)), _RESERVED[cid[0]])
    for prefix, category in (("NC_", "non_conformance"), ("NSA_", "network_support"), ("CA_", "constraint_automation")):
        if cid.startswith(prefix):
            token = cid[len(prefix):].split("_", 1)[0]
            region = _LETTER.get(token) or (token if token in REGIONS else None)
            return info((region,) if region else (), category)
    if cid[:1] == "I" and cid[1:2] in "-_+":
        letters = re.match(r"[A-Z]*", cid[2:]).group(0)  # type: ignore[union-attr]
        return info(tuple(_LETTER[c] for c in letters if c in _LETTER), "interconnector")
    if match := _PAIR.match(cid):
        tail = cid[match.end():]
        return info((_LETTER[match.group(1)], _LETTER[match.group(2)]), "interconnector", tail[:2])
    if (match := _UNDERSCORE_PAIR.match(cid)) and match.group(1) != match.group(2):
        return info((_LETTER[match.group(1)], _LETTER[match.group(2)]), "interconnector")
    if match := _SINGLE.match(cid):
        return info((_LETTER[match.group(1)],), "network", match.group(2))
    return info((), "other")


@dataclass(frozen=True)
class BindingConstraint:
    constraint_id: str
    rhs: Optional[float]
    lhs: Optional[float]
    marginal_value: float      # $/MWh, AEMO's sign
    violation_degree: float


@dataclass(frozen=True)
class ConstraintSnapshot:
    interval_end: datetime     # NEM time, aware
    run_no: Optional[int]
    source: str                # the DispatchIS file name
    constraints: tuple[BindingConstraint, ...]
    evaluated: int             # every CONSTRAINT row for the interval, binding or not


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_constraints(content: str, source: str = "") -> Optional[ConstraintSnapshot]:
    """The binding constraints in one DispatchIS CSV, or None if it has none.

    Columns come from the file's own I,DISPATCH,CONSTRAINT header rather than
    fixed positions, so an added column does not shift the values. Only the
    pricing run (INTERVENTION 0) is read, as for the dispatch price.
    """
    header: Optional[list[str]] = None
    rows: list[BindingConstraint] = []
    evaluated = 0
    interval_end: Optional[datetime] = None
    run_no: Optional[int] = None
    for parts in csv.reader(io.StringIO(content)):
        if len(parts) < 4 or parts[1:3] != ["DISPATCH", "CONSTRAINT"]:
            continue
        if parts[0] == "I":
            header = parts
            continue
        if parts[0] != "D" or header is None:
            continue
        row = dict(zip(header[4:], parts[4:]))
        if row.get("INTERVENTION", "0").strip() != "0":
            continue
        evaluated += 1
        if interval_end is None:
            try:
                interval_end = datetime.strptime(row["SETTLEMENTDATE"], "%Y/%m/%d %H:%M:%S").replace(tzinfo=NEM_TZ)
            except (KeyError, ValueError):
                return None
            run_no = int(_number(row.get("RUNNO")) or 0) or None
        marginal = _number(row.get("MARGINALVALUE")) or 0.0
        violation = _number(row.get("VIOLATIONDEGREE")) or 0.0
        if marginal == 0.0 and violation == 0.0:
            continue
        rows.append(
            BindingConstraint(
                constraint_id=row.get("CONSTRAINTID", "").strip(),
                rhs=_number(row.get("RHS")),
                lhs=_number(row.get("LHS")),
                marginal_value=marginal,
                violation_degree=violation,
            )
        )
    if interval_end is None:
        return None
    rows.sort(key=lambda c: (-abs(c.marginal_value), c.constraint_id))
    return ConstraintSnapshot(interval_end, run_no, source, tuple(rows), evaluated)


def latest_file(html: str) -> Optional[str]:
    """The newest DispatchIS file in a directory listing."""
    names = {m.group(1): m.group(2) for m in _FILENAME_RE.finditer(html)}
    return max(names, key=lambda n: (names[n], n)) if names else None


def unzip_csv(data: bytes) -> Optional[str]:
    """The first CSV member as text. Runs in the executor."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        return zf.read(names[0]).decode("utf-8", errors="replace") if names else None


class ConstraintClient:
    """Fetches the newest DispatchIS file and returns its binding constraints.

    Two NEMWEB requests per new interval, the gzip directory listing (about
    17 KB) and the zip (about 22 KB), both through the shared NEMWEB gate.
    A listing whose newest file was already read costs one request and
    returns None.
    """

    BASE_URL = DISPATCHIS_BASE_URL

    def __init__(
        self,
        session: Any,
        executor_job: ExecutorJob | None = None,
        semaphore: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._executor_job = executor_job
        self._semaphore = semaphore
        self._sleep = sleep
        self.last_file: Optional[str] = None

    async def fetch_latest(self) -> Optional[ConstraintSnapshot]:
        """The newest interval's binding constraints, or None.

        None means nothing new: the listing or the zip failed (already logged
        by fetch_with_retry), or the newest file is the one read last time.
        """
        kwargs: dict[str, Any] = {"semaphore": self._semaphore, "retryable_exceptions": _TRANSPORT_ERRORS, "logger": _LOGGER}
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        html = await fetch_with_retry(
            lambda: self._get(self.BASE_URL, text=True), url=self.BASE_URL,
            label="DispatchIS directory listing", **kwargs,
        )
        name = latest_file(html) if isinstance(html, str) else None
        if name is None or name == self.last_file:
            return None
        url = self.BASE_URL + name
        data = await fetch_with_retry(
            lambda: self._get(url, text=False), url=url, label="DispatchIS zip", **kwargs,
        )
        if data is None:
            return None
        try:
            content = await run_in_executor(self._executor_job, unzip_csv, data)
        except (zipfile.BadZipFile, KeyError, IndexError) as exc:
            _LOGGER.warning("DispatchIS: bad zip from %s: %s", url, exc)
            return None
        snapshot = parse_constraints(content, name) if content else None
        if snapshot is None:
            _LOGGER.warning("DispatchIS: no D,DISPATCH,CONSTRAINT rows in %s", url)
            return None
        self.last_file = name
        return snapshot

    async def _get(self, url: str, *, text: bool) -> Any:
        """One attempt. A 404 on the zip means not published yet."""
        async with self._session.get(url, headers=NEMWEB_HEADERS) as resp:
            classify_status(
                resp.status, url=url, headers=getattr(resp, "headers", None),
                not_published_statuses=() if text else (404,),
            )
            return await resp.text() if text else await resp.read()
