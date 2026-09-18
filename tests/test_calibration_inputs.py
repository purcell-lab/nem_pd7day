"""
calibration_inputs: the serving-side STPASA feature lookup that feeds stage 2.

``stpasa_features_for_interval`` matches a PD7DAY interval to a STPASA row the
same way the stage-2 fit joins them (exactly, tolerating one interval of
END/START convention slip, otherwise None), and the band it serves inside
opens at a floor resolved per forecast run from the run's STPASA coverage.
sensor.py re-exports these under underscore aliases; the code under test is
this module.

Run with:  python -m pytest tests/test_calibration_inputs.py -v
"""
from __future__ import annotations

import math
from datetime import datetime

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()  # stpasa_client imports aiohttp
_const, _nem_time, _engine, _stpasa_client, _inputs = load_chain(
    "const", "nem_time", "calibration_engine", "stpasa_client", "calibration_inputs"
)

from custom_components.nem_pd7day.calibration_inputs import (  # noqa: E402
    STPASA_BAND_EDGE_SLACK_H,
    STPASA_COVERAGE_MARGIN_H,
    STPASA_MAX_HORIZON_H,
    STPASA_MIN_HORIZON_H,
    horizon_hours,
    stpasa_coverage_start,
    stpasa_effective_min_horizon_h,
    stpasa_features_for_interval,
)
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult  # noqa: E402

nem_time = _nem_time

# The live 16:05 run of issues #67 and #68.
RUN_AT = "2026-09-02T16:05:00+10:00"


def _interval(end_iso: str, solar: float = 3510.0) -> StpasaInterval:
    """A complete STPASA interval, keyed on END per AEMO convention."""
    return StpasaInterval(
        interval_datetime=end_iso,
        run_datetime=RUN_AT,
        demand10=7400.0,
        demand50=7000.0,
        demand90=6600.0,
        surpluscapacity=4941.0,
        ss_solar_uigf=solar,
        ss_wind_uigf=900.0,
    )


class _FakeCoordinator:
    """Stand-in exposing ``stpasa_index()`` and ``data``.

    The index is built exactly as PD7DayCoordinator.stpasa_index builds it,
    so the keys under test are real interval-START strings.
    """

    def __init__(self, intervals, run_at=RUN_AT):
        self._result = StpasaResult(
            region="QLD1",
            run_datetime=run_at,
            intervals=list(intervals),
            fetched_at="2026-09-02T06:05:00+00:00",
        )
        self.data = None
        self._map = {}
        self._sorted = []
        for si in intervals:
            start_iso = nem_time.interval_start(si.interval_datetime)
            self._map[start_iso] = si
            self._sorted.append((nem_time.parse_iso(start_iso).timestamp(), si))
        self._sorted.sort(key=lambda t: t[0])

    def stpasa_index(self):
        return self._result, self._map, self._sorted


class _TripwireMap(dict):
    """An index map that refuses to be queried."""

    def get(self, *args, **kwargs):  # noqa: D102
        raise AssertionError("index consulted for an interval below coverage")


def _epoch(iso: str) -> float:
    return nem_time.parse_iso(iso).timestamp()


def _iso(epoch: float) -> str:
    """NEM-local ISO-8601 with an explicit +10:00 offset, from an epoch."""
    return nem_time.to_nem_iso(datetime.fromtimestamp(epoch, tz=NEM_TZ))


# ── The bounded nearest-match ─────────────────────────────────────────────────
# Issue #67: the fallback used to return the closest STPASA interval at any
# time distance. AEMO scopes Short Term PASA to six trading days from the end
# of the trading day covered by the most recent pre-dispatch schedule, so a
# late-afternoon run starts around h39 while the OLS band opens at h22.
# Intervals in that gap were scored against features borrowed from up to 17 h
# away, typically a pre-dawn interval carrying 0 MW of solar in place of
# several thousand, which produced 642 $/MWh in a solar trough whose raw
# forecast was negative. The stage-2 fit joins on an exact
# interval_time|run_at key, so those combinations never appear in training.

@pytest.mark.parametrize("run_at_iso", [None, RUN_AT])
def test_exact_start_match_returns_that_intervals_features(run_at_iso):
    """The common path, with and without run_at (callers that cannot supply
    it keep the static h22 gate and must behave exactly as before)."""
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00", 3510.0)])
    probe = "2026-09-04T13:00:00+10:00"
    feats = stpasa_features_for_interval(coord, probe, horizon_hours(RUN_AT, probe), run_at_iso=run_at_iso)
    assert feats is not None
    assert feats.log_solar == pytest.approx(math.log1p(3510.0))
    assert feats.stpasa_run_at == RUN_AT


@pytest.mark.parametrize("probe, horizon, matched", [
    ("2026-09-04T13:30:00+10:00", 45.5, True),   # one half-hour of slip: convention, not a gap
    ("2026-09-04T14:00:00+10:00", 46.0, False),  # one hour away: a miss, not a substitution
])
def test_nearest_match_is_bounded_to_one_interval(probe, horizon, matched):
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00", 3510.0)])  # START 13:00
    feats = stpasa_features_for_interval(coord, probe, horizon)
    if matched:
        assert feats is not None
        assert feats.log_solar == pytest.approx(math.log1p(3510.0))
    else:
        assert feats is None


def test_uncovered_day_is_not_scored_against_pre_dawn_solar():
    """Regression for the 2026-09-03 solar trough: the 16:05 run covered
    2026-09-04 onwards, the h22 floor put the 2026-09-03 afternoon in scope,
    and the unbounded fallback matched it to 04:00 the next day at 0 MW of
    solar. Nearest indexed START is 12.5 h to 15 h away."""
    coord = _FakeCoordinator([
        _interval("2026-09-04T04:30:00+10:00", 0.0),
        _interval("2026-09-04T05:00:00+10:00", 0.0),
        _interval("2026-09-04T13:30:00+10:00", 3510.0),
    ])
    for probe, horizon in (
        ("2026-09-03T13:00:00+10:00", 22.0),
        ("2026-09-03T14:00:00+10:00", 22.5),
        ("2026-09-03T15:30:00+10:00", 23.5),
    ):
        assert stpasa_features_for_interval(coord, probe, horizon) is None, probe


def test_horizon_outside_the_ols_band_short_circuits():
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00", 3510.0)])
    assert stpasa_features_for_interval(coord, "2026-09-04T13:00:00+10:00", 6.0) is None
    assert stpasa_features_for_interval(coord, "2026-09-04T13:00:00+10:00", 130.0) is None


def test_empty_index_returns_none():
    """No STPASA run means no features, not a substituted zero."""
    assert stpasa_features_for_interval(_FakeCoordinator([]), "2026-09-04T13:00:00+10:00", 45.0) is None


# ── The band floor is resolved from run coverage ──────────────────────────────
# Issue #68: the floor was a flat 22.0, but coverage begins at a trading day
# boundary and the horizon at which it begins moves with the run time. The
# live 16:05 run first reached h39, so the band was open for 17 h over
# intervals no STPASA row could describe; a run nearer the boundary left about
# 2 h. The resolved floor is the earliest covered interval START less one
# interval of slip tolerance, so the one-interval bridge of #67 survives, and
# never below the 22 h hard bound, which encodes the separate judgement that
# Amber and CSIRO cover the near term better.

def test_floor_tracks_the_16_05_run_from_the_issue():
    """First STPASA interval START 2026-09-04T04:00+10:00 is 35.917 h after
    the run; the floor is that less the half hour of slip tolerance."""
    coverage_h = horizon_hours(RUN_AT, "2026-09-04T04:00:00+10:00")
    assert math.isclose(coverage_h, 35.9166666, abs_tol=1e-4), coverage_h
    floor = stpasa_effective_min_horizon_h(RUN_AT, _epoch("2026-09-04T04:00:00+10:00"))
    assert math.isclose(floor, coverage_h - 0.5, abs_tol=1e-3), floor
    assert floor > STPASA_MIN_HORIZON_H
    # The h22 to h39 window of the issue is now outside the band.
    assert horizon_hours(RUN_AT, "2026-09-03T14:00:00+10:00") < floor


def test_floor_falls_back_to_the_constant_when_inputs_are_unknown():
    """Missing run_at or missing coverage must not widen the band."""
    assert stpasa_effective_min_horizon_h(None, _epoch(RUN_AT)) == STPASA_MIN_HORIZON_H
    assert stpasa_effective_min_horizon_h(RUN_AT, None) == STPASA_MIN_HORIZON_H
    assert stpasa_effective_min_horizon_h("not-a-timestamp", 1.0) == STPASA_MIN_HORIZON_H


def test_floor_moves_with_run_time_over_a_full_day_of_runs():
    """For a fixed coverage start the floor falls by one hour for every hour
    later the run is issued, until it clamps to the hard bound: the property
    a constant cannot have."""
    coverage = _epoch("2026-09-04T04:00:00+10:00")
    previous = None
    for hour in range(0, 52):
        run_epoch = _epoch("2026-09-02T00:00:00+10:00") + hour * 3600
        floor = stpasa_effective_min_horizon_h(_iso(run_epoch), coverage)
        expected = max(
            STPASA_MIN_HORIZON_H,
            (coverage - run_epoch) / 3600.0 - STPASA_COVERAGE_MARGIN_H - STPASA_BAND_EDGE_SLACK_H,
        )
        assert math.isclose(floor, expected, abs_tol=1e-6), (hour, floor, expected)
        if previous is not None:
            assert floor <= previous + 1e-9, (hour, floor, previous)
        previous = floor
    # A run one hour before coverage begins is clamped to the hard bound.
    assert previous == STPASA_MIN_HORIZON_H


def test_coverage_start_is_the_earliest_start_regardless_of_input_order():
    result = StpasaResult(
        region="QLD1",
        run_datetime=RUN_AT,
        intervals=[
            _interval("2026-09-04T13:30:00+10:00"),
            _interval("2026-09-04T04:30:00+10:00"),
            _interval("2026-09-05T02:00:00+10:00"),
        ],
        fetched_at="2026-09-02T06:05:00+00:00",
    )
    iso, epoch = stpasa_coverage_start(result)
    assert iso == "2026-09-04T04:00:00+10:00"
    assert epoch == _epoch("2026-09-04T04:00:00+10:00")


def test_coverage_start_is_none_not_zero_when_there_is_nothing_to_read():
    empty = StpasaResult(region="QLD1", run_datetime=RUN_AT, intervals=[], fetched_at=None)
    assert stpasa_coverage_start(empty) == (None, None)
    assert stpasa_coverage_start(None) == (None, None)
    unparseable = StpasaResult(
        region="QLD1", run_datetime=RUN_AT, intervals=[_interval("not-a-timestamp")], fetched_at=None,
    )
    assert stpasa_coverage_start(unparseable) == (None, None)


def test_below_coverage_the_band_is_shut_before_any_lookup():
    """h22 to h39 on the 16:05 run: after #67 the bounded nearest-match
    already declined these one interval at a time; now the band edge says so
    and neither the exact-key lookup nor the bisect fallback runs for them on
    every state write."""
    coord = _FakeCoordinator([
        _interval("2026-09-04T04:30:00+10:00", 0.0), _interval("2026-09-04T13:30:00+10:00"),
    ])
    coord._map = _TripwireMap(coord._map)
    for probe in (
        "2026-09-03T18:00:00+10:00",  # h25.9: inside the static h22 band
        "2026-09-03T23:30:00+10:00",
        "2026-09-04T02:00:00+10:00",
        "2026-09-04T03:00:00+10:00",
    ):
        horizon = horizon_hours(RUN_AT, probe)
        assert horizon > STPASA_MIN_HORIZON_H, probe
        assert stpasa_features_for_interval(coord, probe, horizon, run_at_iso=RUN_AT) is None, probe


def test_the_one_interval_bridge_from_issue_67_still_works():
    """Coverage starts at 04:00; the 03:30 interval is one slot below it, at
    h35.4 from the 16:05 run, and must still be matched to 04:00. The floor
    subtracts the match tolerance precisely so this case survives."""
    coord = _FakeCoordinator([_interval("2026-09-04T04:30:00+10:00")])
    probe = "2026-09-04T03:30:00+10:00"
    feats = stpasa_features_for_interval(coord, probe, horizon_hours(RUN_AT, probe), run_at_iso=RUN_AT)
    assert feats is not None
    assert feats.stpasa_run_at == RUN_AT


def test_sweep_no_interval_below_the_resolved_floor_gets_features():
    """For several run times, walk every half-hourly interval from h0 to h120:
    features are returned only at or above the resolved floor, which also
    holds for runs whose coverage starts nearer."""
    first_end_epoch = _epoch("2026-09-04T04:30:00+10:00")
    coord = _FakeCoordinator([_interval(_iso(first_end_epoch + i * 1800)) for i in range(288)])
    coverage_epoch = _epoch("2026-09-04T04:00:00+10:00")
    gated_below_floor = 0
    served_above_floor = 0
    for run_offset_h in (0, 4, 8, 12, 16, 20, 24, 28):
        run_epoch = _epoch("2026-09-02T00:00:00+10:00") + run_offset_h * 3600
        run_iso = _iso(run_epoch)
        floor = stpasa_effective_min_horizon_h(run_iso, coverage_epoch)
        for step in range(0, 241):
            horizon = step * 0.5
            feats = stpasa_features_for_interval(coord, _iso(run_epoch + horizon * 3600), horizon, run_at_iso=run_iso)
            if horizon < floor or horizon > STPASA_MAX_HORIZON_H:
                assert feats is None, (run_iso, horizon, floor)
                gated_below_floor += 1
            elif feats is not None:
                served_above_floor += 1
    assert gated_below_floor > 0
    assert served_above_floor > 0
