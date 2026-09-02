"""
The stage-2 STPASA band floor is derived from run coverage, not hardcoded.

`_STPASA_MIN_HORIZON_H` was a flat 22.0, but AEMO scopes Short Term PASA to six
trading days from the end of the trading day covered by the most recent
pre-dispatch schedule, so coverage begins at a trading day boundary and the
horizon at which it begins moves with the forecast run time. A 16:05 run first
reached h39, so the band was open for 17h over intervals no STPASA row could
ever describe; a run nearer the boundary left about 2h. Issue #68.

These tests pin the resolved floor to the earliest covered interval START, less
one interval of END/START slip tolerance so the bounded nearest-match kept by
issue #67 still bridges, and never below the 22h hard bound that encodes the
separate judgement that Amber and CSIRO cover the near term better.
"""
import math
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_components.nem_pd7day import nem_time
from custom_components.nem_pd7day.const import NEM_TZ
from custom_components.nem_pd7day.sensor import (
    _STPASA_BAND_EDGE_SLACK_H,
    _STPASA_COVERAGE_MARGIN_H,
    _STPASA_MAX_HORIZON_H,
    _STPASA_MIN_HORIZON_H,
    _horizon_hours,
    _stpasa_coverage_start,
    _stpasa_effective_min_horizon_h,
    _stpasa_features_for_interval,
)
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult

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
    """Minimal stand-in exposing `stpasa_index()` and `data`.

    The index is built exactly as `PD7DayCoordinator.stpasa_index` builds it,
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


def _epoch(iso: str) -> float:
    return nem_time.parse_iso(iso).timestamp()


def _iso(epoch: float) -> str:
    """NEM-local ISO-8601 with an explicit +10:00 offset, from an epoch."""
    return nem_time.to_nem_iso(datetime.fromtimestamp(epoch, tz=NEM_TZ))


# ── the resolved floor itself ────────────────────────────────────────────────

def test_floor_tracks_the_16_05_run_from_the_issue():
    """The regression case: coverage starts at h39, so the band must not open
    at h22.

    Live 16:05 run, first STPASA interval START 2026-09-04T04:00+10:00. That
    is 35.917h after the run, and the resolved floor is that less the half hour
    of END/START slip tolerance.
    """
    coverage_h = _horizon_hours(RUN_AT, "2026-09-04T04:00:00+10:00")
    assert math.isclose(coverage_h, 35.9166666, abs_tol=1e-4), coverage_h
    floor = _stpasa_effective_min_horizon_h(
        RUN_AT, _epoch("2026-09-04T04:00:00+10:00")
    )
    assert math.isclose(floor, coverage_h - 0.5, abs_tol=1e-3), floor
    assert floor > _STPASA_MIN_HORIZON_H
    # The h22 to h39 window of the issue is now outside the band.
    assert _horizon_hours(RUN_AT, "2026-09-03T14:00:00+10:00") < floor


def test_floor_never_drops_below_the_hard_bound():
    """Coverage reaching into the near term must not open the band below h22.

    The 22h bound is a separate judgement about Amber and CSIRO, not a claim
    about STPASA coverage, so wide coverage must not erode it.
    """
    floor = _stpasa_effective_min_horizon_h(
        RUN_AT, _epoch("2026-09-02T17:00:00+10:00")
    )
    assert floor == _STPASA_MIN_HORIZON_H


def test_floor_falls_back_to_the_constant_when_inputs_are_unknown():
    """Missing run_at or missing coverage must not widen the band."""
    assert _stpasa_effective_min_horizon_h(None, _epoch(RUN_AT)) == _STPASA_MIN_HORIZON_H
    assert _stpasa_effective_min_horizon_h(RUN_AT, None) == _STPASA_MIN_HORIZON_H
    assert _stpasa_effective_min_horizon_h("not-a-timestamp", 1.0) == _STPASA_MIN_HORIZON_H


def test_floor_moves_with_run_time_over_a_full_day_of_runs():
    """Invariant sweep: for a fixed coverage start, the floor must fall by one
    hour for every hour later the run is issued, until it hits the hard bound.

    This is the property a constant cannot have, and it is what makes the
    uncovered window 17h wide on one run and about 2h wide on another.
    """
    coverage = _epoch("2026-09-04T04:00:00+10:00")
    previous = None
    for hour in range(0, 52):
        run_epoch = _epoch("2026-09-02T00:00:00+10:00") + hour * 3600
        floor = _stpasa_effective_min_horizon_h(_iso(run_epoch), coverage)
        expected = max(
            _STPASA_MIN_HORIZON_H,
            (coverage - run_epoch) / 3600.0
            - _STPASA_COVERAGE_MARGIN_H
            - _STPASA_BAND_EDGE_SLACK_H,
        )
        assert math.isclose(floor, expected, abs_tol=1e-6), (hour, floor, expected)
        if previous is not None:
            assert floor <= previous + 1e-9, (hour, floor, previous)
        previous = floor
    # A run one hour before coverage begins must be clamped to the hard bound.
    assert previous == _STPASA_MIN_HORIZON_H


# ── coverage start extraction ────────────────────────────────────────────────

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
    iso, epoch = _stpasa_coverage_start(result)
    assert iso == "2026-09-04T04:00:00+10:00"
    assert epoch == _epoch("2026-09-04T04:00:00+10:00")


def test_coverage_start_is_none_not_zero_when_there_is_nothing_to_read():
    empty = StpasaResult(
        region="QLD1", run_datetime=RUN_AT, intervals=[], fetched_at=None
    )
    assert _stpasa_coverage_start(empty) == (None, None)
    assert _stpasa_coverage_start(None) == (None, None)
    unparseable = StpasaResult(
        region="QLD1",
        run_datetime=RUN_AT,
        intervals=[_interval("not-a-timestamp")],
        fetched_at=None,
    )
    assert _stpasa_coverage_start(unparseable) == (None, None)


# ── the serving gate ─────────────────────────────────────────────────────────

def test_in_band_interval_below_coverage_is_gated_before_any_lookup():
    """h22 to h39 on the 16:05 run: the band must be shut, not merely unmatched.

    Post issue #67 the bounded nearest-match already declined these, so the
    served value does not change. What changes is that the band edge now says
    so, instead of the miss being discovered per interval.
    """
    coord = _FakeCoordinator(
        [_interval("2026-09-04T04:30:00+10:00", 0.0), _interval("2026-09-04T13:30:00+10:00")]
    )
    for probe in (
        "2026-09-03T18:00:00+10:00",
        "2026-09-03T23:30:00+10:00",
        "2026-09-04T02:00:00+10:00",
        "2026-09-04T03:00:00+10:00",
    ):
        horizon = _horizon_hours(RUN_AT, probe)
        assert horizon > _STPASA_MIN_HORIZON_H, probe
        assert (
            _stpasa_features_for_interval(coord, probe, horizon, run_at_iso=RUN_AT)
            is None
        ), probe


class _TripwireMap(dict):
    """An index map that refuses to be queried."""

    def get(self, *args, **kwargs):  # noqa: D102
        raise AssertionError("index consulted for an interval below coverage")


def test_below_coverage_the_index_is_not_consulted_at_all():
    """The floor short-circuits ahead of the lookup and its fallback.

    This is the part of the fix that is observable in the serving path. The
    calibrated value for these intervals was already correct after issue #67,
    because the bounded nearest-match declined them one interval at a time.
    What changes is that the band edge now knows they are out of scope, so the
    exact-key lookup and the bisect fallback are not run for every one of them
    on every state write.
    """
    coord = _FakeCoordinator([_interval("2026-09-04T04:30:00+10:00")])
    coord._map = _TripwireMap(coord._map)
    # h25.9 from the 16:05 run: inside the static h22 band, below coverage.
    probe = "2026-09-03T18:00:00+10:00"
    assert (
        _stpasa_features_for_interval(
            coord, probe, _horizon_hours(RUN_AT, probe), run_at_iso=RUN_AT
        )
        is None
    )


def test_the_one_interval_bridge_from_issue_67_still_works():
    """The interval immediately below coverage keeps its nearest-match.

    The floor subtracts the match tolerance precisely so this case survives.
    Coverage starts at 04:00; the 03:30 interval is one slot below it, sits at
    h35.4 from the 16:05 run, and must still be matched to 04:00.
    """
    coord = _FakeCoordinator([_interval("2026-09-04T04:30:00+10:00")])
    probe = "2026-09-04T03:30:00+10:00"
    feats = _stpasa_features_for_interval(
        coord, probe, _horizon_hours(RUN_AT, probe), run_at_iso=RUN_AT
    )
    assert feats is not None
    assert feats.stpasa_run_at == RUN_AT


def test_exact_match_inside_coverage_is_untouched():
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00")])
    probe = "2026-09-04T13:00:00+10:00"
    feats = _stpasa_features_for_interval(
        coord, probe, _horizon_hours(RUN_AT, probe), run_at_iso=RUN_AT
    )
    assert feats is not None
    assert math.isclose(feats.log_solar, math.log1p(3510.0))


def test_omitting_run_at_preserves_the_old_static_gate():
    """Callers that cannot supply run_at must behave exactly as before.

    The existing tolerance tests call this function with three positional
    arguments, and the fallback keeps them meaningful rather than silently
    changing what they assert.
    """
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00")])
    probe = "2026-09-04T13:00:00+10:00"
    assert _stpasa_features_for_interval(coord, probe, 6.0) is None
    assert (
        _stpasa_features_for_interval(coord, probe, _horizon_hours(RUN_AT, probe))
        is not None
    )


def test_sweep_no_interval_below_the_resolved_floor_gets_features():
    """Invariant sweep over the whole band for several run times.

    For each run time, walk every half-hourly interval from h0 to h120 and
    assert the hard invariant: features are returned only at or above the
    resolved floor. This is stronger than checking the h22 to h39 window of the
    issue, because it also holds for runs whose coverage starts nearer.
    """
    first_end_epoch = _epoch("2026-09-04T04:30:00+10:00")
    intervals = [_interval(_iso(first_end_epoch + i * 1800)) for i in range(288)]
    coord = _FakeCoordinator(intervals)
    coverage_epoch = _epoch("2026-09-04T04:00:00+10:00")

    gated_below_floor = 0
    served_above_floor = 0
    for run_offset_h in (0, 4, 8, 12, 16, 20, 24, 28):
        run_epoch = _epoch("2026-09-02T00:00:00+10:00") + run_offset_h * 3600
        run_iso = _iso(run_epoch)
        floor = _stpasa_effective_min_horizon_h(run_iso, coverage_epoch)
        for step in range(0, 241):
            horizon = step * 0.5
            interval_iso = _iso(run_epoch + horizon * 3600)
            feats = _stpasa_features_for_interval(
                coord, interval_iso, horizon, run_at_iso=run_iso
            )
            if horizon < floor or horizon > _STPASA_MAX_HORIZON_H:
                assert feats is None, (run_iso, horizon, floor)
                gated_below_floor += 1
            elif feats is not None:
                served_above_floor += 1

    # Guard against a vacuous sweep: both arms must have been exercised.
    assert gated_below_floor > 0
    assert served_above_floor > 0


def _run_standalone():
    tests = [
        (test_floor_tracks_the_16_05_run_from_the_issue,
         "resolved floor tracks the h39 coverage of the 16:05 run"),
        (test_floor_never_drops_below_the_hard_bound,
         "resolved floor never drops below the 22h hard bound"),
        (test_floor_falls_back_to_the_constant_when_inputs_are_unknown,
         "unknown run_at or coverage falls back to the constant"),
        (test_floor_moves_with_run_time_over_a_full_day_of_runs,
         "floor falls one hour per hour of run time, then clamps"),
        (test_coverage_start_is_the_earliest_start_regardless_of_input_order,
         "coverage start is the earliest interval START"),
        (test_coverage_start_is_none_not_zero_when_there_is_nothing_to_read,
         "missing coverage reports None, not zero"),
        (test_in_band_interval_below_coverage_is_gated_before_any_lookup,
         "h22 to h39 window is gated by the resolved floor"),
        (test_below_coverage_the_index_is_not_consulted_at_all,
         "below coverage the STPASA index is never queried"),
        (test_the_one_interval_bridge_from_issue_67_still_works,
         "one-interval END/START bridge from issue 67 survives"),
        (test_exact_match_inside_coverage_is_untouched,
         "exact match inside coverage is unchanged"),
        (test_omitting_run_at_preserves_the_old_static_gate,
         "omitting run_at preserves the previous static gate"),
        (test_sweep_no_interval_below_the_resolved_floor_gets_features,
         "sweep: nothing below the resolved floor gets features"),
    ]
    for fn, desc in tests:
        fn()
        print(f"  PASS: {desc}")


if __name__ == "__main__":
    _run_standalone()
