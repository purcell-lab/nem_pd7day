"""
The fallbacks for a DispatchIS file without its LOCAL_PRICE table: the P5MIN
run covering the interval, else the 30-minute pre-dispatch period covering
it, else no adjustment at all, the regional price.

AEMO now and then publishes a DispatchIS file with no LOCAL_PRICE table at
all, never an empty one: 11 of 576 files over 30 and 31 Oct 2026, all early
evening (18:40 to 19:00 on the 31st). The P5MIN run solved for the same
interval keeps its table, so its adjustments stand in, flagged
``p5min_fallback``, on the DUID, node and zone sensors; the ROP stays the
dispatch file's.

Run with:  python -m pytest tests/test_dispatch_fallbacks.py -v
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_const, _cat, _lp, _cc, _pdc, _pdl, _coord, _lps, _node, _ldp, _zones, _zs = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "predispatch_client",
    "predispatch_local_price", "local_price_coordinator", "local_price_sensor", "node_price_sensor",
    "load_points", "zones", "zone_sensor",
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
DISPATCH = (_DIR / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()
P5_NAME = "PUBLIC_P5MIN_202610302125_20261030212034.zip"
ENTRY = SimpleNamespace(entry_id="e1")
NOW = datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ)
END = datetime(2026, 10, 30, 21, 25, tzinfo=NEM_TZ)


def _without_table():
    text = "\n".join(line for line in DISPATCH.splitlines() if "LOCAL_PRICE" not in line) + "\n"
    return _lp.parse_dispatch_local_prices(text, "d.zip", datetime(2026, 10, 30, 21, 25, 40, tzinfo=NEM_TZ))


def _run():
    return _lp.parse_p5min_local_prices(P5, P5_NAME, datetime(2026, 10, 30, 21, 25, 45, tzinfo=NEM_TZ))


def _constraints(prices):
    return SimpleNamespace(tracker=SimpleNamespace(latest=SimpleNamespace(local_prices=prices)))


# ── The fallback ─────────────────────────────────────────────────────────────

def test_a_missing_table_takes_the_p5min_runs_rows_for_the_interval():
    prices = _without_table()
    assert prices.table_present is False
    filled = _lp.with_p5_fallback(prices, _run())
    assert filled.table_present is True and filled.fallback_source == P5_NAME
    assert filled.rows["CATHROCK"].adjustment_mwh == -34.08  # dispatch published -33.92
    assert filled.rows["GUTHEGA"].adjustment_mwh == 88.28
    assert filled.rop == prices.rop and filled.interval_end == END  # the dispatch file's own ROP


def test_a_file_with_its_table_or_no_covering_run_is_left_alone():
    with_table = _lp.parse_dispatch_local_prices(DISPATCH, "d.zip", NOW)
    assert _lp.with_p5_fallback(with_table, _run()) is with_table
    prices = _without_table()
    assert _lp.with_p5_fallback(prices, None) is prices
    assert _lp.with_p5_fallback(prices, replace(_run(), table_present=False)) is prices
    later = replace(_run(), interval_ends=tuple(e for e in _run().interval_ends if e != END))
    assert _lp.with_p5_fallback(prices, later) is prices
    assert _lp.with_p5_fallback(None, _run()) is None


def test_statuses_and_values_under_the_fallback():
    filled = _lp.with_p5_fallback(_without_table(), _run())
    assert _lp.dispatch_adjustment(filled, "CATHROCK", NOW, _lps.MAX_AGE) == ("p5min_fallback", -34.08)
    assert _lp.dispatch_adjustment(filled, "GSTONE1", NOW, _lps.MAX_AGE) == ("p5min_fallback", 0.0)
    clash = replace(_run(), conflicts=frozenset({("CATHROCK", END)}))
    conflicted = _lp.with_p5_fallback(_without_table(), clash)
    assert "CATHROCK" not in conflicted.rows
    assert _lp.dispatch_adjustment(conflicted, "CATHROCK", NOW, _lps.MAX_AGE) == ("conflicting_rows", None)
    late = datetime(2026, 10, 30, 21, 45, tzinfo=NEM_TZ)
    assert _lp.dispatch_adjustment(filled, "CATHROCK", late, _lps.MAX_AGE) == ("stale", None)


# ── Sensors ──────────────────────────────────────────────────────────────────

def test_a_duid_sensor_reads_the_fallback_and_says_so(monkeypatch):
    monkeypatch.setattr(_lps, "now_nem", lambda: NOW)
    sensor = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(_without_table()), SimpleNamespace(latest=_run()), ENTRY, "SA1", "CATHROCK",
    )
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == pytest.approx(-0.03408)
    assert attrs["row_status"] == "p5min_fallback" and attrs["source"] == "P5MIN_LOCAL_PRICE"
    assert attrs["fallback_source_file"] == P5_NAME and attrs["source_file"] == "d.zip"
    assert attrs["local_price_mwh"] == pytest.approx(28.67941 - 34.08, abs=1e-4)  # SA1's dispatch ROP
    without_run = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(_without_table()), SimpleNamespace(latest=None), ENTRY, "SA1", "CATHROCK",
    )
    assert without_run.native_value == 0.0  # nothing to stand in: the regional price
    assert without_run.extra_state_attributes["row_status"] == "rrp_fallback"


def test_a_node_and_a_zone_read_it_too(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: NOW)
    monkeypatch.setattr(_zs, "now_nem", lambda: NOW)
    p5 = SimpleNamespace(latest=_run())
    node = _cat.NodeInfo("NGTH1", "NSW1", 0.9, ("GUTHEGA",), ("Guthega",), -36.4, 148.4)
    sensor = _node.NemPd7dayNodePriceSensor(_constraints(_without_table()), p5, ENTRY, "NSW1", "NGTH1", node)
    attrs = sensor.extra_state_attributes
    assert (attrs["adjustment_status"], attrs["adjustment_mwh"]) == ("p5min_fallback", 88.28)
    assert sensor.native_value == pytest.approx((97.62 * 0.9 + 88.28) / 1000, abs=1e-6)
    assert attrs["fallback_source_file"] == P5_NAME
    zone = _zones.Zone("sydney", "Sydney", -33.87, 151.21, 100, (_zones.Member(node, 5.0),), False)
    zone_sensor = _zs.NemPd7dayZonePriceSensor(_constraints(_without_table()), p5, ENTRY, "NSW1", zone)
    zone_attrs = zone_sensor.extra_state_attributes
    assert zone_attrs["row_status"] == "p5min_fallback" and zone_attrs["adjustment_mwh"] == 88.28
    assert zone_attrs["fallback_source_file"] == P5_NAME


# ── Pre-dispatch, then the regional price ────────────────────────────────────

def _pd(run_at=datetime(2026, 10, 30, 21, 2, tzinfo=NEM_TZ), table=True):
    """A pre-dispatch run with its first period ending 21:30, so covering 21:25."""
    ends = [datetime(2026, 10, 30, 21, 30, tzinfo=NEM_TZ), datetime(2026, 10, 30, 22, 0, tzinfo=NEM_TZ)]
    return SimpleNamespace(
        run_at=run_at, source="PUBLIC_PREDISPATCHIS_202610302130_x.zip", local_price_table=table, period_ends=ends,
        local_prices={"CATHROCK": {ends[0]: -40.5, ends[1]: -12.0}, "GUTHEGA": {ends[1]: 7.0}},
    )


def test_without_p5min_the_predispatch_period_stands_in():
    filled = _lp.with_fallbacks(_without_table(), None, _pd())
    assert filled.fallback_status == "predispatch_fallback" and filled.fallback_source.startswith("PUBLIC_PREDISPATCHIS")
    assert _lp.dispatch_adjustment(filled, "CATHROCK", NOW, _lps.MAX_AGE) == ("predispatch_fallback", -40.5)
    assert _lp.dispatch_adjustment(filled, "GUTHEGA", NOW, _lps.MAX_AGE) == ("predispatch_fallback", 0.0)
    assert filled.rop == _without_table().rop


def test_p5min_comes_before_predispatch():
    filled = _lp.with_fallbacks(_without_table(), _run(), _pd())
    assert filled.fallback_status == "p5min_fallback" and filled.rows["CATHROCK"].adjustment_mwh == -34.08


def test_an_old_or_tableless_predispatch_run_leaves_the_regional_price():
    old = _lp.with_fallbacks(_without_table(), None, _pd(run_at=datetime(2026, 10, 30, 20, 0, tzinfo=NEM_TZ)))
    assert old.fallback_status == "rrp_fallback" and old.rows == {} and old.fallback_source == "RRP"
    tableless = _lp.with_fallbacks(_without_table(), None, _pd(table=False))
    assert tableless.fallback_status == "rrp_fallback"
    assert _lp.dispatch_adjustment(tableless, "CATHROCK", NOW, _lps.MAX_AGE) == ("rrp_fallback", 0.0)
    # A file with its table, or none at all, is never filled.
    with_table = _lp.parse_dispatch_local_prices(DISPATCH, "d.zip", NOW)
    assert _lp.with_fallbacks(with_table, None, None) is with_table and _lp.with_fallbacks(None, None, None) is None


def test_the_sensors_report_which_fallback(monkeypatch):
    monkeypatch.setattr(_lps, "now_nem", lambda: NOW)
    monkeypatch.setattr(_node, "now_nem", lambda: NOW)
    pd = SimpleNamespace(latest=_pd())
    duid = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(_without_table()), SimpleNamespace(latest=None), ENTRY, "SA1", "CATHROCK", predispatch=pd,
    )
    attrs = duid.extra_state_attributes
    assert duid.native_value == pytest.approx(-0.0405)
    assert (attrs["row_status"], attrs["source"]) == ("predispatch_fallback", "PREDISPATCH_LOCAL_PRICE")
    rrp = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(_without_table()), SimpleNamespace(latest=None), ENTRY, "SA1", "CATHROCK",
    )
    assert rrp.extra_state_attributes["source"] == "RRP" and rrp.extra_state_attributes["fallback_source_file"] == "RRP"
    node = _cat.NodeInfo("SCRK", "SA1", 0.9417, ("CATHROCK",), ("Cathedral Rocks",), -34.9, 135.6)
    sensor = _node.NemPd7dayNodePriceSensor(_constraints(_without_table()), None, ENTRY, "SA1", "SCRK", node)
    assert sensor.extra_state_attributes["adjustment_status"] == "rrp_fallback"
    assert sensor.native_value == pytest.approx(28.67941 * 0.9417 / 1000, abs=1e-6)  # the static price
