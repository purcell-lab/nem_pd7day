"""Local price adjustments from the 30-minute pre-dispatch, to the end of the next trading day (#287).

Each PredispatchIS run carries D,PREDISPATCH,LOCAL_PRICE: DATETIME (the
period end), DUID, LOCAL_PRICE_ADJUSTMENT and LOCALLY_CONSTRAINED, for every
30-minute period of the run, as DispatchIS does for the interval just run and
P5MIN for the next hour. The shared pre-dispatch coordinator already reads the
file every half hour, so the rows ride on that fetch.

As there, AEMO publishes a row only where the adjustment is not zero, and no
table at all when there is none (#309): a period without one, in a fresh run,
is 0 and counted in ``predispatch_intervals_inferred_zero``. A stale run
gives no points. A node with several DUIDs takes their
common adjustment; where they differ the series stops there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Sequence

from .local_price import (
    STATUS_CONFLICT,
    STATUS_EXPIRED,
    STATUS_NO_DATA,
    STATUS_PUBLISHED,
    STATUS_STALE,
    STATUS_ZERO,
)
from .nem_time import NEM_TZ

PERIOD = timedelta(minutes=30)
# A run is solved every half hour; past two missed runs it no longer stands for now.
MAX_AGE = timedelta(minutes=75)


@dataclass(frozen=True)
class PredispatchAdjustments:
    status: str
    periods: list[tuple[datetime, float]] = field(default_factory=list)  # (period end, $/MWh), still to end
    published: int = 0
    inferred: int = 0


def predispatch_adjustments(snapshot: Any, duids: Sequence[str], now: datetime) -> PredispatchAdjustments:
    """The run's periods still to end, in order, up to the first period where the DUIDs disagree."""
    if snapshot is None:
        return PredispatchAdjustments(STATUS_NO_DATA)
    if now - snapshot.run_at > MAX_AGE:
        return PredispatchAdjustments(STATUS_STALE)
    ends = [end for end in snapshot.period_ends if end > now]
    if not ends:
        return PredispatchAdjustments(STATUS_EXPIRED)
    rows = [snapshot.local_prices.get(duid, {}) for duid in duids]
    periods: list[tuple[datetime, float]] = []
    published = inferred = 0
    for end in ends:
        values = {row[end] for row in rows if end in row}
        if len(values) > 1:
            return PredispatchAdjustments(STATUS_CONFLICT, periods, published, inferred)
        if values:
            published += 1
            periods.append((end, values.pop()))
        else:
            inferred += 1
            periods.append((end, 0.0))
    return PredispatchAdjustments(STATUS_PUBLISHED if published else STATUS_ZERO, periods, published, inferred)


def _iso(value: datetime) -> str:
    return value.astimezone(NEM_TZ).isoformat()


def predispatch_attrs(
    series: PredispatchAdjustments, snapshot: Any,
    value: Callable[[datetime, float], Optional[float]],
    calibrated_rrp: Optional[Callable[[datetime], Optional[float]]] = None,
) -> dict[str, Any]:
    """The pre-dispatch forecast attributes; ``value`` turns (period end, adjustment $/MWh) into a $/kWh point.

    ``calibrated_rrp(end)``, for the node and zone sensors, adds each entry's
    ``calibrated_rrp_mwh``: the region's RRP as the price timeline serves it
    (#333 decision 3, #339), beside the raw-RRP price in ``value``.
    """
    points = []
    for end, adjustment in series.periods:
        point = value(end, adjustment)
        if point is not None:
            entry: dict[str, Any] = {"time": _iso(end - PERIOD), "value": round(point, 6)}
            if calibrated_rrp is not None:
                served = calibrated_rrp(end)
                entry["calibrated_rrp_mwh"] = round(served, 5) if served is not None else None
            points.append(entry)
    return {
        "predispatch_source": "PREDISPATCH_LOCAL_PRICE",
        "predispatch_status": series.status,
        "predispatch_run_at": _iso(snapshot.run_at) if snapshot is not None else None,
        "predispatch_source_file": snapshot.source if snapshot is not None else None,
        "predispatch_interval_minutes": 30,
        "predispatch_intervals_published": series.published,
        "predispatch_intervals_inferred_zero": series.inferred,
        "predispatch_end": _iso(series.periods[-1][0]) if series.periods else None,
        "predispatch_forecast": points,
    }
