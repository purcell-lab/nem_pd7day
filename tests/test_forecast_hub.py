"""forecast_hub.py (#336): series worked out once per run and period, the same for every sensor.

The property that matters: whatever the clock, a series from the hub equals
the series its function returns when called directly. The sweep tests check
that minute by minute across real P5MIN and pre-dispatch runs.
"""
from __future__ import annotations

import gc
import weakref
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_const, _cat, _lp, _cc, _pdc, _pdl, _hub, _lps, _node, _ldp, _zones, _zs = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "predispatch_client",
    "predispatch_local_price", "forecast_hub", "local_price_sensor", "node_price_sensor",
    "load_points", "zones", "zone_sensor",
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()
PD = (_DIR / "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.CSV").read_text()
P5_MAX = _lps.MAX_AGE
PD_MAX = _pdl.MAX_AGE


def _p5():
    return _lp.parse_p5min_local_prices(P5, "PUBLIC_P5MIN_202610302125_20261030212034.zip",
                                        datetime(2026, 10, 30, 21, 25, 45, tzinfo=NEM_TZ))


def _pd():
    return _pdc.parse_predispatch(PD, "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.zip")


def _minutes(start, count):
    return [start + timedelta(minutes=m, seconds=17) for m in range(count)]


# ── The sweeps: hub == direct, at every minute ───────────────────────────────

def test_p5min_series_from_the_hub_equal_the_direct_call_at_every_minute():
    run = _p5()
    hub = _hub.ForecastHub()
    catalogue = _cat.load_catalogue()
    zone = _zones.build_zone(catalogue, "NSW1", "canberra", 100)
    node_duids = tuple(sorted(run.rows))[:2]
    for now in _minutes(datetime(2026, 10, 30, 21, 15, tzinfo=NEM_TZ), 50):  # through to stale
        for duid in run.rows:
            got = hub.p5("duid", run, duid, now, P5_MAX, lambda: _lp.forecast_series(run, duid, now, P5_MAX))
            assert got == _lp.forecast_series(run, duid, now, P5_MAX), (duid, now)
        args = ("NSW1", 0.98, node_duids)
        got = hub.p5("node", run, args, now, P5_MAX, lambda: _node.node_forecast(run, *args, now))
        assert got == _node.node_forecast(run, *args, now), now
        got = hub.p5("zone", run, (zone, "NSW1"), now, P5_MAX, lambda: _zs.zone_p5_forecast(zone, "NSW1", run, now))
        assert got == _zs.zone_p5_forecast(zone, "NSW1", run, now), now
    assert hub.stats.reused > hub.stats.computed  # and it was reused, not rebuilt each minute


def test_predispatch_series_from_the_hub_equal_the_direct_call_at_every_minute():
    snapshot = _pd()
    hub = _hub.ForecastHub()
    zone = _zones.build_zone(_cat.load_catalogue(), "QLD1", "rockhampton", 100)
    duids = sorted(snapshot.local_prices)[:5]
    for now in _minutes(datetime(2026, 10, 31, 13, 0, tzinfo=NEM_TZ), 150):  # 13:00 to 15:30: stale from 14:18
        for duid in duids:
            got = hub.pd("duid", snapshot, duid, now, PD_MAX, lambda: _pdl.predispatch_adjustments(snapshot, [duid], now))
            assert got == _pdl.predispatch_adjustments(snapshot, [duid], now), (duid, now)
        got = hub.pd("zone", snapshot, (zone, "QLD1"), now, PD_MAX, lambda: _zs.zone_predispatch(zone, snapshot, now))
        assert got == _zs.zone_predispatch(zone, snapshot, now), now
    assert hub.stats.reused > 10 * hub.stats.computed


# ── Keys and lifetime ────────────────────────────────────────────────────────

def test_a_series_is_one_object_until_an_interval_ends():
    run = _p5()
    hub = _hub.ForecastHub()
    calls = []

    def compute(now):
        calls.append(now)
        return object()

    t = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)
    first = hub.p5("duid", run, "X", t, P5_MAX, lambda: compute(t))
    assert hub.p5("duid", run, "X", t + timedelta(minutes=3), P5_MAX, lambda: compute(t)) is first
    assert hub.p5("duid", run, "X", t + timedelta(minutes=4), P5_MAX, lambda: compute(t)) is not first  # 21:30 ended
    assert len(calls) == 2
    hub.p5("duid", run, "Y", t, P5_MAX, lambda: compute(t))
    assert len(calls) == 3  # other arguments, other entry


def test_a_new_run_drops_the_old_runs_entries_and_without_a_run_nothing_is_kept():
    run = _p5()
    hub = _hub.ForecastHub()
    t = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)
    hub.p5("duid", run, "X", t, P5_MAX, lambda: 1)
    newer = replace(run, run_at=run.run_at + timedelta(minutes=5), source="newer.zip")
    assert hub.p5("duid", newer, "X", t, P5_MAX, lambda: 2) == 2
    assert len(hub._memo["p5:duid"][1]) == 1
    # A run rebuilt with the same file and time but other rows is not the old run.
    rebuilt = replace(newer, rows={})
    assert hub.p5("duid", rebuilt, "X", t, P5_MAX, lambda: 3) == 3
    calls = []
    for _ in range(3):
        hub.p5("duid", None, "X", t, P5_MAX, lambda: calls.append(1))
    assert len(calls) == 3


def test_kinds_are_kept_apart():
    run, snapshot = _p5(), _pd()
    hub = _hub.ForecastHub()
    t = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)
    assert hub.p5("duid", run, "X", t, P5_MAX, lambda: "duid") == "duid"
    assert hub.p5("node", run, "X", t, P5_MAX, lambda: "node") == "node"
    assert hub.pd("duid", snapshot, "X", t, PD_MAX, lambda: "pd") == "pd"


def test_an_object_in_a_key_is_held_while_its_entry_lives():
    run = _p5()
    hub = _hub.ForecastHub()
    t = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)

    class Zone:
        pass

    zone = Zone()
    ref = weakref.ref(zone)
    hub.p5("zone", run, (zone, "NSW1"), t, P5_MAX, lambda: 1)
    del zone
    gc.collect()
    assert ref() is not None, "a freed zone's id could be reused by a rebuilt zone"
    hub.p5("zone", replace(run, source="next.zip"), ("other", "NSW1"), t, P5_MAX, lambda: 2)
    gc.collect()
    assert ref() is None, "the next run releases it"


def test_the_guard_drops_a_kind_that_grows_past_its_limit(monkeypatch):
    monkeypatch.setattr(_hub, "MAX_ENTRIES", 3)
    run = _p5()
    hub = _hub.ForecastHub()
    t = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)
    for i in range(5):
        hub.p5("duid", run, i, t, P5_MAX, lambda: i)
    assert len(hub._memo["p5:duid"][1]) <= 3


def test_one_hub_per_install():
    data: dict = {}
    assert _hub.shared_forecast_hub(data) is _hub.shared_forecast_hub(data)


def test_kept_values_live_apart_from_the_series_and_compare_cheaply():
    """A kept identity names its inputs by id: the series memo's bookkeeping must
    never hash or deep-compare a run (a frozen dataclass with dict fields)."""
    run = _p5()
    hub = _hub.ForecastHub()
    t = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)
    identity = (id(run), run.run_at)
    assert hub.keep("timeline:NSW1", identity, (t,), lambda: "a", pin=(run,)) == "a"
    assert hub.keep("timeline:NSW1", identity, (t,), lambda: "b", pin=(run,)) == "a"
    # The series memo still works beside it, through a new run.
    assert hub.p5("duid", run, "X", t, P5_MAX, lambda: 1) == 1
    assert hub.p5("duid", replace(run, source="n.zip"), "X", t, P5_MAX, lambda: 2) == 2
