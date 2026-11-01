"""One price timeline per region: dispatch, then P5MIN raw, pre-dispatch, and PD7DAY calibrated (#333, #338).

Each source covers what the one before it does not, in order:

1. **dispatch**: the current 5-minute interval, the RRP being paid (actual).
2. **p5min**: the rest of the P5MIN run, RRP, 5-minute, raw: no band.
3. **predispatch**: to the last pre-dispatch period, RRP, 30-minute. Served as
   ``calibrated_source: passthrough`` until stage 3b fits a pre-dispatch
   calibration (#337); then calibrated, with its band.
4. **pd7day**: beyond that, the calibrated PD7DAY forecast, as the spot forecast
   sensor publishes it, with its band.

A half hour that the source before covers in part is split where that source
ends, so the timeline never repeats or skips a minute. A stale source drops out
and the next one fills its span: P5MIN past 15 minutes, pre-dispatch past 75
minutes, as the local price sensors already treat them. ``sources`` says, for
each, whether it was used, what it covered and how old it was.

All values are $/kWh. Each point is a dict ready to publish:

    time                interval start, ISO +10:00
    nemtime             interval end, ISO +10:00 (AEMO's convention)
    value               $/kWh
    source              dispatch | p5min | predispatch | pd7day
    calibrated_source   actual | raw | passthrough | the calibration's own
    p10, p50, p90       the band, None where the source has none
    resolution_minutes  the point's length: 5, 30, or less where it was split

Domain layer: no Home Assistant imports, no clock (``now`` is passed in).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from .nem_time import to_nem_iso

SOURCE_DISPATCH = "dispatch"
SOURCE_P5MIN = "p5min"
SOURCE_PREDISPATCH = "predispatch"
SOURCE_PD7DAY = "pd7day"
SOURCES = (SOURCE_DISPATCH, SOURCE_P5MIN, SOURCE_PREDISPATCH, SOURCE_PD7DAY)

DISPATCH_INTERVAL = timedelta(minutes=5)
PD_PERIOD = timedelta(minutes=30)
P5_MAX_AGE = timedelta(minutes=15)
PD_MAX_AGE = timedelta(minutes=75)

STATUS_USED = "used"
STATUS_STALE = "stale"
STATUS_NO_DATA = "no_data"
STATUS_COVERED = "covered"  # fresh, but the sources before it already reach past its last point


@dataclass(frozen=True)
class DispatchPoint:
    """The dispatch interval held: its end (NEM, aware), its RRP in $/kWh, its file."""

    interval_end: datetime
    rrp_kwh: float
    source: str = ""


@dataclass
class PriceTimeline:
    region: str
    points: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Parallel to points: (start, end) as datetimes, for the helpers.
    spans: list[tuple[datetime, datetime]] = field(default_factory=list, repr=False)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def value_at(self, t: datetime) -> Optional[float]:
        """The value of the point covering ``t``, None outside the timeline."""
        i = bisect_right([start for start, _end in self.spans], t) - 1
        if 0 <= i < len(self.spans) and self.spans[i][0] <= t < self.spans[i][1]:
            value: Optional[float] = self.points[i]["value"]
            return value
        return None

    def slice(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """The points overlapping [start, end)."""
        return [p for p, (s, e) in zip(self.points, self.spans) if s < end and e > start]

    def min_max(self, start: datetime, end: datetime) -> tuple[Optional[float], Optional[float]]:
        values = [p["value"] for p in self.slice(start, end) if p["value"] is not None]
        return (min(values), max(values)) if values else (None, None)

    def cheapest_window(self, hours: float, after: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        """The ``hours``-long window with the lowest time-weighted average, starting at a point's start.

        Weighted by each point's minutes, so 5-minute and 30-minute points mix fairly.
        """
        length = timedelta(hours=hours)
        best: Optional[dict[str, Any]] = None
        for i, (start, _end) in enumerate(self.spans):
            if after is not None and start < after:
                continue
            stop = start + length
            if self.spans[-1][1] < stop:
                break
            weighted = minutes = 0.0
            for (s, e), p in zip(self.spans[i:], self.points[i:]):
                if s >= stop:
                    break
                if p["value"] is None:
                    weighted = float("nan")
                    break
                m = (min(e, stop) - s).total_seconds() / 60
                weighted += p["value"] * m
                minutes += m
            if minutes and weighted == weighted:  # not NaN
                avg = round(weighted / minutes, 6)
                if best is None or avg < best["avg_value"]:
                    best = {"start": to_nem_iso(start), "end": to_nem_iso(stop), "avg_value": avg, "hours": hours}
        return best

    @property
    def current(self) -> Optional[dict[str, Any]]:
        return self.points[0] if self.points else None


def _point(start: datetime, end: datetime, value: Optional[float], source: str, calibrated_source: str,
           p10: Optional[float] = None, p50: Optional[float] = None, p90: Optional[float] = None) -> dict[str, Any]:
    return {
        "time": to_nem_iso(start), "nemtime": to_nem_iso(end),
        "value": round(value, 6) if value is not None else None,
        "source": source, "calibrated_source": calibrated_source,
        "p10": p10, "p50": p50, "p90": p90,
        "resolution_minutes": round((end - start).total_seconds() / 60),
    }


class _Builder:
    def __init__(self, region: str, now: datetime) -> None:
        self.timeline = PriceTimeline(region)
        self.now = now
        self.cursor: Optional[datetime] = None  # where the coverage so far ends

    def add(self, start: datetime, end: datetime, point: dict[str, Any]) -> None:
        self.timeline.points.append(point)
        self.timeline.spans.append((start, end))
        self.cursor = end

    def span_start(self, start: datetime, end: datetime) -> Optional[datetime]:
        """Where a point [start, end) begins once what is covered is cut off; None if nothing is left."""
        if end <= self.now:
            return None
        if self.cursor is not None:
            if end <= self.cursor:
                return None
            start = max(start, self.cursor)
        return start

    def source(self, name: str, status: str, **info: Any) -> None:
        self.timeline.sources[name] = {"status": status, **info}

    def used(self, name: str, first: int, **info: Any) -> None:
        points = self.timeline.points[first:]
        if not points:
            self.source(name, STATUS_COVERED, **info)
            return
        self.source(name, STATUS_USED, start=points[0]["time"], end=points[-1]["nemtime"], points=len(points), **info)


def _age(now: datetime, at: Optional[datetime]) -> Optional[int]:
    return round((now - at).total_seconds()) if at is not None else None


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _add_dispatch(b: _Builder, dispatch: Optional[DispatchPoint]) -> None:
    """1. The interval running now, from the dispatch price held."""
    if dispatch is None:
        b.source(SOURCE_DISPATCH, STATUS_NO_DATA)
        return
    start = dispatch.interval_end - DISPATCH_INTERVAL
    info: dict[str, Any] = {"interval_end": to_nem_iso(dispatch.interval_end), "source_file": dispatch.source or None}
    if not start <= b.now < dispatch.interval_end:
        b.source(SOURCE_DISPATCH, STATUS_STALE, **info)
        return
    b.add(start, dispatch.interval_end, _point(start, dispatch.interval_end, dispatch.rrp_kwh, SOURCE_DISPATCH, "actual"))
    b.used(SOURCE_DISPATCH, 0, **info)


def _add_run(b: _Builder, name: str, run: Any, series: dict[datetime, float], length: timedelta,
             max_age: timedelta, calibrated_source: str) -> None:
    """2 and 3. A P5MIN or pre-dispatch run's RRP per interval end, from where the coverage so far ends."""
    if run is None:
        b.source(name, STATUS_NO_DATA)
        return
    info: dict[str, Any] = {"run_at": to_nem_iso(run.run_at), "source_file": run.source, "age_seconds": _age(b.now, run.run_at)}
    if b.now - run.run_at > max_age:
        b.source(name, STATUS_STALE, **info)
        return
    first = len(b.timeline.points)
    for end, rrp in sorted(series.items()):
        start = b.span_start(end - length, end)
        if start is not None:
            b.add(start, end, _point(start, end, rrp / 1000.0, name, calibrated_source))
    b.used(name, first, **info)


def _add_pd7day(b: _Builder, pd7day: Sequence[dict[str, Any]], info: dict[str, Any]) -> None:
    """4. The calibrated PD7DAY forecast, as the spot forecast sensor publishes it."""
    if not pd7day:
        b.source(SOURCE_PD7DAY, STATUS_NO_DATA, **info)
        return
    first = len(b.timeline.points)
    for entry in pd7day:
        entry_start, entry_end = _parse(entry.get("time")), _parse(entry.get("nemtime"))
        if entry_start is None or entry_end is None:
            continue
        start = b.span_start(entry_start, entry_end)
        if start is None:
            continue
        value = entry.get("value")
        b.add(start, entry_end, _point(
            start, entry_end, value, SOURCE_PD7DAY, entry.get("calibrated_source") or "raw",
            entry.get("p10"), entry.get("p50", value), entry.get("p90"),
        ))
    b.used(SOURCE_PD7DAY, first, **info)


def build_timeline(
    now: datetime, region: str, *,
    dispatch: Optional[DispatchPoint] = None,
    p5: Any = None,
    pd: Any = None,
    pd7day: Sequence[dict[str, Any]] = (),
    pd7day_run_at: Optional[str] = None,
    pd7day_source_file: Optional[str] = None,
) -> PriceTimeline:
    """The region's timeline from ``now``, each source after the one before it."""
    b = _Builder(region, now)
    _add_dispatch(b, dispatch)
    p5_series = getattr(p5, "rrp", {}).get(region, {}) if p5 is not None else {}
    _add_run(b, SOURCE_P5MIN, p5, p5_series, DISPATCH_INTERVAL, P5_MAX_AGE, "raw")
    # Passthrough until stage 3b calibrates pre-dispatch (#337).
    pd_series = pd.prices.get(region, {}) if pd is not None else {}
    _add_run(b, SOURCE_PREDISPATCH, pd, pd_series, PD_PERIOD, PD_MAX_AGE, "passthrough")
    _add_pd7day(b, pd7day, {"run_at": pd7day_run_at, "source_file": pd7day_source_file})
    return b.timeline
