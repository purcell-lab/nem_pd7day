"""
Refit inputs and records: what the calibration fit is given, and what it leaves.

``CalibrationStore.async_refit`` runs the stage 1 and stage 2 fits in the
executor and owns the fit generation and the order of every await. This module
holds the pure parts around those awaits: the conversion of stored observation
rows into the engine's ``Observation`` tuples, the STPASA feature map the
stage 2 fit joins on, and the compression-ratio record kept per fit in the
store's in-memory ``iso_history`` (spec 004).
"""
from __future__ import annotations

from typing import Final, Iterable

from .calibration_engine import CalibrationResult, Observation, StpasaFeatures

# Keep at most 48 records (48 × 8h fetches ≈ 16 days).
ISO_HISTORY_LIMIT: Final = 48


def engine_observations(rows: Iterable[dict]) -> list[Observation]:
    """The stored observation rows as the engine's ``Observation`` tuples."""
    return [
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
        for o in rows
    ]


def stpasa_feature_map(rows: Iterable[dict]) -> dict[str, StpasaFeatures]:
    """
    Build dict[str → StpasaFeatures] from observations that carry STPASA data.

    Key = interval_time + "|" + forecast_run_at — matches the lookup key used
    by CalibrationEngine.fit_ols_stage2().
    """
    out: dict[str, StpasaFeatures] = {}
    for o in rows:
        # Require every derived feature rather than defaulting absent ones
        # to 0.0. Observations recorded before #43 may hold a partial set,
        # and a zero standing in for a missing feature becomes a training
        # input rather than a skipped interval.
        log_surplus = o.get("stpasa_log_surplus")
        log_solar = o.get("stpasa_log_solar")
        log_demand = o.get("stpasa_log_demand")
        poe_spread_n = o.get("stpasa_poe_spread_n")
        if log_surplus is None or log_solar is None or log_demand is None or poe_spread_n is None:
            continue
        key = f"{o['interval_time']}|{o['forecast_run_at']}"
        out[key] = StpasaFeatures(
            log_surplus=log_surplus,
            log_solar=log_solar,
            log_demand=log_demand,
            poe_spread_n=poe_spread_n,
            stpasa_run_at=o.get("stpasa_run_at", ""),
        )
    return out


def iso_history_record(result: CalibrationResult) -> dict:
    """The compression_ratio snapshot of one fit, per bucket, for iso_history."""
    summary = result.summary()
    return {
        "fitted_at": result.fitted_at,
        "buckets": {
            key: bucket["compression_ratio"]
            for key, bucket in summary["buckets"].items()
        },
    }
