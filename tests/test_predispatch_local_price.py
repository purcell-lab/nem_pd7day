"""
Local price adjustments from the 30-minute pre-dispatch (#287): the
PREDISPATCH,LOCAL_PRICE rows of the PredispatchIS run the integration already
reads, as a forecast to the end of the next trading day on the DUID and node
sensors; and the node's dispatch adjustment, from its DUIDs' DISPATCH_LOCAL_PRICE.

The fixture is the real run written at 13:02 NEM time on 31 Oct 2026, cut to
its case, the QLD1, NSW1 and SA1 prices, one interconnector and the
LOCAL_PRICE rows of three DUIDs: 78 periods, to 04:00 on 2 Nov.

Run with:  python -m pytest tests/test_predispatch_local_price.py -v
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_const, _cat, _lp, _cc, _pdc, _pdl, _coord, _lps, _node = load_chain(
    "const", "duid_catalogue", "local_price", "constraint_client", "predispatch_client",
    "predispatch_local_price", "local_price_coordinator", "local_price_sensor", "node_price_sensor",
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
PD_NAME = "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.zip"
PD = (_DIR / "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.CSV").read_text()
DISPATCH = (_DIR / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
OLD_RUN = next((Path(__file__).parent / "fixtures" / "predispatchis").glob("PUBLIC_PREDISPATCHIS_*.zip"))
ENTRY = SimpleNamespace(entry_id="e1")


def _at(hour, minute=0, day=31):
    return datetime(2026, 10, day, hour, minute, tzinfo=NEM_TZ)


@pytest.fixture(scope="module")
def run():
    return _pdc.parse_predispatch(PD, PD_NAME)


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_the_run_carries_local_prices_per_duid_and_period(run):
    assert run.local_price_table is True
    assert run.run_at == datetime(2026, 10, 31, 13, 2, 29, tzinfo=NEM_TZ)
    assert set(run.local_prices) == {"CATHROCK", "HUMENSW", "URANQ14"}
    assert run.local_prices["URANQ14"][_at(13, 30)] == -1017.04
    assert len(run.local_prices["CATHROCK"]) == 26
    assert len(run.period_ends) == 78 and run.period_ends[-1] == datetime(2026, 11, 2, 4, 0, tzinfo=NEM_TZ)


def test_a_run_without_the_table_says_so():
    import zipfile

    with zipfile.ZipFile(OLD_RUN) as zf:
        old = _pdc.parse_predispatch(zf.read(zf.namelist()[0]).decode(), OLD_RUN.name)
    assert old.local_price_table is False and old.local_prices == {}


# ── The series ───────────────────────────────────────────────────────────────

def test_published_periods_and_zero_by_omission(run):
    series = _pdl.predispatch_adjustments(run, ["URANQ14"], _at(13, 10))
    assert series.status == "published" and len(series.periods) == 78
    assert (series.published, series.inferred) == (24, 54)
    assert series.periods[0] == (_at(13, 30), -1017.04)
    unconstrained = _pdl.predispatch_adjustments(run, ["GSTONE1"], _at(13, 10))
    assert unconstrained.status == "zero_by_omission" and unconstrained.inferred == 78
    assert {value for _end, value in unconstrained.periods} == {0.0}


def test_ended_periods_are_dropped(run):
    series = _pdl.predispatch_adjustments(run, ["URANQ14"], _at(13, 40))
    assert series.periods[0][0] == _at(14, 0) and len(series.periods) == 77


def test_stale_missing_table_no_data_and_expired_give_no_points(run):
    assert _pdl.predispatch_adjustments(run, ["URANQ14"], _at(14, 30)).status == "stale"
    assert _pdl.predispatch_adjustments(replace(run, local_price_table=False), ["URANQ14"], _at(13, 10)).status == "table_missing"
    assert _pdl.predispatch_adjustments(None, ["URANQ14"], _at(13, 10)).status == "no_data"
    late = replace(run, run_at=datetime(2026, 11, 2, 4, 0, tzinfo=NEM_TZ))
    expired = _pdl.predispatch_adjustments(late, ["URANQ14"], datetime(2026, 11, 2, 4, 10, tzinfo=NEM_TZ))
    assert expired.status == "expired" and expired.periods == []


def test_duids_of_one_node_that_disagree_stop_the_series(run):
    series = _pdl.predispatch_adjustments(run, ["URANQ14", "CATHROCK"], _at(13, 10))
    assert series.status == "conflicting_rows"
    first_both = min(set(run.local_prices["URANQ14"]) & set(run.local_prices["CATHROCK"]))
    assert all(end < first_both for end, _value in series.periods)


# ── Sensors ──────────────────────────────────────────────────────────────────

def _constraints(prices):
    return SimpleNamespace(tracker=SimpleNamespace(latest=SimpleNamespace(local_prices=prices)))


def test_a_duid_sensor_publishes_the_predispatch_forecast(monkeypatch, run):
    monkeypatch.setattr(_lps, "now_nem", lambda: _at(13, 10))
    sensor = _lps.NemPd7dayDuidLocalPriceSensor(
        _constraints(None), SimpleNamespace(latest=None), ENTRY, "NSW1", "URANQ14",
        predispatch=SimpleNamespace(latest=run),
    )
    attrs = sensor.extra_state_attributes
    assert attrs["predispatch_status"] == "published" and attrs["predispatch_source"] == "PREDISPATCH_LOCAL_PRICE"
    assert attrs["predispatch_forecast"][0] == {"time": "2026-10-31T13:00:00+10:00", "value": -1.01704}
    assert attrs["predispatch_end"] == "2026-11-02T04:00:00+10:00" and len(attrs["predispatch_forecast"]) == 78
    assert attrs["predispatch_run_at"] == "2026-10-31T13:02:29+10:00" and attrs["predispatch_source_file"] == PD_NAME
    assert (attrs["predispatch_intervals_published"], attrs["predispatch_intervals_inferred_zero"]) == (24, 54)
    assert "predispatch_forecast" in sensor._unrecorded_attributes


def _info(duids=("URANQ14",), region="NSW1", mlf=0.95):
    return _cat.NodeInfo("NTEST1", region, mlf, tuple(duids), ("Test",), None, None)


def test_a_node_forecasts_rrp_times_mlf_plus_its_adjustment(monkeypatch, run):
    monkeypatch.setattr(_node, "now_nem", lambda: _at(13, 10))
    sensor = _node.NemPd7dayNodePriceSensor(
        _constraints(None), None, ENTRY, "NSW1", "NTEST1", node := _info(), SimpleNamespace(latest=run),
    )
    attrs = sensor.extra_state_attributes
    points = attrs["predispatch_forecast"]
    assert points[0] == {"time": "2026-10-31T13:00:00+10:00", "value": round((55.85 * 0.95 - 1017.04) / 1000, 6)}
    assert points[1]["value"] == round((55.85 * 0.95 - 98.34) / 1000, 6)
    assert attrs["predispatch_status"] == "published" and len(points) == 78
    assert "predispatch_forecast" in sensor._unrecorded_attributes and node.mlf == 0.95


def test_an_unknown_node_has_no_predispatch_forecast(monkeypatch, run):
    monkeypatch.setattr(_node, "now_nem", lambda: _at(13, 10))
    attrs = _node.NemPd7dayNodePriceSensor(
        _constraints(None), None, ENTRY, "NSW1", "NNEW1", None, SimpleNamespace(latest=run),
    ).extra_state_attributes
    assert attrs["predispatch_status"] == "unknown_node" and attrs["predispatch_forecast"] == []
    assert (attrs["adjustment_status"], attrs["adjustment_mwh"]) == ("unknown_node", None)


def _dispatch():
    return _lp.parse_dispatch_local_prices(DISPATCH, "d.zip", datetime(2026, 10, 30, 21, 25, 40, tzinfo=NEM_TZ))


def test_the_nodes_dispatch_adjustment_is_its_duids_published_one(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ))
    constrained = _node.NemPd7dayNodePriceSensor(
        _constraints(_dispatch()), None, ENTRY, "NSW1", "NTEST1", _info(("GUTHEGA",), mlf=0.9),
    )
    attrs = constrained.extra_state_attributes
    assert (attrs["adjustment_status"], attrs["adjustment_mwh"]) == ("published", 88.27)
    assert attrs["node_price_mwh"] == pytest.approx(97.62 * 0.9 + 88.27)
    assert attrs["static_price_mwh"] == pytest.approx(97.62 * 0.9, abs=1e-4)
    # The state is the node price, constraint included: ROP x MLF + adjustment.
    assert constrained.native_value == pytest.approx((97.62 * 0.9 + 88.27) / 1000, abs=1e-6)

    free = _node.NemPd7dayNodePriceSensor(
        _constraints(_dispatch()), None, ENTRY, "NSW1", "NTEST1", _info(("NOROWS1",), mlf=0.9),
    ).extra_state_attributes
    assert (free["adjustment_status"], free["adjustment_mwh"]) == ("zero_by_omission", 0.0)
    assert free["node_price_mwh"] == pytest.approx(97.62 * 0.9, abs=1e-4) == free["static_price_mwh"]


def test_a_stale_dispatch_or_disagreeing_duids_leave_the_adjustment_unknown(monkeypatch):
    monkeypatch.setattr(_node, "now_nem", lambda: datetime(2026, 10, 30, 21, 26, tzinfo=NEM_TZ))
    prices = _dispatch()
    two = sorted(d for d, r in prices.rows.items() if r.adjustment_mwh != prices.rows["GUTHEGA"].adjustment_mwh)[0]
    split = _node.NemPd7dayNodePriceSensor(
        _constraints(prices), None, ENTRY, "NSW1", "NTEST1", _info(("GUTHEGA", two)),
    ).extra_state_attributes
    assert (split["adjustment_status"], split["adjustment_mwh"], split["node_price_mwh"]) == ("conflicting_rows", None, None)
    monkeypatch.setattr(_node, "now_nem", lambda: datetime(2026, 10, 30, 21, 50, tzinfo=NEM_TZ))
    stale = _node.NemPd7dayNodePriceSensor(
        _constraints(prices), None, ENTRY, "NSW1", "NTEST1", _info(("GUTHEGA",)),
    ).extra_state_attributes
    assert (stale["adjustment_status"], stale["adjustment_mwh"]) == ("stale", None)


def test_a_node_below_the_floor_is_reported_raw(monkeypatch):
    """GSTONE1 at 16:05 on 31 Oct 2026: 70.0806 x 0.9299 - 1110.09, under the
    -1000 floor, published as it is."""
    end = datetime(2026, 10, 31, 16, 5, tzinfo=NEM_TZ)
    prices = _lp.DispatchLocalPrices(
        end, "d.zip", True, {"GSTONE1": _lp.LocalPriceRow(-1110.09, 1)}, {"QLD1": 70.0806}, end,
    )
    monkeypatch.setattr(_node, "now_nem", lambda: end)
    sensor = _node.NemPd7dayNodePriceSensor(
        _constraints(prices), None, ENTRY, "QLD1", "QGLD1", _info(("GSTONE1",), region="QLD1", mlf=0.9299),
    )
    assert sensor.native_value == pytest.approx(-1.044922, abs=1e-6)
    attrs = sensor.extra_state_attributes
    assert attrs["node_price_mwh"] == pytest.approx(-1044.9221, abs=1e-4)
    assert attrs["static_price_mwh"] == pytest.approx(65.1679, abs=1e-4) and attrs["adjustment_mwh"] == -1110.09
