"""Policy and HAEO contract tests, independent of Home Assistant.

The signal is the mean of the QLD1 dispatch prices of the last 30 minutes
(six five-minute settlement ends), used while the newest is at most 10
minutes old.
"""
from datetime import datetime, timedelta

import pytest

from custom_components.nem_pd7day.scarcity_premium import (
    NEM, aware_time, build_premium, recent_samples, signal_mean,
)


def now(hour=10, minute=5):
    return datetime(2026, 9, 23, hour, minute, tzinfo=NEM)


def observations(price=0.041, at=None, count=6):
    """``count`` five-minute settlement ends up to and including ``at``."""
    end = at or now()
    return {
        (end - timedelta(minutes=5 * i)).isoformat(): price
        for i in range(count)
    }


def base(price=0.010):
    return [
        {"time": (now(10, 0) + timedelta(minutes=i * 30)).isoformat(), "value": price}
        for i in range(8)
    ]


def test_haeo_contract_units_spacing_utc_and_endpoint():
    result = build_premium(now(), observations(), base(), base_fresh=True)
    assert result.status == "active"
    assert result.count == 6
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


def test_extreme_recent_price_cannot_raise_floor_above_cap():
    result = build_premium(now(), observations(10), base(), base_fresh=True)
    assert result.target_floor == .065
    assert result.value == .055


@pytest.mark.parametrize("price", [0, -.020, .030])
def test_threshold_is_strict_and_accepts_zero_negative(price):
    result = build_premium(now(), observations(price), [], base_fresh=False)
    assert result.status == "signal_below_threshold"
    assert all(p["value"] == 0 for p in result.forecast)


def test_signal_is_the_mean_of_the_last_thirty_minutes_only():
    obs = observations(0.040)
    # An hour-old spike and a 30-minute-old price are outside the window;
    # 09:35 is exactly 30 minutes before 10:05, so it is excluded too.
    obs[now(9, 5).isoformat()] = 10.0
    obs[now(9, 35).isoformat()] = 10.0
    kept = recent_samples(obs, now())
    assert len(kept) == 6
    assert signal_mean(kept, now()) == 0.04


def test_rolling_mean_follows_the_market_through_the_window():
    obs = {**observations(0.020, at=now(10, 30)), **observations(0.050, at=now(11, 0))}
    early = build_premium(now(10, 30), obs, base(), base_fresh=True)
    late = build_premium(now(11, 0), obs, base(), base_fresh=True)
    assert early.status == "signal_below_threshold"
    assert late.status == "active" and late.signal_mean == 0.05


def test_partial_window_averages_what_is_present():
    obs = observations(0.041, count=2)
    result = build_premium(now(), obs, base(), base_fresh=True)
    assert result.count == 2
    assert result.signal_mean == 0.041
    assert result.value == 0.031


def test_no_recent_price_is_unavailable_in_the_window():
    result = build_premium(now(), {}, base(), base_fresh=True)
    assert result.status == "no_recent_dispatch"
    assert result.value is None and result.forecast == []


def test_newest_price_older_than_ten_minutes_is_not_used():
    obs = observations(0.041, at=now(9, 50), count=2)  # 09:45 and 09:50
    stale = build_premium(now(10, 1), obs, base(), base_fresh=True)
    assert stale.count == 2
    assert stale.status == "no_recent_dispatch" and stale.value is None
    fresh = build_premium(now(10, 0), obs, base(), base_fresh=True)
    assert fresh.status == "active"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, None, "unknown"])
def test_invalid_observations_are_dropped_not_zero(value):
    obs = observations()
    obs[next(iter(obs))] = value
    result = build_premium(now(), obs, base(), base_fresh=True)
    assert result.count == 5
    assert result.signal_mean == 0.041


def test_interval_end_semantics_and_duplicates():
    obs = observations()
    obs[now(10, 10).isoformat()] = 999   # future
    obs[now(10, 1).isoformat()] = 999    # off the five-minute grid
    # Same instant in another timezone must not create a seventh observation.
    obs["2026-09-23T00:05:00+00:00"] = .041
    result = recent_samples(obs, now())
    assert len(result) == 6
    assert all(value == .041 for value in result.values())
    assert all(key.endswith("+00:00") for key in result)


def test_restart_restore_and_next_day():
    obs = recent_samples(observations(), now())
    assert build_premium(now(), obs, base(), base_fresh=True).value == .031
    tomorrow = now() + timedelta(days=1)
    assert build_premium(tomorrow, obs, base(), base_fresh=True).status == "no_recent_dispatch"


def test_outside_the_window_is_zero_whatever_the_signal():
    for t, status in ((now(9, 55), "before_window"), (now(14, 0), "outside_window")):
        result = build_premium(t, observations(0.041, at=t), [], base_fresh=False)
        assert result.status == status
        assert result.value == 0
        assert all(p["value"] == 0 for p in result.forecast)


def test_fourteen_hundred_resets_even_without_inputs():
    result = build_premium(now(14, 0), {}, [], base_fresh=False)
    assert result.status == "outside_window"
    assert result.value == 0


def test_stale_base_unavailable_only_when_needed():
    result = build_premium(now(), observations(), base(), base_fresh=False)
    assert result.status == "base_forecast_stale"
    assert result.forecast == []


def test_missing_and_invalid_base_rows():
    for rows in [base()[:-1], [{"time": now(10, 0).isoformat(), "value": float("nan")}]]:
        assert build_premium(now(), observations(), rows, base_fresh=True).status == "base_forecast_gap"
    assert build_premium(now(), observations(), base() * 2, base_fresh=True).status == "duplicate_base_interval"


def test_half_hour_bucket_contains_current_time():
    result = build_premium(now(11, 47), observations(at=now(11, 45)), base(), base_fresh=True)
    assert result.forecast[0]["time"] == "2026-09-23T01:30:00+00:00"
    assert result.forecast[5]["value"] == 0


def test_no_naive_timestamps():
    with pytest.raises(ValueError):
        build_premium(datetime(2026, 9, 23, 10), {}, [], base_fresh=True)
