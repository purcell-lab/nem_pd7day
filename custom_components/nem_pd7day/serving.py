"""
NEM PD7DAY serving path
=======================
What a fitted CalibrationResult publishes for one interval (spec 005).

Stage 1 is one of three paths, chosen by ``BucketModel.apply_all`` in this
order: a passthrough when the bucket has no isotonic model, the below-domain
clip, and the isotonic prediction. Each is a function here that builds the
published dict.

Stage 2, the STPASA correction, may then replace the stage 1 value.
``CalibrationResult.apply`` runs the interval through ``SERVING_GATES``, an
ordered tuple of small gates, each carrying the issues it guards; the first
that refuses serves the stage 1 dict itself, unchanged. When every gate
admits, ``stage2_result`` builds the published dict: a copy of stage 1 with
the prediction, its band and its labels. Adding a gate means adding a class
and one entry in the tuple.

The constants and band helpers below are defined here, once, and imported by
``calibration_engine``, which keeps every name importable from there.

Import direction. ``calibration_engine`` imports this module at module level,
so this module imports nothing from it at module level, not even under
``TYPE_CHECKING``: the golden-master harness orders module loads by every
module-level relative import and refuses a cycle. The engine types this module
works on are described by the protocols below, which the engine's dataclasses
satisfy structurally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Protocol

import numpy as np

from .const import ATTR_CAL_BAND_SOURCE, MARKET_PRICE_FLOOR

# ── Below-domain clip ────────────────────────────────────────────────────────
# A raw forecast below the smallest forecast a bucket was fitted on is outside
# the isotonic model's domain, and there is no settled actual in the window
# near it, so the raw value is not relied on. The bucket answers as if the
# forecast were at its floor:
#
#     calibrated(x) = iso(x_min)                             for x < x_min
#     band(x)       = quantile lines at x_min, clamped to contain it
#
# labelled SOURCE_ISOTONIC_BELOW_DOMAIN. This is out_of_bounds="clip", the
# isotonic model's own behaviour, applied to the band as well as the point, so
# the published triple below the floor is the published triple at the floor.
# Continuous, monotone, no constant, and independent of how deep AEMO went.
# The raw value stays available as raw_rrp on every forecast entry.
#
# History. A fixed NEGATIVE_PASSTHROUGH_THRESHOLD of -0.10 $/kWh sent every
# forecast at or below it straight through with a zero-width band (issue
# #117). v3.8.0 replaced it with a raw passthrough below the bucket's domain,
# which put a step at the domain floor on every QLD1 morning ramp (issue
# #120). v3.8.1 shifted the raw value by the edge correction, which removed
# the step but kept AEMO's depth: a -0.667 $/kWh PD7DAY value six days out,
# in a bucket with evidence down to -0.10, was published as -0.639 (issue
# #123). Below the evidence, publish the evidence.
SOURCE_ISOTONIC_BELOW_DOMAIN = "isotonic_below_domain"


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
# Same lines, unclamped, on the passthrough path. See stage1_passthrough.
BAND_SOURCE_STAGE1_RAW = "stage1_quantile_unclamped"
# No fitted quantile line to build a band from on the below-domain path.
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
    (FeatureDomainGate), so a row is fitted from the same number the
    same interval would be served from. Drift between those two is the #68 bug
    class, which is why this is a helper rather than a dict lookup written out
    twice.

    History: apply_all used to floor the published isotonic prediction at
    0.0. For a mildly negative raw forecast, inside the fitted domain and so
    genuinely served by stage 2, that floor set the
    feature to exactly 0.0 while the settled actual was negative, and the
    fitted iso_cal coefficient absorbed the error: +8.1 per cent from one such
    row in a 78 row bucket, +87.5 per cent from sixteen. Issue #85 unfloored
    the feature and kept the floor on the published price. Issue #114 then
    removed the published floor as well: the isotonic model is fitted on
    negative actuals like any other, so a negative prediction is a fitted
    value, and publishing 0.0 in its place turned "paid to consume" into
    "free" on about one interval in nine on the live install.

    The feature and the published price are now the same number on the
    isotonic path. The separate key is kept so a store or caller from before
    the split keeps working, and so the two cannot drift apart again if a
    floor is ever reintroduced on one side.

    The dict fallbacks are for a caller holding a result dict built before this
    split existed, which degrades to the previous behaviour instead of raising.
    """
    value = calibrated.get(ISO_FEATURE_KEY)
    if value is None:
        value = calibrated.get("calibrated", forecast)
    return float(value)


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


# ── What the serving path reads from the engine's models ─────────────────────

class IsotonicModel(Protocol):
    """``calibration_engine.IsotonicRegression``, as stage 1 calls it."""

    def predict(self, x: np.ndarray) -> np.ndarray: ...


class BucketStats(Protocol):
    """``calibration_engine.LinearCoeff``, the fields stage 1 publishes."""

    @property
    def n(self) -> int: ...

    @property
    def mae(self) -> float | None: ...


class Stage1Bucket(Protocol):
    """``calibration_engine.BucketModel``, as the serving path reads it."""

    @property
    def iso_model(self) -> IsotonicModel | None: ...

    @property
    def ols(self) -> BucketStats: ...

    @property
    def domain_min(self) -> float | None: ...

    @property
    def edge_value(self) -> float: ...

    def is_below_domain(self, x: float) -> bool: ...

    def raw_band(self, x: float) -> tuple[float | None, float | None, float | None]: ...


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

    A fitted p10 is first floored at MARKET_PRICE_FLOOR, -$1000/MWh, the only
    price the market cannot go below, and then clamped down to ``calibrated``.
    The floor used to be 0.0, which clamped every lower bound up onto a
    negative point estimate and hid mild negative prices (issue #114). The
    order matters: flooring first and clamping second means a negative point
    estimate below the floor still gets a lower bound no higher than itself.

    A ``None`` quantile means that level was not fitted (fewer than MIN_OBS
    observations) and stays ``None`` rather than being invented.

    Every published point estimate must be clamped through this function.
    Stage 2 originally clamped only against the isotonic value and then
    replaced the point estimate without re-clamping, which published a value
    outside its own band on roughly one interval in six (issue #69).
    """
    if p10 is not None:
        p10 = min(max(MARKET_PRICE_FLOOR, p10), calibrated)
    if p90 is not None:
        p90 = max(calibrated, p90)
    if p50 is not None:
        # Same floor reasoning as p10 when there is no fitted p10 to bound by.
        p50_lo = p10 if p10 is not None else min(MARKET_PRICE_FLOOR, calibrated)
        p50_hi = p90 if p90 is not None else float("inf")
        p50 = max(p50_lo, min(p50_hi, p50))
    return p10, p50, p90


# ── Stage 1: the three paths of BucketModel.apply_all ────────────────────────

def stage1_passthrough(bucket: Stage1Bucket, x: float) -> dict:
    """The raw forecast, when the bucket has no isotonic model."""
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
    p10, p50, p90 = _order_band(*bucket.raw_band(x))
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
        "n_obs": bucket.ols.n,
    }


def stage1_below_domain(bucket: Stage1Bucket, x: float) -> dict:
    """The bucket's answer at its floor, for a forecast below its domain (#123)."""
    # No evidence below the floor, so the raw value is not relied on:
    # the point and the band are the bucket's answer at its floor
    # (see SOURCE_ISOTONIC_BELOW_DOMAIN). Clamped to contain the point
    # like every other published triple; None only when no line is
    # fitted.
    lo = bucket.domain_min
    assert lo is not None
    calibrated = max(bucket.edge_value, MARKET_PRICE_FLOOR)
    p10, p50, p90 = _clamp_band(calibrated, *bucket.raw_band(lo))
    fitted = any(v is not None for v in (p10, p50, p90))
    return {
        "calibrated": round(calibrated, 6),
        "p10": round(p10, 6) if p10 is not None else None,
        "p50": round(p50, 6) if p50 is not None else None,
        "p90": round(p90, 6) if p90 is not None else None,
        # The published value is the feature too; stage 2 never
        # consults this result (see CalibrationResult.apply, gate 2a).
        ISO_FEATURE_KEY: round(calibrated, 6),
        BAND_SOURCE_KEY: BAND_SOURCE_STAGE1 if fitted else BAND_SOURCE_PASSTHROUGH,
        "calibrated_source": SOURCE_ISOTONIC_BELOW_DOMAIN,
        "n_obs": bucket.ols.n,
    }


def stage1_isotonic(bucket: Stage1Bucket, x: float) -> dict:
    """The isotonic prediction, unfloored above the market floor (#114, #144)."""
    # ── Isotonic calibration ────────────────────────────────────────────
    # IsotonicRegression.predict() with out_of_bounds='clip': forecasts
    # above the training x-range are clipped to the last step, a clean
    # normal-market estimate for a spike input; below it is handled by
    # stage1_below_domain.
    # The prediction is published as fitted, negative or not. It used to
    # be floored at 0.0 on the claim that a calibrated price cannot be
    # negative; in the NEM it can, mild negatives are the normal solar
    # trough state, and the model is fitted on negative actuals like any
    # other. The floor published 0.0 on about one interval in nine on the
    # live install and hid the sign (issue #114). The one floor that
    # remains is the market price floor, -$1000/MWh, which a corrupt
    # observation batch can drag a fitted step below and no price can be.
    iso_model = bucket.iso_model
    assert iso_model is not None  # BucketModel.apply_all routes here only with one
    iso_raw = float(iso_model.predict(np.asarray([x], dtype=float))[0])
    calibrated = max(iso_raw, MARKET_PRICE_FLOOR)

    # Clamp the band so it contains calibrated and stays ordered.
    p10, p50, p90 = _clamp_band(calibrated, *bucket.raw_band(x))

    return {
        "calibrated": round(calibrated, 6),
        # Same number as "calibrated", the floored value, not iso_raw:
        # every stage 1 path publishes the feature equal to the point
        # estimate (see stage1_below_domain and stage1_passthrough), and
        # stage2_iso_feature's docstring states it as the invariant. This
        # branch used to publish the unfloored iso_raw here instead, so a
        # fitted step dragged below MARKET_PRICE_FLOOR by a corrupt
        # observation batch fed stage 2 a feature more extreme than the
        # price it was ever shown next to (issue #144).
        ISO_FEATURE_KEY: round(calibrated, 6),
        "p10": round(p10, 6) if p10 is not None else None,
        "p50": round(p50, 6) if p50 is not None else None,
        "p90": round(p90, 6) if p90 is not None else None,
        "ols_mae": bucket.ols.mae,
        BAND_SOURCE_KEY: BAND_SOURCE_STAGE1,
        "calibrated_source": "isotonic",
        "n_obs": bucket.ols.n,
    }


# ── Stage 2: the gate pipeline ───────────────────────────────────────────────

class Stage2Model(Protocol):
    """``calibration_engine.OlsModel``, as the gates and stage2_result call it."""

    @property
    def coef(self) -> list[float]: ...

    def serves(self, features: list[float]) -> bool: ...

    def predict(self, features: list[float]) -> float: ...

    def residual_band(self, prediction: float) -> tuple[float, float, float] | None: ...


class StpasaInputs(Protocol):
    """``calibration_engine.StpasaFeatures``."""

    @property
    def log_surplus(self) -> float: ...

    @property
    def log_solar(self) -> float: ...

    @property
    def log_demand(self) -> float: ...

    @property
    def poe_spread_n(self) -> float: ...

    @property
    def stpasa_run_at(self) -> str: ...


class RunInputs(Protocol):
    """``calibration_engine.RunFeatures``."""

    @property
    def run_max_h6_rrp(self) -> float: ...

    @property
    def run_mean_rrp(self) -> float: ...

    @property
    def run_spread(self) -> float: ...


@dataclass(slots=True)
class Stage2Context:
    """One interval on its way through SERVING_GATES.

    ``stage1`` is the dict BucketModel.apply_all returned, and it is what is
    served, unchanged and as the same object, when any gate refuses.
    ``bucket_key`` is the key CalibrationResult.apply routed the interval by,
    which Stage2ModelGate looks the stage 2 model up by. The last three fields
    are filled in by the gates that establish them.

    Mutable and slotted, and filled in place, because this runs about 1,650
    times per state write: a frozen dataclass rebuilt by each gate that sets a
    field cost more than the whole serving path it wraps. Only the gates and
    ``CalibrationResult.apply`` write to it.
    """

    forecast: float
    horizon_hours: float
    hour_of_day: int
    stage1: dict
    bucket: Stage1Bucket
    stpasa: StpasaInputs | None
    run_features: RunInputs | None
    bucket_key: str
    ols: Stage2Model | None = None             # set by Stage2ModelGate
    features: list[float] | None = None        # set by FeatureDomainGate, in STAGE2_FEATURE_NAMES order
    prediction: float | None = None            # set by FeatureDomainGate after serves() admits


class ServingGate(Protocol):
    """One decision on whether stage 2 may replace stage 1.

    Returns the context to pass on, or None to refuse: the interval is then
    served the stage 1 dict unchanged, and no later gate runs.
    """

    name: str
    issues: tuple[str, ...]

    def __call__(
        self, ctx: Stage2Context, models: Mapping[str, Stage2Model]
    ) -> Stage2Context | None: ...


class BelowDomainGate:
    """Never override a below-domain clip."""

    name: str = "below_domain"
    issues: tuple[str, ...] = ("#73", "#114", "#117")

    def __call__(
        self, ctx: Stage2Context, models: Mapping[str, Stage2Model]
    ) -> Stage2Context | None:
        # WHY: below the bucket's fitted domain the stage-1 value is the
        # edge level, not a fit at this forecast, and the stage-2 OLS was
        # fitted on rows inside that domain, so a prediction there is
        # extrapolation on both features. It also carries an asymmetric cost: a positive
        # prediction over a deeply negative raw forecast flips the
        # published sign, turning "paid to consume" into "pay to consume",
        # which is the one error a battery or controllable load schedule
        # cannot absorb. The sign-disagreement fallback in SignAgreementGate protects
        # the served region; this gate protects the region the fit never
        # covered. See #73, #114 and #117.
        if ctx.stage1.get("calibrated_source") == SOURCE_ISOTONIC_BELOW_DOMAIN:
            return None
        return ctx


class Stage2InputsGate:
    """Stage 2 only inside the OLS horizon band, with both feature groups."""

    name: str = "stage2_inputs"
    issues: tuple[str, ...] = ()

    def __call__(
        self, ctx: Stage2Context, models: Mapping[str, Stage2Model]
    ) -> Stage2Context | None:
        # STPASA correction only inside the OLS horizon band, and only
        # when both feature groups are present.
        if (
            ctx.stpasa is None
            or ctx.run_features is None
            or ctx.horizon_hours < OLS_MIN_HORIZON_H
            or ctx.horizon_hours > OLS_MAX_HORIZON_H
        ):
            return None
        return ctx


class Stage2ModelGate:
    """Stage 2 only where the bucket has a fitted OLS model."""

    name: str = "stage2_model"
    issues: tuple[str, ...] = ()

    def __call__(
        self, ctx: Stage2Context, models: Mapping[str, Stage2Model]
    ) -> Stage2Context | None:
        # Look up the OLS model for this bucket.
        ols = models.get(ctx.bucket_key)
        if ols is None or len(ols.coef) < 2:
            return None
        ctx.ols = ols
        return ctx


class FeatureDomainGate:
    """Stage 2 only where its extrapolation is within the published uncertainty."""

    name: str = "feature_domain"
    issues: tuple[str, ...] = ("#85", "#147", "#153")

    def __call__(
        self, ctx: Stage2Context, models: Mapping[str, Stage2Model]
    ) -> Stage2Context | None:
        ols, stpasa, run_features = ctx.ols, ctx.stpasa, ctx.run_features
        assert ols is not None and stpasa is not None and run_features is not None
        # Build the 8-feature vector (intercept handled inside predict()).
        # The feature is the unfloored stage-1 value, which differs from the
        # published one only inside the floored band, and is read through
        # the same helper fit_ols_stage2 uses. See issue #85.
        iso_cal = stage2_iso_feature(ctx.stage1, ctx.forecast)
        features = [
            float(iso_cal),
            run_features.run_max_h6_rrp,
            run_features.run_mean_rrp,
            run_features.run_spread,
            ctx.horizon_hours / 168.0,
            stpasa.log_surplus,
            stpasa.log_solar,
            stpasa.log_demand,
            stpasa.poe_spread_n,
        ]

        # Serve stage 2 only where its extrapolation is within the
        # uncertainty the bucket already publishes. #123's principle
        # applied to stage 2: below the evidence, publish the evidence.
        # A feature outside its training range means the prediction is an
        # extrapolation of a linear model, and the sign and floor gates
        # below cannot tell a plausible extrapolation from a blow-up that
        # happens to land between the floor and zero, which is exactly
        # what -$0.86 for a raw -$0.10 was (#147). The excursion is
        # weighed by the coefficient it multiplies against half the
        # bucket's residual spread (#153), so a hairline excursion on a
        # feature the model barely uses does not cost the row its
        # correction while the #147 case still cannot be served. See
        # OlsModel.serves.
        if not ols.serves(features):
            return None

        # Predict.
        ctx.features = features
        ctx.prediction = ols.predict(features)
        return ctx


class SignAgreementGate:
    """Stage 2 never flips the sign stage 1 published."""

    name: str = "sign_agreement"
    issues: tuple[str, ...] = ("#73", "#114")

    def __call__(
        self, ctx: Stage2Context, models: Mapping[str, Stage2Model]
    ) -> Stage2Context | None:
        prediction = ctx.prediction
        assert prediction is not None
        # Fall back to the isotonic result when stage 2 disagrees with it on
        # sign. Before issue #114 this was `prediction <= 0.0`, which existed
        # to stop a positive stage-1 value being flipped negative and, as a
        # side effect, made stage 2 unable to publish a negative at all. Now
        # that stage 1 publishes negatives, the protection is symmetric: a
        # negative stage-1 value is not flipped positive, which is the error
        # #73 called out ("paid to consume" becoming "pay to consume"), and
        # a non-negative one is not flipped negative. Where both agree on
        # sign, including both negative, the stage-2 value is served.
        iso_value = float(ctx.stage1["calibrated"])
        if (prediction < 0.0) != (iso_value < 0.0):
            return None
        return ctx


class MarketFloorGate:
    """Stage 2 never publishes below the market price floor."""

    name: str = "market_floor"
    issues: tuple[str, ...] = ("#114",)

    def __call__(
        self, ctx: Stage2Context, models: Mapping[str, Stage2Model]
    ) -> Stage2Context | None:
        prediction = ctx.prediction
        assert prediction is not None
        # Below the market floor is not a price; treat it like the sign
        # disagreement rather than publishing it.
        if prediction < MARKET_PRICE_FLOOR:
            return None
        return ctx


# The order is part of the contract: each gate may rely on the ones before it
# (the model gate on the inputs gate, the sign and floor gates on the
# prediction the feature domain gate makes), and ols.serves and ols.predict
# each run at most once per interval and never after a refusal. A test pins
# the names and issues in this order (spec 005, invariant 3).
SERVING_GATES: Final[tuple[ServingGate, ...]] = (
    BelowDomainGate(),
    Stage2InputsGate(),
    Stage2ModelGate(),
    FeatureDomainGate(),
    SignAgreementGate(),
    MarketFloorGate(),
)


# ── Stage 2: the published result ────────────────────────────────────────────
# Replace the point estimate, then re-clamp the band around it.
#
# apply_all() clamped the band against the *isotonic*
# value.  Replacing the point estimate and inheriting that band
# published a value outside its own p10 to p90 whenever the stage-2
# prediction moved past a stage-1 bound, which on a five-region
# snapshot was 522 of 3075 intervals across 9 sensors (issue #69).
#
# The band is re-derived from the unclamped quantile fits rather
# than from the already-clamped stage-1 band, so the result is
# exactly what apply_all() would have returned had the stage-2
# value been the point estimate all along.  Re-clamping the clamped
# band instead would inherit a p10 pulled down to the isotonic
# value and publish a looser interval than the fits support.
#
# Re-clamping made the triple self-consistent; it did not make the
# band a stage-2 interval.  The quantile fits know nothing about the
# STPASA features, so where the prediction landed outside them the
# nearer bound was pulled onto the point estimate, reporting zero
# uncertainty on that side.  On the first live measurement, a single
# residential premises in SE Queensland, QLD1, the run at
# 2026-09-03T07:30:00+10:00, that was 98 of 330 intervals, up from 36
# before the re-clamp, and strongly one-sided: 82 onto p10 against 16
# onto p90.  The band also did not tighten, median width 0.035764 to
# 0.036862 $/kWh.  See issue #72.
#
# So the band is now built from the stage-2 model's own residual
# quantiles when the bucket has them: prediction plus the 10th, 50th
# and 90th percentile of its leave-one-out residuals.  That band is
# centred on the prediction by construction, so it contains it
# without any clamping and cannot collapse.
#
# _clamp_band is still applied on top, for two reasons that are not
# about containment: it floors p10 at the market price floor, the
# same floor every other published lower bound carries, and it is
# the one place the ordering and containment invariants are enforced,
# so leaving it out would make this the only published triple not
# passing through them.  On the residual path it is a no-op except
# for that floor.

def stage2_result(ctx: Stage2Context) -> dict:
    """The stage 1 dict copied, with the stage 2 prediction and its band."""
    ols, stpasa, prediction = ctx.ols, ctx.stpasa, ctx.prediction
    assert ols is not None and stpasa is not None and prediction is not None
    out = dict(ctx.stage1)
    out["calibrated"] = round(prediction, 6)
    out["calibrated_source"] = "isotonic+stpasa"
    out["stpasa_run_at"] = stpasa.stpasa_run_at

    raw_band: tuple[float | None, float | None, float | None]
    resid_band = ols.residual_band(prediction)
    if resid_band is not None:
        raw_band = resid_band
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
        raw_band = ctx.bucket.raw_band(ctx.forecast)
        band_source = BAND_SOURCE_STAGE2_FALLBACK

    p10, p50, p90 = _clamp_band(prediction, *raw_band)
    out["p10"] = round(p10, 6) if p10 is not None else None
    out["p50"] = round(p50, 6) if p50 is not None else None
    out["p90"] = round(p90, 6) if p90 is not None else None
    out[BAND_SOURCE_KEY] = band_source
    return out
