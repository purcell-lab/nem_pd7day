"""
A DispatchIS file without a LOCAL_PRICE table is AEMO's zero for every DUID
(#309); the fallbacks are for a dispatch file that is missing or stale.

AEMO leaves the LOCAL_PRICE table out when no unit has an adjustment and never
writes an empty one. On 31 Oct 2026 the table was missing exactly when the
only network constraint binding had no unit terms: at 18:45 that was
L_PEC_X_6C_6G_6H_XST, an interconnector-only limit. Such a file, read whole and
fresh, reads ``dispatch_zero``: 0, and nothing stands in.

When there is no dispatch file fresh enough to stand for now, the current
interval comes from the P5MIN run covering it (``p5min_fallback``: its ROP and
rows), else the 30-minute pre-dispatch period (``predispatch_fallback``: its
RRP and rows); neither leaves the sensors stale or without data.

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
ZERO_NAME = "PUBLIC_DISPATCHIS_202610311845_0000000545914233"
ZERO = (_DIR / f"{ZERO_NAME}.CSV").read_text()
ENTRY = SimpleNamespace(entry_id="e1")
NOW = datetime(2026, 10, 30, 21, 22, tzinfo=NEM_TZ)  # the 21:25 interval is running
END = datetime(2026, 10, 30, 21, 25, tzinfo=NEM_TZ)
ZERO_NOW = datetime(2026, 10, 31, 18, 46, tzinfo=NEM_TZ)


def _zero_file():
    return _lp.parse_dispatch_local_prices(ZERO, f"{ZERO_NAME}.zip", datetime(2026, 10, 31, 18, 45, 30, tzinfo=NEM_TZ))


def _stale():
    """The 21:25 file's rows, as if from the 21:00 interval: too old to stand for 21:22."""
    fresh = _lp.parse_dispatch_local_prices(DISPATCH, "d.zip", NOW)
    return replace(fresh, interval_end=datetime(2026, 10, 30, 21, 0, tzinfo=NEM_TZ))


def _run():
    return _lp.parse_p5min_local_prices(P5, P5_NAME, datetime(2026, 10, 30, 21, 20, 45, tzinfo=NEM_TZ))


def _constraints(prices):
    return SimpleNamespace(tracker=SimpleNamespace(latest=SimpleNamespace(local_prices=prices)))


def _pd(run_at=datetime(2026, 10, 30, 21, 2, tzinfo=NEM_TZ)):
    """A pre-dispatch run with its first period ending 21:30, so covering the 21:25 interval."""
    ends = [datetime(2026, 10, 30, 21, 30, tzinfo=NEM_TZ), datetime(2026, 10, 30, 22, 0, tzinfo=NEM_TZ)]
    return SimpleNamespace(
        run_at=run_at, source="PUBLIC_PREDISPATCHIS_202610302130_x.zip", period_ends=ends,
        local_prices={"CATHROCK": {ends[0]: -40.5, ends[1]: -12.0}, "GUTHEGA": {ends[1]: 7.0}},
        prices={"SA1": {ends[0]: 31.5, ends[1]: 40.0}, "NSW1": {ends[0]: 99.0, ends[1]: 101.0}},
    )


# ── A complete file without the table is zero (#309) ─────────────────────────

def test_the_1845_file_has_no_table_and_reads_dispatch_zero():
    prices = _zero_file()
    assert prices.table_present is False and prices.rows == {}
    assert prices.rop["QLD1"] == 83.73
    for duid in ("GSTONE1", "CATHROCK", "GUTHEGA"):
        assert _lp.dispatch_adjustment(prices, duid, ZERO_NOW, _lps.MAX_AGE) == ("dispatch_zero", 0.0)


def test_a_fresh_file_without_the_table_is_never_filled():
    prices = _zero_file()
    assert _lp.with_fallbacks(prices, _run(), _pd(), ZERO_NOW, _lps.MAX_AGE) is prices
    with_table = _lp.parse_dispatch_local_prices(DISPATCH, "d.zip", NOW)
    assert _lp.with_fallbacks(with_table, _run(), _pd(), NOW, _lps.MAX_AGE) is with_table


def test_a_duid_sensor_reads_dispatch_zero_without_a_fallback(monkeypatch):
    monkeypatch.setattr(_lps, "now_nem", lambda: ZERO_NOW)
    sensor = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(_zero_file()), SimpleNamespace(latest=_run()), ENTRY, "QLD1", "GSTONE1",
        predispatch=SimpleNamespace(latest=_pd()),
    )
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == 0.0
    assert attrs["row_status"] == "dispatch_zero" and attrs["source"] == "DISPATCH_LOCAL_PRICE"
    assert attrs["fallback_source_file"] is None and attrs["source_file"] == f"{ZERO_NAME}.zip"
    assert attrs["local_price_mwh"] == pytest.approx(83.73)


def test_a_node_and_a_zone_read_dispatch_zero(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: ZERO_NOW)
    monkeypatch.setattr(_zs, "now_nem", lambda: ZERO_NOW)
    node = _cat.NodeInfo("QGLD", "QLD1", 0.95, ("GSTONE1", "GSTONE2"), ("Gladstone",), -23.8, 151.2)
    sensor = _node.NemPd7dayNodePriceSensor(_constraints(_zero_file()), None, ENTRY, "QLD1", "QGLD", node)
    attrs = sensor.extra_state_attributes
    assert (attrs["adjustment_status"], attrs["adjustment_mwh"]) == ("dispatch_zero", 0.0)
    assert sensor.native_value == pytest.approx(83.73 * 0.95 / 1000, abs=1e-6)
    zone = _zones.Zone("gladstone", "Gladstone", -23.8, 151.2, 100, (_zones.Member(node, 5.0),), False)
    zone_attrs = _zs.NemPd7dayZonePriceSensor(_constraints(_zero_file()), None, ENTRY, "QLD1", zone).extra_state_attributes
    assert zone_attrs["row_status"] == "dispatch_zero" and zone_attrs["adjustment_mwh"] == 0.0
    assert zone_attrs["fallback_source_file"] is None


# ── A missing or stale file: P5MIN, then pre-dispatch ────────────────────────

def test_a_missing_file_takes_the_p5min_run_for_the_running_interval():
    filled = _lp.with_fallbacks(None, _run(), None, NOW, _lps.MAX_AGE)
    assert filled.fallback_status == "p5min_fallback" and filled.fallback_source == P5_NAME
    assert filled.interval_end == END
    assert filled.rows["CATHROCK"].adjustment_mwh == -34.08 and filled.rows["GUTHEGA"].adjustment_mwh == 88.28
    assert filled.rop["SA1"] == 28.84005  # the run's own ROP for the interval
    assert _lp.dispatch_adjustment(filled, "CATHROCK", NOW, _lps.MAX_AGE) == ("p5min_fallback", -34.08)
    assert _lp.dispatch_adjustment(filled, "GSTONE1", NOW, _lps.MAX_AGE) == ("p5min_fallback", 0.0)


def test_a_stale_file_takes_it_too():
    filled = _lp.with_fallbacks(_stale(), _run(), _pd(), NOW, _lps.MAX_AGE)
    assert filled.fallback_status == "p5min_fallback" and filled.interval_end == END


def test_without_p5min_the_predispatch_period_stands_in():
    filled = _lp.with_fallbacks(None, None, _pd(), NOW, _lps.MAX_AGE)
    assert filled.fallback_status == "predispatch_fallback" and filled.fallback_source.startswith("PUBLIC_PREDISPATCHIS")
    assert filled.rop["SA1"] == 31.5  # RRP: pre-dispatch publishes no ROP
    assert _lp.dispatch_adjustment(filled, "CATHROCK", NOW, _lps.MAX_AGE) == ("predispatch_fallback", -40.5)
    assert _lp.dispatch_adjustment(filled, "GUTHEGA", NOW, _lps.MAX_AGE) == ("predispatch_fallback", 0.0)


def test_a_stale_run_or_nothing_leaves_the_file_as_it_is():
    late = datetime(2026, 10, 30, 21, 45, tzinfo=NEM_TZ)  # the run's 21:45 interval, 20 minutes after it
    stale = _stale()
    assert _lp.with_fallbacks(stale, _run(), None, late, _lps.MAX_AGE) is stale
    assert _lp.dispatch_adjustment(stale, "CATHROCK", late, _lps.MAX_AGE) == ("stale", None)
    old_pd = _pd(run_at=datetime(2026, 10, 30, 20, 0, tzinfo=NEM_TZ))
    assert _lp.with_fallbacks(stale, None, old_pd, NOW, _lps.MAX_AGE) is stale
    assert _lp.with_fallbacks(None, None, None, NOW, _lps.MAX_AGE) is None
    assert _lp.dispatch_status(None, "CATHROCK", NOW, _lps.MAX_AGE) == "no_data"


def test_a_conflicting_p5min_row_is_unknown():
    clash = replace(_run(), conflicts=frozenset({("CATHROCK", END)}))
    filled = _lp.with_fallbacks(None, clash, None, NOW, _lps.MAX_AGE)
    assert "CATHROCK" not in filled.rows
    assert _lp.dispatch_adjustment(filled, "CATHROCK", NOW, _lps.MAX_AGE) == ("conflicting_rows", None)


def test_a_p5min_run_without_the_table_is_zero_too():
    filled = _lp.with_fallbacks(None, replace(_run(), table_present=False, rows={}), None, NOW, _lps.MAX_AGE)
    assert _lp.dispatch_adjustment(filled, "CATHROCK", NOW, _lps.MAX_AGE) == ("p5min_fallback", 0.0)


# ── Sensors under a fallback ─────────────────────────────────────────────────

def test_a_duid_sensor_reads_the_fallback_and_says_so(monkeypatch):
    monkeypatch.setattr(_lps, "now_nem", lambda: NOW)
    sensor = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(_stale()), SimpleNamespace(latest=_run()), ENTRY, "SA1", "CATHROCK",
    )
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == pytest.approx(-0.03408)
    assert attrs["row_status"] == "p5min_fallback" and attrs["source"] == "P5MIN_LOCAL_PRICE"
    assert attrs["fallback_source_file"] == P5_NAME
    assert attrs["local_price_mwh"] == pytest.approx(28.84005 - 34.08, abs=1e-4)
    pd_sensor = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(None), SimpleNamespace(latest=None), ENTRY, "SA1", "CATHROCK",
        predispatch=SimpleNamespace(latest=_pd()),
    )
    pd_attrs = pd_sensor.extra_state_attributes
    assert pd_sensor.native_value == pytest.approx(-0.0405)
    assert (pd_attrs["row_status"], pd_attrs["source"]) == ("predispatch_fallback", "PREDISPATCH_LOCAL_PRICE")
    none = _lps.NemPd7dayDuidLocalPriceSensor(_constraints(_stale()), SimpleNamespace(latest=None), ENTRY, "SA1", "CATHROCK")
    assert none.native_value is None and none.extra_state_attributes["row_status"] == "stale"


def test_a_node_and_a_zone_read_it_too(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: NOW)
    monkeypatch.setattr(_zs, "now_nem", lambda: NOW)
    p5 = SimpleNamespace(latest=_run())
    node = _cat.NodeInfo("NGTH1", "NSW1", 0.9, ("GUTHEGA",), ("Guthega",), -36.4, 148.4)
    sensor = _node.NemPd7dayNodePriceSensor(_constraints(_stale()), p5, ENTRY, "NSW1", "NGTH1", node)
    attrs = sensor.extra_state_attributes
    assert (attrs["adjustment_status"], attrs["adjustment_mwh"]) == ("p5min_fallback", 88.28)
    assert sensor.native_value == pytest.approx((97.62 * 0.9 + 88.28) / 1000, abs=1e-6)
    assert attrs["fallback_source_file"] == P5_NAME
    zone = _zones.Zone("sydney", "Sydney", -33.87, 151.21, 100, (_zones.Member(node, 5.0),), False)
    zone_attrs = _zs.NemPd7dayZonePriceSensor(_constraints(_stale()), p5, ENTRY, "NSW1", zone).extra_state_attributes
    assert zone_attrs["row_status"] == "p5min_fallback" and zone_attrs["adjustment_mwh"] == 88.28
    assert zone_attrs["fallback_source_file"] == P5_NAME
