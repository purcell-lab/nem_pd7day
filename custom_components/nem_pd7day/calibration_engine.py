"""
NEM PD7DAY Calibration Engine
==============================
Pure-Python, zero-dependency implementation of:

  1. Ordinary Least Squares (OLS) linear regression
       actual = a * forecast + b
     Used as the primary point-estimate calibrator per horizon/ToD bucket.

  2. Quantile Regression (pinball loss, IRLS)
       Fits P10, P50, P90 simultaneously.
     Gives a confidence interval that widens correctly at longer horizons
     and captures price spike probability without requiring scipy/numpy.

  3. Bucket routing
     Observations are partitioned into 6 horizon × 4 time-of-day = 24
     independent models.  Each bucket is fit separately, so the accuracy
     at 6-hour horizon doesn't contaminate the 5-day horizon model.

  4. Feature vector
     Each observation carries the full feature set collected by the
     integration so the external ML stage (Stage 3, optional) can consume
     the raw log without re-processing.

Design constraints
------------------
- No external imports beyond stdlib.
- Safe to call from inside the HA event loop (all CPU work is sync/fast;
  the coordinator offloads fitting to executor via hass.async_add_executor_job).
- Graceful degradation: any bucket with < MIN_OBS observations returns
  passthrough coefficients (a=1, b=0) so raw PD7DAY values flow through
  unchanged until data accumulates.

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
from typing import NamedTuple

from .const import (
    HORIZON_EDGES,
    HORIZON_LABELS,
    IRLS_EPS,
    IRLS_ITER,
    MAX_CALIBRATED_RATIO,
    MAX_INTERCEPT_ABS,
    MAX_OBS,
    MIN_OBS,
    QUANTILES,
    TOD_BUCKETS,
)
from .nem_time import now_nem, to_nem_iso

_LOGGER = logging.getLogger(__name__)

# ── Spike regime threshold ────────────────────────────────────────────────────
# When the raw AEMO forecast is at or above this level, AEMO is already
# signalling a significant price event.  The linear calibration model (trained
# on normal prices) should not attempt to correct it — pass through unchanged.
# 0.30 $/kWh = $300/MWh ≈ 3× normal peak price.
SPIKE_THRESHOLD = 0.30  # $/kWh

# ── Rolling observation window ────────────────────────────────────────────────
# Only observations within the last N days are used when fitting the
# calibration model.  This prevents stale/seasonal data from corrupting the
# model while all observations are still retained in storage for
# total_increasing state class accounting.
OBSERVATION_WINDOW_DAYS = 90


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


@dataclass
class LinearCoeff:
    """OLS fit: actual ≈ a * forecast + b"""
    a: float = 1.0
    b: float = 0.0
    n: int = 0
    mae: float | None = None
    rmse: float | None = None

    @property
    def is_default(self) -> bool:
        return self.n < MIN_OBS

    def apply(self, x: float) -> float:
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

    def apply_all(self, x: float) -> dict:
        """Return calibrated point estimate + confidence interval."""
        if x <= 0.0:
            return {
                "calibrated": round(x, 6),
                "p10": round(x, 6),
                "p50": round(x, 6),
                "p90": round(x, 6),
                "calibrated_source": "passthrough_negative",
                "n_obs": self.ols.n,
            }

        if x >= SPIKE_THRESHOLD:
            return {
                "calibrated": round(x, 6),
                "p10": round(x, 6),
                "p50": round(x, 6),
                "p90": round(x, 6),
                "calibrated_source": "passthrough_high",
                "n_obs": self.ols.n,
            }

        if self.ols.is_default:
            return {
                "calibrated": round(x, 6),
                "p10": None,
                "p50": None,
                "p90": None,
                "calibrated_source": "passthrough",
                "n_obs": self.ols.n,
            }

        calibrated = self.ols.apply(x)

        # ── Sanity guard ──────────────────────────────────────────────────────
        # If the OLS intercept is physically implausible, or if the calibrated
        # value is wildly different from the raw value, fall back to passthrough
        # rather than emitting nonsense.  This protects against corrupt training
        # data (e.g. duplicate observations, interval key mismatches).
        if abs(self.ols.b) > MAX_INTERCEPT_ABS:
            _LOGGER.warning(
                "Bucket %s sanity check FAILED: intercept b=%.3f exceeds limit %.1f "
                "— falling back to passthrough (raw=%.4f)",
                self.bucket_key, self.ols.b, MAX_INTERCEPT_ABS, x,
            )
            return {
                "calibrated": round(x, 6),
                "p10": None, "p50": None, "p90": None,
                "mae": None,
                "calibrated_source": "passthrough_sanity",
                "n_obs": self.ols.n,
            }

        if abs(x) > 0.01 and abs(calibrated / x) > MAX_CALIBRATED_RATIO:
            _LOGGER.warning(
                "Bucket %s sanity check FAILED: calibrated/raw ratio=%.1f exceeds limit %.1f "
                "(raw=%.4f calibrated=%.4f) — falling back to passthrough",
                self.bucket_key, calibrated / x, MAX_CALIBRATED_RATIO, x, calibrated,
            )
            return {
                "calibrated": round(x, 6),
                "p10": None, "p50": None, "p90": None,
                "mae": None,
                "calibrated_source": "passthrough_sanity",
                "n_obs": self.ols.n,
            }
        # ─────────────────────────────────────────────────────────────────────

        p10 = self.q10.apply(x) if not self.q10.is_default else None
        p50 = self.q50.apply(x) if not self.q50.is_default else None
        p90 = self.q90.apply(x) if not self.q90.is_default else None
        return {
            "calibrated": round(calibrated, 6),
            "p10": round(p10, 6) if p10 is not None else None,
            "p50": round(p50, 6) if p50 is not None else None,
            "p90": round(p90, 6) if p90 is not None else None,
            "mae": self.ols.mae,
            "calibrated_source": "ols",
            "n_obs": self.ols.n,
        }


@dataclass
class CalibrationResult:
    """Full set of fitted models across all buckets."""
    fitted_at: str
    total_observations: int
    observations_in_window: int = 0
    models: dict[str, BucketModel] = field(default_factory=dict)

    def get_bucket(self, horizon_hours: float, hour_of_day: int) -> BucketModel:
        key = _bucket_key(horizon_hours, hour_of_day)
        return self.models.get(key, BucketModel(bucket_key=key))

    def apply(self, forecast: float, horizon_hours: float, hour_of_day: int) -> dict:
        return self.get_bucket(horizon_hours, hour_of_day).apply_all(forecast)

    def summary(self) -> dict:
        """Compact summary for diagnostic sensor attributes."""
        out = {
            "fitted_at": self.fitted_at,
            "total_observations": self.total_observations,
            "observation_window_days": OBSERVATION_WINDOW_DAYS,
            "observations_in_window": self.observations_in_window,
            "buckets": {},
        }
        for key, model in self.models.items():
            out["buckets"][key] = {
                "n": model.ols.n,
                "a": model.ols.a,
                "b": model.ols.b,
                "mae": model.ols.mae,
                "rmse": model.ols.rmse,
                "q10_a": model.q10.a,
                "q90_a": model.q90.a,
            }
        return out


# ── Bucket routing helpers ─────────────────────────────────────────────────────

def _horizon_label(horizon_hours: float) -> str:
    for i, edge in enumerate(HORIZON_EDGES[1:], 1):
        if horizon_hours < edge:
            return HORIZON_LABELS[i - 1]
    return HORIZON_LABELS[-1]


def _tod_label(hour: int) -> str:
    for label, bounds in TOD_BUCKETS.items():
        if bounds is None:
            continue
        lo, hi = bounds
        if lo <= hour < hi:
            return label
    return "offpeak"


def _bucket_key(horizon_hours: float, hour_of_day: int) -> str:
    return f"{_horizon_label(horizon_hours)}__{_tod_label(hour_of_day)}"


def all_bucket_keys() -> list[str]:
    return [
        f"{h}__{t}"
        for h in HORIZON_LABELS
        for t in TOD_BUCKETS
    ]


# ── Pure-Python OLS ───────────────────────────────────────────────────────────

def _ols(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """
    Fit actual = a * forecast + b using ordinary least squares.
    Returns (a, b).  Falls back to (1, 0) if degenerate.
    """
    n = len(pairs)
    if n < MIN_OBS:
        return 1.0, 0.0
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


# ── Engine ────────────────────────────────────────────────────────────────────

class CalibrationEngine:
    """
    Fits and applies OLS + quantile regression calibration models.

    Usage
    -----
    engine = CalibrationEngine()
    result = engine.fit(observations)   # CPU-bound; run in executor
    calibrated = result.apply(raw_price, horizon_hours, hour_of_day)
    """

    def fit(self, observations: list[Observation]) -> CalibrationResult:
        """
        Partition observations into buckets, fit all models.
        Returns a CalibrationResult ready to apply to new forecasts.

        Only observations within the last OBSERVATION_WINDOW_DAYS are used
        for fitting.  All observations remain in storage (the window is a
        fit-time filter only).
        """
        # ── Rolling window filter ────────────────────────────────────────────
        cutoff = datetime.now(timezone.utc) - timedelta(days=OBSERVATION_WINDOW_DAYS)
        windowed: list[Observation] = []
        for obs in observations:
            try:
                obs_dt = datetime.fromisoformat(obs.interval_time)
                if obs_dt.tzinfo is None:
                    # Legacy naive timestamp — assume NEM time (UTC+10)
                    obs_dt = obs_dt.replace(tzinfo=timezone(timedelta(hours=10)))
                if obs_dt >= cutoff:
                    windowed.append(obs)
            except (ValueError, TypeError):
                # Unparseable timestamp — include defensively
                windowed.append(obs)
        observations_in_window = len(windowed)

        # Partition
        buckets: dict[str, list[tuple[float, float]]] = {
            k: [] for k in all_bucket_keys()
        }
        for obs in windowed:
            if obs.is_intervention:
                # Skip intervention periods — prices are not market-driven
                continue
            key = _bucket_key(obs.horizon_hours, obs.hour_of_day)
            if key in buckets:
                # Cap per-bucket to avoid memory bloat; keep most recent
                if len(buckets[key]) < MAX_OBS:
                    buckets[key].append((obs.pd7day_forecast, obs.actual_rrp))

        now_str = to_nem_iso(now_nem())
        models: dict[str, BucketModel] = {}

        for key, pairs in buckets.items():
            model = BucketModel(bucket_key=key)

            # OLS
            a_ols, b_ols = _ols(pairs)
            a_ols = max(a_ols, 0.0)
            mae, rmse = _ols_metrics(pairs, a_ols, b_ols) if len(pairs) >= MIN_OBS else (None, None)
            model.ols = LinearCoeff(
                a=a_ols, b=b_ols, n=len(pairs), mae=mae, rmse=rmse
            )

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
                    "Bucket %s: n=%d OLS(a=%.3f, b=%.4f) MAE=%.4f "
                    "Q10(a=%.3f) Q90(a=%.3f)",
                    key, len(pairs), a_ols, b_ols, mae or 0,
                    model.q10.a, model.q90.a,
                )

        total = len([o for o in windowed if not o.is_intervention])
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
        return CalibrationResult(
            fitted_at=data.get("fitted_at", ""),
            total_observations=data.get("total_observations", 0),
            observations_in_window=data.get("observations_in_window", 0),
            models=models,
        )
