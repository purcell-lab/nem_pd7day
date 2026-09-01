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
rather than silently dropping to isotonic-only. Loading a stale cache also
requests an immediate refetch through StpasaRefreshCoordination, so the stale
window is measured in one download rather than lasting until the next PD7DAY
coordinator update.
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
from .stpasa_refresh import StpasaRefreshCoordination

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


def _opt_float(value: Any) -> float | None:
    """Coerce a cached value to float, returning None when it is missing.

    Every numeric STPASA field previously defaulted to 0.0 here, so a partial
    or truncated cache payload read back as a genuine 0 MW. Zero is meaningful
    for demand, availability and reserve, so missing data has to stay None.
    See issue #43.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _result_from_dict(data: dict[str, Any]) -> StpasaResult:
    intervals = [
        StpasaInterval(
            interval_datetime=si.get("interval_datetime", ""),
            run_datetime=si.get("run_datetime", ""),
            demand10=_opt_float(si.get("demand10")),
            demand50=_opt_float(si.get("demand50")),
            demand90=_opt_float(si.get("demand90")),
            surpluscapacity=_opt_float(si.get("surpluscapacity")),
            ss_solar_uigf=_opt_float(si.get("ss_solar_uigf")),
            ss_wind_uigf=_opt_float(si.get("ss_wind_uigf")),
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

    # Class-level defaults so a store built without a coordination object, or
    # constructed directly in tests, still degrades to the plain warn-only path
    # instead of raising AttributeError.
    _refresh: StpasaRefreshCoordination | None = None
    loaded_stale: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        region: str,
        refresh: StpasaRefreshCoordination | None = None,
    ) -> None:
        self._hass = hass
        self._region = region
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _stpasa_storage_key(region)
        )
        self._latest: StpasaResult | None = None
        # Shared across all five region stores. It is what turns "this cache is
        # stale" into an actual refetch, and it collapses the five identical
        # per-region warnings into one.
        self._refresh = refresh
        # True when load() found a cache past the fresh TTL. Read by
        # async_setup_entry to force an immediate fetch.
        self.loaded_stale = False

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
            self.loaded_stale = True
            # Before v3.1.7 this branch only warned, and nothing downstream read
            # the staleness, so no refetch was triggered and the startup refit
            # consumed the stale cache. Requesting the fetch through the shared
            # coordination object is what makes the warning actionable.
            should_warn = True
            if self._refresh is not None:
                should_warn = self._refresh.note_stale_load(self._region)
            if should_warn:
                _LOGGER.warning(
                    "STPASA store: loaded stale cache (age >90 min, discarded "
                    "at 4 h), forcing an immediate STPASA fetch so the startup "
                    "calibration refit is not fitted on stale STPASA"
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
