"""
Calibration fitting: stage 1 (isotonic and quantile lines) and stage 2 (STPASA OLS).

Spec 006 moved ``CalibrationEngine.fit`` and ``fit_ols_stage2`` here, one
function per step, so a fit can be read, tested and changed a step at a time
without the engine. ``CalibrationEngine`` keeps both methods and delegates;
every fitted number, stored field and log line is what it was before the
move (``tests/test_fit_io.py``).

Stage 1, ``Stage1Fitter``:
  window_observations  the rolling OBSERVATION_WINDOW_DAYS window (#109)
  partition_stage1     exclusions, solar time-of-day keys, the MAX_OBS cap and
                       the decay weights
  fit_stage1_bucket    least-squares line, isotonic model and quantile lines
  training_count       the rows the fit counts as training observations

Stage 2, ``Stage2Fitter``, given the stage 1 result and run features:
  stage2_rows          the row filters, the STPASA join and the feature vector
  fit_stage2_bucket    lstsq, one leverage screen, R², residual quantiles and
                       feature ranges
  log_stage2_fit       the warning and summary lines

Log lines go to the engine's logger, not this module's, so a ``logger:``
entry a user has set for ``calibration_engine`` still catches them.

Two behaviours are pinned here as they are and filed for their own changes:
stage 1 keys by solar time of day while serving and stage 2 key by clock hour
(#208); and the MAX_OBS cap keeps the first rows of a full bucket in input
order, the oldest (#209). Stage 2 now trains against the published stage 1
result and inside its window (#210, #213).
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .calibration_engine import (
    DECAY_LAMBDA,
    OBSERVATION_WINDOW_DAYS,
    BucketModel,
    CalibrationResult,
    IsotonicRegression,
    LinearCoeff,
    Observation,
    OlsModel,
    QuantileCoeff,
    RunFeatures,
    StpasaFeatures,
    bucket_key_for,
    _hat_leverage,
    _horizon_label,
    _ols,
    _ols_metrics,
    _per_bucket_counts,
    _quantile_regression,
    _residual_quantiles,
    _tod_label,
    _tod_label_solar,
    all_bucket_keys,
)
from .const import (
    MAX_OBS,
    MIN_OBS,
    NEM_TZ,
    OLS_MIN_OBS,
    QUANTILES,
    STAGE2_LEVERAGE_MULTIPLE,
)
from .serving import (
    OLS_MAX_HORIZON_H,
    OLS_MIN_HORIZON_H,
    SPIKE_THRESHOLD,
    stage2_iso_feature,
)

_LOGGER = logging.getLogger(f"{__package__}.calibration_engine")

Pair = tuple[float, float]                  # (pd7day_forecast, actual_rrp)
Stage2Row = tuple[list[float], float]       # (feature vector without intercept, actual_rrp)


# ── Stage 1 ──────────────────────────────────────────────────────────────────

def window_observations(
    observations: Sequence[Observation], now_utc: datetime,
) -> list[tuple[Observation, datetime]]:
    """Observations inside the rolling window, each with its interval time.

    Only observations within the last OBSERVATION_WINDOW_DAYS are fitted; all
    of them stay in storage (the window is a fit-time filter only). A naive
    timestamp is read as NEM time, and an unparseable one is kept and
    weighted as of ``now``.
    """
    now_nem_dt = now_utc.astimezone(NEM_TZ)
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
    return windowed


def partition_stage1(
    windowed: Sequence[tuple[Observation, datetime]], region: str, now_nem: datetime,
) -> tuple[dict[str, list[Pair]], dict[str, list[float]]]:
    """Training pairs and decay weights per bucket, keyed by solar time of day.

    Every key of ``all_bucket_keys()`` is present, in that order, even when
    empty. Weights are ``exp(-DECAY_LAMBDA * days_ago)``; the region drives
    the solar elevation classification. Serving looks buckets up by clock
    hour instead (#208).
    """
    buckets: dict[str, list[Pair]] = {k: [] for k in all_bucket_keys()}
    bucket_weights: dict[str, list[float]] = {k: [] for k in all_bucket_keys()}
    # The label depends only on the interval and the region, and each interval
    # is seen by many runs, so compute it once per interval (#212).
    tod_by_interval: dict[datetime, str] = {}
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
        tod = tod_by_interval.get(obs_dt)
        if tod is None:
            tod = _tod_label_solar(obs_nem, region, _tod_label(obs_nem.hour))
            tod_by_interval[obs_dt] = tod
        key = f"{_horizon_label(obs.horizon_hours)}__{tod}"
        if key in buckets:
            buckets[key].append((obs.pd7day_forecast, obs.actual_rrp))
            # Compute exponential time-decay weight
            days_ago = (now_nem - obs_nem).total_seconds() / 86400.0
            bucket_weights[key].append(math.exp(-DECAY_LAMBDA * max(days_ago, 0.0)))
    # Cap each bucket at its MAX_OBS most recent rows (#209). Until then the
    # first MAX_OBS rows in input order were kept, and the input is mostly
    # oldest first, so a full bucket froze on its oldest rows and dropped the
    # ones decay weights most. The weight falls with age, so it ranks recency;
    # ties go to the later row, and kept rows stay in input order.
    for key, weights in bucket_weights.items():
        if len(weights) > MAX_OBS:
            ranked = sorted(range(len(weights)), key=lambda i: (weights[i], i))
            keep = sorted(ranked[-MAX_OBS:])
            buckets[key] = [buckets[key][i] for i in keep]
            bucket_weights[key] = [weights[i] for i in keep]
    return buckets, bucket_weights


def fit_quantile_lines(
    pairs: list[Pair], weights: list[float] | None,
) -> tuple[QuantileCoeff, QuantileCoeff, QuantileCoeff]:
    """The P10, P50 and P90 lines, their slopes sorted and clamped at zero.

    The three slopes are sorted ascending and handed to q10, q50 and q90 in
    that order, each keeping its own intercept and pinball loss, so the lines
    cannot cross in slope. The loss is published only from MIN_OBS rows.
    """
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

    q10, q50, q90 = (
        QuantileCoeff(
            quantile=q, a=q_results[attr][0], b=q_results[attr][1],
            n=len(pairs),
            pinball_loss=q_results[attr][2] if len(pairs) >= MIN_OBS else None,
        )
        for q, attr in zip(QUANTILES, ("q10", "q50", "q90"))
    )
    return q10, q50, q90


def fit_stage1_bucket(key: str, pairs: list[Pair], weights: list[float]) -> BucketModel:
    """One bucket's stage 1 model: least-squares line, isotonic model, quantile lines."""
    model = BucketModel(bucket_key=key)

    # OLS (weighted) — retained to populate LinearCoeff for quantile
    # regression initialisation and diagnostic attributes (a, b, mae, rmse).
    # The OLS calibrated value is not used in apply_all(); that path uses
    # the isotonic model below.
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

    model.q10, model.q50, model.q90 = fit_quantile_lines(pairs, weights)

    if len(pairs) >= MIN_OBS:
        _LOGGER.debug(
            "Bucket %s: n=%d isotonic+OLS(a=%.3f, b=%.4f) MAE=%.4f "
            "Q10(a=%.3f) Q90(a=%.3f)",
            key, len(pairs), a_ols, b_ols, mae or 0,
            model.q10.a, model.q90.a,
        )
    return model


def training_count(windowed: Sequence[tuple[Observation, datetime]]) -> int:
    """Windowed rows that are neither interventions nor spikes, before the MAX_OBS cap."""
    return len([
        obs for obs, _ in windowed
        if not obs.is_intervention and obs.actual_rrp < SPIKE_THRESHOLD and obs.pd7day_forecast < SPIKE_THRESHOLD
    ])


class Stage1Fitter:
    """Fits stage 1: partition observations into buckets and fit every bucket."""

    def fit(
        self,
        observations: Sequence[Observation],
        region: str = "QLD1",
        now: datetime | None = None,
    ) -> CalibrationResult:
        """
        Partition observations into buckets, fit all models.
        Returns a CalibrationResult ready to apply to new forecasts.

        *now* is the aware UTC instant the rolling window and decay weights
        are measured from; it defaults to the wall clock and exists so tests
        can pin it (issue #109).

        Weights are computed per-observation using exponential time decay:
          weight = exp(-DECAY_LAMBDA * days_ago)
        Region is used for solar elevation ToD classification.
        """
        now_utc = now or datetime.now(timezone.utc)
        now_nem_dt = now_utc.astimezone(NEM_TZ)
        windowed = window_observations(observations, now_utc)
        observations_in_window = len(windowed)
        buckets, bucket_weights = partition_stage1(windowed, region, now_nem_dt)

        now_str = now_nem_dt.isoformat()
        models: dict[str, BucketModel] = {
            key: fit_stage1_bucket(key, pairs, bucket_weights[key])
            for key, pairs in buckets.items()
        }

        total = training_count(windowed)
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


# ── Stage 2 ──────────────────────────────────────────────────────────────────

@dataclass
class Stage2Rows:
    """Stage 2 training rows per clock-hour bucket, and the rows dropped below the stage 1 domain.

    ``excluded`` counts, per bucket, the rows dropped below the stage 1
    domain, so the exposure is visible in the log rather than merely assumed
    to be zero (issue #79 noted the count was unknown).
    """

    rows: dict[str, list[Stage2Row]] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)


def _stage2_joined(
    obs: Observation,
    stpasa_by_key: Mapping[str, StpasaFeatures],
    run_features: Mapping[str, RunFeatures],
) -> tuple[StpasaFeatures, RunFeatures] | None:
    """The row's STPASA and run features, or None when a filter or a missing join drops it."""
    if obs.is_intervention:
        return None
    if obs.horizon_hours < OLS_MIN_HORIZON_H or obs.horizon_hours > OLS_MAX_HORIZON_H:
        return None
    if obs.actual_rrp >= SPIKE_THRESHOLD or obs.pd7day_forecast >= SPIKE_THRESHOLD:
        return None

    feat_key = f"{obs.interval_time}|{obs.forecast_run_at}"
    sf = stpasa_by_key.get(feat_key)
    if sf is None:
        return None
    rf = run_features.get(obs.forecast_run_at)
    if rf is None:
        return None
    return sf, rf


def _stage2_row(
    obs: Observation,
    obs_dt: datetime | None, region: str | None,
    stpasa_by_key: Mapping[str, StpasaFeatures],
    run_features: Mapping[str, RunFeatures],
    stage1: CalibrationResult,
) -> tuple[str, Stage2Row | None] | None:
    """One observation's stage 2 row and bucket; None when a filter drops it.

    ``(key, None)`` means the row was dropped below the stage 1 domain and is
    counted against ``key``.
    """
    joined = _stage2_joined(obs, stpasa_by_key, run_features)
    if joined is None:
        return None
    sf, rf = joined

    bucket_key = bucket_key_for(obs.horizon_hours, obs.hour_of_day, obs_dt, region)

    # Drop rows that the serving path never asks this model about.
    #
    # WHY: below the bucket's fitted domain the serving path publishes
    # the edge level and never consults stage 2 (serving.BelowDomainGate),
    # so a row there would be fitted for a region that is never served.
    # Both paths read the boundary from BucketModel.is_below_domain so
    # they cannot drift apart (#68, #79, #117). A row lands here when its
    # forecast changed after the stage-1 fit.
    #
    # The leverage hazard that the old fixed threshold happened to
    # cover (#79: one mis-joined deep negative row, isolated far from
    # the cluster, took the iso_cal coefficient from +1.13 to -0.15) is
    # handled by the hat-leverage screen in fit_stage2_bucket rather than
    # by a price.
    bucket = stage1.get_bucket(obs.horizon_hours, obs.hour_of_day, obs_dt, region)
    if bucket.is_below_domain(obs.pd7day_forecast):
        return bucket_key, None

    # The unfloored stage-1 value, through the same helper the serving
    # path reads, so a row is fitted from the number the same interval
    # would be served from. A floored feature paired with a genuinely
    # negative actual is what biased this coefficient: see
    # stage2_iso_feature and issue #85.
    iso_cal = stage2_iso_feature(
        bucket.apply_all(obs.pd7day_forecast), obs.pd7day_forecast
    )

    # The STAGE2_FEATURE_NAMES order.
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
    return bucket_key, (feature_vec, obs.actual_rrp)


def _interval_start(interval_time: str) -> datetime | None:
    """An interval start as stage 1 reads it, or None when it does not parse."""
    try:
        dt = datetime.fromisoformat(interval_time)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=NEM_TZ)


def stage2_rows(
    observations: Sequence[Observation],
    stpasa_by_key: Mapping[str, StpasaFeatures],
    run_features: Mapping[str, RunFeatures],
    stage1: CalibrationResult,
    region: str | None = None,
) -> Stage2Rows:
    """Every observation's stage 2 row, grouped by its serving bucket, in input order.

    Each observation's interval start, with ``region``, picks the
    solar-elevation bucket stage 1 trained the interval in (#208), the one
    serving uses, so each stage 2 model trains on the intervals and the
    stage 1 output it corrects when served.

    Filters, in order: intervention; horizon outside [OLS_MIN_HORIZON_H,
    OLS_MAX_HORIZON_H]; a spike on either side; no STPASA features for the
    interval and run; no run features for the run; below the stage 1 domain.
    The rolling window is applied by the caller, Stage2Fitter.fit (#210).
    """
    out = Stage2Rows()
    for obs in observations:
        obs_dt = _interval_start(obs.interval_time)  # None keys by clock hour
        found = _stage2_row(obs, obs_dt, region, stpasa_by_key, run_features, stage1)
        if found is None:
            continue
        bucket_key, row = found
        if row is None:
            out.excluded[bucket_key] = out.excluded.get(bucket_key, 0) + 1
            continue
        out.rows.setdefault(bucket_key, []).append(row)
    return out


def _lstsq(X: np.ndarray, y: np.ndarray) -> np.ndarray | None:
    """Least-squares coefficients, or None when LAPACK cannot solve (a non-finite design)."""
    try:
        coef, _resid, _rank, _sv = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    return coef


def _ols_model(bucket_key: str, X: np.ndarray, y: np.ndarray, coef: np.ndarray) -> OlsModel:
    """The published model of one fitted bucket: coefficients, R², residuals and ranges."""
    # R² for diagnostics.
    y_hat = X @ coef
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    # Feature ranges from the rows that survived the leverage screen,
    # so a screened-out row cannot widen the domain the serving gate
    # accepts. Column 0 is the intercept. Rounded OUTWARD to 6 dp:
    # rounding to nearest put a served value that equals a training
    # bound (horizon/168 at the same horizon, iso_cal at the domain
    # edge) a fraction of a micro-unit outside its own range and the
    # gate refused it. Issue #147.
    features_only = X[:, 1:]
    return OlsModel(
        bucket_key=bucket_key,
        coef=[round(float(c), 8) for c in coef],
        n_train=int(X.shape[0]),
        r2=round(r2, 6),
        # Fitted from the same X, y and coef, so the residual quantiles
        # can never describe a different fit than the one they ship
        # with. Issue #72.
        resid=_residual_quantiles(bucket_key, X, y, coef),
        feature_min=[
            math.floor(float(v) * 1e6) / 1e6 for v in features_only.min(axis=0)
        ],
        feature_max=[
            math.ceil(float(v) * 1e6) / 1e6 for v in features_only.max(axis=0)
        ],
    )


def fit_stage2_bucket(bucket_key: str, rows: list[Stage2Row]) -> tuple[OlsModel, int]:
    """One bucket's stage 2 model, and the number of rows the leverage screen removed.

    An empty ``OlsModel`` (no coefficients) means the bucket falls back to
    stage 1 when served. The screened count is returned even then, so the
    summary line reports it.
    """
    # OLS_MIN_OBS is counted AFTER exclusion, deliberately. A bucket
    # that only clears the floor by including rows the model is never
    # served on has not really cleared it, so it falls back to an empty
    # OlsModel and apply() keeps the stage-1 isotonic result. Falling
    # back is the safe direction: the alternative is a 9 feature fit on
    # fewer than 50 points, which is the over-fit that raised this
    # floor from 10 in the first place. See #79.
    if len(rows) < OLS_MIN_OBS:
        return OlsModel(bucket_key=bucket_key), 0
    # Design matrix with leading intercept column of ones.
    X = np.array([[1.0] + r[0] for r in rows], dtype=float)
    y = np.array([r[1] for r in rows], dtype=float)
    coef = _lstsq(X, y)
    if coef is None:
        return OlsModel(bucket_key=bucket_key), 0

    # Leverage screen. A row far from the rest of the design has a
    # hat-matrix diagonal near 1 and is fitted largely by itself, so a
    # single mis-joined actual there can invert a coefficient (#79).
    # Rows above STAGE2_LEVERAGE_MULTIPLE times the mean leverage p/n
    # are dropped and the bucket refitted once; if that leaves fewer
    # than OLS_MIN_OBS rows the bucket falls back like any thin one.
    # Data-driven and two-sided, unlike the price threshold it
    # replaces (#117): a deep negative row among many is ordinary, an
    # isolated one is not, and the same holds for a lone spike.
    high = _hat_leverage(X) > STAGE2_LEVERAGE_MULTIPLE * X.shape[1] / X.shape[0]
    n_high = int(high.sum())
    if n_high:
        X = X[~high]
        y = y[~high]
        if X.shape[0] < OLS_MIN_OBS:
            return OlsModel(bucket_key=bucket_key), n_high
        coef = _lstsq(X, y)
        if coef is None:
            return OlsModel(bucket_key=bucket_key), n_high
    return _ols_model(bucket_key, X, y, coef), n_high


def log_stage2_fit(
    models: Mapping[str, OlsModel], excluded: Mapping[str, int], screened: Mapping[str, int],
) -> None:
    """The stage 2 warning, when a fitted bucket has no residual quantiles, and summary line."""
    n_resid_fitted = sum(
        1
        for m in models.values()
        if m.resid is not None and m.resid.is_fitted
    )
    n_resid_missing = sum(
        1
        for m in models.values()
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
                    k for k, m in models.items()
                    if len(m.coef) >= 2
                    and (m.resid is None or not m.resid.is_fitted)
                )
            ),
        )

    n_excluded = sum(excluded.values())
    n_high_leverage = sum(screened.values())
    _LOGGER.info(
        "OLS stage2 fit: %d buckets evaluated (%d with sufficient STPASA obs, "
        "%d with stage-2 residual quantiles), "
        "%d rows excluded below the stage-1 domain%s, "
        "%d high-leverage rows screened%s",
        len(models),
        sum(1 for m in models.values() if len(m.coef) >= 2),
        n_resid_fitted,
        n_excluded,
        _per_bucket_counts(dict(excluded)),
        n_high_leverage,
        _per_bucket_counts(dict(screened)),
    )


class Stage2Fitter:
    """Fits stage 2 from observations, STPASA features, a stage 1 result and run features."""

    def fit(
        self,
        observations: Sequence[Observation],
        stpasa_by_key: Mapping[str, StpasaFeatures],
        stage1: CalibrationResult,
        run_features: Mapping[str, RunFeatures],
        region: str | None = None,
    ) -> dict[str, OlsModel]:
        """
        Fit per-bucket 9-feature OLS using combined PD7DAY + STPASA features.

        stpasa_by_key: mapping str(interval_time + "|" + run_at) → StpasaFeatures.
        stage1: the isotonic fit whose buckets give each row its stage-1
        feature and domain; run_features: RunFeatures per forecast run.

        Returns dict[bucket_key, OlsModel] in sorted key order. Only buckets
        whose horizon falls in [OLS_MIN_HORIZON_H, OLS_MAX_HORIZON_H] are
        fitted; each requires at least OLS_MIN_OBS observations carrying
        valid STPASA data. Under-populated buckets get an empty OlsModel
        (coef=[]).

        Feature order (after a leading 1.0 intercept term):
          [iso_calibrated, run_max_h6_rrp, run_mean_rrp, run_spread,
           horizon_hours/168, log_surplus, log_solar, log_demand, poe_spread_n]
        """
        # Train only inside stage 1's window, measured from the instant stage 1
        # was fitted at, so both stages see the same rows (#210). Run features
        # still come from every observation the caller passed.
        now_utc = datetime.fromisoformat(stage1.fitted_at).astimezone(timezone.utc)
        in_window = [obs for obs, _ in window_observations(observations, now_utc)]
        rows = stage2_rows(in_window, stpasa_by_key, run_features, stage1, region)
        models: dict[str, OlsModel] = {}
        screened: dict[str, int] = {}
        # Iterate the union so a bucket whose every candidate row was excluded
        # still gets an empty OlsModel rather than disappearing from the
        # result. apply() treats a missing key and an empty coef list the same
        # way, but the diagnostic summary should not lose the bucket.
        # Sorted, so the fit order and the log line are deterministic.
        for bucket_key in sorted(set(rows.rows) | set(rows.excluded)):
            model, n_high = fit_stage2_bucket(bucket_key, rows.rows.get(bucket_key, []))
            if n_high:
                screened[bucket_key] = n_high
            models[bucket_key] = model
        log_stage2_fit(models, rows.excluded, screened)
        return models
