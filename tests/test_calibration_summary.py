"""
calibration_summary: the calibration sensor's diagnostic attributes, with
the current time passed in (spec 004).

Run with:  python -m pytest tests/test_calibration_summary.py -v
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

from custom_components.nem_pd7day.calibration_engine import all_bucket_keys
from custom_components.nem_pd7day.calibration_summary import (
    effective_window_days,
    oldest_observation,
    summary_attributes,
)
from ha_free import assert_imports_without_home_assistant
from support import NEM_TZ

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=NEM_TZ)


def test_imports_without_home_assistant():
    assert_imports_without_home_assistant("calibration_summary")


def test_oldest_observation_is_the_first_row_with_an_interval_time():
    rows = [{"dummy": 1}, {"interval_time": ""}, {"interval_time": "2026-09-01T10:00:00+10:00"},
            {"interval_time": "2026-08-01T10:00:00+10:00"}]
    assert oldest_observation(rows) == "2026-09-01T10:00:00+10:00"
    assert oldest_observation([]) is None


def test_effective_window_days():
    assert effective_window_days(None, NOW) is None
    assert effective_window_days("garbage", NOW) is None
    assert effective_window_days("2026-09-01T00:00:00+10:00", NOW) == 19.5
    # A naive time is read as NEM time.
    assert effective_window_days("2026-09-01T00:00:00", NOW) == 19.5


def test_no_calibration():
    assert summary_attributes(None, observation_count=7, oldest="x", window_days=1.0,
                              active_buckets=3) == {
        "status": "no_calibration", "observation_count": 7, "active_buckets": 0,
    }


def test_active_attributes_in_published_order():
    cal = NS(fitted_at="F", observations_in_window=5, summary=lambda: {"s": 1})
    attrs = summary_attributes(cal, observation_count=7, oldest="O", window_days=2.5,
                               active_buckets=3)
    assert attrs == {
        "status": "active", "fitted_at": "F", "observation_count": 7,
        "observation_window_days": 90, "oldest_observation": "O",
        "effective_window_days": 2.5, "observations_in_window": 5, "active_buckets": 3,
        "total_buckets": len(all_bucket_keys()), "summary": {"s": 1},
    }
    assert list(attrs) == [
        "status", "fitted_at", "observation_count", "observation_window_days",
        "oldest_observation", "effective_window_days", "observations_in_window",
        "active_buckets", "total_buckets", "summary",
    ]
