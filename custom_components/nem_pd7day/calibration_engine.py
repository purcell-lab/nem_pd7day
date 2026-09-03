"""
NEM PD7DAY Calibration Engine
==============================
Implementation of:

  1. Isotonic Regression (pure-numpy PAV IsotonicRegression)
       actual ≈ f(forecast),  f monotone non-decreasing
     Used as the primary point-estimate calibrator per horizon/ToD bucket.
     Replaces the previous weighted OLS (actual = a * forecast + b) to handle
     the non-linear saturation of AEMO PD7DAY at high forecast levels and
     longer horizons.  At h24_48+ the OLS actual/forecast ratio collapses to
     0.60–0.69 for the top forecast decile; isotonic regression fits this
     saturation without a linearity assumption.

  2. Quantile Regression (pinball loss, IRLS)
       Fits P10, P50, P90 simultaneously.
     Gives a confidence interval that widens correctly at longer horizons
     and captures price spike probability without requiring scipy/numpy.
     Retained alongside isotonic for interval estimation.

  3. Bucket routing
     Observations are partitioned into 6 horizon × 4 time-of-day = 24
     independent models.  Each bucket is fit separately, so the accuracy
     at 6-hour horizon doesn't contaminate the 5-day horizon model.

     Horizon bands:
       h00_06:   0 ≤ horizon_hours < 6
       h06_12:   6 ≤ horizon_hours < 12
       h12_24:  12 ≤ horizon_hours < 24
       h24_48:  24 ≤ horizon_hours < 48
       h48_96:  48 ≤ horizon_hours < 96
       h96plus:  horizon_hours ≥ 96

     ToD labels (solar elevation via astral, NEM UTC+10):
       peak:          NEM hour 16–20 (hardcoded, overrides solar)
       solar:         solar elevation > 15°, not peak
       morning_ramp:  solar elevation 0°–15°, not peak (~05:00–09:00 NEM)
       shoulder:      solar elevation ≤ 0° (overnight)

  4. Feature vector
     Each observation carries the full feature set collected by the
     integration so the external ML stage (Stage 3, optional) can consume
     the raw log without re-processing.

IsotonicRegression clipping behaviour
--------------------------------------
out_of_bounds="clip" — forecasts outside the training x-range are clipped
to the nearest training boundary rather than extrapolated.  Spike forecasts
(≥ SPIKE_THRESHOLD) now proceed through the isotonic model; clip returns
the training-range maximum — a clean normal-market estimate.  The raw
spike value is preserved in the forecast attribute for display.

Decay weights
--------------
w_i = exp(-DECAY_LAMBDA × days_ago), half-life ≈ 21 days (ln2/0.033).
Passed as sample_weight to IsotonicRegression so recent observations
influence the fit more strongly.

MIN_OBS guard
--------------
Buckets with < MIN_OBS observations return the raw pd7day_forecast
unchanged (passthrough) until data accumulates.

Design constraints
------------------
- Requires only numpy (already a core HA dependency) and astral.
  No scikit-learn or other optional dependencies.
- Safe to call from inside the HA event loop (all CPU work is sync/fast;
  the coordinator offloads fitting to executor via hass.async_add_executor_job).
- Graceful degradation: any bucket with < MIN_OBS observations returns
  passthrough so raw PD7DAY values flow through unchanged.

Quantile regression algorithm
------------------------------
We use Iteratively Reweighted Least Squares (IRLS) with the pinball loss
gradient as the weight function.  For quantile q:

    weight_i = q        if residual_i >= 0  (under-predicted)
    weight_i = (1 - q)  if residual_i <  0  (over-predicted)

Each IRLS iteration fits weighted OLS, then recomputes weights from
residuals.  Convergence is fast (5-10 iterations typical).

Reference: Koenker & Bassett (1978), "Regression Quantiles",
           Econometrica 46(1):33–50.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .stpasa_client import StpasaInterval

from astral import LocationInfo
from astral.sun import elevation as solar_elevation
# ── Pure-numpy isotonic regression ───────────────────────────────────────────
# Replaces sklearn.isotonic.IsotonicRegression to avoid a heavy optional
# dependency that HA's pip installer cannot resolve in all environments.
# Output is numerically identical to sklearn (max diff < 1e-15 on test data).

def _pav(
    y_sorted: np.ndarray, w_sorted: np.ndarray
) -> np.ndarray:
    """Pool-adjacent-violators algorithm on pre-sorted (y, w) arrays.

    Merges adjacent blocks that violate the monotone non-decreasing constraint
    using weighted means.  Returns the fitted y value for each observation
    (in the same sorted order as the inputs).
    """
    blocks: list[list[float]] = []  # each entry: [sum_wy, sum_w, count]
    for yi, wi in zip(y_sorted, w_sorted):
        blocks.append([float(yi) * float(wi), float(wi), 1])
        # Merge while the previous block's weighted mean exceeds this one's
        while (
            len(blocks) >= 2
            and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1])
        ):
            b1, b2 = blocks.pop(-2), blocks.pop(-1)
            blocks.append([b1[0] + b2[0], b1[1] + b2[1], b1[2] + b2[2]])
    fitted = np.empty(int(sum(b[2] for b in blocks)))
    i = 0
    for sum_wy, sum_w, count in blocks:
        fitted[i : i + int(count)] = sum_wy / sum_w
        i += int(count)
    return fitted


class IsotonicRegression:
    """Weighted isotonic regression (monotone non-decreasing) via PAV.

    Drop-in replacement for
    ``sklearn.isotonic.IsotonicRegression(increasing=True,
    out_of_bounds='clip')``.

    ``predict()`` uses ``numpy.interp`` on the sorted training (x, y) pairs
    so predictions are identical to sklearn's implementation (linear
    interpolation between training points, clipped at the boundary values).

    Requires only numpy — no scikit-learn dependency.
    """

    def __init__(self, increasing: bool = True, out_of_bounds: str = "clip") -> None:
        # Parameters accepted for API compatibility; only increasing=True /
        # out_of_bounds='clip' is supported (the only mode used by this integration).
        self._x_thresholds: np.ndarray | None = None
        self._y_thresholds: np.ndarray | None = None

    def fit(
        self,
        x: np.ndarray,
        y: np.ndarray,
        sample_weight: np.ndarray | None = None,
    ) -> "IsotonicRegression":
        """Fit the isotonic model to (x, y) pairs with optional decay weights."""
        x_arr = np.asarray(x, dtype=float)
        y_arr = np.asarray(y, dtype=float)
        w_arr = (
            np.ones(len(x_arr))
            if sample_weight is None
            else np.asarray(sample_weight, dtype=float)
        )
        order = np.argsort(x_arr, kind="stable")
        self._x_thresholds = x_arr[order]
        self._y_thresholds = _pav(y_arr[order], w_arr[order])
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        """Calibrate new forecast values.

        Uses linear interpolation between training x breakpoints.  Values
        outside the training range are clipped to the boundary fitted values
        (``out_of_bounds='clip'`` semantics).
        """
        if self._x_thresholds is None or self._y_thresholds is None:
            raise RuntimeError("IsotonicRegression.fit() must be called before predict()")
        y_thresholds = self._y_thresholds
        return np.interp(
            np.asarray(x, dtype=float),
            self._x_thresholds,
            y_thresholds,
            left=y_thresholds[0],
            right=y_thresholds[-1],
        )

from .const import (
    NEM_TZ,
    ATTR_CAL_BAND_SOURCE,
    HORIZON_EDGES,
    HORIZON_LABELS,
    IRLS_EPS,
    IRLS_ITER,
    IRLS_TOL,
    MAX_OBS,
    MIN_OBS,
    OLS_MIN_OBS,
    QUANTILES,
    TOD_LABELS,
)

_LOGGER = logging.getLogger(__name__)

# ── Spike regime threshold ────────────────────────────────────────────────────
# SPIKE_THRESHOLD applies to observation training only:
#   Observations where EITHER actual_rrp OR pd7day_forecast >= threshold are
#   excluded from isotonic/quantile fitting.  Spike actuals poison the y-side
#   of the fit; spike forecasts are extreme x leverage points that collapse
#   slopes at non-spike forecast levels.
# All inputs (including spikes) proceed through the isotonic model at calibration
# time; out_of_bounds='clip' returns the training-range maximum for spike inputs.
# $3.00/kWh = $3,000/MWh — well above typical peak volatility, below genuine spike territory.
SPIKE_THRESHOLD = 3.00  # $/kWh

# ── Negative passthrough threshold ───────────────────────────────────────────
# When raw <= this value, pass through unchanged without calibration.
# During the solar window AEMO often forecasts mild negatives (~−$0.03/kWh)
# while actuals are near zero — the isotonic model can correct these usefully
# (the step function maps mild negatives toward zero).
# Only deeply negative raws (genuine negative-price events) should bypass
# calibration.  Set to −0.10 $/kWh (−$100/MWh) as the passthrough boundary.
NEGATIVE_PASSTHROUGH_THRESHOLD = -0.10  # $/kWh


def is_negative_passthrough(forecast: float) -> bool:
    """True when a raw forecast bypasses calibration entirely.

    One definition of the boundary, shared by the serving path
    (BucketModel.apply_all, which returns the raw value and the
    "passthrough_negative" source) and by the stage-2 training path
    (CalibrationEngine.fit_ols_stage2, which drops these rows).

    WHY a helper rather than the comparison inlined twice: the two paths have
    to agree, or stage 2 is fitted on a region it is never asked about, or
    worse is asked about a region it never saw. Issue #68 was that class of
    train and serve drift, and the boundary is now a single place to change.
    """
    return forecast <= NEGATIVE_PASSTHROUGH_THRESHOLD


# Key under which BucketModel.apply_all publishes the stage-1 value that stage 2
# uses as its first feature. It is deliberately NOT the published "calibrated"
# price: see stage2_iso_feature below and issue #85.
ISO_FEATURE_KEY = "iso_feature"

# ── Band provenance ──────────────────────────────────────────────────────────
# Which model produced the published p10/p50/p90, published alongside them
# because the answer is no longer always "the stage-1 quantile lines" and a
# consumer cannot tell from the numbers themselves. Issue #72 asked for this
# explicitly: the fallback band and the stage-2 band look identical in the
# attributes and mean quite different things.
# One definition, imported from const so the engine and the sensor attribute
# cannot drift apart the way the calibration inputs did in issue #66.
BAND_SOURCE_KEY = ATTR_CAL_BAND_SOURCE
# The three stage-1 quantile lines, clamped to contain the isotonic value.
BAND_SOURCE_STAGE1 = "stage1_quantile"
# Same lines, unclamped, on the passthrough path. See apply_all.
BAND_SOURCE_STAGE1_RAW = "stage1_quantile_unclamped"
# The raw forecast repeated on all three levels: the deep negative bypass.
BAND_SOURCE_PASSTHROUGH = "raw_passthrough"
# Stage-2 leave-one-out residual quantiles added to the stage-2 prediction.
BAND_SOURCE_STAGE2 = "stage2_residual"
# Stage-2 point estimate with the stage-1 lines re-clamped around it, which is
# what v3.4.0 always published. Now reached only when a bucket has OLS
# coefficients but no usable residual quantiles, and a bound can still collapse
# onto the point estimate here.
BAND_SOURCE_STAGE2_FALLBACK = "stage1_quantile_reclamped"


def stage2_iso_feature(calibrated: dict, forecast: float) -> float:
    """The stage-1 value that stage 2 takes as its first OLS feature.

    One definition, read by the stage-2 training path
    (CalibrationEngine.fit_ols_stage2) and the serving path
    (CalibrationResult.apply), so a row is fitted from the same number the
    same interval would be served from. Drift between those two is the #68 bug
    class, which is why this is a helper rather than a dict lookup written out
    twice.

    WHY this is not the published "calibrated" price: apply_all floors the
    isotonic prediction at 0.0, because a published negative calibrated price
    is not credible above the negative passthrough boundary. For a raw forecast
    in the open interval (-0.10, 0.0), which is above the boundary and so is
    genuinely served by stage 2, that floor set the feature to exactly 0.0
    while the settled actual for the same interval was negative. The regression
    was then asked to explain a negative actual from a feature pinned at zero,
    and the fitted iso_cal coefficient absorbed the error: measured +8.1 per
    cent from one such row in a 78 row bucket and +87.5 per cent from sixteen,
    monotone in the count and the same sign in every seed. Those rows are also
    LESS leveraged than an average row, about 0.55x the bucket mean, so no
    leverage or influence diagnostic would ever surface them. See issue #85.

    The floor stays on the published price. Only the feature is unfloored, so
    nothing a user sees moves. The isotonic model is already fitted on every
    (forecast, actual) pair including negative forecasts, so the unfloored
    prediction is a genuine fitted value rather than an extrapolation, and it
    closes the gap the feature used to have between the boundary and zero.

    The dict fallbacks are for a caller holding a result dict built before this
    split existed, which degrades to the previous behaviour instead of raising.
    """
    value = calibrated.get(ISO_FEATURE_KEY)
    if value is None:
        value = calibrated.get("calibrated", forecast)
    return float(value)


# ── Rolling observation window ────────────────────────────────────────────────
# Only observations within the last N days are used when fitting the
# calibration model.  This prevents stale/seasonal data from corrupting the
# model while all observations are still retained in storage for
# total_increasing state class accounting.
OBSERVATION_WINDOW_DAYS = 90

# ── Observation decay weights ────────────────────────────────────────────────
# Exponential time decay constant for IsotonicRegression sample_weight.
# λ = 0.033 → half-life ≈ 21 days (ln2 / 0.033 ≈ 21).
# Applied to both isotonic and quantile regression fitting.
DECAY_LAMBDA = 0.033

# ── Region capital coordinates (latitude, longitude) ─────────────────────────
REGION_COORDS: dict[str, tuple[float, float]] = {
    "QLD1": (-27.4698, 153.0251),  # Brisbane
    "NSW1": (-33.8688, 151.2093),  # Sydney
    "VIC1": (-37.8136, 144.9631),  # Melbourne
    "SA1":  (-34.9285, 138.6007),  # Adelaide
    "TAS1": (-42.8821, 147.3272),  # Hobart
}


# ── Data structures ───────────────────────────────────────────────────────────

class Observation(NamedTuple):
    """One paired (forecast, actual) data point plus covariates."""
    interval_time: str        # ISO-8601 local naive
    horizon_hours: float      # hours from run_at to interval_time
    pd7day_forecast: float    # raw PD7DAY price $/kWh
    actual_rrp: float         # observed actual RRP $/kWh
    forecast_run_at: str      # ISO-8601 when the PD7DAY study ran
    hour_of_day: int          # 0-23 local
    day_of_week: int          # 0=Mon … 6=Sun
    month: int                # 1-12
    gas_forecast_tj: float | None
    qni_mwflow: float | None
    qni_violation_degree: float | None
    is_intervention: bool


# ── STPASA OLS stage2 horizon gate ───────────────────────────────────────────
# OLS residual correction is applied only inside this horizon band.  Below
# OLS_MIN_HORIZON_H, Amber/CSIRO short-term forecasts dominate; above
# OLS_MAX_HORIZON_H STPASA is empirically counterproductive (backtest).
#
# These stay static deliberately. STPASA coverage begins at a trading day
# boundary, so the horizon at which it begins moves with run time, and the
# serving path narrows its band per run in
# sensor._stpasa_effective_min_horizon_h. The fit must not: its rows span many
# historical runs with different coverage, so filtering them by the current
# run's coverage would drop training data that was genuinely covered when it
# was recorded. The fit already excludes uncovered intervals structurally,
# because it joins on an exact interval_time|run_at key and skips rows with no
# STPASA match.
OLS_MIN_HORIZON_H = 22.0
OLS_MAX_HORIZON_H = 120.0


@dataclass
class StpasaFeatures:
    """Derived STPASA features for a single forecast interval."""
    log_surplus: float       # log1p(surpluscapacity)
    log_solar: float         # log1p(ss_solar_uigf)
    log_demand: float        # log(max(demand50, 1))
    poe_spread_n: float      # (demand10 - demand90) / max(demand50, 1)
    stpasa_run_at: str       # ISO-8601, for attribute tagging

    @classmethod
    def from_interval(cls, interval: "StpasaInterval") -> "StpasaFeatures | None":
        """Derive features, or None when the interval is missing an input.

        Every field below is now optional on the interval, because a missing
        MW value is no longer coerced to 0.0 at parse time. An interval short
        of any input is skipped rather than fitted on a substituted zero,
        which would bias the fit rather than merely display wrongly. The
        caller treats None as "no STPASA features for this interval". See
        issue #43.
        """
        surplus = interval.surpluscapacity
        solar = interval.ss_solar_uigf
        demand50 = interval.demand50
        demand10 = interval.demand10
        demand90 = interval.demand90
        if None in (surplus, solar, demand50, demand10, demand90):
            return None
        return cls(
            log_surplus=math.log1p(max(surplus, 0.0)),
            log_solar=math.log1p(max(solar, 0.0)),
            log_demand=math.log(max(demand50, 1.0)),
            poe_spread_n=(demand10 - demand90) / max(demand50, 1.0),
            stpasa_run_at=interval.run_datetime,
        )


@dataclass
class RunFeatures:
    """PD7DAY run-level features shared by all intervals in one run."""
    run_max_h6_rrp: float    # max raw RRP for h < 6 intervals ($/kWh)
    run_mean_rrp: float      # mean raw RRP for h < 24 intervals ($/kWh)
    run_spread: float        # p90 − p10 of raw RRP for h < 24 intervals ($/kWh)


@dataclass
class ResidualQuantiles:
    """Quantiles of the stage-2 leave-one-out residual for one OLS bucket.

    ``actual - prediction`` in $/kWh, so a band is built by adding these to a
    stage-2 point estimate.  ``q10`` is normally negative and ``q90`` positive.

    WHY these exist at all: the stage-1 q10/q50/q90 lines are single-variable
    regressions of the actual on the RAW PD7DAY forecast.  A stage-2 prediction
    is a different function of nine features those lines have never seen, so
    their spread does not describe the stage-2 error.  Reading it as if it did
    is what forced the #69 re-clamp to collapse a bound onto the point estimate
    on 98 of 330 published intervals in the first live measurement on issue #72.
    """
    bucket_key: str
    q10: float | None = None
    q50: float | None = None
    q90: float | None = None
    n: int = 0

    @property
    def is_fitted(self) -> bool:
        """True when this triple can be published as a stage-2 band.

        Four conditions, all of them load-bearing:

        * ``n >= OLS_MIN_OBS``.  The residuals come from exactly the rows that
          fitted the coefficients, so this is the same floor the OLS itself
          cleared; it is re-checked here because a stored payload from any
          other source has not been through that check.
        * all three levels present.
        * ordered.
        * ``q10 <= 0 <= q90``.  An OLS residual vector sums to zero, so its
          10th and 90th percentiles bracket zero for any ordinary sample, and a
          triple that does not is a symptom rather than a band: it would publish
          an interval that excludes its own point estimate and then be dragged
          back onto it by _clamp_band, which is the collapse this is meant to
          remove.  Such a fit is treated as unfitted and falls back instead.
        """
        if self.n < OLS_MIN_OBS:
            return False
        if self.q10 is None or self.q50 is None or self.q90 is None:
            return False
        if not (self.q10 <= self.q50 <= self.q90):
            return False
        return self.q10 <= 0.0 <= self.q90


@dataclass
class OlsModel:
    """Fitted OLS coefficients for one (horizon_band, tod_bucket) cell."""
    bucket_key: str
    coef: list[float] = field(default_factory=list)  # intercept first, then 8 features
    n_train: int = 0
    r2: float = 0.0
    # Residual quantiles from the same rows and the same fit as ``coef``.
    # WHY they live on this object rather than in a parallel dict on
    # CalibrationResult: the residuals only describe THESE coefficients. Held
    # side by side, a partial storage write or a hand-built result could pair
    # one bucket's coefficients with another's residuals, or keep coefficients
    # and lose residuals, and nothing would notice. Travelling together the
    # pairing cannot come apart.
    resid: ResidualQuantiles | None = None

    def predict(self, features: list[float]) -> float:
        """Apply: intercept + dot(coef[1:], features)."""
        if len(self.coef) < 2:
            return 0.0
        return self.coef[0] + sum(c * x for c, x in zip(self.coef[1:], features))

    def residual_band(
        self, prediction: float
    ) -> tuple[float, float, float] | None:
        """Stage-2 band around ``prediction``, or None when unfitted.

        Additive: the residual quantiles are a single spread per bucket rather
        than a function of the price level.  Bucketing by horizon and
        time-of-day already separates most of the level variation, and a
        location-scale residual model on 50 to 100 rows would be fitting the
        scale on the same handful of order statistics that give the location.
        Stated as a known limitation on issue #72 rather than hidden.
        """
        r = self.resid
        if r is None or not r.is_fitted:
            return None
        return (prediction + r.q10, prediction + r.q50, prediction + r.q90)


@dataclass
class LinearCoeff:
    """Diagnostic coefficients from the weighted OLS fit.

    OLS (actual ≈ a × forecast + b) is retained alongside IsotonicRegression
    to provide interpretable diagnostic attributes in the HA sensor state
    (a, b, mae, rmse) and to initialise the quantile regression solver.
    The OLS prediction (apply()) is no longer used as the primary calibrated
    value — that role belongs to BucketModel.iso_model.
    """
    a: float = 1.0
    b: float = 0.0
    n: int = 0
    mae: float | None = None
    rmse: float | None = None

    @property
    def is_default(self) -> bool:
        return self.n < MIN_OBS

    def apply(self, x: float) -> float:
        """OLS point estimate — used only for quantile initialisation and diagnostics."""
        return self.a * x + self.b


@dataclass
class QuantileCoeff:
    """Quantile regression fit for one quantile level."""
    quantile: float
    a: float = 1.0
    b: float = 0.0
    n: int = 0
    pinball_loss: float | None = None

    @property
    def is_default(self) -> bool:
        return self.n < MIN_OBS

    def apply(self, x: float) -> float:
        return self.a * x + self.b


def _order_band(
    p10: float | None, p50: float | None, p90: float | None
) -> tuple[float | None, float | None, float | None]:
    """Sort the fitted quantile values so that ``p10 <= p50 <= p90``.

    The three quantile lines are fitted independently, so ``a * x + b`` can
    invert for a negative forecast: with slopes 0.4 and 0.7 and x = -0.076 the
    p10 line returns -0.030 while the p90 line returns -0.053.  Ordering is a
    property of a band that holds regardless of how the point estimate was
    produced, so it is enforced separately from containment (see _clamp_band).

    Levels that were not fitted stay ``None`` and keep their slot; the fitted
    values are redistributed across the remaining slots in ascending order.
    """
    fitted = sorted(v for v in (p10, p50, p90) if v is not None)
    ordered = iter(fitted)
    return tuple(  # type: ignore[return-value]
        next(ordered) if level is not None else None for level in (p10, p50, p90)
    )


def _clamp_band(
    calibrated: float,
    p10: float | None,
    p50: float | None,
    p90: float | None,
) -> tuple[float | None, float | None, float | None]:
    """Clamp a quantile band so it contains ``calibrated`` and stays ordered.

    Quantile IRLS sorts slopes but not intercepts, so the fitted lines can
    cross near the x-axis intercept and produce p10 > p90, or a band that does
    not contain the published point estimate.  This enforcement guarantees the
    published triple satisfies ``p10 <= calibrated <= p90`` and
    ``p10 <= p50 <= p90``.

    A fitted p10 is first floored at 0.0, because a quantile line extrapolated
    below zero is not a credible price, and then clamped down to
    ``calibrated``.  Both callers pass a non-negative point estimate, for which
    that order is equivalent to flooring last; it is written this way so a
    future caller passing a negative estimate cannot end up with a lower bound
    above the value it is meant to bracket.

    A ``None`` quantile means that level was not fitted (fewer than MIN_OBS
    observations) and stays ``None`` rather than being invented.

    Every published point estimate must be clamped through this function.
    Stage 2 originally clamped only against the isotonic value and then
    replaced the point estimate without re-clamping, which published a value
    outside its own band on roughly one interval in six (issue #69).
    """
    if p10 is not None:
        p10 = min(max(0.0, p10), calibrated)
    if p90 is not None:
        p90 = max(calibrated, p90)
    if p50 is not None:
        # Same floor reasoning as p10 when there is no fitted p10 to bound by.
        p50_lo = p10 if p10 is not None else min(0.0, calibrated)
        p50_hi = p90 if p90 is not None else float("inf")
        p50 = max(p50_lo, min(p50_hi, p50))
    return p10, p50, p90


@dataclass
class BucketModel:
    """All models for one (horizon, tod) bucket."""
    bucket_key: str
    ols: LinearCoeff = field(default_factory=LinearCoeff)
    q10: QuantileCoeff = field(default_factory=lambda: QuantileCoeff(0.1))
    q50: QuantileCoeff = field(default_factory=lambda: QuantileCoeff(0.5))
    q90: QuantileCoeff = field(default_factory=lambda: QuantileCoeff(0.9))
    # Fitted IsotonicRegression instance (internal PAV), or None when the bucket
    # has fewer than MIN_OBS training observations.  Set during engine.fit().
    # Uses out_of_bounds='clip': forecasts outside the training x-range are
    # clipped to the nearest boundary rather than extrapolated.
    iso_model: IsotonicRegression | None = None

    def raw_band(
        self, x: float
    ) -> tuple[float | None, float | None, float | None]:
        """Unclamped quantile-regression band for forecast ``x``.

        Returns the three fitted quantile lines evaluated at ``x``, before any
        clamping against a point estimate.  Each level is ``None`` when its
        coefficients were not fitted (fewer than MIN_OBS observations).

        Exposed separately from ``apply_all`` so stage 2 can re-derive the band
        from the fits rather than inherit a band already clamped against a
        point estimate it then discards.
        """
        return (
            self.q10.apply(x) if not self.q10.is_default else None,
            self.q50.apply(x) if not self.q50.is_default else None,
            self.q90.apply(x) if not self.q90.is_default else None,
        )

    def apply_all(self, x: float) -> dict:
        """Return calibrated point estimate + confidence interval.

        Calibration path (evaluated in order):
          1. Negative passthrough  — deeply negative forecasts bypass calibration.
          2. Insufficient data     — raw forecast returned if iso_model is None
                                     (bucket has < MIN_OBS training observations).
          3. Isotonic calibration  — IsotonicRegression.predict([x]), clipped >= 0.
                                     Spike inputs (>= SPIKE_THRESHOLD) are handled by
                                     out_of_bounds='clip', returning the training-range
                                     maximum — a clean normal-market estimate.
        """
        if is_negative_passthrough(x):
            return {
                "calibrated": round(x, 6),
                "p10": round(x, 6),
                "p50": round(x, 6),
                "p90": round(x, 6),
                # Nothing is floored on this path, so the feature is the same
                # raw value. Stage 2 is gated off here anyway (PR #74) and
                # these rows are out of the fit (PR #83); the key is present so
                # no caller has to fall back to the published value.
                ISO_FEATURE_KEY: round(x, 6),
                BAND_SOURCE_KEY: BAND_SOURCE_PASSTHROUGH,
                "calibrated_source": "passthrough_negative",
                "n_obs": self.ols.n,
            }

        if self.iso_model is None:
            # Isotonic model not available (< MIN_OBS or not persisted) —
            # pass raw forecast through but still compute quantile intervals
            # if the quantile coefficients are fitted (they survive serialisation).
            # Deliberately NOT clamped against x.  On this path the point
            # estimate is the un-calibrated raw forecast, while the band comes
            # from quantile fits that did survive serialisation, so the two can
            # legitimately disagree: a fitted p10 above the raw forecast is the
            # calibration saying the forecast is too low.  Clamping would erase
            # that signal.  This is the one path where the published value may
            # sit outside its own band, and it is transient — the next
            # engine.fit() restores the isotonic model.
            # Ordering is still enforced: the fitted lines invert for a
            # negative forecast, which no reading of the band can justify.
            p10, p50, p90 = _order_band(*self.raw_band(x))
            return {
                "calibrated": round(x, 6),
                "p10": round(p10, 6) if p10 is not None else None,
                "p50": round(p50, 6) if p50 is not None else None,
                "p90": round(p90, 6) if p90 is not None else None,
                # No isotonic model, so there is nothing to floor and the
                # feature is the raw forecast, exactly as the point estimate is.
                ISO_FEATURE_KEY: round(x, 6),
                BAND_SOURCE_KEY: BAND_SOURCE_STAGE1_RAW,
                "calibrated_source": "passthrough",
                "n_obs": self.ols.n,
            }

        # ── Isotonic calibration ────────────────────────────────────────────
        # IsotonicRegression.predict() with out_of_bounds='clip': forecasts
        # outside the training x-range are clipped to the nearest boundary.
        # Result floored at 0.0 — calibrated prices cannot be physically
        # negative (negative forecasts are caught by passthrough_negative).
        iso_raw = float(self.iso_model.predict(np.asarray([x], dtype=float))[0])
        calibrated = max(iso_raw, 0.0)

        # Clamp the band so it contains calibrated and stays ordered.
        p10, p50, p90 = _clamp_band(calibrated, *self.raw_band(x))

        return {
            "calibrated": round(calibrated, 6),
            # The unfloored prediction, for stage 2 only. Published fields are
            # all derived from the floored value above and are unchanged by
            # this key existing. See stage2_iso_feature and issue #85.
            ISO_FEATURE_KEY: round(iso_raw, 6),
            "p10": round(p10, 6) if p10 is not None else None,
            "p50": round(p50, 6) if p50 is not None else None,
            "p90": round(p90, 6) if p90 is not None else None,
            "ols_mae": self.ols.mae,
            BAND_SOURCE_KEY: BAND_SOURCE_STAGE1,
            "calibrated_source": "isotonic",
            "n_obs": self.ols.n,
        }


@dataclass
class CalibrationResult:
    """Full set of fitted models across all buckets."""
    fitted_at: str
    total_observations: int
    observations_in_window: int = 0
    models: dict[str, BucketModel] = field(default_factory=dict)
    ols_models: dict[str, OlsModel] = field(default_factory=dict)

    def get_bucket(self, horizon_hours: float, hour_of_day: int) -> BucketModel:
        key = _bucket_key(horizon_hours, hour_of_day)
        return self.models.get(key, BucketModel(bucket_key=key))

    def apply(
        self,
        forecast: float,
        horizon_hours: float,
        hour_of_day: int,
        stpasa: "StpasaFeatures | None" = None,
        run_features: "RunFeatures | None" = None,
    ) -> dict:
        # 1. Isotonic (existing) result.
        bucket = self.get_bucket(horizon_hours, hour_of_day)
        result = bucket.apply_all(forecast)

        # 2a. Gate: never override the deliberate negative bypass.
        #
        #     WHY: the stage-2 OLS is fitted in fit_ols_stage2 whose first
        #     feature is the stage-1 output, and apply_all floors that output at
        #     0.0 for every raw forecast above NEGATIVE_PASSTHROUGH_THRESHOLD
        #     while returning the raw value below it. The training set therefore
        #     holds no row whatever between the threshold and zero, and below the
        #     threshold only the deeply negative rows the observation store
        #     happened to accumulate. Those are rare: NEM negative prices are
        #     common but shallow, with the large majority of negative intervals
        #     sitting above -$30/MWh against a -$100/MWh threshold here, and a
        #     bucket needs OLS_MIN_OBS rows before it is fitted at all. A
        #     prediction at a deeply negative forecast is extrapolation, not fit.
        #     It also carries an asymmetric cost: a positive prediction over a
        #     negative raw forecast flips the published sign, turning "paid to
        #     consume" into "pay to consume", which is the one error a battery or
        #     controllable load schedule cannot absorb. The later
        #     `prediction <= 0.0` guard blocks that only by accident, and only
        #     when the prediction happens to be non-positive itself. See #73.
        if result.get("calibrated_source") == "passthrough_negative":
            return result

        # 2b. Gate: STPASA correction only inside the OLS horizon band, and only
        #    when both feature groups are present.
        if (
            stpasa is None
            or run_features is None
            or horizon_hours < OLS_MIN_HORIZON_H
            or horizon_hours > OLS_MAX_HORIZON_H
        ):
            return result

        # 3. Look up the OLS model for this bucket.
        key = _bucket_key(horizon_hours, hour_of_day)
        ols = self.ols_models.get(key)
        if ols is None or len(ols.coef) < 2:
            return result

        # 4. Build the 8-feature vector (intercept handled inside predict()).
        #    The feature is the unfloored stage-1 value, which differs from the
        #    published one only inside the floored band, and is read through
        #    the same helper fit_ols_stage2 uses. See issue #85.
        iso_cal = stage2_iso_feature(result, forecast)
        feature_vec = [
            float(iso_cal),
            run_features.run_max_h6_rrp,
            run_features.run_mean_rrp,
            run_features.run_spread,
            horizon_hours / 168.0,
            stpasa.log_surplus,
            stpasa.log_solar,
            stpasa.log_demand,
            stpasa.poe_spread_n,
        ]

        # 5. Predict.
        prediction = ols.predict(feature_vec)

        # 6. If OLS yields a non-positive value fall back to the isotonic result
        #    rather than clamping to 0.  A clamped-zero calibrated price is
        #    indistinguishable from a genuine zero-forecast and silently discards
        #    the isotonic correction that was already applied in step 1.
        if prediction <= 0.0:
            return result

        # 7. Replace the point estimate, then re-clamp the band around it.
        #
        #    apply_all() in step 1 clamped the band against the *isotonic*
        #    value.  Replacing the point estimate and inheriting that band
        #    published a value outside its own p10 to p90 whenever the stage-2
        #    prediction moved past a stage-1 bound, which on a five-region
        #    snapshot was 522 of 3075 intervals across 9 sensors (issue #69).
        #
        #    The band is re-derived from the unclamped quantile fits rather
        #    than from the already-clamped stage-1 band, so the result is
        #    exactly what apply_all() would have returned had the stage-2
        #    value been the point estimate all along.  Re-clamping the clamped
        #    band instead would inherit a p10 pulled down to the isotonic
        #    value and publish a looser interval than the fits support.
        #
        #    Re-clamping made the triple self-consistent; it did not make the
        #    band a stage-2 interval.  The quantile fits know nothing about the
        #    STPASA features, so where the prediction landed outside them the
        #    nearer bound was pulled onto the point estimate, reporting zero
        #    uncertainty on that side.  On the first live measurement, a single
        #    residential premises in SE Queensland, QLD1, the run at
        #    2026-09-03T07:30:00+10:00, that was 98 of 330 intervals, up from 36
        #    before the re-clamp, and strongly one-sided: 82 onto p10 against 16
        #    onto p90.  The band also did not tighten, median width 0.035764 to
        #    0.036862 $/kWh.  See issue #72.
        #
        #    So the band is now built from the stage-2 model's own residual
        #    quantiles when the bucket has them: prediction plus the 10th, 50th
        #    and 90th percentile of its leave-one-out residuals.  That band is
        #    centred on the prediction by construction, so it contains it
        #    without any clamping and cannot collapse.
        #
        #    _clamp_band is still applied on top, for two reasons that are not
        #    about containment: it floors p10 at 0.0, which is the same floor
        #    every other published lower bound carries, and it is the one place
        #    the ordering and containment invariants are enforced, so leaving it
        #    out would make this the only published triple not passing through
        #    them.  On the residual path it is a no-op except for that floor.
        out = dict(result)
        out["calibrated"] = round(prediction, 6)
        out["calibrated_source"] = "isotonic+stpasa"
        out["stpasa_run_at"] = stpasa.stpasa_run_at

        resid_band = ols.residual_band(prediction)
        if resid_band is not None:
            raw_p10, raw_p50, raw_p90 = resid_band
            band_source = BAND_SOURCE_STAGE2
        else:
            # Fallback: a bucket with coefficients but no usable residual
            # quantiles.  Reached by a store written before issue #72, until the
            # next engine fit rewrites it, and by a bucket whose residual sample
            # failed the validity check in ResidualQuantiles.is_fitted.
            #
            # WHY the old behaviour rather than something safer: the choice is
            # between publishing v3.4.0's re-clamped stage-1 band, which is
            # self-consistent but can collapse a bound, and withholding the
            # stage-2 point estimate entirely, which would move the published
            # price on a path that is otherwise working.  Moving the price to
            # improve the band is the larger change of the two, so the point
            # estimate is kept and the band is labelled.  This is a judgement
            # call and it is written up on the pull request: nothing collapses
            # silently, because BAND_SOURCE_STAGE2_FALLBACK is published on the
            # interval and the fit logs a warning naming the buckets.
            raw_p10, raw_p50, raw_p90 = bucket.raw_band(forecast)
            band_source = BAND_SOURCE_STAGE2_FALLBACK

        p10, p50, p90 = _clamp_band(prediction, raw_p10, raw_p50, raw_p90)
        out["p10"] = round(p10, 6) if p10 is not None else None
        out["p50"] = round(p50, 6) if p50 is not None else None
        out["p90"] = round(p90, 6) if p90 is not None else None
        out[BAND_SOURCE_KEY] = band_source
        return out

    def summary(self) -> dict[str, Any]:
        """Compact summary for diagnostic sensor attributes.

        Per-bucket fields emitted:
          n              — training observation count
          ols_a          — OLS slope (diagnostic only, not used for calibration)
          iso_n_steps    — number of distinct PAV step levels (None if < MIN_OBS)
          x_min          — minimum training forecast value (clip lower bound)
          x_max          — maximum training forecast value (clip upper bound)
          compression_ratio — (y_max - y_min) / (x_max - x_min); <1 = over-forecast
                              None if x_range < 1e-6 or < MIN_OBS
          iso_mae        — isotonic training MAE (mean |y_fitted - y_actual|)
                           None if < MIN_OBS
          spot_010       — calibrated output at 0.10 $/kWh forecast
          spot_020       — calibrated output at 0.20 $/kWh forecast
          q10_a          — quantile P10 slope (used for P10 interval)
          q90_a          — quantile P90 slope (used for P90 interval)
        """
        out: dict[str, Any] = {
            "fitted_at": self.fitted_at,
            "total_observations": self.total_observations,
            "observation_window_days": OBSERVATION_WINDOW_DAYS,
            "observations_in_window": self.observations_in_window,
            "buckets": {},
        }
        for key, model in self.models.items():
            bucket: dict[str, Any] = {
                "n": model.ols.n,
                "ols_a": round(model.ols.a, 4),
                "iso_n_steps": None,
                "x_min": None,
                "x_max": None,
                "compression_ratio": None,
                "iso_mae": None,
                "spot_010": None,
                "spot_020": None,
                "q10_a": round(model.q10.a, 4),
                "q90_a": round(model.q90.a, 4),
            }
            iso = model.iso_model
            if (
                iso is not None
                and iso._x_thresholds is not None
                and iso._y_thresholds is not None
                and len(iso._x_thresholds) > 0
            ):
                xt = iso._x_thresholds
                yt = iso._y_thresholds
                x_min = float(xt[0])
                x_max = float(xt[-1])
                y_min = float(yt[0])
                y_max = float(yt[-1])
                x_range = x_max - x_min
                # n_steps: count distinct PAV blocks (unique consecutive y values)
                n_steps = int(1 + np.sum(np.diff(yt) != 0))
                bucket["iso_n_steps"] = n_steps
                bucket["x_min"] = round(x_min, 4)
                bucket["x_max"] = round(x_max, 4)
                if x_range > 1e-6:
                    bucket["compression_ratio"] = round((y_max - y_min) / x_range, 4)
                # iso_mae: mean absolute calibration shift (mean |fitted - raw|)
                # This measures how much the isotonic model moves forecasts on average.
                calibration_shift_mae = float(np.mean(np.abs(yt - xt)))
                bucket["iso_mae"] = round(calibration_shift_mae, 6)
                # Spot values
                bucket["spot_010"] = round(float(iso.predict(np.array([0.10]))[0]), 4)
                bucket["spot_020"] = round(float(iso.predict(np.array([0.20]))[0]), 4)
            out["buckets"][key] = bucket
        return out

    def get_iso_diagnostics(self, bucket_key: str) -> dict[str, Any] | None:
        """Return the isotonic diagnostics dict for a single bucket, or None."""
        s = self.summary()
        return s["buckets"].get(bucket_key)


# ── Bucket routing helpers ─────────────────────────────────────────────────────

def _horizon_label(horizon_hours: float) -> str:
    for i, edge in enumerate(HORIZON_EDGES[1:], 1):
        if horizon_hours < edge:
            return HORIZON_LABELS[i - 1]
    return HORIZON_LABELS[-1]


def _tod_label(hour: int) -> str:
    """Legacy clock-hour ToD label (used as fallback when no region/datetime available)."""
    if 16 <= hour < 21:
        return "peak"
    if 10 <= hour < 16:
        return "solar"
    return "shoulder"


def _tod_label_solar(dt_nem: datetime, region: str, raw_label: str) -> str:
    """
    Classify a NEM interval into ToD label using solar elevation.

    dt_nem: aware datetime in NEM timezone (UTC+10)
    region: NEM region string e.g. "QLD1"
    raw_label: fallback label if region not in REGION_COORDS
    """
    nem_hour = dt_nem.hour
    # Peak: hardcoded 16:00–21:00 NEM (hour 16,17,18,19,20)
    if 16 <= nem_hour < 21:
        return "peak"

    coords = REGION_COORDS.get(region)
    if coords is None:
        return raw_label  # fallback for unknown regions

    lat, lon = coords
    loc = LocationInfo(latitude=lat, longitude=lon)
    dt_utc = dt_nem.astimezone(timezone.utc)
    el = solar_elevation(loc.observer, dt_utc)

    if el > 15.0:
        return "solar"
    if el > 0.0:
        return "morning_ramp"
    return "shoulder"


def _bucket_key(horizon_hours: float, hour_of_day: int) -> str:
    return f"{_horizon_label(horizon_hours)}__{_tod_label(hour_of_day)}"


def _bucket_key_solar(horizon_hours: float, dt_nem: datetime, region: str) -> str:
    """Bucket key using solar elevation ToD classification."""
    raw = _tod_label(dt_nem.hour)
    tod = _tod_label_solar(dt_nem, region, raw)
    return f"{_horizon_label(horizon_hours)}__{tod}"


def all_bucket_keys() -> list[str]:
    return [
        f"{h}__{t}"
        for h in HORIZON_LABELS
        for t in TOD_LABELS
    ]


# ── Pure-Python OLS ───────────────────────────────────────────────────────────

def _ols(
    pairs: list[tuple[float, float]],
    weights: list[float] | None = None,
) -> tuple[float, float]:
    """
    Fit actual = a * forecast + b using ordinary least squares.

    If *weights* is provided, performs weighted OLS by accumulating the
    weighted normal equations directly: each row's contribution to the sums
    is scaled by its weight w_i. (The sqrt(w) scaling trick applies when a
    design matrix is handed to a least-squares solver; here the sums are
    formed by hand, so the weights go in as-is, issue #110.)

    Returns (a, b).  Falls back to (1, 0) if degenerate.
    """
    n = len(pairs)
    if n < MIN_OBS:
        return 1.0, 0.0

    if weights is not None:
        sx = sum(weights[i] * pairs[i][0] for i in range(n))
        sy = sum(weights[i] * pairs[i][1] for i in range(n))
        sxx = sum(weights[i] * pairs[i][0] * pairs[i][0] for i in range(n))
        sxy = sum(weights[i] * pairs[i][0] * pairs[i][1] for i in range(n))
        wsum = sum(w for w in weights)
        denom = wsum * sxx - sx * sx
        if abs(denom) < 1e-12:
            return 1.0, 0.0
        a = (wsum * sxy - sx * sy) / denom
        b = (sy - a * sx) / wsum
    else:
        sx = sum(x for x, _ in pairs)
        sy = sum(y for _, y in pairs)
        sxx = sum(x * x for x, _ in pairs)
        sxy = sum(x * y for x, y in pairs)
        denom = n * sxx - sx * sx
        if abs(denom) < 1e-12:
            return 1.0, 0.0
        a = (n * sxy - sx * sy) / denom
        b = (sy - a * sx) / n
    return a, b


def _ols_metrics(
    pairs: list[tuple[float, float]], a: float, b: float
) -> tuple[float, float]:
    """Return (MAE, RMSE) for a fitted OLS model."""
    if not pairs:
        return 0.0, 0.0
    residuals = [y - (a * x + b) for x, y in pairs]
    mae = sum(abs(r) for r in residuals) / len(residuals)
    rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    return round(mae, 6), round(rmse, 6)


# ── Pure-Python Quantile Regression (IRLS) ────────────────────────────────────

def _quantile_regression(
    pairs: list[tuple[float, float]],
    quantile: float,
    n_iter: int = IRLS_ITER,
    weights: list[float] | None = None,
) -> tuple[float, float, float]:
    """
    Fit quantile regression for the given quantile level using IRLS.

    Minimises the pinball (check) loss

        sum_i w_i * rho_tau(r_i),   rho_tau(r) = r * (tau - 1[r < 0])

    by iteratively reweighted least squares. At each step row i enters the
    weighted OLS with

        v_i = w_i * (tau if r_i >= 0 else 1 - tau) / max(|r_i|, IRLS_EPS)

    so that v_i * r_i^2 equals w_i * rho_tau(r_i) at the current residuals.

    The 1/|r_i| divisor is what makes this a quantile fit. Without it the
    weights depend only on the sign of the residual, which is asymmetric
    least squares and converges to the tau-expectile instead. On right-skewed
    price data that put the fitted P10 line far too high: about a quarter of
    actuals fell below a line published as the 10th percentile (issue #103).

    IRLS_EPS floors the residual in the denominator, bounding the weight a
    near-zero residual can take. IRLS_ITER caps the iterations and the loop
    also stops once the objective has stopped falling by more than IRLS_TOL
    relative, which is the convergence test that matters; a coefficient
    tolerance alone stopped the old loop long before the quantile was reached.

    *weights* are optional per-pair sample weights, the exponential decay
    weights the OLS and isotonic fits use, so all three fits see the same
    effective sample.

    Returns (a, b, pinball_loss). Falls back to (1, 0, inf) below MIN_OBS.
    """
    n = len(pairs)
    if n < MIN_OBS:
        return 1.0, 0.0, float("inf")

    xs = np.array([p[0] for p in pairs], dtype=float)
    ys = np.array([p[1] for p in pairs], dtype=float)
    sw = np.array(weights, dtype=float) if weights else np.ones(n)
    sw_sum = float(sw.sum())
    if sw_sum <= 0.0:
        return 1.0, 0.0, float("inf")

    def _objective(a_: float, b_: float) -> float:
        r = ys - (a_ * xs + b_)
        rho = np.where(r >= 0, quantile * r, (quantile - 1.0) * r)
        return float((sw * rho).sum() / sw_sum)

    # Initialise with (weighted) OLS
    a, b = _ols(pairs, weights=weights if weights else None)
    obj = _objective(a, b)

    for _ in range(n_iter):
        r = ys - (a * xs + b)
        tau_w = np.where(r >= 0, quantile, 1.0 - quantile)
        v = sw * tau_w / np.maximum(np.abs(r), IRLS_EPS)

        s = float(v.sum())
        sx = float((v * xs).sum())
        sy = float((v * ys).sum())
        sxx = float((v * xs * xs).sum())
        sxy = float((v * xs * ys).sum())

        denom = s * sxx - sx * sx
        # Cauchy-Schwarz gives denom >= 0, with equality only when every x is
        # the same; scale the degeneracy test to the sums rather than testing
        # against an absolute 1e-12 that the 1/|r| weights would dwarf.
        if denom <= 1e-12 * max(s * sxx, 1e-300):
            break
        a_new = (s * sxy - sx * sy) / denom
        b_new = (sy - a_new * sx) / s
        obj_new = _objective(a_new, b_new)
        a, b = a_new, b_new
        converged = abs(obj - obj_new) <= IRLS_TOL * max(obj, 1e-12)
        obj = obj_new
        if converged:
            break

    pinball = _objective(a, b)
    return round(float(a), 6), round(float(b), 6), round(pinball, 6)


# ── Stage-2 residual quantiles ────────────────────────────────────────────────

# A leave-one-out residual is e_i / (1 - h_ii), and h_ii approaches 1 for a row
# the fit is essentially interpolating, which sends that ratio to infinity.
# The divisor is floored so such a row contributes a large but finite number.
# This is numerical safety, not a statistical choice: the band is read off the
# 10th and 90th percentiles, which are order statistics and do not move unless
# more than a tenth of the rows are affected.
_LOO_DIVISOR_FLOOR = 0.05


def _loo_residuals(X: Any, y: Any, coef: Any) -> Any:
    """Leave-one-out (PRESS) residuals of an OLS fit, in closed form.

    For ordinary least squares the residual the model would have made on row
    ``i`` had row ``i`` been left out of the fit is exactly ``e_i / (1 - h_ii)``
    where ``h_ii`` is that row's hat-matrix diagonal.  No refit is needed.

    WHY not the plain in-sample residual: an in-sample OLS residual is
    systematically too small, because the fit has already spent degrees of
    freedom moving toward the row it is being scored on
    (``E[e_i^2] = sigma^2 (1 - h_ii)``).  With ten coefficients on 50 to 100
    rows the mean ``h_ii`` is 0.10 to 0.20, so in-sample residual quantiles
    understate the real predictive spread by roughly 5 to 10 per cent on
    average and far more on a leveraged row.  A published band that is too
    narrow is the failure mode worth avoiding here, and the whole complaint on
    issue #72 is that the band does not describe the error it claims to.

    WHY not an explicit holdout: at ``OLS_MIN_OBS`` of 50 a 30 per cent holdout
    leaves 15 rows, and a 10th percentile taken from 15 points is one or two
    order statistics wide.  Leave-one-out uses every row as its own holdout and
    costs one matrix inverse.
    """
    resid = y - X @ coef
    # h = diag(X (X'X)^+ X'), computed row-wise so the n x n hat matrix is
    # never materialised. pinv, not inv: a rank-deficient design must degrade
    # rather than raise, the same way the lstsq call above tolerates it.
    gram_inv = np.linalg.pinv(X.T @ X)
    leverage = np.einsum("ij,jk,ik->i", X, gram_inv, X)
    divisor = np.clip(1.0 - leverage, _LOO_DIVISOR_FLOOR, 1.0)
    return resid / divisor


def _conformal_index(n: int, level: float) -> int:
    """0-based index into ``n`` sorted values for a finite-sample quantile.

    Split-conformal style: the upper bound takes the
    ``ceil(level * (n + 1))``-th smallest value and the lower bound the
    ``floor(level * (n + 1))``-th, which is one order statistic wider than the
    plain empirical quantile.  With 50 rows that is the 46th of 50 rather than
    the 45th at the top and the 5th rather than the 6th at the bottom.

    WHY err wide: the correction is only worth roughly one order statistic, and
    at these sample sizes the estimate of a tail quantile is genuinely noisy.
    Given a choice of direction for that noise, a band slightly too wide
    overstates uncertainty while a band slightly too narrow understates it, and
    understating it is the defect being fixed.
    """
    if level >= 0.5:
        idx = math.ceil(level * (n + 1)) - 1
    else:
        idx = math.floor(level * (n + 1)) - 1
    return max(0, min(n - 1, idx))


def _residual_quantiles(
    bucket_key: str, X: Any, y: Any, coef: Any
) -> ResidualQuantiles:
    """Fit the stage-2 residual quantile triple for one bucket.

    Empirical quantiles of the leave-one-out residuals.  Nothing parametric:
    a normal assumption on a residual that is bounded below by the market floor
    and unbounded above through the spike regime would be worse than the order
    statistics, and there are enough rows for the order statistics to exist.
    """
    loo = np.sort(_loo_residuals(X, y, coef))
    n = int(loo.size)
    if n < OLS_MIN_OBS:
        return ResidualQuantiles(bucket_key=bucket_key)
    lo = float(loo[_conformal_index(n, 0.1)])
    hi = float(loo[_conformal_index(n, 0.9)])
    # The median needs no conservative direction: it is a location estimate,
    # not a bound, and is the best-supported order statistic in the sample.
    mid = float(np.median(loo))
    return ResidualQuantiles(
        bucket_key=bucket_key,
        q10=round(lo, 6),
        q50=round(mid, 6),
        q90=round(hi, 6),
        n=n,
    )


# ── Run-level feature computation (for OLS stage2) ────────────────────────────

def _p90_minus_p10(values: list[float]) -> float:
    """Return p90 − p10 of *values* via sort + linear index (pure stdlib)."""
    n = len(values)
    if n == 0:
        return 0.0
    if n == 1:
        return 0.0
    s = sorted(values)

    def _pct(p: float) -> float:
        # Linear interpolation between closest ranks (numpy 'linear' method).
        idx = p * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return s[lo]
        frac = idx - lo
        return s[lo] * (1.0 - frac) + s[hi] * frac

    return _pct(0.9) - _pct(0.1)


def _compute_run_features(
    observations: list[Observation],
) -> dict[str, RunFeatures]:
    """
    Build dict[forecast_run_at → RunFeatures] from the observation set.

    Per run_at:
      run_max_h6_rrp : max raw forecast for horizon_hours < 6
      run_mean_rrp   : mean raw forecast for horizon_hours < 24
      run_spread     : p90 − p10 of raw forecast for horizon_hours < 24
    """
    h6: dict[str, list[float]] = {}
    h24: dict[str, list[float]] = {}
    for obs in observations:
        if obs.horizon_hours < 6:
            h6.setdefault(obs.forecast_run_at, []).append(obs.pd7day_forecast)
        if obs.horizon_hours < 24:
            h24.setdefault(obs.forecast_run_at, []).append(obs.pd7day_forecast)

    run_ats = set(h6) | set(h24)
    out: dict[str, RunFeatures] = {}
    for run_at in run_ats:
        near = h6.get(run_at, [])
        day = h24.get(run_at, [])
        out[run_at] = RunFeatures(
            run_max_h6_rrp=max(near) if near else 0.0,
            run_mean_rrp=(sum(day) / len(day)) if day else 0.0,
            run_spread=_p90_minus_p10(day),
        )
    return out


# ── Engine ────────────────────────────────────────────────────────────────────

class CalibrationEngine:
    """
    Fits and applies OLS + quantile regression calibration models.

    Usage
    -----
    engine = CalibrationEngine()
    result = engine.fit(observations, region="QLD1")   # CPU-bound; run in executor
    calibrated = result.apply(raw_price, horizon_hours, hour_of_day)
    """

    def fit(
        self,
        observations: list[Observation],
        region: str = "QLD1",
        now: datetime | None = None,
    ) -> CalibrationResult:
        """
        Partition observations into buckets, fit all models.
        Returns a CalibrationResult ready to apply to new forecasts.

        *now* is the aware UTC instant the rolling window and decay weights
        are measured from; it defaults to the wall clock and exists so tests
        can pin it (issue #109). This module holds no hass reference.

        Only observations within the last OBSERVATION_WINDOW_DAYS are used
        for fitting.  All observations remain in storage (the window is a
        fit-time filter only).

        Weights are computed per-observation using exponential time decay:
          weight = exp(-DECAY_LAMBDA * days_ago)
        Region is used for solar elevation ToD classification.
        """
        now_utc = now or datetime.now(timezone.utc)
        now_nem_dt = now_utc.astimezone(NEM_TZ)
        # ── Rolling window filter ────────────────────────────────────────────
        cutoff = now_utc - timedelta(days=OBSERVATION_WINDOW_DAYS)
        windowed: list[tuple[Observation, datetime]] = []
        for obs in observations:
            try:
                obs_dt = datetime.fromisoformat(obs.interval_time)
                if obs_dt.tzinfo is None:
                    # Legacy naive timestamp — assume NEM time (UTC+10)
                    obs_dt = obs_dt.replace(tzinfo=NEM_TZ)
                if obs_dt >= cutoff:
                    windowed.append((obs, obs_dt))
            except (ValueError, TypeError):
                # Unparseable timestamp — include defensively; use now for weight
                windowed.append((obs, now_nem_dt))
        observations_in_window = len(windowed)

        # Partition into buckets using solar elevation ToD classification
        buckets: dict[str, list[tuple[float, float]]] = {
            k: [] for k in all_bucket_keys()
        }
        bucket_weights: dict[str, list[float]] = {
            k: [] for k in all_bucket_keys()
        }
        for obs, obs_dt in windowed:
            if obs.is_intervention:
                # Skip intervention periods — prices are not market-driven
                continue
            if obs.actual_rrp >= SPIKE_THRESHOLD or obs.pd7day_forecast >= SPIKE_THRESHOLD:
                # Exclude spike observations from OLS training — extreme prices
                # follow a different distribution and poison the fit.
                # Both sides must be checked: spike actuals poison y, and spike
                # forecasts (served as passthrough) are extreme x leverage points
                # that collapse the OLS slope even when actual_rrp is bounded.
                continue
            # Solar elevation ToD classification
            obs_nem = obs_dt.astimezone(NEM_TZ)
            key = _bucket_key_solar(obs.horizon_hours, obs_nem, region)
            if key in buckets:
                # Cap per-bucket to avoid memory bloat; keep most recent
                if len(buckets[key]) < MAX_OBS:
                    buckets[key].append((obs.pd7day_forecast, obs.actual_rrp))
                    # Compute exponential time-decay weight
                    days_ago = (now_nem_dt - obs_nem).total_seconds() / 86400.0
                    weight = math.exp(-DECAY_LAMBDA * max(days_ago, 0.0))
                    bucket_weights[key].append(weight)

        now_str = now_nem_dt.isoformat()
        models: dict[str, BucketModel] = {}

        for key, pairs in buckets.items():
            model = BucketModel(bucket_key=key)
            weights = bucket_weights[key]

            # OLS (weighted) — retained to populate LinearCoeff for quantile
            # regression initialisation and diagnostic attributes (a, b, mae, rmse).
            # The OLS calibrated value is no longer used in apply_all(); that path
            # now uses the isotonic model below.
            a_ols, b_ols = _ols(pairs, weights=weights if weights else None)
            a_ols = max(a_ols, 0.0)
            mae, rmse = _ols_metrics(pairs, a_ols, b_ols) if len(pairs) >= MIN_OBS else (None, None)
            model.ols = LinearCoeff(
                a=a_ols, b=b_ols, n=len(pairs), mae=mae, rmse=rmse
            )

            # Isotonic regression (internal PAV IsotonicRegression) — primary point estimator.
            # Fitted with exponential decay sample weights (same as OLS above).
            # out_of_bounds='clip': forecasts outside the training x-range are clipped
            # to the nearest training boundary rather than extrapolated.
            # MIN_OBS guard: iso_model remains None below threshold; apply_all() falls
            # back to passthrough when iso_model is None.
            if len(pairs) >= MIN_OBS:
                _xs = np.array([p[0] for p in pairs])
                _ys = np.array([p[1] for p in pairs])
                _ws = np.array(weights) if weights else np.ones(len(pairs))
                iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
                iso.fit(_xs, _ys, sample_weight=_ws)
                model.iso_model = iso
            # else: iso_model stays None (set by dataclass default)

            # Quantile regression (P10, P50, P90)
            q_results: dict[str, tuple[float, float, float]] = {}
            for q, attr in zip(QUANTILES, ("q10", "q50", "q90")):
                a_q, b_q, pl = _quantile_regression(
                    pairs, q, weights=weights if weights else None
                )
                q_results[attr] = (a_q, b_q, pl)

            # Enforce monotonic ordering of quantile slopes: q10_a <= q50_a <= q90_a
            q10_a, q50_a, q90_a = sorted([q_results["q10"][0], q_results["q50"][0], q_results["q90"][0]])
            # Clamp negative quantile slopes to 0 (same logic as OLS clamp)
            q10_a = max(q10_a, 0.0)
            q50_a = max(q50_a, 0.0)
            q90_a = max(q90_a, 0.0)
            q_results["q10"] = (q10_a, q_results["q10"][1], q_results["q10"][2])
            q_results["q50"] = (q50_a, q_results["q50"][1], q_results["q50"][2])
            q_results["q90"] = (q90_a, q_results["q90"][1], q_results["q90"][2])

            for q, attr in zip(QUANTILES, ("q10", "q50", "q90")):
                a_q, b_q, pl = q_results[attr]
                setattr(model, attr, QuantileCoeff(
                    quantile=q, a=a_q, b=b_q,
                    n=len(pairs),
                    pinball_loss=pl if len(pairs) >= MIN_OBS else None,
                ))

            models[key] = model
            if len(pairs) >= MIN_OBS:
                _LOGGER.debug(
                    "Bucket %s: n=%d isotonic+OLS(a=%.3f, b=%.4f) MAE=%.4f "
                    "Q10(a=%.3f) Q90(a=%.3f)",
                    key, len(pairs), a_ols, b_ols, mae or 0,
                    model.q10.a, model.q90.a,
                )

        total = len([
            obs for obs, _ in windowed
            if not obs.is_intervention and obs.actual_rrp < SPIKE_THRESHOLD and obs.pd7day_forecast < SPIKE_THRESHOLD
        ])
        _LOGGER.info(
            "Calibration fit complete: %d observations in %d-day window "
            "(%d total stored), %d buckets active",
            total,
            OBSERVATION_WINDOW_DAYS,
            len(observations),
            sum(1 for m in models.values() if not m.ols.is_default),
        )

        return CalibrationResult(
            fitted_at=now_str,
            total_observations=total,
            observations_in_window=observations_in_window,
            models=models,
        )

    def fit_ols_stage2(
        self,
        observations: list[Observation],
        stpasa_by_key: dict[str, "StpasaFeatures"],
        region: str = "QLD1",
    ) -> dict[str, OlsModel]:
        """
        Fit per-bucket 9-feature OLS using combined PD7DAY + STPASA features.

        observations : the same observations used for the isotonic fit().
        stpasa_by_key: mapping str(interval_time + "|" + run_at) → StpasaFeatures.

        Returns dict[bucket_key, OlsModel].  Only buckets whose horizon falls in
        [OLS_MIN_HORIZON_H, OLS_MAX_HORIZON_H] are fitted; each requires at least
        OLS_MIN_OBS observations carrying valid STPASA data.  Under-populated buckets
        get an empty OlsModel (coef=[]).

        Rows whose raw forecast reaches the negative passthrough boundary are
        dropped before fitting; see the comment on the filter below.

        Feature order (after a leading 1.0 intercept term):
          [iso_calibrated, run_max_h6_rrp, run_mean_rrp, run_spread,
           horizon_hours/168, log_surplus, log_solar, log_demand, poe_spread_n]
        """
        run_features = _compute_run_features(observations)

        # We need an isotonic model to produce iso_calibrated for the feature
        # vector.  Refit on the same observations so OLS trains against the
        # exact isotonic output it will see at apply() time.
        iso_result = self.fit(observations, region=region)

        # Group rows by bucket.  ``bucket_excluded`` counts the rows dropped by
        # the negative passthrough filter, per bucket, so the exposure is
        # visible in the log rather than merely assumed to be zero (issue #79
        # noted the count was unknown).
        bucket_rows: dict[str, list[tuple[list[float], float]]] = {}
        bucket_excluded: dict[str, int] = {}
        for obs in observations:
            if obs.is_intervention:
                continue
            if obs.horizon_hours < OLS_MIN_HORIZON_H or obs.horizon_hours > OLS_MAX_HORIZON_H:
                continue
            if obs.actual_rrp >= SPIKE_THRESHOLD or obs.pd7day_forecast >= SPIKE_THRESHOLD:
                continue

            feat_key = f"{obs.interval_time}|{obs.forecast_run_at}"
            sf = stpasa_by_key.get(feat_key)
            if sf is None:
                continue
            rf = run_features.get(obs.forecast_run_at)
            if rf is None:
                continue

            bucket_key = _bucket_key(obs.horizon_hours, obs.hour_of_day)

            # Drop rows that the serving path never asks this model about.
            #
            # WHY: the first feature below is the stage-1 output, and
            # apply_all floors that output at 0.0 for every raw forecast above
            # NEGATIVE_PASSTHROUGH_THRESHOLD while returning the raw value at
            # or below it. The feature therefore has no attainable value in the
            # open interval (-0.10, 0.0), and a sub-threshold row lands on the
            # far side of that gap with nothing between it and the cluster. In
            # ordinary least squares leverage grows with squared distance from
            # the feature mean, so such a row is fitted largely by itself:
            # measured hat leverage for a single one is 0.92 to 0.98 against a
            # bucket mean of 0.13, that is roughly 7x. One mis-joined deep
            # negative observation moved the fitted iso_cal coefficient from
            # +1.13 to -0.15 in a 78 row bucket, a sign flip, while the same
            # corruption on an ordinary row moved it by 6 percent. A negative
            # iso_cal coefficient of -1.879 was observed in the wild on
            # h24_48__shoulder and is pinned by
            # test_apply_stpasa_negative_ols_falls_back_to_isotonic.
            #
            # Dropping them also removes train and serve skew rather than
            # creating it: PR #74 made CalibrationResult.apply return early on
            # a "passthrough_negative" result, so stage 2 is never consulted
            # below the boundary. Training on a region that is never served
            # only lets it distort the coefficient used for every ordinary
            # in-band interval. Both paths now read the boundary from
            # is_negative_passthrough so they cannot drift apart. See #79.
            #
            # Deliberately narrow: the stage-1 isotonic fit above and the run
            # level features still see these rows, because the serving path
            # computes both the same way from the live run. Only the stage-2
            # design matrix changes.
            if is_negative_passthrough(obs.pd7day_forecast):
                bucket_excluded[bucket_key] = bucket_excluded.get(bucket_key, 0) + 1
                continue

            bucket = iso_result.get_bucket(obs.horizon_hours, obs.hour_of_day)
            # The unfloored stage-1 value, through the same helper the serving
            # path reads, so a row is fitted from the number the same interval
            # would be served from. A floored feature paired with a genuinely
            # negative actual is what biased this coefficient: see
            # stage2_iso_feature and issue #85.
            iso_cal = stage2_iso_feature(
                bucket.apply_all(obs.pd7day_forecast), obs.pd7day_forecast
            )

            feature_vec = [
                float(iso_cal),
                rf.run_max_h6_rrp,
                rf.run_mean_rrp,
                rf.run_spread,
                obs.horizon_hours / 168.0,
                sf.log_surplus,
                sf.log_solar,
                sf.log_demand,
                sf.poe_spread_n,
            ]
            bucket_rows.setdefault(bucket_key, []).append((feature_vec, obs.actual_rrp))

        ols_models: dict[str, OlsModel] = {}
        # Iterate the union so a bucket whose every candidate row was excluded
        # still gets an empty OlsModel rather than disappearing from the
        # result. apply() treats a missing key and an empty coef list the same
        # way, but the diagnostic summary should not lose the bucket.
        # Sorted, so the fit order and the log line are deterministic.
        for bucket_key in sorted(set(bucket_rows) | set(bucket_excluded)):
            rows = bucket_rows.get(bucket_key, [])
            # OLS_MIN_OBS is counted AFTER exclusion, deliberately. A bucket
            # that only clears the floor by including rows the model is never
            # served on has not really cleared it, so it falls back to an empty
            # OlsModel and apply() keeps the stage-1 isotonic result. Falling
            # back is the safe direction: the alternative is a 9 feature fit on
            # fewer than 50 points, which is the over-fit that raised this
            # floor from 10 in the first place. See #79.
            if len(rows) < OLS_MIN_OBS:
                ols_models[bucket_key] = OlsModel(bucket_key=bucket_key)
                continue
            # Design matrix with leading intercept column of ones.
            X = np.array([[1.0] + r[0] for r in rows], dtype=float)
            y = np.array([r[1] for r in rows], dtype=float)
            try:
                coef, _resid, _rank, _sv = np.linalg.lstsq(X, y, rcond=None)
            except np.linalg.LinAlgError:
                ols_models[bucket_key] = OlsModel(bucket_key=bucket_key)
                continue
            # R² for diagnostics.
            y_hat = X @ coef
            ss_res = float(np.sum((y - y_hat) ** 2))
            ss_tot = float(np.sum((y - np.mean(y)) ** 2))
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
            ols_models[bucket_key] = OlsModel(
                bucket_key=bucket_key,
                coef=[round(float(c), 8) for c in coef],
                n_train=len(rows),
                r2=round(r2, 6),
                # Fitted from the same X, y and coef, so the residual quantiles
                # can never describe a different fit than the one they ship
                # with. Issue #72.
                resid=_residual_quantiles(bucket_key, X, y, coef),
            )

        n_resid_fitted = sum(
            1
            for m in ols_models.values()
            if m.resid is not None and m.resid.is_fitted
        )
        n_resid_missing = sum(
            1
            for m in ols_models.values()
            if len(m.coef) >= 2 and (m.resid is None or not m.resid.is_fitted)
        )
        if n_resid_missing:
            # Loud rather than silent: a bucket with coefficients but no usable
            # residual quantiles publishes the old re-clamped stage-1 band and
            # so can still collapse a bound onto the point estimate. See #72.
            _LOGGER.warning(
                "OLS stage2 fit: %d fitted bucket(s) have no usable residual "
                "quantiles and will publish a re-clamped stage-1 band: %s",
                n_resid_missing,
                ", ".join(
                    sorted(
                        k for k, m in ols_models.items()
                        if len(m.coef) >= 2
                        and (m.resid is None or not m.resid.is_fitted)
                    )
                ),
            )

        n_excluded = sum(bucket_excluded.values())
        _LOGGER.info(
            "OLS stage2 fit: %d buckets evaluated (%d with sufficient STPASA obs, "
            "%d with stage-2 residual quantiles), "
            "%d rows excluded at or below the negative passthrough boundary%s",
            len(ols_models),
            sum(1 for m in ols_models.values() if len(m.coef) >= 2),
            n_resid_fitted,
            n_excluded,
            (
                " (" + ", ".join(
                    f"{k}: {v}" for k, v in sorted(bucket_excluded.items())
                ) + ")"
            ) if n_excluded else "",
        )
        return ols_models

    def to_storage(self, result: CalibrationResult) -> dict:
        """Serialise CalibrationResult to a JSON-safe dict for .storage."""
        out: dict = {
            "fitted_at": result.fitted_at,
            "total_observations": result.total_observations,
            "observations_in_window": result.observations_in_window,
            "models": {},
        }
        for key, model in result.models.items():
            out["models"][key] = {
                "ols": {
                    "a": model.ols.a,
                    "b": model.ols.b,
                    "n": model.ols.n,
                    "mae": model.ols.mae,
                    "rmse": model.ols.rmse,
                },
                "q10": {"a": model.q10.a, "b": model.q10.b, "n": model.q10.n, "pl": model.q10.pinball_loss},
                "q50": {"a": model.q50.a, "b": model.q50.b, "n": model.q50.n, "pl": model.q50.pinball_loss},
                "q90": {"a": model.q90.a, "b": model.q90.b, "n": model.q90.n, "pl": model.q90.pinball_loss},
            }
        # "resid" is written in the same dict as "coef" so a reader cannot get
        # one without the other; a payload from before issue #72 simply has no
        # "resid" key and deserialises to None, which the serving path treats as
        # unfitted. Persisting these matters: the isotonic model is not
        # serialisable and does not survive a restart, but the OLS coefficients
        # do, so stage 2 keeps overriding after a restart and would otherwise
        # have no stage-2 band to publish with until the next fit.
        out["ols_models"] = {
            key: {
                "coef": m.coef,
                "n_train": m.n_train,
                "r2": m.r2,
                **(
                    {
                        "resid": {
                            "q10": m.resid.q10,
                            "q50": m.resid.q50,
                            "q90": m.resid.q90,
                            "n": m.resid.n,
                        }
                    }
                    if m.resid is not None
                    else {}
                ),
            }
            for key, m in result.ols_models.items()
        }
        return out

    def from_storage(self, data: dict) -> CalibrationResult:
        """Deserialise a CalibrationResult from .storage dict."""
        models: dict[str, BucketModel] = {}
        for key, md in data.get("models", {}).items():
            o = md.get("ols", {})
            model = BucketModel(
                bucket_key=key,
                ols=LinearCoeff(
                    a=o.get("a", 1.0), b=o.get("b", 0.0),
                    n=o.get("n", 0), mae=o.get("mae"), rmse=o.get("rmse"),
                ),
                q10=QuantileCoeff(0.1, a=md["q10"]["a"], b=md["q10"]["b"], n=md["q10"]["n"], pinball_loss=md["q10"].get("pl")),
                q50=QuantileCoeff(0.5, a=md["q50"]["a"], b=md["q50"]["b"], n=md["q50"]["n"], pinball_loss=md["q50"].get("pl")),
                q90=QuantileCoeff(0.9, a=md["q90"]["a"], b=md["q90"]["b"], n=md["q90"]["n"], pinball_loss=md["q90"].get("pl")),
            )
            models[key] = model

        # OLS stage2 models — absent on pre-STPASA installs (graceful default).
        ols_models: dict[str, OlsModel] = {}
        for key, md in data.get("ols_models", {}).items():
            rd = md.get("resid")
            resid = (
                ResidualQuantiles(
                    bucket_key=key,
                    q10=rd.get("q10"),
                    q50=rd.get("q50"),
                    q90=rd.get("q90"),
                    n=rd.get("n", 0),
                )
                if isinstance(rd, dict)
                else None
            )
            ols_models[key] = OlsModel(
                bucket_key=key,
                coef=md.get("coef", []),
                n_train=md.get("n_train", 0),
                r2=md.get("r2", 0.0),
                resid=resid,
            )

        return CalibrationResult(
            fitted_at=data.get("fitted_at", ""),
            total_observations=data.get("total_observations", 0),
            observations_in_window=data.get("observations_in_window", 0),
            models=models,
            ols_models=ols_models,
        )
