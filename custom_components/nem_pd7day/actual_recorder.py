"""
Actual recording: pair a settled price with the forecasts that covered it.

An actual price for an interval is matched against every forecast history
entry for that interval (see ``forecast_history``). Each (interval, forecast
run) pair becomes one observation, the row the calibration fit trains on.
Amber reports five-minute prices and PD7DAY forecasts the half-hour average,
so repeat readings for a pair update a running average in place rather than
adding rows.

The observation field order is part of the stored format in ``.storage``;
keep it. This module reads no clock and does not save: the caller persists
the observation log when ``record_actual`` returns a count (spec 004).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol

from .calibration_engine import stpasa_feature_values
from .const import MAX_HORIZON_HOURS, MAX_TOTAL_OBS, NEM_TZ

_LOGGER = logging.getLogger(__name__)


class ObservationSink(Protocol):
    """The observation log as the recorder uses it (``ObservationLog``)."""

    @property
    def observations(self) -> list[dict]: ...
    def append(self, obs: dict) -> None: ...
    def touch(self, obs: dict) -> None: ...
    def prune(self, max_total: int) -> Iterable[dict]: ...


def _parse_nem_iso(s: str) -> datetime:
    """Parse an ISO-8601 NEM time string to a tz-aware datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NEM_TZ)
    return dt


def rebuild_accumulator(observations: Iterable[dict]) -> dict[tuple[str, str], dict]:
    """The running-average accumulator for observations loaded from storage.

    It holds the observation dict itself, not its position: positions shift
    when the log is pruned and the accumulator was never rebuilt (issue #132).
    """
    return {
        (o["interval_time"], o["forecast_run_at"]): {
            "sum": o["actual_rrp"],
            "count": 1,
            "obs": o,
        }
        for o in observations
        if "interval_time" in o and "forecast_run_at" in o
    }


def _add_reading(
    acc: dict, actual_rrp: float, log: ObservationSink, interval_time: str, run_at: str
) -> None:
    """Subsequent Amber 5-min reading within same 30-min interval.

    Update the running average in the existing observation in-place.
    """
    acc["sum"] += actual_rrp
    acc["count"] += 1
    new_avg = acc["sum"] / acc["count"]
    acc["obs"]["actual_rrp"] = round(new_avg, 6)
    log.touch(acc["obs"])
    _LOGGER.debug(
        "Updated actual_rrp for interval %s run_at %s: "
        "avg=%.4f over %d readings",
        interval_time, run_at, new_avg, acc["count"],
    )


def _new_observation(
    interval_time: str,
    interval_dt: datetime,
    horizon_h: float,
    fc: dict,
    actual_rrp: float,
    source: str,
) -> dict:
    """First Amber reading for this pair — create a new observation."""
    obs: dict[str, Any] = {
        "interval_time": interval_time,
        "horizon_hours": round(horizon_h, 2),
        "pd7day_forecast": fc["forecast_price"],
        "actual_rrp": actual_rrp,
        "forecast_run_at": fc["run_at"],
        "hour_of_day": interval_dt.hour,   # NEM local hour (UTC+10)
        "day_of_week": interval_dt.weekday(),
        "month": interval_dt.month,
        "gas_forecast_tj": fc.get("gas_tj"),
        "qni_mwflow": fc.get("qni_mwflow"),
        "qni_violation_degree": fc.get("qni_violation"),
        "is_intervention": fc.get("is_intervention", False),
        "actual_source": source,
    }

    # Derive STPASA features only when this forecast entry carries every
    # input. A missing MW field is now None rather than 0.0, and the
    # previous `.get(key, 0.0)` defaults would have turned that back
    # into a zero and fed it to the fit as a real observation. An
    # incomplete interval is omitted from the fit instead. See #43.
    # The transform itself lives in calibration_engine so the training
    # and serving sides cannot drift apart; it also returns None for a
    # demand50 below its floor, where the features are degenerate
    # (issue #147).
    values = stpasa_feature_values(
        fc.get("stpasa_surplus"),
        fc.get("stpasa_solar"),
        fc.get("stpasa_demand50"),
        fc.get("stpasa_demand10"),
        fc.get("stpasa_demand90"),
    )
    if values is not None:
        (
            obs["stpasa_log_surplus"],
            obs["stpasa_log_solar"],
            obs["stpasa_log_demand"],
            obs["stpasa_poe_spread_n"],
        ) = values
        obs["stpasa_run_at"] = fc.get("stpasa_run_at", "")
    return obs


def _horizon_hours(
    interval_dt: datetime, fc: dict, calibration_region: str | None
) -> float | None:
    """Hours from the entry's run to the interval, or None to skip the entry.

    Skipped: another region's entry when a calibration region is given, a
    run time that does not parse, and a horizon outside [0, MAX_HORIZON_HOURS].
    """
    if calibration_region and fc.get("region") != calibration_region:
        return None

    try:
        run_dt = _parse_nem_iso(fc["run_at"])
    except (ValueError, KeyError):
        return None

    # Both datetimes are tz-aware (UTC+10) — subtraction is unambiguous
    horizon_h = (interval_dt - run_dt).total_seconds() / 3600
    if horizon_h < 0 or horizon_h > MAX_HORIZON_HOURS:
        return None
    return horizon_h


def _prune(accum: dict[tuple[str, str], dict], log: ObservationSink) -> None:
    """Prune the log to MAX_TOTAL_OBS and retire the dropped pairs."""
    for dropped in log.prune(MAX_TOTAL_OBS):
        interval = dropped.get("interval_time")
        run_at = dropped.get("forecast_run_at")
        if isinstance(interval, str) and isinstance(run_at, str):
            accum.pop((interval, run_at), None)


def record_actual(
    history: Mapping[str, list[dict]],
    accum: dict[tuple[str, str], dict],
    log: ObservationSink,
    *,
    interval_time: str,
    actual_rrp: float,
    calibration_region: str | None,
    source: str,
) -> int:
    """
    Match the actual RRP for an interval against all PD7DAY forecasts
    that covered it.  Horizon is computed from tz-aware datetimes so it
    is accurate regardless of system timezone.

    Returns the number of observations added or updated. When that is not
    zero the log has been pruned and the accumulator trimmed to match; the
    caller saves.
    """
    forecasts = history.get(interval_time, [])
    if not forecasts:
        _LOGGER.debug(
            "No forecast history for interval %s — skipping", interval_time
        )
        return 0

    interval_dt = _parse_nem_iso(interval_time)
    new_count = 0

    for fc in forecasts:
        horizon_h = _horizon_hours(interval_dt, fc, calibration_region)
        if horizon_h is None:
            continue

        pair_key = (interval_time, fc["run_at"])

        if pair_key in accum:
            _add_reading(accum[pair_key], actual_rrp, log, interval_time, fc["run_at"])
            new_count += 1   # signal that a save is needed
            continue

        obs = _new_observation(interval_time, interval_dt, horizon_h, fc, actual_rrp, source)
        log.append(obs)
        accum[pair_key] = {
            "sum": actual_rrp,
            "count": 1,
            "obs": obs,
        }
        new_count += 1

    if new_count:
        _prune(accum, log)

    return new_count
