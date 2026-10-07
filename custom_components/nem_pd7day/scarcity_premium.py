"""Experimental QLD recent-price overlay. All internal prices are $/kWh.

This is an explicit policy heuristic, NOT a fitted expected forecast error.
No Home Assistant dependencies, network IO or use of future observations.

The signal is the mean of the QLD1 dispatch prices (ELEC_NEM_SUMMARY, one per
five-minute interval) whose settlement ends fall in the last 30 minutes. It
replaced a mean of all 36 prices from 07:00 to 10:00, which needed every one
of them observed live and left the sensor unavailable for the whole
application window after any restart or missed interval that morning.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from collections.abc import Mapping, Sequence

NEM = timezone(timedelta(hours=10))
UTC = timezone.utc
TRIGGER = 0.030
FLOOR_CAP = 0.065
PREMIUM_CAP = 0.065
# The signal averages the dispatch prices of the last 30 minutes, and is used
# only while its newest price is at most 10 minutes old: two missed polls, not
# a stale snapshot standing in for the current market.
SIGNAL_WINDOW = timedelta(minutes=30)
SIGNAL_FRESHNESS = timedelta(minutes=10)


def aware_time(value: str) -> datetime:
    """Require an explicit timezone rather than silently accepting host time."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def finite_price(value: object) -> float:
    """Accept numeric prices, including zero and negative, never NaN or bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Numeric price required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Finite price required")
    return result


def recent_samples(
    samples: Mapping[str, object], now: datetime
) -> dict[str, float]:
    """Keep unique, completed 5-min ends in the 30 minutes up to ``now``.

    Keys are normalised to UTC ISO strings, so the same instant written with
    another offset is one sample, not two. Anything older, in the future, off
    the five-minute grid, or not a finite number is dropped.
    """
    start = now - SIGNAL_WINDOW
    clean: dict[str, float] = {}
    for stamp, value in samples.items():
        try:
            t = aware_time(stamp)
            price = finite_price(value)
        except (ValueError, TypeError):
            continue
        if start < t <= now and t.minute % 5 == 0 and not t.second and not t.microsecond:
            clean[t.astimezone(UTC).isoformat()] = price
    return clean


def signal_mean(samples: Mapping[str, float], now: datetime) -> float | None:
    """Mean of the recent samples, or None when the newest is not fresh."""
    if not samples:
        return None
    newest = max(aware_time(stamp) for stamp in samples)
    if now - newest > SIGNAL_FRESHNESS:
        return None
    return round(math.fsum(samples.values()) / len(samples), 12)


@dataclass(frozen=True)
class PremiumResult:
    status: str
    forecast: list[dict]
    count: int
    signal_mean: float | None
    target_floor: float | None

    @property
    def value(self) -> float | None:
        return self.forecast[0]["value"] if self.forecast else None


def build_premium(
    now: datetime,
    samples: Mapping[str, object],
    base_forecast: Sequence[Mapping],
    *,
    base_fresh: bool,
) -> PremiumResult:
    """Build seven days of half-hour additions plus an explicit zero endpoint.

    No fresh dispatch price inside 10:00-14:00 means unavailable, not zero.
    Outside that window zero means "policy inactive", not "forecast certain".
    Later days are zero: the signal describes the market now, not then.
    """
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    local = now.astimezone(NEM)
    samples = recent_samples(samples, now)
    count = len(samples)
    mean = signal_mean(samples, now)
    floor = min(mean, FLOOR_CAP) if mean is not None and mean > TRIGGER else None
    active_window = 10 <= local.hour < 14
    status = "before_window" if local.hour < 10 else "outside_window"
    if active_window:
        if mean is None:
            return PremiumResult("no_recent_dispatch", [], count, None, None)
        status = "signal_below_threshold" if floor is None else "active"
        if floor is not None and not base_fresh:
            return PremiumResult("base_forecast_stale", [], count, mean, floor)

    start = local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)
    base: dict[datetime, float] = {}
    for item in base_forecast:
        try:
            t = aware_time(item["time"]).astimezone(UTC)
            value = finite_price(item["value"])
        except (KeyError, ValueError, TypeError):
            continue
        # Duplicate times are ambiguous: fail rather than pick a convenient row.
        if t in base:
            return PremiumResult("duplicate_base_interval", [], count, mean, floor)
        base[t] = value

    forecast = []
    for index in range(7 * 48 + 1):
        t = start + timedelta(minutes=30 * index)
        value = 0.0
        if active_window and floor is not None and t.date() == local.date() and 10 <= t.hour < 14:
            price = base.get(t.astimezone(UTC))
            if price is None:
                return PremiumResult("base_forecast_gap", [], count, mean, floor)
            value = round(min(PREMIUM_CAP, max(0.0, floor - price)), 6)
        # Reported in NEM time, +10:00, like every other forecast this
        # integration publishes. Lookups above stay keyed on UTC instants.
        forecast.append({"time": t.isoformat(), "value": value})
    return PremiumResult(status, forecast, count, mean, floor)
