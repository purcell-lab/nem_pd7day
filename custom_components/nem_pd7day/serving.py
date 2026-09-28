"""
NEM PD7DAY serving path
=======================
What a fitted CalibrationResult publishes for one interval (spec 005).

Stage 1 is one of three paths, chosen by ``BucketModel.apply_all`` in this
order: a passthrough when the bucket has no isotonic model, the below-domain
clip, and the isotonic prediction. Each is a function here that builds the
published dict.

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

from typing import Protocol

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
# price: see calibration_engine.stage2_iso_feature and issue #85.
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
