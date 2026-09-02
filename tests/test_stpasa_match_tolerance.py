"""
Bound on the STPASA nearest-match fallback in
``sensor._stpasa_features_for_interval``.

The fallback used to return the closest STPASA interval at any time distance.
STPASA does not reach the near horizon: AEMO scopes Short Term PASA to six
trading days from the end of the trading day covered by the most recent
pre-dispatch schedule, so a late-afternoon run starts around h39 while the OLS
band opens at h22. Intervals in that gap were scored against features borrowed
from up to 17h away, typically a pre-dawn interval carrying 0 MW of solar in
place of several thousand. The stage-2 fit joins on an exact
``interval_time|run_at`` key and skips intervals with no STPASA row, so those
substituted combinations never appear in training.

These tests pin the serving path to the same rule the fit already follows:
match exactly, tolerate one interval of convention slip, otherwise return None.
"""
import math

import pytest

from custom_components.nem_pd7day import nem_time
from custom_components.nem_pd7day.sensor import _stpasa_features_for_interval
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult

RUN_AT = "2026-09-02T16:05:00+10:00"


def _interval(end_iso: str, solar: float) -> StpasaInterval:
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
    """Minimal stand-in exposing only ``stpasa_index()``.

    The index is built exactly as ``PD7DayCoordinator.stpasa_index`` builds it,
    so the keys under test are real interval-START strings.
    """

    def __init__(self, intervals):
        self._result = StpasaResult(
            region="QLD1",
            run_datetime=RUN_AT,
            intervals=list(intervals),
            fetched_at="2026-09-02T06:05:00+00:00",
        )
        self._map = {}
        self._sorted = []
        for si in intervals:
            start_iso = nem_time.interval_start(si.interval_datetime)
            self._map[start_iso] = si
            self._sorted.append((nem_time.parse_iso(start_iso).timestamp(), si))
        self._sorted.sort(key=lambda t: t[0])

    def stpasa_index(self):
        return self._result, self._map, self._sorted


def test_exact_start_match_returns_that_intervals_features():
    """The common path is unchanged: an exact START hit is used as-is."""
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00", 3510.0)])
    feats = _stpasa_features_for_interval(coord, "2026-09-04T13:00:00+10:00", 45.0)
    assert feats is not None
    assert feats.log_solar == pytest.approx(math.log1p(3510.0))
    assert feats.stpasa_run_at == RUN_AT


def test_one_interval_of_slip_is_still_matched():
    """A single half-hour offset is a convention slip, not a coverage gap."""
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00", 3510.0)])
    # START of the indexed interval is 13:00; probe 30 minutes later.
    feats = _stpasa_features_for_interval(coord, "2026-09-04T13:30:00+10:00", 45.5)
    assert feats is not None
    assert feats.log_solar == pytest.approx(math.log1p(3510.0))


def test_match_beyond_tolerance_returns_none():
    """A gap wider than one interval is a miss, not a substitution."""
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00", 3510.0)])
    # Nearest indexed START is 13:00, one hour away, past the half-hour bound.
    assert _stpasa_features_for_interval(
        coord, "2026-09-04T14:00:00+10:00", 46.0
    ) is None


def test_uncovered_day_is_not_scored_against_pre_dawn_solar():
    """Regression for the 2026-09-03 solar trough.

    The 16:05 run covered 2026-09-04 onwards. The h22 band floor put the
    2026-09-03 afternoon in scope, and the unbounded fallback matched it to
    the first available interval, 04:00 the following day, carrying 0 MW of
    solar. Every one of those intervals must now decline to match.
    """
    coord = _FakeCoordinator(
        [
            _interval("2026-09-04T04:30:00+10:00", 0.0),
            _interval("2026-09-04T05:00:00+10:00", 0.0),
            _interval("2026-09-04T13:30:00+10:00", 3510.0),
        ]
    )
    # 2026-09-03 13:00 to 15:30 sits at h21 to h23.5 from a 16:05 run; probe the
    # in-band ones. Nearest indexed START is 2026-09-04T04:00, 12.5h to 15h away.
    for probe, horizon in (
        ("2026-09-03T13:00:00+10:00", 22.0),
        ("2026-09-03T14:00:00+10:00", 22.5),
        ("2026-09-03T15:30:00+10:00", 23.5),
    ):
        assert _stpasa_features_for_interval(coord, probe, horizon) is None, probe


def test_horizon_outside_the_ols_band_short_circuits():
    """Unchanged guard: no STPASA lookup below h22 or above h120."""
    coord = _FakeCoordinator([_interval("2026-09-04T13:30:00+10:00", 3510.0)])
    assert _stpasa_features_for_interval(coord, "2026-09-04T13:00:00+10:00", 6.0) is None
    assert _stpasa_features_for_interval(coord, "2026-09-04T13:00:00+10:00", 130.0) is None


def test_empty_index_returns_none():
    """No STPASA run means no features, not a substituted zero."""
    coord = _FakeCoordinator([])
    assert _stpasa_features_for_interval(coord, "2026-09-04T13:00:00+10:00", 45.0) is None
