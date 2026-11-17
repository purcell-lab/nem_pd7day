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

Structure (spec 004, issue #366)
--------------------------------
CalibrationStore is the facade every caller uses. It builds the Home Assistant
files, reads the clock (``_now_nem`` below, which tests replace module-wide),
loads at start and serves the calibrated price and the diagnostics. It is
assembled from two parts that do not import Home Assistant, one per job:
``calibration_observations.CalibrationObservations`` (forecast ingest, actual
recording and the observation log, issue #130) and
``calibration_fit.CalibrationFit`` (restoring and refitting the calibration,
the fit generation and the iso history). The state of all three lives on the
one store object. The pure work is done by ``json_repository`` (the
coefficient and forecast history files with their legacy-key migration),
``forecast_history``, ``actual_recorder``, ``refit_service`` and
``calibration_summary`` (the diagnostics).

Timezone policy
---------------
All stored datetime strings are ISO-8601 with explicit +10:00 offset
(NEM time).  Horizon calculations always operate on tz-aware datetimes
so they are correct regardless of the HA system timezone.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .actual_recorder import rebuild_accumulator
from .calibration_engine import CalibrationEngine, RunFeatures, StpasaFeatures
from .calibration_fit import CalibrationFit
from .calibration_summary import effective_window_days as _effective_window_days
from .calibration_summary import summary_attributes as _summary_attributes
from .const import (
    _LEGACY_COEFF_KEY,
    _LEGACY_FH_KEY,
    _LEGACY_OBS_KEY,
    NEM_TZ,
    STORAGE_VERSION,
    storage_keys,
)
from .json_repository import JsonRepository
from .observation_log import ObservationLog
from .serving import annotate_spike, passthrough_result

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


class CalibrationStore(CalibrationFit):
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
        # What each of these holds is set out where the part that owns it
        # declares it: calibration_fit and calibration_observations.
        self._calibration = None
        self._fit_generation = 0
        self._forecast_history = {}
        self._actual_accum = {}
        self._iso_history = []

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

    # The coefficient and forecast history files are built on access from the
    # store's ``_coeff_store`` and ``_fh_store``, not held, so a store made
    # through ``__new__`` with only those attributes set (as the tests do)
    # works. The legacy Store is built only when the scoped key turns out to
    # be empty.

    def _coefficient_file(self) -> JsonRepository:
        return JsonRepository(
            self._coeff_store,
            lambda: Store(self._hass, STORAGE_VERSION, _LEGACY_COEFF_KEY),
            _COEFF_MIGRATION,
        )

    def _history_file(self) -> JsonRepository:
        return JsonRepository(
            self._fh_store,
            lambda: Store(self._hass, STORAGE_VERSION, _LEGACY_FH_KEY),
            _FH_MIGRATION,
        )

    def _now(self) -> datetime:
        # Looked up at each call, so a test that replaces _now_nem reaches ingest.
        return _now_nem()

    # ── Startup ───────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Load calibration state from storage, migrating legacy keys if needed."""
        # ── Load observations ────────────────────────────────────────────────
        # From the daily segments, or else from the single file store and
        # then the unscoped legacy key, which are migrated and removed.
        await self._load_observations(
            self._obs_store, Store(self._hass, STORAGE_VERSION, _LEGACY_OBS_KEY)
        )

        # ── Load coefficients ────────────────────────────────────────────────
        self._restore(await self._coefficient_file().load(self._region))

        # ── Load forecast history ─────────────────────────────────────────────
        fh_data = await self._history_file().load(self._region)
        self._forecast_history = (fh_data or {}).get("forecast_history", {})

        # Rebuild the in-memory accumulator from observations (issue #132).
        self._actual_accum = rebuild_accumulator(self._observations)

        _LOGGER.info(
            "CalibrationStore loaded: %d observations, %d forecast history keys (region=%s)",
            len(self._observations),
            len(self._forecast_history),
            self._region,
        )

    # ── Serving ───────────────────────────────────────────────────────────────

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
        interval_dt: "datetime | None" = None,
    ) -> dict:
        if self._calibration is None:
            return passthrough_result(raw_price)
        # The interval start and this store's region pick the bucket the
        # interval was trained in (#208).
        cal = self._calibration.apply(
            raw_price, horizon_hours, hour_of_day, stpasa=stpasa_features, run_features=run_features,
            interval_dt=interval_dt, region=getattr(self, "_region", None),
        )
        # The spike credibility annotation (#176), informational only.
        return annotate_spike(cal, raw_price, gas_forecast_tj, network_tight)

    # ── Diagnostics ───────────────────────────────────────────────────────────

    @property
    def effective_window_days(self) -> float | None:
        """Days from the oldest retained observation to now, one decimal."""
        oldest = self.oldest_observation
        if oldest is None:
            return None
        return _effective_window_days(oldest, _now_nem())

    def summary_attributes(self) -> dict:
        return _summary_attributes(
            self._calibration,
            observation_count=self.observation_count,
            oldest=self.oldest_observation,
            window_days=self.effective_window_days,
            active_buckets=self.active_bucket_count,
        )
