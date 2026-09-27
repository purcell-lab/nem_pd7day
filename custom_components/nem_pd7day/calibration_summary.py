"""
Calibration diagnostics: the attributes the calibration sensor publishes.

Pure functions of the store's state. The current time is an argument, read by
``CalibrationStore`` through its module-level ``_now_nem`` (spec 004), so the
tests that pin that clock keep pinning these numbers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .calibration_engine import OBSERVATION_WINDOW_DAYS, CalibrationResult, all_bucket_keys
from .const import NEM_TZ


def oldest_observation(observations: Iterable[dict]) -> str | None:
    """Interval time of the oldest retained observation, or None."""
    for obs in observations:
        value = obs.get("interval_time")
        if value:
            return str(value)
    return None


def effective_window_days(oldest: str | None, now: datetime) -> float | None:
    """Days from the oldest retained observation to ``now``, one decimal."""
    if oldest is None:
        return None
    try:
        oldest_dt = datetime.fromisoformat(oldest)
    except ValueError:
        return None
    if oldest_dt.tzinfo is None:
        oldest_dt = oldest_dt.replace(tzinfo=NEM_TZ)
    return round((now - oldest_dt).total_seconds() / 86400.0, 1)


def summary_attributes(
    calibration: CalibrationResult | None,
    *,
    observation_count: int,
    oldest: str | None,
    window_days: float | None,
    active_buckets: int,
) -> dict:
    """The calibration sensor's attributes, in their published key order."""
    if not calibration:
        return {
            "status": "no_calibration",
            "observation_count": observation_count,
            "active_buckets": 0,
        }
    return {
        "status": "active",
        "fitted_at": calibration.fitted_at,
        "observation_count": observation_count,
        "observation_window_days": OBSERVATION_WINDOW_DAYS,
        # What the fit could actually see: the age of the oldest retained
        # observation. Shorter than the window whenever MAX_TOTAL_OBS
        # binds (issue #127), and the only honest number to publish.
        "oldest_observation": oldest,
        "effective_window_days": window_days,
        "observations_in_window": calibration.observations_in_window,
        "active_buckets": active_buckets,
        "total_buckets": len(all_bucket_keys()),
        "summary": calibration.summary(),
    }
