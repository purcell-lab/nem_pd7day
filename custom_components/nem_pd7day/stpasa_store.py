"""
NEM PD7DAY STPASA Store
=======================
HA .storage persistence for the latest StpasaResult, per region.

Storage key : nem_pd7day.stpasa.{region.lower()}
Version     : 1
Cache TTL   : 90 minutes fresh, up to 4 hours stale (is_stale=True) before discarding.

On load failure the in-memory latest() returns None.

When the cache is between 90 min and 4 h old, latest() returns the result
with is_stale=True so the OLS calibration continues with slightly old data
rather than silently dropping to isotonic-only.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION
from .stpasa_client import StpasaInterval, StpasaResult

_LOGGER = logging.getLogger(__name__)

STPASA_CACHE_TTL = timedelta(minutes=90)   # fresh window
STAPASA_STALE_TTL = timedelta(hours=4)       # max age before discarding entirely


def _stpasa_storage_key(region: str) -> str:
    return f"nem_pd7day.stpasa.{region.lower()}"


def _is_fresh(fetched_at: str) -> bool:
    """Return True if fetched_at (UTC ISO-8601) is within the fresh cache TTL."""
    if not fetched_at:
        return False
    try:
        dt = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) <= STPASA_CACHE_TTL


def _cache_status(fetched_at: str) -> str:
    """Return 'fresh', 'stale', or 'expired' for a fetched_at UTC ISO-8601 string.

    - fresh   : age <= STPASA_CACHE_TTL (90 min) — use normally
    - stale   : STPASA_CACHE_TTL < age <= STAPASA_STALE_TTL (4 h) — use with is_stale=True
    - expired : age > STAPASA_STALE_TTL — discard
    """
    if not fetched_at:
        return "expired"
    try:
        dt = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return "expired"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - dt
    if age <= STPASA_CACHE_TTL:
        return "fresh"
    if age <= STAPASA_STALE_TTL:
        return "stale"
    return "expired"


def _result_from_dict(data: dict[str, Any]) -> StpasaResult:
    intervals = [
        StpasaInterval(
            interval_datetime=si.get("interval_datetime", ""),
            run_datetime=si.get("run_datetime", ""),
            demand10=float(si.get("demand10", 0.0)),
            demand50=float(si.get("demand50", 0.0)),
            demand90=float(si.get("demand90", 0.0)),
            surpluscapacity=float(si.get("surpluscapacity", 0.0)),
            ss_solar_uigf=float(si.get("ss_solar_uigf", 0.0)),
            ss_wind_uigf=float(si.get("ss_wind_uigf", 0.0)),
        )
        for si in data.get("intervals", [])
    ]
    return StpasaResult(
        region=data.get("region", ""),
        run_datetime=data.get("run_datetime", ""),
        intervals=intervals,
        fetched_at=data.get("fetched_at", ""),
    )


class StpasaStore:
    """Per-region persistence for the latest STPASA REGIONSOLUTION result."""

    def __init__(self, hass: HomeAssistant, region: str) -> None:
        self._hass = hass
        self._region = region
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _stpasa_storage_key(region)
        )
        self._latest: StpasaResult | None = None

    async def load(self) -> StpasaResult | None:
        """Load the persisted STPASA result.  Returns None if stale/missing."""
        try:
            data = await self._store.async_load()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("STPASA store load failed (non-fatal): %s", exc)
            return None
        if not data:
            return None
        status = _cache_status(data.get("fetched_at", ""))
        if status == "expired":
            _LOGGER.debug("STPASA store: cached result is expired — ignoring")
            return None
        result = _result_from_dict(data)
        if status == "stale":
            result.is_stale = True
            _LOGGER.warning(
                "STPASA store: loaded stale cache (age >90 min) — "
                "OLS calibration will use stale STPASA data until next fetch"
            )
        self._latest = result
        return self._latest

    async def save(self, result: StpasaResult) -> None:
        """Persist *result* and update the in-memory latest."""
        self._latest = result
        await self._store.async_save(asdict(result))

    def latest(self) -> StpasaResult | None:
        """Return the in-memory latest result if available and not expired.

        Returns the result with is_stale=True when the cache is between
        STPASA_CACHE_TTL (90 min) and STAPASA_STALE_TTL (4 h) old.
        Returns None only when the result is missing or older than 4 h.
        """
        if self._latest is None:
            return None
        status = _cache_status(self._latest.fetched_at)
        if status == "expired":
            return None
        self._latest.is_stale = status == "stale"
        return self._latest
