"""Pre-dispatch forecasts as a forecast history, for calibration (#333, #337 stage 3a).

Each 30-minute PredispatchIS run forecasts every region's RRP for each trading
period to the end of the next trading day. Recording those forecasts against the
TradingIS settlement price builds the observations a pre-dispatch calibration
(stage 3b) will fit, as PD7DAY's forecasts and actuals are recorded today.

The run's region prices are keyed by period END in $/MWh; the forecast history
is keyed by interval START in $/kWh, so a period becomes one entry at its start.
The horizon is the period start minus the run time. The run is solved a minute or
two after its first period starts, so that period has a negative horizon and is
left out, as ``record_actual`` would drop it anyway.

Thinning. Pre-dispatch runs 48 times a day and covers 40 to 80 periods, so every
run would give about 50 observations per interval and fill the 100,000-row cap
in about 6 weeks. The calibration buckets horizons at 0, 1, 3, 6, 12, 24 and 40 h,
and the far buckets change slowly, so the runs kept thin out with the horizon:

    horizon < 6 h     every run         (12 per interval)
    6 h to 12 h       every 2nd run     (6)
    12 h to 24 h      every 4th run     (6)
    24 h and beyond   every 8th run     (1 or 2)

about 25 observations per interval, 1,200 a day, so the cap holds about 83 days.
A run's place in the sequence is its run time floored to the half hour, so the
same runs are kept on every install and after a restart.

Domain layer: no Home Assistant imports, no clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .nem_time import to_nem_iso

PERIOD = timedelta(minutes=30)
# (horizon below this many hours, keep every Nth run)
THINNING: tuple[tuple[float, int], ...] = ((6.0, 1), (12.0, 2), (24.0, 4), (float("inf"), 8))


@dataclass(frozen=True)
class ForecastPeriod:
    """One forecast interval as ``forecast_history.ingest_run`` reads it: start ISO, $/kWh."""

    time: str
    value: float


@dataclass(frozen=True)
class RunForecast:
    """``price_data`` for ``ingest_run``: the periods kept from one run."""

    forecast: list[ForecastPeriod]


def run_slot(run_at: datetime) -> int:
    """The run's place in the half-hourly sequence, the same on every install."""
    return int(run_at.timestamp() // PERIOD.total_seconds())


def keep(horizon_h: float, slot: int) -> bool:
    """Whether a run at ``slot`` is kept for a period ``horizon_h`` hours ahead."""
    if horizon_h < 0:
        return False
    every = next(n for limit, n in THINNING if horizon_h < limit)
    return slot % every == 0


def run_forecast(snapshot: Any, region: str) -> RunForecast:
    """The periods of ``snapshot`` (a ``PredispatchSnapshot``) kept for ``region``, oldest first."""
    slot = run_slot(snapshot.run_at)
    periods = []
    for end, rrp in sorted(snapshot.prices.get(region, {}).items()):
        start = end - PERIOD
        horizon_h = (start - snapshot.run_at).total_seconds() / 3600
        if keep(horizon_h, slot):
            periods.append(ForecastPeriod(to_nem_iso(start), round(rrp / 1000.0, 6)))
    return RunForecast(periods)
