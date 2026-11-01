"""timeline_forecast.py (#339 stage 5a): the spot sensors' ``forecast`` from the price timeline.

Each entry keeps the keys ``forecast`` has always had; the 24 hour min/max and
the cheapest window go by time, so 5-minute and 30-minute entries mix fairly.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_tl, _tf = load_chain("price_timeline", "timeline_forecast")

FORECAST_KEYS = {
    "nemtime", "time", "raw_value", "horizon_hours", "calibrated", "p10", "p50", "p90", "ols_mae",
    "calibrated_source", "band_source", "n_obs", "value", "spike_credible",
}


def _at(hour, minute=0, day=30):
    return datetime(2026, 10, day, hour, minute, tzinfo=NEM_TZ)


def _calibrated(first_start, count, base=0.2):
    """Calibrated PD7DAY entries as the spot sensor builds them."""
    return [
        {"nemtime": (first_start + timedelta(minutes=30 * (i + 1))).isoformat(),
         "time": (first_start + timedelta(minutes=30 * i)).isoformat(),
         "raw_value": 0.3, "horizon_hours": 1.0 + i / 2, "calibrated": round(base + i / 100, 6),
         "p10": 0.1, "p50": round(base + i / 100, 6), "p90": 0.5, "ols_mae": 0.01,
         "calibrated_source": "isotonic", "band_source": "stage2_residual", "n_obs": 40,
         "value": round(base + i / 100, 6), "spike_credible": False}
        for i in range(count)
    ]


def _p5(run_at, first_end, count, base=50.0, region="NSW1"):
    rrp = {region: {first_end + timedelta(minutes=5 * i): base + i for i in range(count)}}
    return SimpleNamespace(run_at=run_at, source="p5.zip", rrp=rrp)


def _timeline(now, calibrated, p5=None, dispatch=None):
    return _tl.build_timeline(now, "NSW1", dispatch=dispatch, p5=p5, pd7day=calibrated)


def test_pd7day_entries_are_the_calibrated_entries_and_others_carry_the_same_keys():
    now = _at(21, 27)
    calibrated = _calibrated(_at(21, 0), 6)
    timeline = _timeline(now, calibrated, p5=_p5(_at(21, 25), _at(21, 30), 6),
                         dispatch=_tl.DispatchPoint(_at(21, 30), 0.0951))
    entries = _tf.forecast_entries(timeline, calibrated)
    assert [e["source"] for e in entries] == ["dispatch"] + ["p5min"] * 5 + ["pd7day"] * 5
    for e in entries:
        assert FORECAST_KEYS <= set(e), sorted(FORECAST_KEYS - set(e))
    head = entries[0]
    assert head["value"] == head["calibrated"] == head["raw_value"] == 0.0951
    assert head["calibrated_source"] == "actual" and head["p10"] is None and head["resolution_minutes"] == 5
    assert entries[1]["calibrated_source"] == "raw" and entries[1]["value"] == 0.051  # 21:30-21:35
    # PD7DAY's 21:30-22:00 is covered by P5MIN to 21:55: its last 5 minutes are the calibrated entry, moved.
    split = entries[6]
    assert split["time"] == "2026-10-30T21:55:00+10:00" and split["resolution_minutes"] == 5
    assert {k: v for k, v in split.items() if k not in ("time", "source", "resolution_minutes")} == {
        k: v for k, v in calibrated[1].items() if k != "time"}
    assert entries[7] == {**calibrated[2], "source": "pd7day", "resolution_minutes": 30}


def test_ended_intervals_are_dropped():
    calibrated = _calibrated(_at(21, 0), 6)
    entries = _tf.forecast_entries(_timeline(_at(22, 10), calibrated), calibrated)
    assert entries[0]["time"] == "2026-10-30T22:00:00+10:00", "the current interval, whole"
    assert len(entries) == 4


def test_min_max_go_by_time_from_the_first_entry():
    calibrated = _calibrated(_at(21, 0), 60)  # 30 h, rising 0.01 per half hour
    entries = _tf.forecast_entries(_timeline(_at(21, 27), calibrated, p5=_p5(_at(21, 25), _at(21, 30), 12)), calibrated)
    low, high = _tf.min_max_24h(entries)
    assert low == 0.05  # the first P5MIN interval
    # 24 h from 21:25 runs to 21:25 tomorrow: the 21:00-21:30 half hour, entry 48.
    assert high == round(0.2 + 48 / 100, 6)
    assert _tf.min_max_24h([]) == (None, None)


def test_cheapest_window_weights_entries_by_their_minutes():
    # Twelve 5-minute entries at 0.10 (21:00-22:00), then half hours at 0.50, 0.00, 0.00, 0.50.
    now = _at(21, 1)
    calibrated = _calibrated(_at(21, 0), 6)
    for entry, value in zip(calibrated[2:], (0.5, 0.0, 0.0, 0.5)):
        entry["value"] = value
    p5 = _p5(_at(21, 0), _at(21, 5), 12, base=100.0)
    p5.rrp["NSW1"] = {k: 100.0 for k in p5.rrp["NSW1"]}
    entries = _tf.forecast_entries(_timeline(now, calibrated, p5=p5), calibrated)
    best = _tf.cheapest_window(entries, hours=1.0)
    assert best == {"nemtime_start": "2026-10-30T23:00:00+10:00", "nemtime_end": "2026-10-30T23:30:00+10:00",
                    "start": "2026-10-30T22:30:00+10:00", "end": "2026-10-30T23:00:00+10:00",
                    "avg_value": 0.0, "points": 2}
    # From 21:30: 30 minutes at 0.10, 30 at 0.50, 60 at 0.00, weighted by minutes.
    two = _tf.cheapest_window(entries, hours=2.0)
    assert two["avg_value"] == 0.15 and two["start"] == "2026-10-30T21:30:00+10:00" and two["points"] == 9
    # Twelve 5-minute entries count by their minutes, not as twelve half hours.
    first = _tf.cheapest_window(entries[:12], hours=1.0)
    assert first["points"] == 12 and first["avg_value"] == 0.1


def test_a_window_needs_full_contiguous_coverage():
    calibrated = _calibrated(_at(21, 0), 6)
    entries = _tf.forecast_entries(_timeline(_at(21, 1), calibrated), calibrated)
    assert _tf.cheapest_window(entries, hours=4.0) is None  # only three hours
    gapped = entries[:1] + entries[2:]
    best = _tf.cheapest_window(gapped, hours=1.0)
    assert best["start"] == entries[2]["time"], "no window spans the missing half hour"
    entries[0]["value"] = None
    assert _tf.cheapest_window(entries, hours=1.0)["start"] == entries[1]["time"]
