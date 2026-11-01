"""Typed price source snapshots, each with an identity (#333, #335).

Every price source the integration reads is fetched once for all entries:
DispatchIS every 5 minutes, P5MIN after each dispatch poll, PredispatchIS every
half hour, PD7DAY three times a day. A derived series (a calibrated forecast, a
zone's station-voted adjustments, the price timeline) only needs computing
again when one of those changes, so each snapshot carries an ``identity``:

    (kind, source, run_at)

``kind`` names the source, ``source`` is the NEMWEB file it came from and
``run_at`` is when AEMO solved it (for DispatchIS, the interval end). Two
snapshots with the same identity hold the same data, so a cache keyed on it is
safe to reuse. Stage 2's forecast hub and stage 4's timeline key on these.

Domain layer: no Home Assistant imports. ``PredispatchSnapshot`` stays in
predispatch_client with its interconnector and constraint types and takes its
identity from the ``HasIdentity`` mixin here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, NamedTuple, Optional

KIND_DISPATCH = "dispatch"
KIND_P5MIN = "p5min"
KIND_PREDISPATCH = "predispatch"
KIND_PD7DAY = "pd7day"


class SourceIdentity(NamedTuple):
    kind: str
    source: str
    run_at: Optional[datetime]


class HasIdentity:
    """``identity`` from a snapshot's ``source`` and ``run_at``, with the class's ``KIND``."""

    KIND: ClassVar[str] = ""

    @property
    def identity(self) -> SourceIdentity:
        return SourceIdentity(self.KIND, getattr(self, "source", ""), getattr(self, "run_at", None))


@dataclass(frozen=True)
class LocalPriceRow:
    adjustment_mwh: float
    locally_constrained: Optional[int]


@dataclass(frozen=True)
class P5MinRun(HasIdentity):
    """One P5MIN run: its interval ends, the published rows, and each region's ROP and RRP.

    ``rop`` and ``rrp`` are per region and interval end, in $/MWh, from the
    pricing run (INTERVENTION 0). The RRP is what the price timeline will
    publish raw for the next hour (#333, #335).
    """

    KIND: ClassVar[str] = KIND_P5MIN

    run_at: datetime
    source: str
    table_present: bool
    interval_ends: tuple[datetime, ...]
    rows: dict[str, dict[datetime, LocalPriceRow]]
    rop: dict[str, dict[datetime, float]]
    conflicts: frozenset[tuple[str, datetime]] = field(default_factory=frozenset)
    retrieved_at: Optional[datetime] = None
    rrp: dict[str, dict[datetime, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchSnapshot(HasIdentity):
    """One DispatchIS interval: each region's RRP and ROP, $/MWh, from the pricing run."""

    KIND: ClassVar[str] = KIND_DISPATCH

    interval_end: datetime
    source: str
    rrp: dict[str, float]
    rop: dict[str, float]

    @property
    def run_at(self) -> datetime:
        """A dispatch interval is solved for its end, so that stands for the run."""
        return self.interval_end

    @classmethod
    def of(cls, prices: Any) -> Optional["DispatchSnapshot"]:
        """The snapshot of a parsed DispatchIS file (``DispatchLocalPrices``), or None without one."""
        if prices is None:
            return None
        return cls(prices.interval_end, prices.source, dict(getattr(prices, "rrp", {})), dict(prices.rop))


@dataclass(frozen=True)
class Pd7dayRun(HasIdentity):
    """A view of one PD7DAY download (``PD7DayResult``): its file, its run time, and the result."""

    KIND: ClassVar[str] = KIND_PD7DAY

    source: str
    run_at: Optional[datetime]
    result: Any = field(compare=False, repr=False)

    @classmethod
    def of(cls, result: Any) -> Optional["Pd7dayRun"]:
        """The view of a ``PD7DayResult``; its run time is CASESOLUTION's RUN_DATETIME, when parsed."""
        if result is None:
            return None
        case = getattr(result, "case", None)
        run_iso = getattr(case, "run_datetime", None) if case is not None else None
        try:
            run_at = datetime.fromisoformat(run_iso) if run_iso else None
        except (TypeError, ValueError):
            run_at = None
        return cls(result.source_file, run_at, result)
