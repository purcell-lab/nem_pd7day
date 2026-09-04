"""
Calibrated prices and bands can be negative (issue #114).

The published isotonic value used to be floored at 0.0 and the band's lower
bound with it, so a mildly negative NEM price, the normal solar-trough state,
was published as "free". These tests pin the new behaviour end to end on a
bucket whose isotonic map is negative below about -0.0055 $/kWh.
"""
from __future__ import annotations

import numpy as np

from custom_components.nem_pd7day.calibration_engine import (
    SOURCE_PASSTHROUGH_BELOW_DOMAIN,
    BucketModel,
    CalibrationResult,
    IsotonicRegression,
    LinearCoeff,
    OlsModel,
    QuantileCoeff,
    ResidualQuantiles,
    RunFeatures,
    StpasaFeatures,
    _bucket_key,
    _clamp_band,
)
from custom_components.nem_pd7day.const import MARKET_PRICE_FLOOR, OLS_MIN_OBS

HORIZON = 36.0
HOUR = 12
KEY = _bucket_key(HORIZON, HOUR)
STPASA = StpasaFeatures(
    log_surplus=0.0, log_solar=0.0, log_demand=0.0,
    poe_spread_n=0.0, stpasa_run_at="2026-09-04T04:00:00+10:00",
)
RUN_FEATURES = RunFeatures(run_max_h6_rrp=0.0, run_mean_rrp=0.0, run_spread=0.0)

_XS = [round(-0.08 + 0.02 * i, 6) for i in range(21)]
_YS = [round(1.10 * x + 0.006, 6) for x in _XS]


def _bucket() -> BucketModel:
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray(_XS, dtype=float), np.asarray(_YS, dtype=float))
    return BucketModel(
        bucket_key=KEY,
        ols=LinearCoeff(a=1.10, b=0.006, n=120, mae=0.0075, rmse=0.0095),
        q10=QuantileCoeff(quantile=0.1, a=1.00, b=-0.010, n=120),
        q50=QuantileCoeff(quantile=0.5, a=1.05, b=0.000, n=120),
        q90=QuantileCoeff(quantile=0.9, a=1.10, b=0.020, n=120),
        iso_model=iso,
    )


def _iso(bucket: BucketModel, x: float) -> float:
    return float(bucket.iso_model.predict(np.asarray([x], dtype=float))[0])


def _result(bucket: BucketModel, prediction: float, resid: ResidualQuantiles | None = None):
    return CalibrationResult(
        fitted_at="2026-09-04T00:00:00+10:00",
        total_observations=500,
        models={KEY: bucket},
        ols_models={
            KEY: OlsModel(bucket_key=KEY, coef=[prediction] + [0.0] * 8, n_train=100, r2=0.6, resid=resid)
        },
    )


def _apply(res: CalibrationResult, forecast: float) -> dict:
    return res.apply(forecast, horizon_hours=HORIZON, hour_of_day=HOUR, stpasa=STPASA, run_features=RUN_FEATURES)


# ── Stage 1 ──────────────────────────────────────────────────────────────────


def test_mildly_negative_forecast_publishes_a_negative_calibrated_price():
    bucket = _bucket()
    for x in (-0.07, -0.05, -0.02):
        assert not bucket.is_below_domain(x)
        out = bucket.apply_all(x)
        assert out["calibrated_source"] == "isotonic"
        assert out["calibrated"] == round(_iso(bucket, x), 6)
        assert out["calibrated"] < 0.0, f"{x}: {out}"


def test_lower_bound_can_sit_below_a_negative_point_estimate():
    """p10 is not clamped up onto a negative point estimate any more."""
    bucket = _bucket()
    out = bucket.apply_all(-0.05)
    assert out["p10"] < out["calibrated"] < out["p90"], out
    assert out["p10"] == round(1.00 * -0.05 - 0.010, 6)


def test_clamp_band_floors_at_the_market_floor_not_zero():
    p10, p50, p90 = _clamp_band(-0.3, -1.5, -0.4, -0.1)
    assert p10 == MARKET_PRICE_FLOOR
    assert p50 == -0.4 and p90 == -0.1
    p10, p50, p90 = _clamp_band(0.05, -0.02, 0.01, 0.09)
    assert p10 == -0.02, "a negative lower bound below a positive estimate is kept"


def test_point_estimate_is_floored_at_the_market_floor():
    """A corrupt fit below -$1000/MWh publishes the floor, not the step."""
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray([0.001, 0.002, 0.003], dtype=float), np.asarray([-3.2, -3.1, -3.0], dtype=float))
    bucket = _bucket()
    bucket.iso_model = iso
    out = bucket.apply_all(0.002)
    assert out["calibrated"] == MARKET_PRICE_FLOOR
    assert out["p10"] is None or out["p10"] <= out["calibrated"]


def test_below_domain_passes_through_with_a_band():
    """A forecast below every training forecast is extrapolation: AEMO's value
    is published, and the quantile lines, which are linear, supply a band
    clamped to contain it (issue #117). The fixture's domain starts at -0.08."""
    bucket = _bucket()
    assert bucket.domain_min == -0.08
    assert bucket.is_below_domain(-0.25) and not bucket.is_below_domain(-0.08)
    out = bucket.apply_all(-0.25)
    assert out["calibrated_source"] == SOURCE_PASSTHROUGH_BELOW_DOMAIN
    assert out["calibrated"] == -0.25
    # q10 line: 1.00x - 0.010 = -0.26; q90 line: 1.10x + 0.020 = -0.255,
    # below the point estimate and so raised onto it.
    assert out["p10"] == -0.26 and out["p90"] == -0.25, out
    assert out["p10"] <= out["p50"] <= out["p90"]
    assert out["band_source"] == "stage1_quantile"


# ── Stage 2 ──────────────────────────────────────────────────────────────────


def test_stage_two_serves_a_negative_prediction_when_stage_one_is_negative():
    bucket = _bucket()
    x = -0.05
    assert _iso(bucket, x) < 0.0
    resid = ResidualQuantiles(bucket_key=KEY, q10=-0.02, q50=-0.001, q90=0.03, n=OLS_MIN_OBS * 2)
    out = _apply(_result(bucket, -0.03, resid), x)
    assert out["calibrated_source"] == "isotonic+stpasa"
    assert out["calibrated"] == -0.03
    assert out["p10"] == -0.05 and out["p90"] == 0.0, out
    assert out["p10"] < out["calibrated"] < out["p90"]


def test_stage_two_will_not_flip_a_negative_stage_one_value_positive():
    """The #73 hazard: "paid to consume" must not become "pay to consume"."""
    bucket = _bucket()
    x = -0.05
    out = _apply(_result(bucket, +0.02), x)
    assert out["calibrated_source"] == "isotonic"
    assert out["calibrated"] == round(_iso(bucket, x), 6) < 0.0


def test_stage_two_will_not_flip_a_positive_stage_one_value_negative():
    bucket = _bucket()
    x = 0.08
    out = _apply(_result(bucket, -0.02), x)
    assert out["calibrated_source"] == "isotonic"
    assert out["calibrated"] == round(_iso(bucket, x), 6) > 0.0


def test_stage_two_refuses_a_prediction_below_the_market_floor():
    bucket = _bucket()
    x = -0.05
    out = _apply(_result(bucket, -1.5), x)
    assert out["calibrated_source"] == "isotonic"


def test_stage_two_never_overrides_a_below_domain_passthrough():
    bucket = _bucket()
    out = _apply(_result(bucket, +0.05), -0.25)
    assert out["calibrated_source"] == SOURCE_PASSTHROUGH_BELOW_DOMAIN
    assert out["calibrated"] == -0.25
