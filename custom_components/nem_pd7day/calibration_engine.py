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

import functools
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .stpasa_client import StpasaInterval

from astral import LocationInfo
from astral.sun import elevation as solar_elevation

# The fit constants moved with the fit to fitting.py (spec 006); they stay
# importable from here for the callers that import them from this module.
from .const import (
    MARKET_PRICE_FLOOR as MARKET_PRICE_FLOOR,  # importable from here, used in serving.py
    NEM_TZ as NEM_TZ,
    HORIZON_EDGES,
    HORIZON_LABELS,
    IRLS_EPS,
    IRLS_ITER,
    IRLS_TOL,
    MAX_OBS as MAX_OBS,
    MIN_OBS,
    OLS_MIN_OBS,
    STAGE2_LEVERAGE_MULTIPLE as STAGE2_LEVERAGE_MULTIPLE,
    QUANTILES as QUANTILES,
    TOD_LABELS,
)
# The serving path (spec 005). The band provenance labels, the stage-1 source
# and feature keys, the spike threshold, the OLS horizon band, the stage-2
# feature helper and the band helpers are defined there, once; the names are
# imported here too so every caller that imports them from this module keeps
# working.
from .serving import (
    BAND_SOURCE_KEY as BAND_SOURCE_KEY,
    BAND_SOURCE_PASSTHROUGH as BAND_SOURCE_PASSTHROUGH,
    BAND_SOURCE_STAGE1 as BAND_SOURCE_STAGE1,
    BAND_SOURCE_STAGE1_RAW as BAND_SOURCE_STAGE1_RAW,
    BAND_SOURCE_STAGE2 as BAND_SOURCE_STAGE2,
    BAND_SOURCE_STAGE2_FALLBACK as BAND_SOURCE_STAGE2_FALLBACK,
    ISO_FEATURE_KEY as ISO_FEATURE_KEY,
    OLS_MAX_HORIZON_H as OLS_MAX_HORIZON_H,
    OLS_MIN_HORIZON_H as OLS_MIN_HORIZON_H,
    SERVING_GATES,
    SOURCE_ISOTONIC_BELOW_DOMAIN as SOURCE_ISOTONIC_BELOW_DOMAIN,
    SPIKE_THRESHOLD as SPIKE_THRESHOLD,
    Stage2Context,
    _clamp_band as _clamp_band,
    _order_band as _order_band,
    stage1_below_domain,
    stage1_isotonic,
    stage1_passthrough,
    stage2_iso_feature as stage2_iso_feature,
    stage2_result,
)
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

    @property
    def x_min(self) -> float | None:
        """Smallest training forecast, the lower edge of the fitted domain.

        Below it the step function is clipped to its first level, which is
        not a fit but a boundary value; callers use this to tell the two
        apart (issue #117).
        """
        if self._x_thresholds is None or len(self._x_thresholds) == 0:
            return None
        return float(self._x_thresholds[0])

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


_LOGGER = logging.getLogger(__name__)

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


# Stage-2 feature order, after the intercept. Shared by fit_ols_stage2, the
# serving path in serving.FeatureDomainGate and the diagnostic summary, so the
# ranges published on the calibration sensor are keyed by the name of the
# feature they bound (issue #147).
STAGE2_FEATURE_NAMES = (
    "iso_cal",
    "run_max_h6_rrp",
    "run_mean_rrp",
    "run_spread",
    "horizon_frac",
    "log_surplus",
    "log_solar",
    "log_demand",
    "poe_spread_n",
)


# Smallest scheduled demand (MW) the STPASA transform accepts.
#
# WHY: two of the four STPASA features divide or take a log of demand50, so
# below this floor the transform stops being a function of its input:
# log_demand is 0 for every value and poe_spread_n, whose divisor is clamped
# here, turns a 5 MW POE spread into 5.0 against a training range near 0.2.
# STPASA does publish such rows: on the SA1 weekend of 12 September 2026 the
# midday demand50 ran to -27 MW with rooftop PV over operational demand. A
# vector built from those numbers is not evidence about the price, and before
# issue #147 only the sign and floor gates in CalibrationResult.apply stood
# between it and the sensors. An interval at or below the floor now yields no
# features at all, on both the training and the serving side, through the one
# helper below.
STPASA_DEMAND_FLOOR_MW = 1.0


def stpasa_feature_values(
    surplus: float | None,
    solar: float | None,
    demand50: float | None,
    demand10: float | None,
    demand90: float | None,
) -> tuple[float, float, float, float] | None:
    """The four STPASA features from the raw MW inputs, or None.

    Returns ``(log_surplus, log_solar, log_demand, poe_spread_n)``.

    None when any input is missing (issue #43: a missing MW value is None
    rather than a substituted zero) or when demand50 is below
    STPASA_DEMAND_FLOOR_MW (issue #147: the transform is degenerate there).

    This is the single definition of the transform. ``StpasaFeatures.from_interval``
    reads it for the serving path and ``CalibrationStore.async_record_actual``
    for the observation log, so a row is fitted from exactly the numbers the
    same interval would be served from.
    """
    if (
        surplus is None
        or solar is None
        or demand50 is None
        or demand10 is None
        or demand90 is None
    ):
        return None
    if demand50 < STPASA_DEMAND_FLOOR_MW:
        return None
    return (
        math.log1p(max(surplus, 0.0)),
        math.log1p(max(solar, 0.0)),
        math.log(demand50),
        (demand10 - demand90) / demand50,
    )


@dataclass
class StpasaFeatures:
    """Derived STPASA features for a single forecast interval."""
    log_surplus: float       # log1p(surpluscapacity)
    log_solar: float         # log1p(ss_solar_uigf)
    log_demand: float        # log(demand50), demand50 >= STPASA_DEMAND_FLOOR_MW
    poe_spread_n: float      # (demand10 - demand90) / demand50
    stpasa_run_at: str       # ISO-8601, for attribute tagging

    @classmethod
    def from_interval(cls, interval: "StpasaInterval") -> "StpasaFeatures | None":
        """Derive features, or None when the interval cannot honestly give any.

        Every field below is now optional on the interval, because a missing
        MW value is no longer coerced to 0.0 at parse time. An interval short
        of any input is skipped rather than fitted on a substituted zero,
        which would bias the fit rather than merely display wrongly. The
        caller treats None as "no STPASA features for this interval". See
        issue #43. An interval whose demand50 is below STPASA_DEMAND_FLOOR_MW
        is skipped for the reason given on that constant (issue #147).
        """
        values = stpasa_feature_values(
            interval.surpluscapacity,
            interval.ss_solar_uigf,
            interval.demand50,
            interval.demand10,
            interval.demand90,
        )
        if values is None:
            return None
        log_surplus, log_solar, log_demand, poe_spread_n = values
        return cls(
            log_surplus=log_surplus,
            log_solar=log_solar,
            log_demand=log_demand,
            poe_spread_n=poe_spread_n,
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
    # Per-feature training minimum and maximum, in feature order (no
    # intercept), from the rows that fitted ``coef`` after the leverage screen.
    #
    # WHY: a linear model evaluated outside the range it was fitted on is an
    # extrapolation with no evidence behind it, and nothing in the serving
    # gates before issue #147 asked the question. The live case: SA1
    # Saturday 12 September 12:30, STPASA demand50 of 24 MW gave log_demand
    # 3.18 against a training range of about 5.7 to 7.3, and the regression
    # extrapolated two to four natural-log units to publish -$0.86/kWh for a
    # raw -$0.10 that stage 1 put at -$0.012. The ranges travel with the
    # coefficients for the same reason ``resid`` does: they only describe
    # THESE rows. Empty on a model stored before this field existed, which
    # in_feature_domain treats as "no evidence either way" so the old
    # behaviour holds until the next fit rewrites the store.
    feature_min: list[float] = field(default_factory=list)
    feature_max: list[float] = field(default_factory=list)

    def predict(self, features: list[float]) -> float:
        """Apply: intercept + dot(coef[1:], features)."""
        if len(self.coef) < 2:
            return 0.0
        return self.coef[0] + sum(c * x for c, x in zip(self.coef[1:], features))

    def in_feature_domain(self, features: list[float]) -> bool:
        """True when every feature lies within its training range.

        Inclusive at both ends, with a tolerance of one part in 1e9 so a
        feature computed by the same code from the same inputs cannot fall
        outside its own range on floating-point jitter. Legacy models with no
        ranges pass. A range list of the wrong length is a corrupt store and
        fails, which is the safe direction: stage 1 is served instead.
        See issue #147.
        """
        lo, hi = self.feature_min, self.feature_max
        if not lo and not hi:
            return True
        if len(lo) != len(features) or len(hi) != len(features):
            return False
        for x, a, b in zip(features, lo, hi):
            tol = 1e-9 * max(1.0, abs(a), abs(b))
            if x < a - tol or x > b + tol:
                return False
        return True

    def out_of_domain_features(self, features: list[float]) -> list[int]:
        """Indices of the features outside their training range (diagnostic)."""
        lo, hi = self.feature_min, self.feature_max
        if len(lo) != len(features) or len(hi) != len(features):
            return []
        out = []
        for i, (x, a, b) in enumerate(zip(features, lo, hi)):
            tol = 1e-9 * max(1.0, abs(a), abs(b))
            if x < a - tol or x > b + tol:
                out.append(i)
        return out

    def extrapolation_cost(self, features: list[float]) -> float | None:
        """What the out-of-range part of ``features`` adds to the prediction.

        The sum over features of |coef_i| times the distance by which the
        feature lies outside its training range, in $/kWh. Zero when every
        feature is inside. None when the model carries no ranges (legacy
        store) or the range lists do not match the feature vector.

        WHY this rather than the range test alone: a linear model's
        extrapolation error on a feature is bounded by its coefficient times
        the excursion, so a hairline excursion on a feature the model barely
        weights costs nothing, while the #147 case (log_demand 2.5 units
        below its floor at a coefficient near 0.05 $/kWh per unit) costs
        more than the whole band. Issue #153 measured the range test alone
        on the live install: of 141 refusals across five regions, 120 were
        on excursions worth under a cent, mostly poe_spread_n a hundredth
        below a range 0.05 wide and log_solar a hair above its spring
        maximum, and only the handful the gate was built for were worth
        refusing.
        """
        lo, hi = self.feature_min, self.feature_max
        if not lo and not hi:
            return None
        if (
            len(lo) != len(features)
            or len(hi) != len(features)
            or len(self.coef) != len(features) + 1
        ):
            return None
        cost = 0.0
        for c, x, a, b in zip(self.coef[1:], features, lo, hi):
            tol = 1e-9 * max(1.0, abs(a), abs(b))
            if x < a - tol:
                cost += abs(c) * (a - x)
            elif x > b + tol:
                cost += abs(c) * (x - b)
        return cost

    @property
    def extrapolation_allowance(self) -> float | None:
        """The extrapolation cost this bucket's band can absorb, or None.

        Half the residual spread, ``(q90 - q10) / 2``: the distance from the
        point estimate to either edge of the band the model publishes. An
        extrapolation adding less than that to the prediction stays inside
        the uncertainty the bucket already claims; one adding more would
        publish a value the band itself does not cover. Per bucket and
        fitted from the same rows as the coefficients, so there is no
        constant to choose. None when the bucket has no usable residual
        quantiles.
        """
        r = self.resid
        if r is None or not r.is_fitted or r.q10 is None or r.q90 is None:
            return None
        return (r.q90 - r.q10) / 2.0

    def serves(self, features: list[float]) -> bool:
        """Whether stage 2 may be published for this feature vector.

        Three regimes, in order (issues #147 and #153):

        * No ranges stored (legacy payload): serve, as before #147, until the
          next fit writes ranges.
        * Ranges stored and residual quantiles usable: serve when the
          extrapolation cost is within the bucket's allowance. Inside the
          ranges the cost is zero and this always serves.
        * Ranges stored but no usable residual quantiles: nothing to size the
          allowance from, so fall back to the strict range test.

        A range list of the wrong length is a corrupt store and refuses,
        which is the safe direction: stage 1 is served instead.
        """
        lo, hi = self.feature_min, self.feature_max
        if not lo and not hi:
            return True
        cost = self.extrapolation_cost(features)
        if cost is None:
            return False
        if cost == 0.0:
            return True
        allowance = self.extrapolation_allowance
        if allowance is None:
            return False
        return cost <= allowance

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

    @property
    def domain_min(self) -> float | None:
        """Lower edge of the isotonic model's fitted domain, or None."""
        return self.iso_model.x_min if self.iso_model is not None else None

    @property
    def edge_value(self) -> float:
        """The isotonic prediction at the lower edge of the domain, ``iso(x_min)``.

        What the bucket publishes for any forecast below its domain (issue
        #123): the deepest value the evidence supports. Falls back to 0.0
        when there is no fitted model; callers check ``domain_min`` first.
        """
        lo = self.domain_min
        if lo is None or self.iso_model is None:
            return 0.0
        return float(self.iso_model.predict(np.asarray([lo], dtype=float))[0])

    @property
    def edge_offset(self) -> float:
        """Correction at the lower edge of the domain, ``iso(x_min) - x_min``."""
        lo = self.domain_min
        if lo is None:
            return 0.0
        return self.edge_value - lo

    def is_below_domain(self, x: float) -> bool:
        """True when ``x`` is below every forecast this bucket was fitted on.

        One definition, read by the serving path (apply_all) and the stage-2
        training path (CalibrationEngine.fit_ols_stage2), so the two cannot
        drift apart (issue #68 was that class of bug). Inclusive at the edge:
        the smallest training forecast is inside the domain.
        """
        lo = self.domain_min
        return lo is not None and x < lo

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
          1. Insufficient data     — raw forecast returned if iso_model is None
                                     (bucket has < MIN_OBS training observations).
          2. Below the fitted domain — a forecast below the smallest training
                                     forecast is answered as if it were at
                                     the floor: iso(x_min) and the quantile
                                     band at x_min.
          3. Isotonic calibration  — IsotonicRegression.predict([x]), unfloored.
                                     Spike inputs (>= SPIKE_THRESHOLD) are handled by
                                     out_of_bounds='clip', returning the training-range
                                     maximum — a clean normal-market estimate.

        Each path is built by its function in serving.py (spec 005).
        """
        if self.iso_model is None:
            return stage1_passthrough(self, x)
        if self.is_below_domain(x):
            return stage1_below_domain(self, x)
        return stage1_isotonic(self, x)


@dataclass
class CalibrationResult:
    """Full set of fitted models across all buckets."""
    fitted_at: str
    total_observations: int
    observations_in_window: int = 0
    models: dict[str, BucketModel] = field(default_factory=dict)
    ols_models: dict[str, OlsModel] = field(default_factory=dict)

    def get_bucket(
        self, horizon_hours: float, hour_of_day: int,
        interval_dt: datetime | None = None, region: str | None = None,
    ) -> BucketModel:
        return self._bucket_for(bucket_key_for(horizon_hours, hour_of_day, interval_dt, region))

    def _bucket_for(self, key: str) -> BucketModel:
        # The same answer as models.get(key, BucketModel(bucket_key=key)), but
        # the default, four dataclasses deep, is built only when the key is
        # missing rather than on every interval served (spec 005 benchmark).
        models = self.models
        if key in models:
            return models[key]
        return BucketModel(bucket_key=key)

    def apply(
        self,
        forecast: float,
        horizon_hours: float,
        hour_of_day: int,
        stpasa: "StpasaFeatures | None" = None,
        run_features: "RunFeatures | None" = None,
        interval_dt: datetime | None = None,
        region: str | None = None,
    ) -> dict:
        """Calibrated price, band and labels for one interval.

        With the interval's start and the region, the bucket is the one the
        interval was trained in, by solar elevation (#208); without them it
        falls back to the clock-hour key.

        Stage 1 is the bucket's apply_all. Stage 2, the STPASA correction,
        replaces it only when every gate in serving.SERVING_GATES admits the
        interval; the first gate that refuses serves the stage 1 dict itself,
        unchanged. See serving.py (spec 005) for each gate and its issues.
        """
        # 1. Isotonic (existing) result. The key routes both stages, as
        #    get_bucket and the stage 2 model lookup each computed it before.
        key = bucket_key_for(horizon_hours, hour_of_day, interval_dt, region)
        bucket = self._bucket_for(key)
        stage1 = bucket.apply_all(forecast)

        # 2. The gates, in order; then the stage 2 result. Positional, in
        #    Stage2Context's field order: keywords cost a third of the
        #    pipeline's overhead on the serving hot path.
        ctx = Stage2Context(
            forecast, horizon_hours, hour_of_day, stage1, bucket, stpasa, run_features, key
        )
        for gate in SERVING_GATES:
            admitted = gate(ctx, self.ols_models)
            if admitted is None:
                return stage1
            ctx = admitted
        return stage2_result(ctx)

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

        ``stage2`` carries one entry per bucket with a fitted OLS model
        (issue #147: before this, n_train, r2, the residual quantiles and the
        feature ranges were on no sensor and diagnosing an extrapolation took
        inference from neighbouring rows):
          n_train        — rows that fitted the coefficients, after the
                           leverage screen
          r2             — in-sample R² of the fit
          coef           — the fitted coefficients, intercept first then
                           STAGE2_FEATURE_NAMES order (#153)
          resid_q10/q50/q90 — leave-one-out residual quantiles ($/kWh), None
                           when the bucket has no usable stage-2 band
          feature_min/feature_max — per-feature training range, keyed by
                           feature name; the serving gate refuses stage 2
                           outside it
        """
        out: dict[str, Any] = {
            "fitted_at": self.fitted_at,
            "total_observations": self.total_observations,
            "observation_window_days": OBSERVATION_WINDOW_DAYS,
            "observations_in_window": self.observations_in_window,
            "buckets": {},
            "stage2": self._stage2_summary(),
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

    def _stage2_summary(self) -> dict[str, dict[str, Any]]:
        """Per-bucket stage-2 diagnostics for fitted OLS models only."""
        out: dict[str, dict[str, Any]] = {}
        for key in sorted(self.ols_models):
            m = self.ols_models[key]
            if len(m.coef) < 2:
                continue
            r = m.resid if (m.resid is not None and m.resid.is_fitted) else None
            entry: dict[str, Any] = {
                "n_train": m.n_train,
                "r2": m.r2,
                # Intercept first, then STAGE2_FEATURE_NAMES order. Without
                # these the serving gate cannot be reconstructed from the
                # sensors: the #153 analysis could say which feature was
                # outside its range but not what that cost the prediction.
                "coef": list(m.coef),
                "resid_q10": r.q10 if r is not None else None,
                "resid_q50": r.q50 if r is not None else None,
                "resid_q90": r.q90 if r is not None else None,
                "feature_min": None,
                "feature_max": None,
            }
            if len(m.feature_min) == len(STAGE2_FEATURE_NAMES) and len(
                m.feature_max
            ) == len(STAGE2_FEATURE_NAMES):
                entry["feature_min"] = dict(zip(STAGE2_FEATURE_NAMES, m.feature_min))
                entry["feature_max"] = dict(zip(STAGE2_FEATURE_NAMES, m.feature_max))
            out[key] = entry
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


@functools.lru_cache(maxsize=16384)
def _solar_tod(dt_nem: datetime, region: str) -> str:
    """The training label for an interval, computed once per interval and region.

    Serving asks for the same interval about 1,650 times per state write and
    each answer is an astral elevation call, so it is cached. Aware datetimes
    hash by instant, so the same interval written in UTC or NEM time hits the
    same entry.
    """
    return _tod_label_solar(dt_nem, region, _tod_label(dt_nem.hour))


def bucket_key_for(
    horizon_hours: float, hour_of_day: int, interval_dt: datetime | None, region: str | None,
) -> str:
    """The bucket an interval is trained in and served from (#208).

    Stage 1 trains by solar elevation (``_bucket_key_solar``). Until #208 the
    serving path and stage 2 looked buckets up by clock hour, so the
    ``morning_ramp`` buckets were fitted and never served, about 05:00 to
    10:00 was served by the night-time ``shoulder`` model, and ``solar``
    served 10:00 to 16:00 by the clock whatever the sun was doing. Every path
    now keys through here. Without an interval start or a region the key
    falls back to the clock hour, as before.
    """
    if interval_dt is None or not region:
        return _bucket_key(horizon_hours, hour_of_day)
    if interval_dt.tzinfo is None:
        interval_dt = interval_dt.replace(tzinfo=NEM_TZ)
    return f"{_horizon_label(horizon_hours)}__{_solar_tod(interval_dt.astimezone(NEM_TZ), region)}"


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
    divisor = np.clip(1.0 - _hat_leverage(X), _LOO_DIVISOR_FLOOR, 1.0)
    return resid / divisor


def _per_bucket_counts(counts: dict[str, int]) -> str:
    """Format per-bucket counts for the stage-2 summary log, empty when zero."""
    if not counts:
        return ""
    return " (" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) + ")"


def _hat_leverage(X: Any) -> Any:
    """Hat-matrix diagonal, ``diag(X (X'X)^+ X')``, one value per row.

    Computed row-wise so the n x n hat matrix is never materialised. pinv,
    not inv: a rank-deficient design must degrade rather than raise, the same
    way the lstsq calls tolerate it. Shared by the leave-one-out residuals
    and the stage-2 leverage screen so the two agree on what a leveraged row
    is.
    """
    gram_inv = np.linalg.pinv(X.T @ X)
    return np.einsum("ij,jk,ik->i", X, gram_inv, X)


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
        Partition observations into buckets, fit all models (fitting.Stage1Fitter).
        Returns a CalibrationResult ready to apply to new forecasts.

        *now* is the aware UTC instant the rolling window and decay weights
        are measured from; it defaults to the wall clock and exists so tests
        can pin it (issue #109). This module holds no hass reference.
        """
        # Imported here, not at module level: fitting imports this module's
        # value types, so a module-level import either way round is a cycle
        # (spec 006). A fit runs a few times a day, never on the serving path.
        from .fitting import Stage1Fitter

        return Stage1Fitter().fit(observations, region, now)

    def fit_ols_stage2(
        self,
        observations: list[Observation],
        stpasa_by_key: dict[str, "StpasaFeatures"],
        region: str = "QLD1",
        stage1: "CalibrationResult | None" = None,
    ) -> dict[str, OlsModel]:
        """
        Fit per-bucket 9-feature OLS using combined PD7DAY + STPASA features
        (fitting.Stage2Fitter).

        observations : the same observations used for the isotonic fit().
        stpasa_by_key: mapping str(interval_time + "|" + run_at) → StpasaFeatures.
        stage1       : the stage 1 result being published. Stage 2 trains
                       against its buckets and inside its window, so the rows
                       see the exact stage 1 output serving applies (#210) and
                       stage 1 is not fitted twice (#213). Omitted, stage 1 is
                       fitted here on the wall clock.

        Returns dict[bucket_key, OlsModel]; see Stage2Fitter.fit.
        """
        from .fitting import Stage2Fitter  # see fit() for why it is imported here

        run_features = _compute_run_features(observations)
        if stage1 is None:
            stage1 = self.fit(observations, region=region)
        return Stage2Fitter().fit(observations, stpasa_by_key, stage1, run_features, region)

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
        # "feature_min"/"feature_max" likewise (issue #147): a payload from
        # before them has neither key and loads as empty lists, which the
        # serving gate treats as legacy and passes.
        out["ols_models"] = {
            key: {
                "coef": m.coef,
                "n_train": m.n_train,
                "r2": m.r2,
                "feature_min": m.feature_min,
                "feature_max": m.feature_max,
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
                feature_min=[float(v) for v in (md.get("feature_min") or [])],
                feature_max=[float(v) for v in (md.get("feature_max") or [])],
            )

        return CalibrationResult(
            fitted_at=data.get("fitted_at", ""),
            total_observations=data.get("total_observations", 0),
            observations_in_window=data.get("observations_in_window", 0),
            models=models,
            ols_models=ols_models,
        )
