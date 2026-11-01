"""The spot forecast sensors' ``forecast`` from the price timeline (#333, #339 stage 5).

Until v3.44.0 ``forecast`` was the PD7DAY run alone, from the run's first
interval, past intervals included. It is now the region's price timeline from
the current interval: dispatch, P5MIN 5-minute RRP, pre-dispatch, then
calibrated PD7DAY.

Every entry keeps the keys a ``forecast`` entry has always had, so dashboards
and automations reading them keep working:

- a **pd7day** entry is the calibrated PD7DAY entry itself; where the timeline
  split its half hour, its ``time`` is moved to where the split begins;
- a **dispatch**, **p5min** or **predispatch** entry is built with the same
  keys: ``raw_value``, ``calibrated`` and ``value`` are its price, the band and
  the calibration's own diagnostics are None, ``calibrated_source`` says
  ``actual``, ``raw`` or ``passthrough``.

Each entry also carries ``source`` and ``resolution_minutes`` (5, 30, or less
where it was split). Entries are no longer all 30 minutes long, so the 24 hour
min and max and the cheapest window go by time, not by entry count.

Domain layer: no Home Assistant imports, no clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional, Sequence



def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def forecast_entries(timeline: Any, calibrated: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The timeline's points as ``forecast`` entries, in the keys ``forecast`` has always had."""
    by_end = {entry.get("nemtime"): entry for entry in calibrated}
    entries: list[dict[str, Any]] = []
    for point in timeline.points:
        if point["source"] == "pd7day" and point["nemtime"] in by_end:
            # Keys starting "_" are the memo's own (the chart's raw spike gate), never published.
            entry = {k: v for k, v in by_end[point["nemtime"]].items() if not k.startswith("_")}
            entry["time"] = point["time"]
        else:
            entry = {
                "nemtime": point["nemtime"], "time": point["time"],
                "raw_value": point["value"], "horizon_hours": None,
                "calibrated": point["value"], "p10": point["p10"], "p50": point["p50"], "p90": point["p90"],
                "ols_mae": None, "calibrated_source": point["calibrated_source"], "band_source": None,
                "n_obs": None, "value": point["value"], "spike_credible": None,
            }
        entry["source"] = point["source"]
        entry["resolution_minutes"] = point["resolution_minutes"]
        entries.append(entry)
    return entries


def _spans(entries: Sequence[dict[str, Any]]) -> list[tuple[datetime, datetime, dict[str, Any]]]:
    spans = []
    for entry in entries:
        start, end = _parse(entry.get("time")), _parse(entry.get("nemtime"))
        if start is not None and end is not None and end > start:
            spans.append((start, end, entry))
    return spans


def min_max_24h(entries: Sequence[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    """Min and max ``value`` over the 24 hours from the first entry's start, by time (#148)."""
    spans = _spans(entries)
    if not spans:
        return None, None
    stop = spans[0][0] + timedelta(hours=24)
    values = [e["value"] for start, _end, e in spans if start < stop and e.get("value") is not None]
    if not values:
        return None, None
    return round(min(values), 6), round(max(values), 6)


def cheapest_window(entries: Sequence[dict[str, Any]], hours: float = 2.0) -> Optional[dict[str, Any]]:
    """The ``hours``-long window with the lowest time-weighted average, starting at an entry's start.

    The keys are those ``cheapest_2h_window`` has always had: ``start`` and
    ``end`` are the first and last entry's start, ``nemtime_start`` and
    ``nemtime_end`` their ends, ``points`` how many entries it spans. A
    window must be covered in full by entries with a value.
    """
    spans = _spans(entries)
    length = timedelta(hours=hours)
    best: Optional[dict[str, Any]] = None
    for i, (start, _end, first) in enumerate(spans):
        stop = start + length
        weighted = covered = 0.0
        last = first
        count = 0
        cursor = start
        for s, e, entry in spans[i:]:
            if s >= stop:
                break
            if s != cursor or entry.get("value") is None:
                covered = -1.0  # a gap or an entry without a value: no window here
                break
            minutes = (min(e, stop) - s).total_seconds() / 60
            weighted += entry["value"] * minutes
            covered += minutes
            cursor, last, count = e, entry, count + 1
        if covered != length.total_seconds() / 60:
            continue
        avg = round(weighted / covered, 6)
        if best is None or avg < best["avg_value"]:
            best = {
                "nemtime_start": first.get("nemtime"), "nemtime_end": last.get("nemtime"),
                "start": first.get("time"), "end": last.get("time"),
                "avg_value": avg, "points": count,
            }
    return best
