"""The tariff sensors' ``forecast`` from the price timeline (#333, #339 stage 5b).

Until v3.45.0 a tariff sensor priced every interval of the PD7DAY run, from the
run's first, against the calibrated spot. It now prices the region's price
timeline from the current interval, the same entries the spot forecast sensor
publishes: dispatch, P5MIN 5-minute, pre-dispatch, then calibrated PD7DAY.

Each entry keeps the keys a tariff ``forecast`` entry has always had,
``time``, ``nemtime``, ``spot_raw``, ``spot``, ``value``, ``period`` and
``network_rate``, and adds ``source`` and ``resolution_minutes``.

A price depends only on the interval's end, its spot and the usage fee, so
``PriceCache`` keeps each one while the calibrated forecast and the fee are
unchanged. A new timeline every 5 minutes then prices its near-term entries
only; the PD7DAY span is priced once per run.

Service layer: no Home Assistant imports, no clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

# Room for two runs' worth of intervals; cleared, not evicted, past it.
MAX_CACHED = 2048


@dataclass(frozen=True)
class Interval:
    """What a tariff sensor's pricing reads of an interval: its start, end and raw spot."""

    time: str
    nemtime: str
    value: Optional[float]


class PriceCache:
    """Prices and ToU periods per interval end, kept while ``generation`` holds."""

    def __init__(self) -> None:
        self._generation: Any = None
        self._pin: Any = None
        self._prices: dict[tuple[str, float], Optional[float]] = {}
        self._periods: dict[str, tuple[Optional[str], Optional[float]]] = {}

    def renew(self, generation: Any, pin: Any = None) -> None:
        """Drop everything kept when ``generation`` (calibrated forecast id, fee) moves.

        ``pin`` holds what ``generation`` names by id, so the id is not reused.
        """
        if generation != self._generation or len(self._prices) > MAX_CACHED:
            self._generation = generation
            self._pin = pin
            self._prices = {}
            self._periods = {}

    def price(self, interval: Interval, spot: float, compute: Callable[[Interval, float], Optional[float]]) -> Optional[float]:
        key = (interval.nemtime, round(spot, 6))
        if key not in self._prices:
            self._prices[key] = compute(interval, spot)
        return self._prices[key]

    def period(self, interval: Interval, compute: Callable[[Interval], tuple[Optional[str], Optional[float]]]) -> tuple[Optional[str], Optional[float]]:
        if interval.nemtime not in self._periods:
            self._periods[interval.nemtime] = compute(interval)
        return self._periods[interval.nemtime]


def _round(value: Any) -> Optional[float]:
    return round(value, 6) if isinstance(value, (int, float)) else None


def tariff_entries(
    entries: Iterable[dict[str, Any]],
    price: Callable[[Interval, float], Optional[float]],
    lookup: Callable[[Interval], tuple[Optional[str], Optional[float]]],
    cache: PriceCache,
) -> list[dict[str, Any]]:
    """One tariff ``forecast`` entry per timeline ``forecast`` entry.

    ``spot`` is the entry's ``value``: the calibrated PD7DAY spot, or the
    dispatch, P5MIN or pre-dispatch price. A None spot means the interval
    could not be calibrated, and its tariff ``value`` stays None rather than
    being priced on a stand-in.
    """
    out: list[dict[str, Any]] = []
    for entry in entries:
        interval = Interval(entry["time"], entry["nemtime"], entry.get("raw_value"))
        spot = entry.get("value")
        period_name, network_rate = cache.period(interval, lookup)
        out.append({
            "time": interval.time,                        # interval START
            "nemtime": interval.nemtime,                  # interval END (AEMO convention)
            "spot_raw": _round(interval.value),           # uncalibrated spot $/kWh
            "spot": _round(spot),                         # spot $/kWh as priced
            "value": None if spot is None else cache.price(interval, spot, price),  # tariff $/kWh
            "period": period_name,
            "network_rate": network_rate,
            "source": entry.get("source"),
            "resolution_minutes": entry.get("resolution_minutes"),
        })
    return out
