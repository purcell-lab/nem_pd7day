"""
The observation side of the calibration store (issue #366).

``CalibrationObservations`` is the part of ``CalibrationStore`` that keeps the
observation log: it caches each PD7DAY run in the forecast history, pairs
actual prices with the forecasts that covered them, persists both, and answers
the reads made of the log (the rows, their count, the oldest and the STPASA
feature map). The pure work is in ``forecast_history`` (ingest and pruning),
``actual_recorder`` (matching and averaging actuals) and ``refit_service`` (the
feature map); this class holds the state those functions are given and the
order of its awaits.

It imports nothing from Home Assistant. ``CalibrationStore`` builds the files
and reads the clock, through ``_history_file`` and ``_now`` below, so that the
Home Assistant ``Store`` and the module-wide ``_now_nem`` the tests replace
both stay in ``calibration_store``. The state lives on the store object, set
in its ``__init__``, so a store the tests build with ``__new__`` and plain
attribute assignments keeps working.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Sequence

from .actual_recorder import record_actual
from .calibration_engine import StpasaFeatures
from .calibration_summary import oldest_observation as _oldest_observation
from .const import MAX_FORECAST_AGE_DAYS
from .forecast_history import ingest_run, prune_history
from .refit_service import stpasa_feature_map

if TYPE_CHECKING:
    from .json_repository import JsonRepository
    from .observation_log import ObservationLog
    from .pd7day_client import PD7DayData, InterconnectorData, CaseSolutionData, MarketSummaryData
    from .stpasa_client import StpasaResult

_LOGGER = logging.getLogger(__name__)


class CalibrationObservations:
    """Forecast history, actual pairing and the observation log of one region."""

    # The daily segmented observation log (issue #130); CalibrationStore
    # defines it, built on first use.
    _log: ObservationLog

    # Forecast history: interval_time_iso → list of forecast entries
    # Keys and run_at values are ISO-8601 +10:00 strings.
    _forecast_history: dict[str, list[dict]]

    # Running average accumulator for actual RRP per (interval_time, forecast_run_at).
    # Amber reports 5-minute dispatch prices; PD7DAY forecasts 30-minute trading
    # interval averages.  We average all Amber readings within the interval so the
    # actual_rrp stored in the observation log matches the quantity PD7DAY forecasts.
    #
    # Structure: {(interval_time, forecast_run_at): {"sum": float, "count": int, "obs_idx": int}}
    # obs_idx is the index into _observations so we can update actual_rrp in-place.
    _actual_accum: dict[tuple[str, str], dict]

    def _now(self) -> datetime:
        """The current NEM time; CalibrationStore reads its module's clock."""
        raise NotImplementedError

    def _history_file(self) -> JsonRepository:
        """The forecast history file; CalibrationStore builds it."""
        raise NotImplementedError

    @property
    def _observations(self) -> list[dict[str, Any]]:
        """The flat observation list, owned by the daily segmented log."""
        return self._log.observations

    @_observations.setter
    def _observations(self, rows: list[dict[str, Any]]) -> None:
        self._log.replace_all(rows)

    async def _load_observations(self, single_file: Any, legacy_unscoped: Any) -> None:
        """Load the daily segments, migrating the older stores into them.

        Daily segments are the current format. With no manifest the single
        file store is split into segments and removed, and before that the
        unscoped legacy key is tried, so an install from any version lands
        in the current format after one load (issue #130).
        """
        migrated_from: list[Any] = []

        async def _from_store(store: Any) -> list[dict] | None:
            data = await store.async_load()
            rows = (data or {}).get("observations") if isinstance(data, dict) else None
            if rows:
                migrated_from.append(store)
            return rows

        await self._log.async_load(
            legacy_loaders=(
                lambda: _from_store(single_file),
                lambda: _from_store(legacy_unscoped),
            )
        )
        for store in migrated_from:
            remove = getattr(type(store), "async_remove", None)
            if remove is not None:
                await store.async_remove()

    # ── Forecast history management ───────────────────────────────────────────

    async def ingest_forecast(
        self,
        region: str,
        price_data: "PD7DayData",
        interconnectors: dict[str, "InterconnectorData"],
        case: "CaseSolutionData | None",
        market_summary: "MarketSummaryData | None" = None,
        stpasa: "StpasaResult | None" = None,
    ) -> None:
        """
        Called by the coordinator on each successful fetch.
        All interval_time keys and run_at values are ISO-8601 +10:00 strings.
        """
        run_at_str = price_data.forecast_generated_at or self._now().isoformat()
        ingest_run(
            self._forecast_history,
            region=region,
            run_at=run_at_str,
            price_data=price_data,
            interconnectors=interconnectors,
            case=case,
            market_summary=market_summary,
            stpasa=stpasa,
        )

        # Prune old history — compare ISO strings directly (fixed offset sorts correctly)
        cutoff = (self._now() - timedelta(days=MAX_FORECAST_AGE_DAYS)).isoformat()
        self._forecast_history = prune_history(self._forecast_history, cutoff)

        await self._save_forecast_history()

    async def _save_forecast_history(self) -> None:
        await self._history_file().save({"forecast_history": self._forecast_history})

    # ── Observation logging ───────────────────────────────────────────────────

    async def async_record_actual(
        self,
        interval_time: str,   # ISO-8601 +10:00 NEM time
        actual_rrp: float,
        calibration_region: str | None = None,
        source: str = "unknown",
    ) -> int:
        """
        Match the actual RRP for an interval against all PD7DAY forecasts
        that covered it.  Horizon is computed from tz-aware datetimes so it
        is accurate regardless of system timezone.
        """
        new_count = record_actual(
            self._forecast_history,
            self._actual_accum,
            self._log,
            interval_time=interval_time,
            actual_rrp=actual_rrp,
            calibration_region=calibration_region,
            source=source,
        )

        if new_count:
            await self._save_observations()
            _LOGGER.debug(
                "Logged %d observations for interval %s (total=%d)",
                new_count, interval_time, len(self._observations),
            )

        return new_count

    async def _save_observations(self) -> None:
        """Persist whatever the daily segmented log has changed.

        Only the days touched since the last save are written, through Home
        Assistant's delayed save so a burst inside OBS_SAVE_DELAY_S becomes
        one write per day, and days dropped by pruning are removed. The
        previous design rewrote the whole log, about 50 MB per region at
        MAX_TOTAL_OBS, on every settled interval (issue #130).
        """
        await self._log.async_save()

    # ── Reads ─────────────────────────────────────────────────────────────────

    def build_stpasa_feature_map(self) -> dict[str, StpasaFeatures]:
        """
        Build dict[str → StpasaFeatures] from observations that carry STPASA data.

        Key = interval_time + "|" + forecast_run_at — matches the lookup key used
        by CalibrationEngine.fit_ols_stage2().
        """
        return stpasa_feature_map(self._observations)

    @property
    def observations(self) -> Sequence[dict]:
        """The raw observation list, for tod_stats computation.

        This is the live list, not a copy: it can hold MAX_TOTAL_OBS entries
        and is read on every tod_stats.compute, so copying is not free. The
        Sequence annotation is the contract; callers must not mutate it
        (issue #110).
        """
        return self._observations

    @property
    def observation_count(self) -> int:
        return len(self._observations)

    @property
    def oldest_observation(self) -> str | None:
        """Interval time of the oldest retained observation, or None."""
        return _oldest_observation(self._observations)
