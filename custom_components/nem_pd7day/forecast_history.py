"""
Forecast history: every PD7DAY run's price per interval, joined with its inputs.

The history maps an interval START (ISO-8601 +10:00 string) to one entry per
forecast run that covered it. Each entry carries the run's price and the
covariates known when the run was published: the gas forecast for that day,
the QNI interconnector flow and violation for that interval, the intervention
flag and, when STPASA covers the interval, its demand, surplus, solar and wind.
``CalibrationStore.async_record_actual`` later pairs an actual price with these
entries to build observations.

The stored field order is part of the file format in ``.storage``; keep it.
This module reads no clock: the caller passes the run time and the pruning
cutoff (spec 004).
"""
from __future__ import annotations

from typing import Any, Mapping

from .const import NEM_TZ, QNI_INTERCONNECTOR_ID
from .nem_time import interval_start


def _stpasa_by_start(stpasa: Any | None) -> dict[str, Any]:
    """Build an interval-START → StpasaInterval lookup for O(1) join.

    STPASA interval_datetime is interval-END (AEMO convention); the
    forecast_history key is interval-START (= END − 30 min), so we key
    the lookup by the START to match.
    """
    by_start: dict[str, Any] = {}
    if stpasa is not None:
        for si in stpasa.intervals:
            try:
                start_key = interval_start(si.interval_datetime)
            except (ValueError, TypeError):
                continue
            by_start[start_key] = si
    return by_start


def _qni_by_time(
    interconnectors: Mapping[str, Any],
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Per-interval QNI flow and violation lookups from the interconnector forecast."""
    qni = interconnectors.get(QNI_INTERCONNECTOR_ID)
    mwflow_by_time: dict[str, float | None] = {}
    violation_by_time: dict[str, float | None] = {}
    if qni:
        for p in qni.forecast:
            mwflow_by_time[p.time] = p.mwflow
            violation_by_time[p.time] = p.violationdegree
    return mwflow_by_time, violation_by_time


def _gas_by_date(market_summary: Any | None) -> dict[str, float | None]:
    """Build a date→gas_tj lookup from market_summary for O(1) per-interval access.

    Gas forecast is daily resolution — key is the date portion of the AEMO nemtime.
    Use nemtime (interval-END / raw AEMO timestamp), NOT time (interval-START),
    because interval_start() subtracts 30 min, which for midnight timestamps
    shifts the date back by one day and breaks the lookup.
    """
    gas_by_date: dict[str, float | None] = {}
    if market_summary:
        for g in market_summary.forecast:
            date_key = g.nemtime[:10]
            gas_by_date[date_key] = g.value_tj
    return gas_by_date


def _join_stpasa(entry: dict[str, Any], si: Any | None) -> None:
    """Join STPASA signals for this interval if available."""
    if si is not None:
        entry["stpasa_run_at"] = si.run_datetime
        entry["stpasa_demand10"] = si.demand10
        entry["stpasa_demand50"] = si.demand50
        entry["stpasa_demand90"] = si.demand90
        entry["stpasa_surplus"] = si.surpluscapacity
        entry["stpasa_solar"] = si.ss_solar_uigf
        entry["stpasa_wind"] = si.ss_wind_uigf


def ingest_run(
    history: dict[str, list[dict]],
    *,
    region: str,
    run_at: str,
    price_data: Any,
    interconnectors: Mapping[str, Any],
    case: Any | None,
    market_summary: Any | None,
    stpasa: Any | None,
) -> None:
    """Append one entry per interval of ``price_data`` to ``history``, in place.

    All interval_time keys and run_at values are ISO-8601 +10:00 strings.
    """
    is_intervention = case.intervention if case else False
    stpasa_by_start = _stpasa_by_start(stpasa)
    qni_mwflow_by_time, qni_violation_by_time = _qni_by_time(interconnectors)
    gas_by_date = _gas_by_date(market_summary)

    for period in price_data.forecast:
        # Key must be ISO string — period.time is already an ISO string
        # (interval START). current_nem_interval() also returns ISO strings
        # so both sides of the lookup are consistent str keys.
        key = period.time if isinstance(period.time, str) else period.time.astimezone(NEM_TZ).isoformat()
        if key not in history:
            history[key] = []

        # Deduplicate by (interval_time, run_at): if this forecast run was
        # already ingested (e.g. HA restarted and refetched the same AEMO
        # file, or startup + scheduled fetch returned identical data), skip
        # it.  Without this guard, each Amber reading would be averaged
        # against N duplicate run_at entries and corrupt the running average.
        if any(e["run_at"] == run_at for e in history[key]):
            continue

        # Match gas_tj by the date of the interval start
        interval_date = key[:10]  # "YYYY-MM-DD" prefix of ISO string
        gas_tj = gas_by_date.get(interval_date)  # None if no gas data for this date

        entry: dict[str, Any] = {
            "run_at": run_at,
            "forecast_price": period.value,
            "gas_tj": gas_tj,
            "qni_mwflow": qni_mwflow_by_time.get(key),
            "qni_violation": qni_violation_by_time.get(key),
            "is_intervention": is_intervention,
            "region": region,
        }
        _join_stpasa(entry, stpasa_by_start.get(key))
        history[key].append(entry)


def prune_history(history: dict[str, list[dict]], cutoff_iso: str) -> dict[str, list[dict]]:
    """A new history without the intervals before ``cutoff_iso``.

    Compares ISO strings directly: every key carries the fixed +10:00 offset,
    so string order is time order.
    """
    return {k: v for k, v in history.items() if k >= cutoff_iso}
