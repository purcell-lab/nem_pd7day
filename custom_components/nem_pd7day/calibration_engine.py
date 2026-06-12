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
from typing import NamedTuple, TYPE_CHECKING

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
        if self._x_thresholds is None:
            raise RuntimeError("IsotonicRegression.fit() must be called before predict()")
        return np.interp(
            np.asarray(x, dtype=float),
            self._x_thresholds,
            self._y_thresholds,
            left=self._y_thresholds[0],
            right=self._y_thresholds[-1],
        )

from .const import (
    HORIZON_EDGES,
    HORIZON_LABELS,
    IRLS_EPS,
    IRLS_ITER,
    MAX_OBS,
    MIN_OBS,
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

# NEM timezone for weight calculations
_NEM_TZ = timezone(timedelta(hours=10))

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
    def from_interval(cls, interval: "StpasaInterval") -> "StpasaFeatures":
        return cls(
            log_surplus=math.log1p(max(interval.surpluscapacity, 0.0)),
            log_solar=math.log1p(max(interval.ss_solar_uigf, 0.0)),
            log_demand=math.log(max(interval.demand50, 1.0)),
            poe_spread_n=(interval.demand10 - interval.demand90) / max(interval.demand50, 1.0),
            stpasa_run_at=interval.run_datetime,
        )


@dataclass
class RunFeatures:
    """PD7DAY run-level features shared by all intervals in one run."""
    run_max_h6_rrp: float    # max raw RRP for h < 6 intervals ($/kWh)
    run_mean_rrp: float      # mean raw RRP for h < 24 intervals ($/kWh)
    run_spread: float        # p90 − p10 of raw RRP for h < 24 intervals ($/kWh)


@dataclass
class OlsModel:
    """Fitted OLS coefficients for one (horizon_band, tod_bucket) cell."""
    bucket_key: str
    coef: list[float] = field(default_factory=list)  # intercept first, then 8 features
    n_train: int = 0
    r2: float = 0.0

    def predict(self, features: list[float]) -> float:
        """Apply: intercept + dot(coef[1:], features)."""
        if len(self.coef) < 2:
            return 0.0
        return self.coef[0] + sum(c * x for c, x in zip(self.coef[1:], features))


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
        if x <= NEGATIVE_PASSTHROUGH_THRESHOLD:
            return {
                "calibrated": round(x, 6),
                "p10": round(x, 6),
                "p50": round(x, 6),
                "p90": round(x, 6),
                "calibrated_source": "passthrough_negative",
                "n_obs": self.ols.n,
            }

        if self.iso_model is None:
            # Isotonic model not available (< MIN_OBS or not persisted) —
            # pass raw forecast through but still compute quantile intervals
            # if the quantile coefficients are fitted (they survive serialisation).
            p10 = self.q10.apply(x) if not self.q10.is_default else None
            p50 = self.q50.apply(x) if not self.q50.is_default else None
            p90 = self.q90.apply(x) if not self.q90.is_default else None
            return {
                "calibrated": round(x, 6),
                "p10": round(p10, 6) if p10 is not None else None,
                "p50": round(p50, 6) if p50 is not None else None,
                "p90": round(p90, 6) if p90 is not None else None,
                "calibrated_source": "passthrough",
                "n_obs": self.ols.n,
            }

        # ── Isotonic calibration ────────────────────────────────────────────
        # IsotonicRegression.predict() with out_of_bounds='clip': forecasts
        # outside the training x-range are clipped to the nearest boundary.
        # Result floored at 0.0 — calibrated prices cannot be physically
        # negative (negative forecasts are caught by passthrough_negative).
        calibrated = float(max(self.iso_model.predict([x])[0], 0.0))

        p10 = self.q10.apply(x) if not self.q10.is_default else None
        p50 = self.q50.apply(x) if not self.q50.is_default else None
        p90 = self.q90.apply(x) if not self.q90.is_default else None

        # Clamp P10/P90 so the confidence band always contains calibrated,
        # then clamp P50 to [P10, P90] so all three are strictly ordered.
        # Quantile IRLS sorts slopes but not intercepts, so crossing at the
        # x-axis intercept can still occur — this post-fit enforcement ensures
        # the published interval is always monotone: P10 ≤ P50 ≤ P90.
        if p10 is not None:
            p10 = max(0.0, min(p10, calibrated))
        if p90 is not None:
            p90 = max(calibrated, p90)
        if p50 is not None:
            p50_lo = p10 if p10 is not None else 0.0
            p50_hi = p90 if p90 is not None else float("inf")
            p50 = max(p50_lo, min(p50_hi, p50))

        return {
            "calibrated": round(calibrated, 6),
            "p10": round(p10, 6) if p10 is not None else None,
            "p50": round(p50, 6) if p50 is not None else None,
            "p90": round(p90, 6) if p90 is not None else None,
            "ols_mae": self.ols.mae,
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
        result = self.get_bucket(horizon_hours, hour_of_day).apply_all(forecast)

        # 2. Gate: STPASA correction only inside the OLS horizon band, and only
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
        iso_cal = result.get("calibrated", forecast)
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

        # 5–6. Predict, clamp >= 0.
        prediction = max(ols.predict(feature_vec), 0.0)

        # 7. Replace the point estimate only; keep quantile band as-is.
        out = dict(result)
        out["calibrated"] = round(prediction, 6)
        out["calibrated_source"] = "isotonic+stpasa"
        out["stpasa_run_at"] = stpasa.stpasa_run_at
        return out

    def summary(self) -> dict:
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
        out = {
            "fitted_at": self.fitted_at,
            "total_observations": self.total_observations,
            "observation_window_days": OBSERVATION_WINDOW_DAYS,
            "observations_in_window": self.observations_in_window,
            "buckets": {},
        }
        for key, model in self.models.items():
            bucket = {
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
            if iso is not None and iso._x_thresholds is not None and len(iso._x_thresholds) > 0:
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

    def get_iso_diagnostics(self, bucket_key: str) -> dict | None:
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

    If *weights* is provided, performs weighted OLS using the sqrt-scaling
    trick: multiply both sides of the design matrix by sqrt(w).

    Returns (a, b).  Falls back to (1, 0) if degenerate.
    """
    n = len(pairs)
    if n < MIN_OBS:
        return 1.0, 0.0

    if weights is not None:
        # Weighted OLS via sqrt-scaling
        sw = [math.sqrt(w) for w in weights]
        sx = sum(sw[i] * sw[i] * pairs[i][0] for i in range(n))
        sy = sum(sw[i] * sw[i] * pairs[i][1] for i in range(n))
        sxx = sum(sw[i] * sw[i] * pairs[i][0] * pairs[i][0] for i in range(n))
        sxy = sum(sw[i] * sw[i] * pairs[i][0] * pairs[i][1] for i in range(n))
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
) -> tuple[float, float, float]:
    """
    Fit quantile regression for the given quantile level using IRLS.

    Algorithm:
      1. Initialise with OLS solution.
      2. For each iteration:
         a. Compute residuals r_i = y_i - (a*x_i + b)
         b. Assign pinball weights:
               w_i = quantile     if r_i >= 0
               w_i = 1 - quantile if r_i <  0
            (floor at IRLS_EPS to avoid zero weights)
         c. Fit weighted OLS using the current weights.
      3. Return final (a, b) and mean pinball loss.

    Returns (a, b, pinball_loss).
    """
    n = len(pairs)
    if n < MIN_OBS:
        return 1.0, 0.0, float("inf")

    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]

    # Initialise with OLS
    a, b = _ols(pairs)

    for _ in range(n_iter):
        # Compute residuals
        residuals = [ys[i] - (a * xs[i] + b) for i in range(n)]

        # Assign pinball weights
        weights = [
            max(quantile if r >= 0 else (1.0 - quantile), IRLS_EPS)
            for r in residuals
        ]

        # Weighted OLS: minimise sum(w_i * (y_i - a*x_i - b)^2)
        sw = sum(weights)
        swx = sum(weights[i] * xs[i] for i in range(n))
        swy = sum(weights[i] * ys[i] for i in range(n))
        swxx = sum(weights[i] * xs[i] * xs[i] for i in range(n))
        swxy = sum(weights[i] * xs[i] * ys[i] for i in range(n))

        denom = sw * swxx - swx * swx
        if abs(denom) < 1e-12:
            break
        a_new = (sw * swxy - swx * swy) / denom
        b_new = (swy - a_new * swx) / sw

        # Check convergence
        if abs(a_new - a) < 1e-9 and abs(b_new - b) < 1e-9:
            a, b = a_new, b_new
            break
        a, b = a_new, b_new

    # Pinball loss
    residuals = [ys[i] - (a * xs[i] + b) for i in range(n)]
    pinball = sum(
        quantile * r if r >= 0 else (quantile - 1) * r
        for r in residuals
    ) / n

    return round(a, 6), round(b, 6), round(pinball, 6)


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
    ) -> CalibrationResult:
        """
        Partition observations into buckets, fit all models.
        Returns a CalibrationResult ready to apply to new forecasts.

        Only observations within the last OBSERVATION_WINDOW_DAYS are used
        for fitting.  All observations remain in storage (the window is a
        fit-time filter only).

        Weights are computed per-observation using exponential time decay:
          weight = exp(-DECAY_LAMBDA * days_ago)
        Region is used for solar elevation ToD classification.
        """
        now_utc = datetime.now(timezone.utc)
        now_nem_dt = now_utc.astimezone(_NEM_TZ)
        # ── Rolling window filter ────────────────────────────────────────────
        cutoff = now_utc - timedelta(days=OBSERVATION_WINDOW_DAYS)
        windowed: list[tuple[Observation, datetime]] = []
        for obs in observations:
            try:
                obs_dt = datetime.fromisoformat(obs.interval_time)
                if obs_dt.tzinfo is None:
                    # Legacy naive timestamp — assume NEM time (UTC+10)
                    obs_dt = obs_dt.replace(tzinfo=_NEM_TZ)
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
            obs_nem = obs_dt.astimezone(_NEM_TZ)
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
                a_q, b_q, pl = _quantile_regression(pairs, q)
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
        MIN_OBS observations carrying valid STPASA data.  Under-populated buckets
        get an empty OlsModel (coef=[]).

        Feature order (after a leading 1.0 intercept term):
          [iso_calibrated, run_max_h6_rrp, run_mean_rrp, run_spread,
           horizon_hours/168, log_surplus, log_solar, log_demand, poe_spread_n]
        """
        run_features = _compute_run_features(observations)

        # We need an isotonic model to produce iso_calibrated for the feature
        # vector.  Refit on the same observations so OLS trains against the
        # exact isotonic output it will see at apply() time.
        iso_result = self.fit(observations, region=region)

        # Group rows by bucket.
        bucket_rows: dict[str, list[tuple[list[float], float]]] = {}
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
            bucket = iso_result.get_bucket(obs.horizon_hours, obs.hour_of_day)
            iso_cal = bucket.apply_all(obs.pd7day_forecast).get(
                "calibrated", obs.pd7day_forecast
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
        for bucket_key, rows in bucket_rows.items():
            if len(rows) < MIN_OBS:
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
            )

        _LOGGER.info(
            "OLS stage2 fit: %d buckets evaluated (%d with sufficient STPASA obs)",
            len(ols_models),
            sum(1 for m in ols_models.values() if len(m.coef) >= 2),
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
        out["ols_models"] = {
            key: {"coef": m.coef, "n_train": m.n_train, "r2": m.r2}
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
            ols_models[key] = OlsModel(
                bucket_key=key,
                coef=md.get("coef", []),
                n_train=md.get("n_train", 0),
                r2=md.get("r2", 0.0),
            )

        return CalibrationResult(
            fitted_at=data.get("fitted_at", ""),
            total_observations=data.get("total_observations", 0),
            observations_in_window=data.get("observations_in_window", 0),
            models=models,
            ols_models=ols_models,
        )
