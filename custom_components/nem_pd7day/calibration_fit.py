"""
The refit side of the calibration store (issue #366).

``CalibrationFit`` is the part of ``CalibrationStore`` that turns the
observation log into a calibration: it restores the fitted coefficients at
start, refits stage 1 and the STPASA stage 2 in the executor, saves the result,
keeps the rolling ``iso_history`` and bumps the fit generation every time the
calibrated output can change. ``refit_service`` holds the pure parts around
those awaits; this class holds the published result and the order of the
awaits.

It imports nothing from Home Assistant: ``CalibrationStore`` builds the
coefficient file through ``_coefficient_file`` below. As with the observation
side, the state lives on the store object, set in its ``__init__``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .calibration_engine import CalibrationEngine, CalibrationResult
from .calibration_observations import CalibrationObservations
from .refit_service import ISO_HISTORY_LIMIT, engine_observations, iso_history_record

if TYPE_CHECKING:
    from .json_repository import JsonRepository

_LOGGER = logging.getLogger(__name__)


class CalibrationFit(CalibrationObservations):
    """The fitted calibration of one region, refitted from its observations."""

    _hass: Any
    _region: str
    _engine: CalibrationEngine
    _calibration: CalibrationResult | None

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
    _fit_generation: int

    # Rolling history of compression_ratio per bucket across recent fit cycles.
    # In-memory only (not persisted) — resets on HA restart. A plain list:
    # the store is built per region, so the dict keyed by region that used
    # to sit here could only ever hold one key (issue #110).
    _iso_history: list[dict]

    def _coefficient_file(self) -> JsonRepository:
        """The calibration coefficient file; CalibrationStore builds it."""
        raise NotImplementedError

    def _restore(self, coeff_data: dict | None) -> None:
        """Publish the coefficients loaded from storage, if they parse."""
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
                    self._engine.fit_ols_stage2, obs_list, stpasa_map, self._region, result
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

        await self._coefficient_file().save(self._engine.to_storage(result))

        # Append compression_ratio snapshot to rolling iso_history.
        self._iso_history.append(iso_history_record(result))
        if len(self._iso_history) > ISO_HISTORY_LIMIT:
            self._iso_history = self._iso_history[-ISO_HISTORY_LIMIT:]

        return result

    # ── Reads ─────────────────────────────────────────────────────────────────

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
