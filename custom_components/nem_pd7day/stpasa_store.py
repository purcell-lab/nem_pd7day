"""
NEM PD7DAY STPASA Store
=======================
HA .storage persistence for the latest StpasaResult, per region.

Storage key : nem_pd7day.stpasa.{region.lower()}
Version     : 1
Cache TTL   : 90 minutes (STPASA publishes ~30 min; 90 min = 3 cycles tolerance)

On load failure or stale cache the in-memory latest() returns None, which
makes the calibration pipeline fall through to isotonic-only silently.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION
from .stpasa_client import StpasaInterval, StpasaResult

_LOGGER = logging.getLogger(__name__)

STPASA_CACHE_TTL = timedelta(minutes=90)


def _stpasa_storage_key(region: str) -> str:
    return f"nem_pd7day.stpasa.{region.lower()}"


def _is_fresh(fetched_at: str) -> bool:
    """Return True if fetched_at (UTC ISO-8601) is within the cache TTL."""
    if not fetched_at:
        return False
    try:
        dt = datetime.fromisoformat(fetched_at)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) <= STPASA_CACHE_TTL


def _result_from_dict(data: dict) -> StpasaResult:
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
        self._store = Store(hass, STORAGE_VERSION, _stpasa_storage_key(region))
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
        if not _is_fresh(data.get("fetched_at", "")):
            _LOGGER.debug("STPASA store: cached result is stale — ignoring")
            return None
        self._latest = _result_from_dict(data)
        return self._latest

    async def save(self, result: StpasaResult) -> None:
        """Persist *result* and update the in-memory latest."""
        self._latest = result
        await self._store.async_save(asdict(result))

    def latest(self) -> StpasaResult | None:
        """Return the in-memory latest result if still fresh, else None."""
        if self._latest is None:
            return None
        if not _is_fresh(self._latest.fetched_at):
            return None
        return self._latest
