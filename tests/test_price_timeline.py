"""price_timeline.py (#338): one timeline, each source starting where the one before ends.

Covers the merge order, the half-hour split at the P5MIN end, staleness
falling through to the next source, the resolution of each point, and the
helpers, against real P5MIN and pre-dispatch runs where they matter.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_const, _lp, _cc, _pdc, _tl = load_chain("const", "local_price", "constraint_client", "predispatch_client", "price_timeline")

_DIR = Path(__file__).parent / "fixtures" / "local_price"
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()


def _at(hour, minute=0, day=30):
    return datetime(2026, 10, day, hour, minute, tzinfo=NEM_TZ)


def _p5():
    return _lp.parse_p5min_local_prices(P5, "p5.zip", _at(21, 25) + timedelta(seconds=45))


def _pd(run_at, first_end, periods=6, region="NSW1", base=100.0):
    """A pre-dispatch run with ``periods`` half hours from ``first_end``."""
    prices = {region: {first_end + timedelta(minutes=30 * i): base + i for i in range(periods)}}
    return SimpleNamespace(run_at=run_at, source="pd.zip", prices=prices)


def _pd7day(first_start, count, base=0.2):
    return [
        {"time": (first_start + timedelta(minutes=30 * i)).isoformat(),
         "nemtime": (first_start + timedelta(minutes=30 * (i + 1))).isoformat(),
         "value": round(base + i / 100, 6), "p10": 0.1, "p50": round(base + i / 100, 6), "p90": 0.5,
         "calibrated_source": "isotonic"}
        for i in range(count)
    ]


def _spans_contiguous(timeline):
    for (_s0, e0), (s1, _e1) in zip(timeline.spans, timeline.spans[1:]):
        assert e0 == s1, (e0, s1)


# ── The merge ────────────────────────────────────────────────────────────────

def test_each_source_starts_where_the_one_before_ends():
    now = _at(21, 27)
    run = _p5()  # 21:25 run, intervals ending 21:25 to 22:20
    timeline = _tl.build_timeline(
        now, "NSW1",
        dispatch=_tl.DispatchPoint(_at(21, 30), 0.0951, "d.zip"),
        p5=run,
        pd=_pd(_at(21, 2), _at(21, 30), periods=8),     # periods ending 21:30 to 01:00
        pd7day=_pd7day(_at(21, 0), 48),                  # 21:00 for a day
    )
    sources = [p["source"] for p in timeline.points]
    assert sources[0] == "dispatch"
    first_of = {s: sources.index(s) for s in _tl.SOURCES}
    assert first_of["dispatch"] < first_of["p5min"] < first_of["predispatch"] < first_of["pd7day"]
    assert sources == sorted(sources, key=_tl.SOURCES.index), "no source comes back after the next"
    _spans_contiguous(timeline)
    # Dispatch: 21:25-21:30, the RRP being paid.
    assert timeline.points[0]["time"] == "2026-10-30T21:25:00+10:00" and timeline.points[0]["value"] == 0.0951
    # P5MIN: 21:30 to 22:20, raw RRP, 5-minute, no band.
    p5_points = [p for p in timeline.points if p["source"] == "p5min"]
    assert p5_points[0]["time"] == "2026-10-30T21:30:00+10:00" and p5_points[-1]["nemtime"] == "2026-10-30T22:20:00+10:00"
    assert {p["resolution_minutes"] for p in p5_points} == {5}
    assert all(p["p10"] is None and p["calibrated_source"] == "raw" for p in p5_points)
    assert p5_points[1]["value"] == round(run.rrp["NSW1"][_at(21, 40)] / 1000, 6)
    # Pre-dispatch: the 22:00-22:30 half hour split at 22:20, then whole half hours.
    pd_points = [p for p in timeline.points if p["source"] == "predispatch"]
    assert pd_points[0]["time"] == "2026-10-30T22:20:00+10:00" and pd_points[0]["resolution_minutes"] == 10
    assert {p["resolution_minutes"] for p in pd_points[1:]} == {30}
    assert all(p["calibrated_source"] == "passthrough" for p in pd_points)
    # PD7DAY from where pre-dispatch ends, calibrated with its band.
    pd7 = [p for p in timeline.points if p["source"] == "pd7day"]
    assert pd7[0]["time"] == "2026-10-31T01:00:00+10:00" and pd7[0]["p90"] == 0.5
    assert pd7[0]["calibrated_source"] == "isotonic"
    assert timeline.sources["p5min"]["status"] == "used" and timeline.sources["p5min"]["age_seconds"] == 120


def test_without_dispatch_p5min_covers_the_current_interval():
    timeline = _tl.build_timeline(_at(21, 27), "NSW1", p5=_p5())
    assert timeline.points[0]["source"] == "p5min" and timeline.points[0]["time"] == "2026-10-30T21:25:00+10:00"
    assert timeline.sources["dispatch"]["status"] == "no_data"


def test_a_dispatch_interval_that_has_ended_is_not_the_current_one():
    timeline = _tl.build_timeline(_at(21, 31), "NSW1", dispatch=_tl.DispatchPoint(_at(21, 30), 0.09), p5=_p5())
    assert timeline.sources["dispatch"]["status"] == "stale"
    assert timeline.points[0]["source"] == "p5min" and timeline.points[0]["time"] == "2026-10-30T21:30:00+10:00"


# ── Staleness falls through ──────────────────────────────────────────────────

def test_a_stale_p5min_run_falls_through_to_predispatch():
    now = _at(21, 41)  # the 21:25 run is 16 minutes old
    timeline = _tl.build_timeline(now, "NSW1", p5=_p5(), pd=_pd(_at(21, 32), _at(22, 0), periods=4))
    assert timeline.sources["p5min"]["status"] == "stale"
    assert timeline.points[0]["source"] == "predispatch"
    assert timeline.points[0]["time"] == "2026-10-30T21:30:00+10:00", "the current half hour, whole"


def test_a_stale_predispatch_run_falls_through_to_pd7day():
    now = _at(21, 27)
    timeline = _tl.build_timeline(now, "NSW1", p5=_p5(), pd=_pd(_at(20, 0), _at(20, 30), periods=10),
                                  pd7day=_pd7day(_at(21, 0), 6))
    assert timeline.sources["predispatch"]["status"] == "stale"
    after = [p for p in timeline.points if p["source"] != "p5min"]
    assert after[0]["source"] == "pd7day" and after[0]["time"] == "2026-10-30T22:20:00+10:00"
    assert after[0]["resolution_minutes"] == 10  # PD7DAY's 22:00-22:30, split at the P5MIN end
    _spans_contiguous(timeline)


def test_a_source_the_others_already_cover_is_marked_covered():
    timeline = _tl.build_timeline(_at(21, 27), "NSW1", p5=_p5(), pd=_pd(_at(21, 2), _at(21, 30), periods=1))
    assert timeline.sources["predispatch"]["status"] == "covered"


def test_nothing_at_all_is_an_empty_timeline():
    timeline = _tl.build_timeline(_at(21, 27), "NSW1")
    assert timeline.points == [] and timeline.value_at(_at(21, 27)) is None
    assert {s["status"] for s in timeline.sources.values()} == {"no_data"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _simple():
    return _tl.build_timeline(
        _at(21, 27), "NSW1", dispatch=_tl.DispatchPoint(_at(21, 30), 0.30), pd=_pd(_at(21, 2), _at(22, 0), periods=4, base=100),
    )  # 21:25-21:30 at 0.30, then 21:30-22:00 0.100, 22:00-22:30 0.101, ...


def test_value_at_finds_the_point_covering_a_time():
    t = _simple()
    assert t.value_at(_at(21, 27)) == 0.30
    assert t.value_at(_at(21, 30)) == 0.1
    assert t.value_at(_at(22, 15)) == 0.101
    assert t.value_at(_at(20, 0)) is None and t.value_at(_at(23, 59)) is None


def test_slice_and_min_max():
    t = _simple()
    assert [p["value"] for p in t.slice(_at(21, 29), _at(22, 1))] == [0.30, 0.1, 0.101]
    assert t.min_max(_at(21, 25), _at(23, 30)) == (0.1, 0.30)
    assert t.min_max(_at(1, 0, day=31), _at(2, 0, day=31)) == (None, None)


def test_cheapest_window_weights_points_by_their_minutes():
    t = _simple()
    best = t.cheapest_window(1)
    assert best == {"start": "2026-10-30T21:30:00+10:00", "end": "2026-10-30T22:30:00+10:00",
                    "avg_value": 0.1005, "hours": 1}
    # From 21:25, the hour includes 5 minutes at 0.30 and 55 at 0.1/0.101.
    first = t.cheapest_window(2.5)
    assert first is None or first["start"] >= "2026-10-30T21:25:00+10:00"
    assert t.cheapest_window(10) is None  # longer than the timeline
