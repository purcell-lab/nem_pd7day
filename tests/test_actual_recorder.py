"""
actual_recorder: pairing a settled price with the forecasts that covered it,
the per-pair running average, and pruning (spec 004). The observation field
order is part of the stored observation segments.

Run with:  python -m pytest tests/test_actual_recorder.py -v
"""
from __future__ import annotations

import logging
import math

import pytest

from custom_components.nem_pd7day.actual_recorder import rebuild_accumulator, record_actual
from ha_free import assert_imports_without_home_assistant

INTERVAL = "2026-09-20T18:00:00+10:00"
RUN = "2026-09-19T13:00:00+10:00"   # 29 h before INTERVAL


class _Sink:
    """ObservationSink over a plain list, recording touch and prune calls."""

    def __init__(self, rows=None, drop=()) -> None:
        self.observations: list[dict] = list(rows or [])
        self.touched: list[dict] = []
        self.pruned: list[int] = []
        self._drop = list(drop)

    def append(self, obs: dict) -> None:
        self.observations.append(obs)

    def touch(self, obs: dict) -> None:
        self.touched.append(obs)

    def prune(self, max_total: int):
        self.pruned.append(max_total)
        dropped, self._drop = self._drop, []
        return dropped


def _entry(run_at=RUN, region="QLD1", **extra):
    entry = {
        "run_at": run_at, "forecast_price": 0.112, "gas_tj": 131.4, "qni_mwflow": -512.25,
        "qni_violation": 0.0, "is_intervention": False, "region": region,
    }
    entry.update(extra)
    return entry


STPASA = dict(stpasa_run_at="S", stpasa_demand10=5600.0, stpasa_demand50=6100.0,
              stpasa_demand90=6650.0, stpasa_surplus=1350.0, stpasa_solar=0.0, stpasa_wind=420.0)


def _record(history, accum, sink, rrp=0.11, region=None, source="amber", interval=INTERVAL):
    return record_actual(history, accum, sink, interval_time=interval, actual_rrp=rrp,
                         calibration_region=region, source=source)


def test_imports_without_home_assistant():
    assert_imports_without_home_assistant("actual_recorder")


def test_first_reading_builds_the_observation_in_stored_field_order():
    sink, accum = _Sink(), {}
    assert _record({INTERVAL: [_entry(**STPASA)]}, accum, sink) == 1
    (obs,) = sink.observations
    assert list(obs) == [
        "interval_time", "horizon_hours", "pd7day_forecast", "actual_rrp", "forecast_run_at",
        "hour_of_day", "day_of_week", "month", "gas_forecast_tj", "qni_mwflow",
        "qni_violation_degree", "is_intervention", "actual_source", "stpasa_log_surplus",
        "stpasa_log_solar", "stpasa_log_demand", "stpasa_poe_spread_n", "stpasa_run_at",
    ]
    assert obs["horizon_hours"] == 29.0 and obs["hour_of_day"] == 18 and obs["day_of_week"] == 6
    assert obs["stpasa_log_demand"] == math.log(6100.0)
    assert accum == {(INTERVAL, RUN): {"sum": 0.11, "count": 1, "obs": obs}}
    # Pruned to the module's cap, read from the function's own globals.
    assert sink.pruned == [record_actual.__globals__["MAX_TOTAL_OBS"]] == [100_000]


@pytest.mark.parametrize("missing", ["stpasa_demand10", "stpasa_surplus", "stpasa_solar"])
def test_stpasa_features_only_when_every_input_is_present(missing):
    sink = _Sink()
    _record({INTERVAL: [_entry(**{**STPASA, missing: None})]}, {}, sink)
    assert not [k for k in sink.observations[0] if k.startswith("stpasa_")]


def test_stpasa_features_skipped_below_the_demand_floor():
    sink = _Sink()
    _record({INTERVAL: [_entry(**{**STPASA, "stpasa_demand50": -5.0})]}, {}, sink)
    assert "stpasa_log_demand" not in sink.observations[0]


def test_a_repeat_reading_updates_the_running_average_in_place(caplog):
    sink, accum = _Sink(), {}
    history = {INTERVAL: [_entry()]}
    _record(history, accum, sink, rrp=0.11)
    with caplog.at_level(logging.DEBUG, logger="custom_components.nem_pd7day"):
        assert _record(history, accum, sink, rrp=0.14) == 1
    assert len(sink.observations) == 1
    assert sink.observations[0]["actual_rrp"] == round((0.11 + 0.14) / 2, 6)
    assert sink.touched == [sink.observations[0]]
    assert [r.getMessage() for r in caplog.records] == [
        f"Updated actual_rrp for interval {INTERVAL} run_at {RUN}: avg=0.1250 over 2 readings"
    ]


def test_region_run_time_and_horizon_filters():
    history = {INTERVAL: [
        _entry(region="NSW1"),
        _entry(run_at="not a time"),
        _entry(run_at="2026-09-20T18:30:00+10:00"),          # negative horizon
        _entry(run_at="2026-09-13T18:00:00+10:00"),          # exactly 168 h: kept
        _entry(run_at="2026-09-13T17:30:00+10:00"),          # 168.5 h: skipped
        _entry(run_at=INTERVAL),                             # 0 h: kept
    ]}
    sink = _Sink()
    assert _record(history, {}, sink, region="QLD1") == 2
    assert [o["horizon_hours"] for o in sink.observations] == [168.0, 0.0]
    assert _record(history, {}, _Sink(), region="SA1") == 0


def test_no_history_logs_and_returns_zero_without_pruning(caplog):
    sink = _Sink()
    with caplog.at_level(logging.DEBUG, logger="custom_components.nem_pd7day"):
        assert _record({}, {}, sink) == 0
    assert sink.pruned == []
    assert [r.getMessage() for r in caplog.records] == [
        f"No forecast history for interval {INTERVAL} — skipping"
    ]


def test_pruned_rows_leave_the_accumulator():
    old = {"interval_time": "2026-09-01T10:00:00+10:00", "forecast_run_at": "2026-08-31T13:00:00+10:00"}
    accum = rebuild_accumulator([dict(old, actual_rrp=0.1)])
    sink = _Sink(drop=[old, {"interval_time": None}])
    _record({INTERVAL: [_entry()]}, accum, sink)
    assert list(accum) == [(INTERVAL, RUN)]


def test_rebuild_accumulator_holds_the_row_itself_and_skips_incomplete_rows():
    row = {"interval_time": INTERVAL, "forecast_run_at": RUN, "actual_rrp": 0.2}
    accum = rebuild_accumulator([row, {"interval_time": INTERVAL}, {"dummy": 1}])
    assert list(accum) == [(INTERVAL, RUN)]
    assert accum[(INTERVAL, RUN)] == {"sum": 0.2, "count": 1, "obs": row}
    assert accum[(INTERVAL, RUN)]["obs"] is row


def test_naive_times_are_read_as_nem_time():
    naive_interval = "2026-09-20T18:00:00"
    sink = _Sink()
    history = {naive_interval: [_entry(run_at="2026-09-19T13:00:00")]}
    assert _record(history, {}, sink, interval=naive_interval) == 1
    assert sink.observations[0]["horizon_hours"] == 29.0
    assert sink.observations[0]["hour_of_day"] == 18
