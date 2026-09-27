"""
NEM PD7DAY Calibration Store
==============================
Manages two persistent JSON files in HA's .storage directory:

  nem_pd7day.observation_log
    Rolling window of paired (forecast, actual) observations.
    Written every time an actual RRP is received from Amber.
    Pruned to MAX_TOTAL_OBS entries (oldest dropped first).

  nem_pd7day.calibration_coefficients
    Serialised CalibrationResult produced by CalibrationEngine.fit().
    Written every time a refit completes (default: every 24 hours).

Timezone policy
---------------
All stored datetime strings are ISO-8601 with explicit +10:00 offset
(NEM time).  Horizon calculations always operate on tz-aware datetimes
so they are correct regardless of the HA system timezone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Sequence

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .actual_recorder import rebuild_accumulator, record_actual
from .forecast_history import ingest_run, prune_history
from .json_repository import JsonRepository
from .observation_log import ObservationLog
from .refit_service import (
    ISO_HISTORY_LIMIT,
    engine_observations,
    iso_history_record,
    stpasa_feature_map,
)
from .calibration_engine import (
    OBSERVATION_WINDOW_DAYS,
    CalibrationEngine,
    CalibrationResult,
    RunFeatures,
    StpasaFeatures,
    all_bucket_keys,
)
from .const import (
    _LEGACY_COEFF_KEY,
    _LEGACY_FH_KEY,
    _LEGACY_OBS_KEY,
    MAX_FORECAST_AGE_DAYS,
    NEM_TZ,
    SPIKE_GAS_THRESHOLD_TJ,
    STORAGE_VERSION,
    storage_keys,
)

if TYPE_CHECKING:
    from .pd7day_client import PD7DayData, InterconnectorData, CaseSolutionData, MarketSummaryData
    from .stpasa_client import StpasaResult

_LOGGER = logging.getLogger(__name__)

_COEFF_MIGRATION = (
    "Migrating calibration coefficients from legacy storage key to "
    "nem_pd7day.%s.calibration_coefficients"
)
_FH_MIGRATION = (
    "Migrating forecast history from legacy storage key to "
    "nem_pd7day.%s.forecast_history"
)


def _now_nem() -> datetime:
    """Return the current time in NEM timezone (AEST, UTC+10)."""
    return datetime.now(NEM_TZ)


# The coefficient and forecast history files are built on access from the
# store's ``_coeff_store`` and ``_fh_store``, not held, so a store made through
# ``__new__`` with only those attributes set (as the tests do) works. The
# legacy Store is built only when the scoped key turns out to be empty.

def _coefficient_file(store: CalibrationStore) -> JsonRepository:
    return JsonRepository(
        store._coeff_store,
        lambda: Store(store._hass, STORAGE_VERSION, _LEGACY_COEFF_KEY),
        _COEFF_MIGRATION,
    )


def _history_file(store: CalibrationStore) -> JsonRepository:
    return JsonRepository(
        store._fh_store,
        lambda: Store(store._hass, STORAGE_VERSION, _LEGACY_FH_KEY),
        _FH_MIGRATION,
    )


class CalibrationStore:
    """
    Coordinates observation logging, coefficient persistence, and
    forecast history caching for the calibration pipeline.
    """

    def __init__(self, hass: HomeAssistant, region: str) -> None:
        self._hass = hass
        self._region = region
        obs_key, coeff_key, fh_key = storage_keys(region)
        self._obs_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, obs_key)
        self._coeff_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, coeff_key)
        self._fh_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, fh_key)
        self._engine = CalibrationEngine()

        # Daily segmented observation log (issue #130). ``_obs_store`` is the
        # previous single-file store, read once to migrate and then removed.
        self._log = ObservationLog(hass, region)
        self._calibration: CalibrationResult | None = None
        # Monotonic counter, bumped every time the calibration changes in a way
        # that changes calibrated output. Consumers memoise calibrated forecasts
        # and need a cache key that is stable while the fit is unchanged and
        # different afterwards.
        #
        # Identity of the CalibrationResult object cannot serve that purpose.
        # async_refit assigns the result and then mutates it in place, setting
        # result.ols_models for the OLS stage 2 fit, so the object is the same
        # object before and after a change that moves every calibrated price.
        # CPython also recycles id() values, so a fresh result allocated where a
        # discarded one used to live can compare equal to the stale key.
        self._fit_generation = 0

        # Forecast history: interval_time_iso → list of forecast entries
        # Keys and run_at values are ISO-8601 +10:00 strings.
        self._forecast_history: dict[str, list[dict]] = {}

        # Running average accumulator for actual RRP per (interval_time, forecast_run_at).
        # Amber reports 5-minute dispatch prices; PD7DAY forecasts 30-minute trading
        # interval averages.  We average all Amber readings within the interval so the
        # actual_rrp stored in the observation log matches the quantity PD7DAY forecasts.
        #
        # Structure: {(interval_time, forecast_run_at): {"sum": float, "count": int, "obs_idx": int}}
        # obs_idx is the index into _observations so we can update actual_rrp in-place.
        self._actual_accum: dict[tuple[str, str], dict] = {}

        # Rolling history of compression_ratio per bucket across recent fit cycles.
        # In-memory only (not persisted) — resets on HA restart. A plain list:
        # the store is built per region, so the dict keyed by region that used
        # to sit here could only ever hold one key (issue #110).
        self._iso_history: list[dict] = []

    @property
    def _log(self) -> ObservationLog:
        """The daily segmented observation log, built on first use.

        Built in ``__init__`` for real stores. Many tests construct the store
        with ``__new__`` and assign only the attributes they need, so the log
        is also created lazily from ``_hass`` and ``_region`` rather than
        requiring every such helper to know about it.
        """
        log = self.__dict__.get("_log")
        if log is None:
            log = ObservationLog(
                getattr(self, "_hass", None), getattr(self, "_region", "")
            )
            self.__dict__["_log"] = log
        return log

    @_log.setter
    def _log(self, log: ObservationLog) -> None:
        self.__dict__["_log"] = log

    @property
    def _observations(self) -> list[dict[str, Any]]:
        """The flat observation list, owned by the daily segmented log."""
        return self._log.observations

    @_observations.setter
    def _observations(self, rows: list[dict[str, Any]]) -> None:
        self._log.replace_all(rows)

    # ── Startup ───────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load calibration state from storage, migrating legacy keys if needed."""

        # ── Load observations ────────────────────────────────────────────────
        # Daily segments are the current format. With no manifest the single
        # file store is split into segments and removed, and before that the
        # unscoped legacy key is tried, so an install from any version lands
        # in the current format after one load (issue #130).
        single_file = self._obs_store
        legacy_unscoped: Store[dict[str, Any]] = Store(
            self._hass, STORAGE_VERSION, _LEGACY_OBS_KEY
        )
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

        # ── Load coefficients ────────────────────────────────────────────────
        coeff_data = await _coefficient_file(self).load(self._region)

        if coeff_data:
            try:
                self._calibration = self._engine.from_storage(coeff_data)
                self._fit_generation += 1
                _LOGGER.info(
                    "PD7DAY calibration: restored coefficients fitted at %s (%d obs)",
                    self._calibration.fitted_at,
                    self._calibration.total_observations,
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "PD7DAY calibration: could not restore coefficients: %s", exc
                )

        # ── Load forecast history ─────────────────────────────────────────────
        fh_data = await _history_file(self).load(self._region)

        self._forecast_history = (fh_data or {}).get("forecast_history", {})

        # Rebuild the in-memory accumulator from observations (issue #132).
        self._actual_accum = rebuild_accumulator(self._observations)

        _LOGGER.info(
            "CalibrationStore loaded: %d observations, %d forecast history keys (region=%s)",
            len(self._observations),
            len(self._forecast_history),
            self._region,
        )

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
        run_at_str = price_data.forecast_generated_at or _now_nem().isoformat()
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
        cutoff = (_now_nem() - timedelta(days=MAX_FORECAST_AGE_DAYS)).isoformat()
        self._forecast_history = prune_history(self._forecast_history, cutoff)

        await self._save_forecast_history()

    async def _save_forecast_history(self) -> None:
        await _history_file(self).save({"forecast_history": self._forecast_history})

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

    # ── STPASA feature map ─────────────────────────────────────────────────────

    def build_stpasa_feature_map(self) -> dict[str, StpasaFeatures]:
        """
        Build dict[str → StpasaFeatures] from observations that carry STPASA data.

        Key = interval_time + "|" + forecast_run_at — matches the lookup key used
        by CalibrationEngine.fit_ols_stage2().
        """
        return stpasa_feature_map(self._observations)

    # ── Calibration fitting ───────────────────────────────────────────────────

    async def async_refit(self) -> CalibrationResult:
        obs_list = engine_observations(self._observations)

        result = await self._hass.async_add_executor_job(
            self._engine.fit, obs_list, self._region
        )
        self._calibration = result
        self._fit_generation += 1

        # ── OLS stage2 (STPASA) ──────────────────────────────────────────────
        # Best-effort: only fit when STPASA-tagged observations exist.  Failure
        # leaves the isotonic-only result intact.
        stpasa_map = self.build_stpasa_feature_map()
        if stpasa_map:
            try:
                ols_models = await self._hass.async_add_executor_job(
                    self._engine.fit_ols_stage2, obs_list, stpasa_map, self._region
                )
                result.ols_models = ols_models
                # Mutates the object already published as self._calibration, so
                # the generation has to move again or memoised consumers keep
                # serving stage 1 output.
                self._fit_generation += 1
                _LOGGER.info(
                    "OLS stage2 fit: %d buckets with STPASA data", len(ols_models)
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("OLS stage2 fit failed (non-fatal): %s", exc)

        await _coefficient_file(self).save(self._engine.to_storage(result))

        # Append compression_ratio snapshot to rolling iso_history.
        self._iso_history.append(iso_history_record(result))
        if len(self._iso_history) > ISO_HISTORY_LIMIT:
            self._iso_history = self._iso_history[-ISO_HISTORY_LIMIT:]

        return result

    # ── Public accessors ──────────────────────────────────────────────────────

    @property
    def calibration(self) -> CalibrationResult | None:
        return self._calibration

    @property
    def fit_generation(self) -> int:
        """Monotonic counter identifying the current fit.

        Increments on restore from storage, on refit, and on the in place OLS
        stage 2 update. Safe to use as part of a memoisation key, which object
        identity is not. Zero means nothing has been fitted or restored yet.
        """
        return self._fit_generation

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
    def active_bucket_count(self) -> int:
        if not self._calibration:
            return 0
        return sum(
            1 for m in self._calibration.models.values()
            if not m.ols.is_default
        )

    @property
    def iso_history(self) -> list[dict]:
        """Rolling compression_ratio history for this region (in-memory only)."""
        return self._iso_history

    def apply_to_price(
        self,
        raw_price: float,
        horizon_hours: float,
        hour_of_day: int,
        *,
        gas_forecast_tj: float | None = None,
        network_tight: bool | None = None,
        stpasa_features: "StpasaFeatures | None" = None,
        run_features: "RunFeatures | None" = None,
    ) -> dict:
        if self._calibration is None:
            return {
                "calibrated": round(raw_price, 6),
                "p10": None,
                "p50": None,
                "p90": None,
                "ols_mae": None,
                "calibrated_source": "passthrough",
                "n_obs": 0,
            }
        cal = self._calibration.apply(
            raw_price,
            horizon_hours,
            hour_of_day,
            stpasa=stpasa_features,
            run_features=run_features,
        )

        # Spike credibility annotation: when raw_price is in spike territory,
        # annotate whether the gas and network covariates support the spike
        # signal. The calibrated value is NEVER modified by this gate, it always
        # uses the isotonic result. The gate is purely informational.
        #
        # network_tight is computed per region from that region's own
        # interconnectors, in that region's own direction. It replaced a
        # hardcoded Queensland to New South Wales flow test that scored every
        # region on one link and left three regions unable to return anything
        # but None. See issue #176.
        #
        # This is the raw gate result and it stays raw. The short-lead
        # suppression that calibration called for is applied where the flag is
        # published as a sensor attribute, not here, because the chart callout
        # path reads this value and is deliberately left on the gate.
        # See SPIKE_COVARIATE_MIN_HORIZON_H and sensor._published_spike_credible.
        from .calibration_engine import SPIKE_THRESHOLD
        if raw_price >= SPIKE_THRESHOLD:
            if (
                gas_forecast_tj is not None
                and network_tight is not None
            ):
                cal["spike_credible"] = bool(
                    gas_forecast_tj > SPIKE_GAS_THRESHOLD_TJ
                    and network_tight
                )
            else:
                cal["spike_credible"] = None
        # else: raw below spike territory — no spike_credible key

        return cal

    def apply_calibration(
        self,
        raw_price: float,
        horizon_hours: float,
        hour_of_day: int,
        stpasa_features: "StpasaFeatures | None" = None,
        run_features: "RunFeatures | None" = None,
    ) -> dict:
        """
        Apply calibration with optional STPASA OLS stage2 correction.

        Wraps apply_to_price(): isotonic-only when STPASA features are absent
        or the horizon is outside the OLS band; otherwise applies the 9-feature
        OLS correction.  Passthrough when no calibration is loaded.
        """
        return self.apply_to_price(
            raw_price,
            horizon_hours,
            hour_of_day,
            stpasa_features=stpasa_features,
            run_features=run_features,
        )

    @property
    def oldest_observation(self) -> str | None:
        """Interval time of the oldest retained observation, or None."""
        for obs in self._observations:
            value = obs.get("interval_time")
            if value:
                return str(value)
        return None

    @property
    def effective_window_days(self) -> float | None:
        """Days from the oldest retained observation to now, one decimal."""
        oldest = self.oldest_observation
        if oldest is None:
            return None
        try:
            oldest_dt = datetime.fromisoformat(oldest)
        except ValueError:
            return None
        if oldest_dt.tzinfo is None:
            oldest_dt = oldest_dt.replace(tzinfo=NEM_TZ)
        return round((_now_nem() - oldest_dt).total_seconds() / 86400.0, 1)

    def summary_attributes(self) -> dict:
        if not self._calibration:
            return {
                "status": "no_calibration",
                "observation_count": self.observation_count,
                "active_buckets": 0,
            }
        return {
            "status": "active",
            "fitted_at": self._calibration.fitted_at,
            "observation_count": self.observation_count,
            "observation_window_days": OBSERVATION_WINDOW_DAYS,
            # What the fit could actually see: the age of the oldest retained
            # observation. Shorter than the window whenever MAX_TOTAL_OBS
            # binds (issue #127), and the only honest number to publish.
            "oldest_observation": self.oldest_observation,
            "effective_window_days": self.effective_window_days,
            "observations_in_window": self._calibration.observations_in_window,
            "active_buckets": self.active_bucket_count,
            "total_buckets": len(all_bucket_keys()),
            "summary": self._calibration.summary(),
        }
