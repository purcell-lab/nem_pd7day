"""
Shared calibration inputs for every sensor that calibrates a PD7DAY price.

Why this module exists
----------------------
The price forecast sensor and the tariff sensors calibrate the same raw price
for the same interval of the same run, and they used to assemble the inputs to
``CalibrationStore.apply_to_price`` separately. The forecast path passed STPASA
features, run features and the gas/QNI covariates; the tariff path passed only
raw price, horizon and hour of day, so it silently took the isotonic only
branch and published a different spot price for the same interval. See issue
#66, where 183 of 183 in band intervals disagreed on a live five region
install, by up to 0.63 $/kWh.

The fix is not to copy the forecast path's feature assembly into the tariff
classes, because the defect is precisely that two copies drifted. Everything
that decides what goes into a calibration call lives here, once, and every
caller goes through ``calibrate_interval``. Parity is then structural: the two
sensors cannot disagree without this function returning two different answers
for identical inputs.

``sensor`` imports ``tariff_sensor`` to build its entity list, so the tariff
classes cannot import ``sensor``. This module is imported by both and imports
neither.
"""
from __future__ import annotations

import bisect
import logging
from typing import TYPE_CHECKING

from .nem_time import parse_iso, to_nem_iso

if TYPE_CHECKING:
    from .calibration_engine import RunFeatures, StpasaFeatures
    from .coordinator import PD7DayCoordinator

_LOGGER = logging.getLogger(__name__)

# STPASA OLS stage2 is applied only within this forecast-horizon band.
# Below 22h Amber/CSIRO covers the near-term; beyond 120h STPASA is
# counterproductive and the pipeline falls through to isotonic-only.
STPASA_MIN_HORIZON_H = 22.0
STPASA_MAX_HORIZON_H = 120.0

# Largest time distance the nearest-match fallback may bridge, in seconds.
# STPASA is a half-hourly product, so a genuine match is either exact or one
# interval away after an END/START convention slip. Anything further means the
# run does not cover this interval at all, and the honest answer is None.
#
# Without this bound the fallback returned the closest interval at any
# distance. AEMO defines Short Term PASA as covering six trading days from the
# end of the trading day covered by the most recent pre-dispatch schedule, so
# it structurally does not reach the near horizon: a 16:05 run began at h39,
# 17h after the h22 band floor. Every in-band interval below h39 was therefore
# scored against pre-dawn features borrowed from up to 17h away, chiefly
# ss_solar_uigf of 0 MW in place of ~3510 MW. That is a feature combination the
# stage-2 fit never sees, because the fit joins on an exact
# interval_time|run_at key and skips intervals with no STPASA row. Serving a
# substitute where training skipped is train/serve skew, and it produced
# 642 $/MWh in a solar trough whose raw forecast was negative.
STPASA_MAX_MATCH_SECONDS = 1800.0


def horizon_hours(run_at_str: str | None, interval_time_str: str) -> float:
    """
    Compute forecast horizon in hours between run_at and interval_time.
    Both inputs are ISO-8601 +10:00 strings; subtraction of tz-aware
    datetimes is unambiguous regardless of the HA system timezone.
    """
    if not run_at_str:
        return 0.0
    try:
        run_at = parse_iso(run_at_str)
        interval = parse_iso(interval_time_str)
        return max(0.0, (interval - run_at).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0


def interval_key_for_period(period) -> str:
    """Canonical interval-START key for a PricePeriod.

    Both call sites must key STPASA and covariate lookups off the same string
    or the lookups can miss on one path and hit on the other, which is a second
    way for the two sensors to disagree. ``to_nem_iso`` normalises the offset
    formatting; an unparseable value falls back to the raw attribute so the
    caller still gets a lookup attempt rather than an exception during a state
    write.
    """
    raw = getattr(period, "time", None)
    try:
        return to_nem_iso(parse_iso(raw))
    except (ValueError, TypeError):
        return raw if isinstance(raw, str) else ""


def stpasa_features_for_interval(
    coordinator: "PD7DayCoordinator",
    interval_time_iso: str,
    horizon_hours_value: float,
) -> "StpasaFeatures | None":
    """
    Look up STPASA features for a forecast interval from the coordinator's
    STPASA store.  Returns None when STPASA is unavailable or the horizon is
    outside the OLS band (h < 22 or h > 120).

    STPASA interval_datetime is the interval END (AEMO convention); the
    forecast_history / PricePeriod key is the interval START.  We match by
    comparing the STPASA END to the PricePeriod END (nemtime) when available;
    here we match on the START-derived value passed in, falling back to the
    nearest interval by absolute time distance.

    The fallback is bounded by STPASA_MAX_MATCH_SECONDS. When the run does not
    cover this interval within that distance the result is None, matching what
    the stage-2 fit does with the same gap, so the interval keeps its
    isotonic-only value rather than being scored against another interval's
    weather.
    """
    if (
        horizon_hours_value < STPASA_MIN_HORIZON_H
        or horizon_hours_value > STPASA_MAX_HORIZON_H
    ):
        return None
    # Use the coordinator's cached interval-START index (built once per STPASA
    # run) instead of a per-interval linear scan over all STPASA intervals.
    # NOTE: staleness is intentionally NOT logged here. This function runs once
    # per forecast interval (~196 intervals across the h22-h120 OLS band, per
    # sensor, every coordinator update), so logging here produced ~2 warnings/s
    # (~212k/day). The stale/failed-fetch condition is logged at most once per
    # cycle in __init__'s _fetch_and_distribute_stpasa instead.
    try:
        result, index_map, sorted_intervals = coordinator.stpasa_index()
    except (AttributeError, TypeError, ValueError):
        # A coordinator that cannot produce an index has no features to offer,
        # which is the same isotonic-only degrade as an empty index. This must
        # never raise into a state write, and it must degrade identically on
        # both call sites or the parity this module exists to guarantee would
        # depend on which sensor asked first.
        return None
    if result is None or not index_map:
        return None

    from .calibration_engine import StpasaFeatures

    # Match on interval START: STPASA interval_datetime is the END, already
    # converted to START in the index.
    chosen = index_map.get(interval_time_iso)
    if chosen is None:
        # O(log n) nearest-match fallback against the sorted (epoch, interval) list.
        try:
            target_epoch = parse_iso(interval_time_iso).timestamp()
        except (ValueError, TypeError):
            return None

        epochs = [e for e, _ in sorted_intervals]
        pos = bisect.bisect_left(epochs, target_epoch)
        best = None
        best_delta: float | None = None
        for cand in (pos - 1, pos):
            if 0 <= cand < len(sorted_intervals):
                e, si = sorted_intervals[cand]
                delta = abs(e - target_epoch)
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best = si
        # Reject a match the run cannot honestly support. Deliberately not
        # logged: see the note above on this function's call frequency.
        if best_delta is None or best_delta > STPASA_MAX_MATCH_SECONDS:
            return None
        chosen = best

    if chosen is None:
        return None
    return StpasaFeatures.from_interval(chosen)


def covariates_for_interval(
    coordinator: "PD7DayCoordinator", interval_key: str
) -> dict:
    """Extract gas_forecast_tj and qni_mwflow for an interval from coordinator data.

    These two only annotate ``spike_credible``; they never move the calibrated
    value. They are still assembled here so that one function decides the whole
    argument list and no caller can pass a subset of it.
    """
    gas_tj: float | None = None
    qni_mw: float | None = None
    data = getattr(coordinator, "data", None)
    if data is None:
        return {"gas_forecast_tj": gas_tj, "qni_mwflow": qni_mw}

    # QNI MW flow lookup
    interconnectors = getattr(data, "interconnectors", None)
    qni_data = interconnectors.get("NSW1-QLD1") if interconnectors else None
    if qni_data:
        for p in qni_data.forecast:
            if p.time == interval_key:
                qni_mw = p.mwflow
                break

    # Gas TJ lookup (daily resolution, keyed by date)
    ms = getattr(data, "market_summary", None)
    if ms:
        interval_date = interval_key[:10]
        for g in ms.forecast:
            if g.nemtime[:10] == interval_date:
                gas_tj = g.value_tj
                break

    return {"gas_forecast_tj": gas_tj, "qni_mwflow": qni_mw}


def run_features_for_coordinator(
    coordinator: "PD7DayCoordinator",
) -> "RunFeatures | None":
    """Run recency features for the current PD7DAY run, or None."""
    try:
        return coordinator.current_run_features
    except (AttributeError, TypeError, ValueError):
        return None


def calibrate_interval(
    store,
    coordinator: "PD7DayCoordinator",
    raw_price: float | None,
    interval_key: str,
    horizon_hours_value: float,
    hour_of_day: int,
) -> dict | None:
    """Calibrate one interval with the full input set, or return None.

    This is the single entry point for calibrating a forecast interval. Every
    sensor that publishes a calibrated spot price for an interval of a PD7DAY
    run calls this, so two sensors describing the same interval of the same run
    cannot disagree.

    Returns None when there is nothing honest to publish: no calibration store,
    or no raw price. Callers must surface that as ``None``/``unavailable``, not
    as 0 and not as the uncalibrated raw price, which would look plausible and
    be wrong.
    """
    if store is None or raw_price is None:
        return None
    covariates = covariates_for_interval(coordinator, interval_key)
    stpasa_features = stpasa_features_for_interval(
        coordinator, interval_key, horizon_hours_value
    )
    run_features = run_features_for_coordinator(coordinator)
    return store.apply_to_price(
        raw_price,
        horizon_hours_value,
        hour_of_day,
        stpasa_features=stpasa_features,
        run_features=run_features,
        **covariates,
    )
