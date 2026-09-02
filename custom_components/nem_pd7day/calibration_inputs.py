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
# STPASA_MIN_HORIZON_H is the hard lower bound, not the effective one: it
# encodes the judgement that Amber/CSIRO cover the near term better, which
# holds whatever STPASA happens to cover. The effective lower edge is resolved
# per run by stpasa_effective_min_horizon_h. Beyond 120h STPASA is
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

# The nearest-match bridge above is one interval wide, so a run's usable
# coverage effectively reaches half an hour below its earliest interval START.
STPASA_COVERAGE_MARGIN_H = STPASA_MAX_MATCH_SECONDS / 3600.0

# One second of slack on the resolved band edge. The interval that the coverage
# margin exists to admit lands exactly on the edge, so two different float
# divisions by 3600 decide whether it is in band. Horizons here are half-hourly,
# so a second cannot admit an interval that was not already on the boundary.
STPASA_BAND_EDGE_SLACK_H = 1.0 / 3600.0


def stpasa_effective_min_horizon_h(
    run_at_iso: str | None,
    coverage_start_epoch: float | None,
) -> float:
    """
    Resolve the lower edge of the stage-2 STPASA band for one forecast run.

    WHY this cannot be a constant: AEMO scopes Short Term PASA to six trading
    days from the end of the trading day covered by the most recent
    pre-dispatch schedule, so coverage begins at a trading day boundary and the
    horizon at which it begins moves with the forecast run time. Observed live
    on this install, a 16:05 run first reached h39, leaving 17h of open band
    with no data behind it, while a later run left only about 2h. A single
    hardcoded floor cannot track that, and it drifts silently if AEMO changes
    the product horizon.

    The resolved edge is the earliest covered interval START expressed as a
    horizon against run_at, less STPASA_COVERAGE_MARGIN_H. Subtracting the
    margin is deliberate: it puts the band edge exactly where the bounded
    nearest-match in stpasa_features_for_interval already stops matching, so
    this function never removes a match that used to succeed. Without the
    margin the one-interval END/START bridge kept by issue #67 would be lost
    for the single interval immediately below coverage.

    STPASA_MIN_HORIZON_H remains the floor of the floor, and the static value
    is returned unchanged when run_at or coverage is unknown, because widening
    the band on the strength of missing data is the failure mode being fixed.

    This lives here rather than in sensor.py (issue #75 put it there) so that
    the tariff path, which now shares calibrate_interval, resolves the same
    per-run edge. Two copies of this decision is exactly the drift issue #66
    was about.
    """
    if not run_at_iso or coverage_start_epoch is None:
        return STPASA_MIN_HORIZON_H
    try:
        run_at_epoch = parse_iso(run_at_iso).timestamp()
    except (ValueError, TypeError):
        return STPASA_MIN_HORIZON_H
    coverage_h = (coverage_start_epoch - run_at_epoch) / 3600.0
    return max(
        STPASA_MIN_HORIZON_H,
        coverage_h - STPASA_COVERAGE_MARGIN_H - STPASA_BAND_EDGE_SLACK_H,
    )


def stpasa_coverage_start(result) -> "tuple[str | None, float | None]":
    """
    Earliest covered interval START of a STPASA run, as (iso, epoch).

    Returns (None, None) when the run holds no parseable interval, so callers
    surface missing coverage rather than a zero horizon.
    """
    if result is None or not getattr(result, "intervals", None):
        return None, None
    from .nem_time import interval_start

    best_iso: str | None = None
    best_epoch: float | None = None
    for si in result.intervals:
        try:
            start_iso = interval_start(si.interval_datetime)
            epoch = parse_iso(start_iso).timestamp()
        except (ValueError, TypeError):
            continue
        if best_epoch is None or epoch < best_epoch:
            best_iso = start_iso
            best_epoch = epoch
    return best_iso, best_epoch


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
    run_at_iso: str | None = None,
) -> "StpasaFeatures | None":
    """
    Look up STPASA features for a forecast interval from the coordinator's
    STPASA store.  Returns None when STPASA is unavailable or the horizon is
    outside the OLS band.

    The band's upper edge is the constant STPASA_MAX_HORIZON_H. Its lower edge
    is resolved per run by stpasa_effective_min_horizon_h, which never returns
    less than STPASA_MIN_HORIZON_H. When run_at_iso is not supplied the lower
    edge falls back to that constant.

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

    # Dynamic lower edge. Checked here rather than beside the static gate above
    # because it is a property of this run's coverage, which is only known once
    # the index is loaded. sorted_intervals is sorted by epoch, so element 0
    # carries the earliest covered START. Deliberately not logged: see the note
    # above on this function's call frequency.
    coverage_start_epoch = sorted_intervals[0][0] if sorted_intervals else None
    if horizon_hours_value < stpasa_effective_min_horizon_h(
        run_at_iso, coverage_start_epoch
    ):
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

        # Bisect the (epoch, interval) tuples directly. A one-element probe
        # compares on epoch alone and never reaches the StpasaInterval, so no
        # per-call copy of the run's epochs is built for every in-band interval
        # that misses the exact key.
        pos = bisect.bisect_left(sorted_intervals, (target_epoch,))
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
    run_at_iso: str | None = None,
) -> dict | None:
    """Calibrate one interval with the full input set, or return None.

    This is the single entry point for calibrating a forecast interval. Every
    sensor that publishes a calibrated spot price for an interval of a PD7DAY
    run calls this, so two sensors describing the same interval of the same run
    cannot disagree.

    run_at_iso is the PD7DAY run timestamp, the same value the caller used to
    compute horizon_hours_value. It is threaded through because the stage-2
    band floor is a property of the run's STPASA coverage, not a constant: see
    stpasa_effective_min_horizon_h. Passing it here rather than only on the
    forecast path is what gives the tariff sensors the same per-run floor. When
    it is None the floor falls back to the static STPASA_MIN_HORIZON_H, which
    is the wider band, so callers that cannot supply it are no worse off than
    before but they do lose the tighter gate.

    Returns None when there is nothing honest to publish: no calibration store,
    or no raw price. Callers must surface that as ``None``/``unavailable``, not
    as 0 and not as the uncalibrated raw price, which would look plausible and
    be wrong.
    """
    if store is None or raw_price is None:
        return None
    covariates = covariates_for_interval(coordinator, interval_key)
    stpasa_features = stpasa_features_for_interval(
        coordinator, interval_key, horizon_hours_value, run_at_iso=run_at_iso
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
