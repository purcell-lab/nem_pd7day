"""One region's pre-dispatch forecasts and their observations against TradingIS (#337 stage 3a).

Stage 3b fits pre-dispatch's own stage 1 on these observations
(``predispatch_fit``), after each PD7DAY refit and once they reach back
``MIN_HISTORY_DAYS``; ``predispatch_calibration`` serves it. Kept apart from the
PD7DAY calibration store, under
its own storage keys (``nem_pd7day.<region>.pd_forecast_history`` and the
``pd_`` observation segments), so the PD7DAY data is untouched.

- Each new PredispatchIS run is ingested into the forecast history, thinned
  (``predispatch_history``), and the history is pruned to PD_HISTORY_DAYS.
- The shared TradingIS service records each settled half hour here, as it does
  into the PD7DAY store, through the same ``record_actual``. The observation's
  ``pd7day_forecast`` field holds the pre-dispatch forecast: the row format is
  shared so stage 3b can reuse the fitting code.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from homeassistant.helpers.storage import Store

from .actual_recorder import rebuild_accumulator, record_actual
from .const import MAX_TOTAL_OBS_PD, PD_HISTORY_DAYS, PD_KEY_PREFIX, STORAGE_VERSION, pd_forecast_history_key
from .forecast_history import ingest_run, prune_history
from .nem_time import now_nem, to_nem_iso
from .observation_log import ObservationLog
from .predispatch_fit import MIN_HISTORY_DAYS, PdCalibration, fit_pd_calibration
from .predispatch_history import run_forecast
from .refit_service import engine_observations

_LOGGER = logging.getLogger(__name__)


class PredispatchRecord:
    """A region's pre-dispatch forecast history and observation log."""

    def __init__(self, hass: Any, region: str, *, history_store: Any = None, log: Optional[ObservationLog] = None) -> None:
        self._hass = hass
        self._region = region
        self._history_store = history_store or Store(hass, STORAGE_VERSION, pd_forecast_history_key(region))
        self._log = log or ObservationLog(hass, region, key_prefix=PD_KEY_PREFIX)
        self._history: dict[str, list[dict]] = {}
        self._accum: dict[tuple[str, str], dict] = {}
        self._last_run: Optional[datetime] = None
        self._calibration: Optional[PdCalibration] = None
        self._fit_generation = 0

    async def async_load(self) -> None:
        data = await self._history_store.async_load()
        self._history = (data or {}).get("forecast_history", {})
        await self._log.async_load()
        self._accum = rebuild_accumulator(self._log.observations)

    # ── Forecasts in ────────────────────────────────────────────────────────

    async def async_ingest(self, snapshot: Any, now: Optional[datetime] = None) -> int:
        """Ingest one PredispatchIS run; returns the periods kept, 0 for a run already held."""
        if snapshot is None or (self._last_run is not None and snapshot.run_at <= self._last_run):
            return 0
        self._last_run = snapshot.run_at
        forecast = run_forecast(snapshot, self._region)
        ingest_run(
            self._history, region=self._region, run_at=to_nem_iso(snapshot.run_at), price_data=forecast,
            interconnectors={}, case=None, market_summary=None, stpasa=None,
        )
        cutoff = to_nem_iso((now or now_nem()) - timedelta(days=PD_HISTORY_DAYS))
        self._history = prune_history(self._history, cutoff)
        await self._history_store.async_save({"forecast_history": self._history})
        return len(forecast.forecast)

    def listen(self, coordinator: Any) -> Callable[[], None]:
        """Ingest each new run of the shared pre-dispatch coordinator, and the one it holds now."""

        def _on_run() -> None:
            self._hass.async_create_task(self.async_ingest(coordinator.latest))

        if coordinator.latest is not None:
            _on_run()
        unsub: Callable[[], None] = coordinator.async_add_listener(_on_run)
        return unsub

    # ── Actuals in ──────────────────────────────────────────────────────────

    async def async_record_actual(
        self, interval_time: str, actual_rrp: float, calibration_region: Optional[str] = None, source: str = "unknown",
    ) -> int:
        """Pair the settled price with every kept pre-dispatch forecast of the interval."""
        count = record_actual(
            self._history, self._accum, self._log,
            interval_time=interval_time, actual_rrp=actual_rrp,
            calibration_region=calibration_region or self._region, source=source,
            max_total=MAX_TOTAL_OBS_PD,
        )
        if count:
            await self._log.async_save()
        return count

    # ── Stage 3b: fit ───────────────────────────────────────────────────────

    @property
    def calibration(self) -> Optional[PdCalibration]:
        """The fit served, None until the first refit with enough history."""
        return self._calibration

    @property
    def fit_generation(self) -> int:
        """Moves on every refit, for the consumers' memo keys."""
        return self._fit_generation

    async def async_refit(self, pd7day_store: Any = None, now: Optional[datetime] = None) -> Optional[PdCalibration]:
        """Fit pre-dispatch's stage 1 in the executor, once the record reaches back MIN_HISTORY_DAYS.

        ``pd7day_store``, the region's PD7DAY CalibrationStore, serves the
        holdout as the interim would, so ``p24_40`` is admitted only where it
        beats the interim.
        """
        from .predispatch_calibration import interim_kwh

        now = now or now_nem()
        days = self.stats(now)["pd_history_days"]
        if days is None or days < MIN_HISTORY_DAYS:
            _LOGGER.debug("%s: pre-dispatch fit waits for %.0f days of observations (%s)",
                          self._region, MIN_HISTORY_DAYS, days)
            return None
        observations = engine_observations(self._log.observations)  # a copy, on the loop

        def interim(raw: float, horizon: float, start: datetime) -> Optional[float]:
            return interim_kwh(pd7day_store, raw, horizon, start)

        result: PdCalibration = await self._hass.async_add_executor_job(
            fit_pd_calibration, observations, self._region, now, interim if pd7day_store is not None else None,
        )
        self._calibration = result
        self._fit_generation += 1
        _LOGGER.info(
            "%s: pre-dispatch calibration fitted on %d observations; bands served: %s",
            self._region, result.total_observations, ", ".join(sorted(result.admitted)) or "none",
        )
        return result

    def pd_summary(self) -> dict[str, Any]:
        """The ``pd_calibration`` diagnostic: the fit's summary, or why there is none."""
        if self._calibration is None:
            return {"status": "not_fitted", "min_history_days": MIN_HISTORY_DAYS, **self.stats()}
        return {"status": "fitted", **self._calibration.summary()}

    # ── Diagnostics ─────────────────────────────────────────────────────────

    def stats(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """``pd_observations``, and ``pd_history_days``: how far back they reach.

        The log is held oldest day first, so its first row dates the span to the
        day without a scan; the sensor reads this after every dispatch poll.
        """
        observations = self._log.observations
        days: Optional[float] = None
        if observations:
            try:
                oldest = datetime.fromisoformat(observations[0]["interval_time"])
                days = round(((now or now_nem()) - oldest).total_seconds() / 86400, 1)
            except (KeyError, TypeError, ValueError):
                days = None
        return {"pd_observations": len(observations), "pd_history_days": days}
