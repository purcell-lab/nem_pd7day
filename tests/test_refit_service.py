"""
refit_service: the engine inputs built from stored observations, the STPASA
feature map stage 2 joins on, and the iso history record (spec 004).

Run with:  python -m pytest tests/test_refit_service.py -v
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from custom_components.nem_pd7day.refit_service import (
    ISO_HISTORY_LIMIT,
    engine_observations,
    iso_history_record,
    stpasa_feature_map,
)
from ha_free import assert_imports_without_home_assistant

ROW = {
    "interval_time": "2026-09-20T18:00:00+10:00", "horizon_hours": 29.0,
    "pd7day_forecast": 0.112, "actual_rrp": 0.11, "forecast_run_at": "2026-09-19T13:00:00+10:00",
    "hour_of_day": 18, "day_of_week": 6, "month": 9,
}
FEATURES = dict(stpasa_log_surplus=7.2, stpasa_log_solar=0.0, stpasa_log_demand=8.7,
                stpasa_poe_spread_n=-0.17)


def test_imports_without_home_assistant():
    assert_imports_without_home_assistant("refit_service")


def test_engine_observations_carry_every_field_with_the_stored_defaults():
    full = dict(ROW, gas_forecast_tj=131.4, qni_mwflow=-500.0, qni_violation_degree=1.5,
                is_intervention=True)
    first, bare = engine_observations([full, ROW])
    assert first._asdict() == {k: full[k] for k in first._fields}
    assert (bare.gas_forecast_tj, bare.qni_mwflow, bare.qni_violation_degree,
            bare.is_intervention) == (None, None, None, False)


def test_feature_map_keeps_complete_rows_only_and_zero_is_not_missing():
    partial = dict(ROW, forecast_run_at="P", **{**FEATURES, "stpasa_log_demand": None})
    missing = dict(ROW, forecast_run_at="M", stpasa_log_surplus=7.0)
    complete = dict(ROW, **FEATURES)
    fmap = stpasa_feature_map([partial, missing, complete])
    key = f"{ROW['interval_time']}|{ROW['forecast_run_at']}"
    assert list(fmap) == [key]
    feat = fmap[key]
    assert (feat.log_surplus, feat.log_solar, feat.log_demand, feat.poe_spread_n) == (7.2, 0.0, 8.7, -0.17)
    assert feat.stpasa_run_at == ""
    assert stpasa_feature_map([dict(complete, stpasa_run_at="S")])[key].stpasa_run_at == "S"


def test_iso_history_record_takes_the_compression_ratio_per_bucket():
    result = NS(fitted_at="2026-09-20T12:00:00+10:00", summary=lambda: {"buckets": {
        "h00_06__peak": {"compression_ratio": 0.8, "n": 12},
        "h06_12__solar": {"compression_ratio": None},
    }})
    assert iso_history_record(result) == {
        "fitted_at": "2026-09-20T12:00:00+10:00",
        "buckets": {"h00_06__peak": 0.8, "h06_12__solar": None},
    }
    assert ISO_HISTORY_LIMIT == 48
