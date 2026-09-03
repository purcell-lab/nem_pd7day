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
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Sequence

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .calibration_engine import (
    OBSERVATION_WINDOW_DAYS,
    CalibrationEngine,
    CalibrationResult,
    Observation,
    RunFeatures,
    StpasaFeatures,
    all_bucket_keys,
)
from .const import (
    _LEGACY_COEFF_KEY,
    _LEGACY_FH_KEY,
    _LEGACY_OBS_KEY,
    MAX_FORECAST_AGE_DAYS,
    MAX_HORIZON_HOURS,
    MAX_TOTAL_OBS,
    NEM_TZ,
    SPIKE_COVARIATE_CAP,
    SPIKE_COVARIATE_RAW_FLOOR,
    SPIKE_GAS_THRESHOLD_TJ,
    SPIKE_QNI_THRESHOLD_MW,
    STORAGE_VERSION,
    storage_keys,
)

if TYPE_CHECKING:
    from .pd7day_client import PD7DayData, InterconnectorData, CaseSolutionData, MarketSummaryData
    from .stpasa_client import StpasaResult

_LOGGER = logging.getLogger(__name__)


def _now_nem() -> datetime:
    """Return the current time in NEM timezone (AEST, UTC+10)."""
    return datetime.now(NEM_TZ)


class CalibrationStore:
    """
    Coordinates observation logging, coefficient persistence, and
    forecast history caching for the calibration pipeline.
    """

    @staticmethod
    def _parse_nem_iso(s: str) -> datetime:
        """Parse an ISO-8601 NEM time string to a tz-aware datetime."""
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NEM_TZ)
        return dt

    def __init__(self, hass: HomeAssistant, region: str) -> None:
        self._hass = hass
        self._region = region
        obs_key, coeff_key, fh_key = storage_keys(region)
        self._obs_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, obs_key)
        self._coeff_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, coeff_key)
        self._fh_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, fh_key)
        self._engine = CalibrationEngine()

        self._observations: list[dict[str, Any]] = []
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

    # ── Startup ───────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load calibration state from storage, migrating legacy keys if needed."""

        # ── Load observations ────────────────────────────────────────────────
        obs_data = await self._obs_store.async_load()

        if obs_data is None:
            legacy_obs_store: Store[dict[str, Any]] = Store(
                self._hass, STORAGE_VERSION, _LEGACY_OBS_KEY
            )
            legacy_data = await legacy_obs_store.async_load()
            if legacy_data:
                _LOGGER.info(
                    "Migrating observation log from legacy storage key to "
                    "nem_pd7day.%s.observation_log", self._region.lower()
                )
                await self._obs_store.async_save(legacy_data)
                obs_data = legacy_data

        self._observations = (obs_data or {}).get("observations", [])

        # ── Load coefficients ────────────────────────────────────────────────
        coeff_data = await self._coeff_store.async_load()

        if coeff_data is None:
            legacy_coeff_store: Store[dict[str, Any]] = Store(
                self._hass, STORAGE_VERSION, _LEGACY_COEFF_KEY
            )
            legacy_data = await legacy_coeff_store.async_load()
            if legacy_data:
                _LOGGER.info(
                    "Migrating calibration coefficients from legacy storage key to "
                    "nem_pd7day.%s.calibration_coefficients", self._region.lower()
                )
                await self._coeff_store.async_save(legacy_data)
                coeff_data = legacy_data

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
        fh_data = await self._fh_store.async_load()

        if fh_data is None:
            legacy_fh_store: Store[dict[str, Any]] = Store(
                self._hass, STORAGE_VERSION, _LEGACY_FH_KEY
            )
            legacy_data = await legacy_fh_store.async_load()
            if legacy_data:
                _LOGGER.info(
                    "Migrating forecast history from legacy storage key to "
                    "nem_pd7day.%s.forecast_history", self._region.lower()
                )
                await self._fh_store.async_save(legacy_data)
                fh_data = legacy_data

        self._forecast_history = (fh_data or {}).get("forecast_history", {})

        # Rebuild in-memory accumulator index from observations
        self._actual_accum = {
            (o["interval_time"], o["forecast_run_at"]): {
                "sum": o["actual_rrp"],
                "count": 1,
                "obs_idx": i,
            }
            for i, o in enumerate(self._observations)
            if "interval_time" in o and "forecast_run_at" in o
        }

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
        is_intervention = case.intervention if case else False

        # Build an interval-START → StpasaInterval lookup for O(1) join.
        # STPASA interval_datetime is interval-END (AEMO convention); the
        # forecast_history key is interval-START (= END − 30 min), so we key
        # the lookup by the START to match.
        stpasa_by_start: dict[str, object] = {}
        if stpasa is not None:
            from .nem_time import interval_start
            for si in stpasa.intervals:
                try:
                    start_key = interval_start(si.interval_datetime)
                except (ValueError, TypeError):
                    continue
                stpasa_by_start[start_key] = si

        # Build per-interval lookups from the interconnector forecast
        qni = interconnectors.get("NSW1-QLD1")
        qni_mwflow_by_time: dict[str, float | None] = {}
        qni_violation_by_time: dict[str, float | None] = {}
        if qni:
            for p in qni.forecast:
                qni_mwflow_by_time[p.time] = p.mwflow
                qni_violation_by_time[p.time] = p.violationdegree

        # Build a date→gas_tj lookup from market_summary for O(1) per-interval access.
        # Gas forecast is daily resolution — key is the date portion of the AEMO nemtime.
        # Use nemtime (interval-END / raw AEMO timestamp), NOT time (interval-START),
        # because interval_start() subtracts 30 min, which for midnight timestamps
        # shifts the date back by one day and breaks the lookup.
        gas_by_date: dict[str, float | None] = {}
        if market_summary:
            for g in market_summary.forecast:
                date_key = g.nemtime[:10]
                gas_by_date[date_key] = g.value_tj

        for period in price_data.forecast:
            # Key must be ISO string — period.time is already an ISO string
            # (interval START). current_nem_interval() also returns ISO strings
            # so both sides of the lookup are consistent str keys.
            key = period.time if isinstance(period.time, str) else period.time.astimezone(NEM_TZ).isoformat()
            if key not in self._forecast_history:
                self._forecast_history[key] = []

            # Deduplicate by (interval_time, run_at): if this forecast run was
            # already ingested (e.g. HA restarted and refetched the same AEMO
            # file, or startup + scheduled fetch returned identical data), skip
            # it.  Without this guard, each Amber reading would be averaged
            # against N duplicate run_at entries and corrupt the running average.
            if any(e["run_at"] == run_at_str for e in self._forecast_history[key]):
                continue

            # Match gas_tj by the date of the interval start
            interval_date = key[:10]  # "YYYY-MM-DD" prefix of ISO string
            gas_tj = gas_by_date.get(interval_date)  # None if no gas data for this date

            entry: dict[str, Any] = {
                "run_at": run_at_str,
                "forecast_price": period.value,
                "gas_tj": gas_tj,
                "qni_mwflow": qni_mwflow_by_time.get(key),
                "qni_violation": qni_violation_by_time.get(key),
                "is_intervention": is_intervention,
                "region": region,
            }

            # Join STPASA signals for this interval if available.
            si = stpasa_by_start.get(key)
            if si is not None:
                entry["stpasa_run_at"] = si.run_datetime
                entry["stpasa_demand10"] = si.demand10
                entry["stpasa_demand50"] = si.demand50
                entry["stpasa_demand90"] = si.demand90
                entry["stpasa_surplus"] = si.surpluscapacity
                entry["stpasa_solar"] = si.ss_solar_uigf
                entry["stpasa_wind"] = si.ss_wind_uigf

            self._forecast_history[key].append(entry)

        # Prune old history — compare ISO strings directly (fixed offset sorts correctly)
        cutoff = (_now_nem() - timedelta(days=MAX_FORECAST_AGE_DAYS)).isoformat()
        self._forecast_history = {
            k: v for k, v in self._forecast_history.items() if k >= cutoff
        }

        await self._save_forecast_history()

    async def _save_forecast_history(self) -> None:
        await self._fh_store.async_save({"forecast_history": self._forecast_history})

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
        forecasts = self._forecast_history.get(interval_time, [])
        if not forecasts:
            _LOGGER.debug(
                "No forecast history for interval %s — skipping", interval_time
            )
            return 0

        interval_dt = self._parse_nem_iso(interval_time)
        new_count = 0

        for fc in forecasts:
            if calibration_region and fc.get("region") != calibration_region:
                continue

            try:
                run_dt = self._parse_nem_iso(fc["run_at"])
            except (ValueError, KeyError):
                continue

            # Both datetimes are tz-aware (UTC+10) — subtraction is unambiguous
            horizon_h = (interval_dt - run_dt).total_seconds() / 3600
            if horizon_h < 0 or horizon_h > MAX_HORIZON_HOURS:
                continue

            pair_key = (interval_time, fc["run_at"])

            if pair_key in self._actual_accum:
                # Subsequent Amber 5-min reading within same 30-min interval.
                # Update the running average in the existing observation in-place.
                acc = self._actual_accum[pair_key]
                acc["sum"] += actual_rrp
                acc["count"] += 1
                new_avg = acc["sum"] / acc["count"]
                self._observations[acc["obs_idx"]]["actual_rrp"] = round(new_avg, 6)
                _LOGGER.debug(
                    "Updated actual_rrp for interval %s run_at %s: "
                    "avg=%.4f over %d readings",
                    interval_time, fc["run_at"], new_avg, acc["count"],
                )
                new_count += 1   # signal that a save is needed
                continue

            # First Amber reading for this pair — create a new observation.
            obs = {
                "interval_time": interval_time,
                "horizon_hours": round(horizon_h, 2),
                "pd7day_forecast": fc["forecast_price"],
                "actual_rrp": actual_rrp,
                "forecast_run_at": fc["run_at"],
                "hour_of_day": interval_dt.hour,   # NEM local hour (UTC+10)
                "day_of_week": interval_dt.weekday(),
                "month": interval_dt.month,
                "gas_forecast_tj": fc.get("gas_tj"),
                "qni_mwflow": fc.get("qni_mwflow"),
                "qni_violation_degree": fc.get("qni_violation"),
                "is_intervention": fc.get("is_intervention", False),
                "actual_source": source,
            }

            # Derive STPASA features only when this forecast entry carries every
            # input. A missing MW field is now None rather than 0.0, and the
            # previous `.get(key, 0.0)` defaults would have turned that back
            # into a zero and fed it to the fit as a real observation. An
            # incomplete interval is omitted from the fit instead. See #43.
            surplus = fc.get("stpasa_surplus")
            solar = fc.get("stpasa_solar")
            demand50 = fc.get("stpasa_demand50")
            demand10 = fc.get("stpasa_demand10")
            demand90 = fc.get("stpasa_demand90")
            if None not in (surplus, solar, demand50, demand10, demand90):
                obs["stpasa_log_surplus"] = math.log1p(max(surplus, 0.0))
                obs["stpasa_log_solar"] = math.log1p(max(solar, 0.0))
                obs["stpasa_log_demand"] = math.log(max(demand50, 1.0))
                obs["stpasa_poe_spread_n"] = (
                    demand10 - demand90
                ) / max(demand50, 1.0)
                obs["stpasa_run_at"] = fc.get("stpasa_run_at", "")

            obs_idx = len(self._observations)
            self._observations.append(obs)
            self._actual_accum[pair_key] = {
                "sum": actual_rrp,
                "count": 1,
                "obs_idx": obs_idx,
            }
            new_count += 1

        if new_count:
            if len(self._observations) > MAX_TOTAL_OBS:
                self._observations = self._observations[-MAX_TOTAL_OBS:]
            await self._save_observations()
            _LOGGER.debug(
                "Logged %d observations for interval %s (total=%d)",
                new_count, interval_time, len(self._observations),
            )

        return new_count

    async def _save_observations(self) -> None:
        await self._obs_store.async_save({"observations": self._observations})

    # ── STPASA feature map ─────────────────────────────────────────────────────

    def build_stpasa_feature_map(self) -> dict[str, StpasaFeatures]:
        """
        Build dict[str → StpasaFeatures] from observations that carry STPASA data.

        Key = interval_time + "|" + forecast_run_at — matches the lookup key used
        by CalibrationEngine.fit_ols_stage2().
        """
        out: dict[str, StpasaFeatures] = {}
        for o in self._observations:
            # Require every derived feature rather than defaulting absent ones
            # to 0.0. Observations recorded before #43 may hold a partial set,
            # and a zero standing in for a missing feature becomes a training
            # input rather than a skipped interval.
            features = (
                o.get("stpasa_log_surplus"),
                o.get("stpasa_log_solar"),
                o.get("stpasa_log_demand"),
                o.get("stpasa_poe_spread_n"),
            )
            if None in features:
                continue
            log_surplus, log_solar, log_demand, poe_spread_n = features
            key = f"{o['interval_time']}|{o['forecast_run_at']}"
            out[key] = StpasaFeatures(
                log_surplus=log_surplus,
                log_solar=log_solar,
                log_demand=log_demand,
                poe_spread_n=poe_spread_n,
                stpasa_run_at=o.get("stpasa_run_at", ""),
            )
        return out

    # ── Calibration fitting ───────────────────────────────────────────────────

    async def async_refit(self) -> CalibrationResult:
        obs_list = [
            Observation(
                interval_time=o["interval_time"],
                horizon_hours=o["horizon_hours"],
                pd7day_forecast=o["pd7day_forecast"],
                actual_rrp=o["actual_rrp"],
                forecast_run_at=o["forecast_run_at"],
                hour_of_day=o["hour_of_day"],
                day_of_week=o["day_of_week"],
                month=o["month"],
                gas_forecast_tj=o.get("gas_forecast_tj"),
                qni_mwflow=o.get("qni_mwflow"),
                qni_violation_degree=o.get("qni_violation_degree"),
                is_intervention=o.get("is_intervention", False),
            )
            for o in self._observations
        ]

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

        await self._coeff_store.async_save(self._engine.to_storage(result))

        # Append compression_ratio snapshot to rolling iso_history.
        summary = result.summary()
        history_record = {
            "fitted_at": result.fitted_at,
            "buckets": {
                key: bucket["compression_ratio"]
                for key, bucket in summary["buckets"].items()
            },
        }
        self._iso_history.append(history_record)
        # Keep at most 48 records (48 × 8h fetches ≈ 16 days).
        if len(self._iso_history) > 48:
            self._iso_history = self._iso_history[-48:]

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
        qni_mwflow: float | None = None,
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
        # annotate whether the gas+QNI covariates support the spike signal.
        # The calibrated value is NEVER modified by this gate — it always uses
        # the isotonic result.  The gate is purely informational.
        from .calibration_engine import SPIKE_THRESHOLD
        if raw_price >= SPIKE_THRESHOLD:
            if (
                gas_forecast_tj is not None
                and qni_mwflow is not None
            ):
                cal["spike_credible"] = (
                    gas_forecast_tj > SPIKE_GAS_THRESHOLD_TJ
                    and qni_mwflow < SPIKE_QNI_THRESHOLD_MW
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
            "observations_in_window": self._calibration.observations_in_window,
            "active_buckets": self.active_bucket_count,
            "total_buckets": len(all_bucket_keys()),
            "summary": self._calibration.summary(),
        }
