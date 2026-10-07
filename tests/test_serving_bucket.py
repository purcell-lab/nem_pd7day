"""Serving and stage 2 use the bucket stage 1 trained the interval in (#208).

Stage 1 trains by solar elevation. Before #208 serving and stage 2 looked
buckets up by clock hour, so the morning_ramp buckets were fitted and never
served, and 07:00 to 10:00 in Queensland, with the sun well up, was served by
the night-time shoulder model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()
load_chain("const", "nem_time", "calibration_engine", "stpasa_client")

from custom_components.nem_pd7day import calibration_engine as ce  # noqa: E402
from custom_components.nem_pd7day.fitting import stage2_rows  # noqa: E402

HORIZON = 36.0
LEVELS = {"shoulder": 0.08, "morning_ramp": 0.05, "solar": 0.02, "peak": 0.16}


def _bucket(key: str, level: float) -> ce.BucketModel:
    iso = ce.IsotonicRegression(increasing=True, out_of_bounds="clip")
    iso.fit(np.asarray([0.0, 0.5, 1.0]), np.asarray([level, level, level]))
    return ce.BucketModel(bucket_key=key, iso_model=iso)


def _result() -> ce.CalibrationResult:
    h = ce._horizon_label(HORIZON)
    models = {f"{h}__{t}": _bucket(f"{h}__{t}", v) for t, v in LEVELS.items()}
    return ce.CalibrationResult(fitted_at="2026-10-07T00:00:00+00:00", total_observations=1, models=models)


def _label(dt: datetime, region: str = "QLD1") -> str:
    return ce._tod_label_solar(dt, region, ce._tod_label(dt.hour))


# A Brisbane October morning: sunrise near 05:15, the sun past 15 degrees by 07:00.
RAMP = datetime(2026, 10, 7, 5, 30, tzinfo=NEM_TZ)
SUN_UP = datetime(2026, 10, 7, 8, 30, tzinfo=NEM_TZ)


def test_the_fixture_times_are_the_cases_they_claim():
    assert _label(RAMP) == "morning_ramp" and ce._tod_label(RAMP.hour) == "shoulder"
    assert _label(SUN_UP) == "solar" and ce._tod_label(SUN_UP.hour) == "shoulder"


@pytest.mark.parametrize("dt, tod", [(RAMP, "morning_ramp"), (SUN_UP, "solar")])
def test_serving_uses_the_trained_bucket(dt, tod):
    out = _result().apply(0.3, HORIZON, dt.hour, interval_dt=dt, region="QLD1")
    assert out["calibrated"] == pytest.approx(LEVELS[tod])


def test_without_a_start_or_region_serving_keys_by_clock_hour():
    res = _result()
    assert res.apply(0.3, HORIZON, SUN_UP.hour)["calibrated"] == pytest.approx(LEVELS["shoulder"])
    assert res.apply(0.3, HORIZON, SUN_UP.hour, interval_dt=SUN_UP)["calibrated"] == pytest.approx(LEVELS["shoulder"])


def test_the_same_instant_in_utc_keys_the_same_bucket():
    utc = SUN_UP.astimezone(timezone.utc)
    assert ce.bucket_key_for(HORIZON, 0, utc, "QLD1") == ce.bucket_key_for(HORIZON, SUN_UP.hour, SUN_UP, "QLD1")
    naive = SUN_UP.replace(tzinfo=None)  # a legacy naive time is NEM time, as stage 1 reads it
    assert ce.bucket_key_for(HORIZON, SUN_UP.hour, naive, "QLD1") == ce.bucket_key_for(HORIZON, 8, SUN_UP, "QLD1")


def test_serving_agrees_with_training_over_a_year_of_intervals():
    """Every half hour for a year, in every region: the served key is _bucket_key_solar's."""
    start = datetime(2026, 1, 1, tzinfo=NEM_TZ)
    for region in ("QLD1", "NSW1", "VIC1", "SA1", "TAS1"):
        for i in range(0, 365 * 48, 7):  # a stride that walks through every half hour
            dt = start + timedelta(minutes=30 * i)
            assert ce.bucket_key_for(HORIZON, dt.hour, dt, region) == ce._bucket_key_solar(HORIZON, dt, region)


def _obs(dt: datetime) -> ce.Observation:
    return ce.Observation(
        interval_time=dt.isoformat(), horizon_hours=HORIZON, pd7day_forecast=0.3, actual_rrp=0.3,
        forecast_run_at=(dt - timedelta(hours=HORIZON)).isoformat(), hour_of_day=dt.hour,
        day_of_week=dt.weekday(), month=dt.month, gas_forecast_tj=None, qni_mwflow=None,
        qni_violation_degree=None, is_intervention=False,
    )


def test_stage2_rows_are_grouped_by_the_serving_bucket():
    obs = [_obs(RAMP), _obs(SUN_UP)]
    sf = ce.StpasaFeatures(log_surplus=7.0, log_solar=7.0, log_demand=9.0, poe_spread_n=0.2, stpasa_run_at="r")
    stpasa = {f"{o.interval_time}|{o.forecast_run_at}": sf for o in obs}
    runs = {o.forecast_run_at: ce.RunFeatures(run_max_h6_rrp=0.1, run_mean_rrp=0.1, run_spread=0.05) for o in obs}
    h = ce._horizon_label(HORIZON)
    rows = stage2_rows(obs, stpasa, runs, _result(), "QLD1")
    assert sorted(rows.rows) == [f"{h}__morning_ramp", f"{h}__solar"]
    # Each row's stage 1 feature is its serving bucket's level, not shoulder's.
    assert rows.rows[f"{h}__morning_ramp"][0][0][0] == pytest.approx(LEVELS["morning_ramp"])
    assert rows.rows[f"{h}__solar"][0][0][0] == pytest.approx(LEVELS["solar"])
    # Without a region the rows fall back to the clock hour, as before.
    assert sorted(stage2_rows(obs, stpasa, runs, _result()).rows) == [f"{h}__shoulder"]
