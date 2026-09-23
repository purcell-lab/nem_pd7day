"""Policy and HAEO contract tests, independent of Home Assistant."""
from datetime import datetime, timedelta

import pytest

from custom_components.nem_pd7day.scarcity_premium import (
    NEM, aware_time, build_premium, morning_samples,
)


def now(hour=10, minute=5):
    return datetime(2026, 9, 23, hour, minute, tzinfo=NEM)


def observations(price=0.041):
    return {
        (now(7, 0) + timedelta(minutes=i * 5)).isoformat(): price
        for i in range(1, 37)
    }


def base(price=0.010):
    return [
        {"time": (now(10, 0) + timedelta(minutes=i * 30)).isoformat(), "value": price}
        for i in range(8)
    ]


def test_haeo_contract_units_spacing_utc_and_endpoint():
    result = build_premium(now(), observations(), base(), base_fresh=True)
    assert result.status == "active"
    assert result.count == 36
    assert result.value == 0.031
    assert len(result.forecast) == 337
    assert result.forecast[-1]["value"] == 0
    for a, b in zip(result.forecast, result.forecast[1:]):
        assert set(a) == {"time", "value"}
        assert a["time"].endswith("+00:00")
        assert aware_time(b["time"]) - aware_time(a["time"]) == timedelta(minutes=30)
    assert all(p["value"] == 0 for p in result.forecast[8:])


@pytest.mark.parametrize("price,expected", [(0, .041), (.041, 0), (.080, 0), (-.050, .065)])
def test_forecast_catchup_and_negative_price_cap(price, expected):
    result = build_premium(now(), observations(), base(price), base_fresh=True)
    assert result.value == expected


def test_extreme_morning_price_cannot_raise_floor_above_cap():
    result = build_premium(now(), observations(10), base(), base_fresh=True)
    assert result.target_floor == .065
    assert result.value == .055


@pytest.mark.parametrize("price", [0, -.020, .030])
def test_threshold_is_strict_and_accepts_zero_negative(price):
    result = build_premium(now(), observations(price), [], base_fresh=False)
    assert result.status == "signal_below_threshold"
    assert all(p["value"] == 0 for p in result.forecast)


def test_partial_window_not_used_early():
    result = build_premium(now(9, 55), observations(), base(), base_fresh=True)
    assert result.count == 35
    assert result.morning_mean is None
    assert result.status == "waiting_for_morning"
    assert all(p["value"] == 0 for p in result.forecast)


def test_one_missing_observation_is_unavailable():
    obs = observations()
    obs.pop(next(iter(obs)))
    result = build_premium(now(), obs, base(), base_fresh=True)
    assert result.status == "incomplete_morning"
    assert result.value is None and result.forecast == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, None, "unknown"])
def test_invalid_observations_are_not_silently_zero(value):
    obs = observations()
    obs[next(iter(obs))] = value
    assert build_premium(now(), obs, base(), base_fresh=True).status == "incomplete_morning"


def test_interval_end_semantics_and_duplicates():
    obs = observations()
    obs[now(7, 0).isoformat()] = 999
    obs[now(10, 5).isoformat()] = 999
    obs[now(8, 1).isoformat()] = 999
    # Same instant in another timezone must not create a 37th observation.
    obs["2026-09-22T22:00:00+00:00"] = .041
    result = morning_samples(obs, now())
    assert len(result) == 36
    assert all(value == .041 for value in result.values())


def test_restart_restore_timezone_normalisation_and_next_day_reset():
    obs = morning_samples(observations(), now())
    assert build_premium(now(), obs, base(), base_fresh=True).value == .031
    tomorrow = now() + timedelta(days=1)
    assert build_premium(tomorrow, obs, base(), base_fresh=True).status == "incomplete_morning"


def test_fourteen_hundred_resets_even_without_inputs():
    result = build_premium(now(14, 0), {}, [], base_fresh=False)
    assert result.status == "outside_window"
    assert result.value == 0
    assert all(p["value"] == 0 for p in result.forecast)


def test_stale_base_unavailable_only_when_needed():
    result = build_premium(now(), observations(), base(), base_fresh=False)
    assert result.status == "base_forecast_stale"
    assert result.forecast == []


def test_missing_and_invalid_base_rows():
    for rows in [base()[:-1], [{"time": now(10, 0).isoformat(), "value": float("nan")}]]:
        assert build_premium(now(), observations(), rows, base_fresh=True).status == "base_forecast_gap"
    assert build_premium(now(), observations(), base() * 2, base_fresh=True).status == "duplicate_base_interval"


def test_half_hour_bucket_contains_current_time():
    result = build_premium(now(11, 47), observations(), base(), base_fresh=True)
    assert result.forecast[0]["time"] == "2026-09-23T01:30:00+00:00"
    assert result.forecast[5]["value"] == 0


def test_no_naive_timestamps():
    with pytest.raises(ValueError):
        build_premium(datetime(2026, 9, 23, 10), {}, [], base_fresh=True)
