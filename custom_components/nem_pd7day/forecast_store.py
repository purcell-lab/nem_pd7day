"""
Per-region PD7DAY forecast cache.

Persists the last successful PD7DayResult to HA's .storage directory so the
integration can restore sensors instantly on restart (phase 1 of two-phase
startup) without blocking HA boot on a NEMWEB fetch.

Storage key:  nem_pd7day.forecast.{region}   (one file per region)
Version:      1

Cache validity
--------------
A cached result is considered usable only if its ``updated_at`` timestamp is
within _CACHE_MAX_AGE_S (35 minutes) of now — i.e. younger than one NEM
trading interval plus margin. Older caches return None from load(), so the
caller falls back to a blocking first-refresh (e.g. after a long HA outage).

Serialisation
-------------
PD7DayResult is a tree of dataclasses whose fields are all JSON-native
primitives, ISO-8601 strings, or nested dataclasses — there are no raw
datetime objects. dataclasses.asdict() therefore produces clean JSON, and
from_dict() rebuilds the typed object tree.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.helpers.storage import Store

from .const import NEM_TZ
from .pd7day_client import (
    CaseSolutionData,
    CheapestWindow,
    GasForecastPeriod,
    InterconnectorData,
    InterconnectorPeriod,
    MarketSummaryData,
    PD7DayData,
    PD7DayResult,
    PricePeriod,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

FORECAST_STORE_VERSION = 1
# Cached forecast is usable only if updated_at is within this many seconds of now.
_CACHE_MAX_AGE_S = 35 * 60  # 35 minutes


def _forecast_store_key(region: str) -> str:
    return f"nem_pd7day.forecast.{region.lower()}"


# ── Deserialisation helpers ────────────────────────────────────────────────


def _price_data_from_dict(d: dict) -> PD7DayData:
    cw = d.get("cheapest_2h_window")
    return PD7DayData(
        region=d["region"],
        source_file=d["source_file"],
        forecast_generated_at=d.get("forecast_generated_at"),
        interval_minutes=d["interval_minutes"],
        current_value=d["current_value"],
        next_value=d.get("next_value"),
        min_24h_value=d.get("min_24h_value"),
        max_24h_value=d.get("max_24h_value"),
        cheapest_2h_window=CheapestWindow(**cw) if cw else None,
        forecast=[PricePeriod(**p) for p in d.get("forecast", [])],
    )


def _market_summary_from_dict(d: dict | None) -> MarketSummaryData | None:
    if not d:
        return None
    return MarketSummaryData(
        run_datetime=d["run_datetime"],
        forecast=[GasForecastPeriod(**p) for p in d.get("forecast", [])],
    )


def _interconnector_from_dict(d: dict) -> InterconnectorData:
    return InterconnectorData(
        interconnector_id=d["interconnector_id"],
        source_file=d["source_file"],
        run_datetime=d["run_datetime"],
        forecast=[InterconnectorPeriod(**p) for p in d.get("forecast", [])],
    )


def _result_from_dict(d: dict) -> PD7DayResult:
    case = d.get("case")
    return PD7DayResult(
        source_file=d["source_file"],
        case=CaseSolutionData(**case) if case else None,
        prices={r: _price_data_from_dict(pd) for r, pd in d.get("prices", {}).items()},
        market_summary=_market_summary_from_dict(d.get("market_summary")),
        interconnectors={
            ic: _interconnector_from_dict(icd)
            for ic, icd in d.get("interconnectors", {}).items()
        },
        updated_at=d.get("updated_at"),
    )


class ForecastStore:
    """Persists and restores the last PD7DayResult for one region."""

    def __init__(self, hass: "HomeAssistant", region: str) -> None:
        self._region = region
        self._store = Store(hass, FORECAST_STORE_VERSION, _forecast_store_key(region))

    async def load(self) -> PD7DayResult | None:
        """Return the cached PD7DayResult, or None if absent or stale (>35 min)."""
        data = await self._store.async_load()
        if not data:
            return None
        updated_at = data.get("updated_at")
        if not _is_fresh(updated_at):
            _LOGGER.debug(
                "Forecast cache for %s is stale (updated_at=%s) — ignoring",
                self._region,
                updated_at,
            )
            return None
        try:
            return _result_from_dict(data)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not restore forecast cache for %s: %s", self._region, exc
            )
            return None

    async def save(self, data: PD7DayResult) -> None:
        """Persist a PD7DayResult to .storage."""
        await self._store.async_save(dataclasses.asdict(data))


def _is_fresh(updated_at: str | None) -> bool:
    """True if updated_at (ISO-8601 NEM time) is within _CACHE_MAX_AGE_S of now."""
    if not updated_at:
        return False
    try:
        dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NEM_TZ)
    age = (datetime.now(NEM_TZ) - dt).total_seconds()
    return age <= _CACHE_MAX_AGE_S
