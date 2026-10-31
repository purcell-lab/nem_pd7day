"""
Tests for DUID local price adjustments (#275): local_price.py (parsers, series,
price limits), local_price_coordinator.py (P5MIN client and shared
coordinator), the DispatchIS hook in constraint_client.py and
local_price_sensor.py.

The fixtures are the real DispatchIS file for the interval ending 21:25 NEM
time on 30 Oct 2026 and the P5MIN run of the same time, trimmed to the tables
read here (plus a few constraint rows); headers and rows are unchanged from
NEMWEB. Seven DUIDs carry an adjustment: CATHROCK and POR03 (SA, -33.92),
DUNDWF1-3 (VIC, -1000.01), GUTHEGA (NSW, +88.27) and HUMENSW (-385.47).
Every other case is the real text with rows removed, added or edited.

Run with:  python -m pytest tests/test_local_price.py -v
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const, _lp, _cc, _coord, _sensor = load_chain(
    "const", "local_price", "constraint_client", "local_price_coordinator", "local_price_sensor"
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
DISPATCH_NAME = "PUBLIC_DISPATCHIS_202610302125_0000000545750856.zip"
P5_NAME = "PUBLIC_P5MIN_202610302125_20261030212034.zip"
DISPATCH = (_DIR / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()


def _at(hour, minute=0, second=0, day=30, month=10, year=2026):
    return datetime(year, month, day, hour, minute, second, tzinfo=NEM_TZ)


def _without(text, *needles):
    return "\n".join(line for line in text.splitlines() if not all(n in line for n in needles)) + "\n"


def _dispatch(text=DISPATCH):
    return _lp.parse_dispatch_local_prices(text, DISPATCH_NAME, _at(21, 25, 40))


def _p5(text=P5):
    return _lp.parse_p5min_local_prices(text, P5_NAME, _at(21, 25, 45))


# ── DUIDs ────────────────────────────────────────────────────────────────────

def test_parse_duids_normalises_and_separates_the_invalid():
    assert _lp.parse_duids(" gstone1,CATHROCK;\nGSTONE1  por03 ") == (["GSTONE1", "CATHROCK", "POR03"], [])
    assert _lp.parse_duids("GSTONE1, TOOLONGDUID9, BAD@ID") == (["GSTONE1"], ["TOOLONGDUID9", "BAD@ID"])
    assert _lp.parse_duids("") == ([], []) and _lp.parse_duids(None) == ([], [])
    assert _lp.parse_duids(["dundwf1"]) == (["DUNDWF1"], [])


# ── Dispatch ─────────────────────────────────────────────────────────────────

def test_dispatch_real_file_rows_and_rops():
    prices = _dispatch()
    assert prices.interval_end == _at(21, 25)
    assert prices.source == DISPATCH_NAME and prices.table_present
    assert sorted(prices.rows) == ["CATHROCK", "DUNDWF1", "DUNDWF2", "DUNDWF3", "GUTHEGA", "HUMENSW", "POR03"]
    assert prices.rows["GUTHEGA"] == _lp.LocalPriceRow(88.27, 2)
    assert prices.rows["DUNDWF1"].adjustment_mwh == pytest.approx(-1000.01)
    assert prices.rop == {
        "NSW1": 97.62, "QLD1": 85.25, "SA1": pytest.approx(28.67941), "TAS1": 0.0104, "VIC1": 0.01,
    }


def test_dispatch_reads_rop_not_rrp_and_skips_the_intervention_run():
    """An intervention interval publishes RRP apart from ROP; the local price uses ROP (#275)."""
    rrp_edited = DISPATCH.replace(
        'D,DISPATCH,PRICE,5,"2026/10/30 21:25:00",1,NSW1,20261030209,0,97.62,0,97.62',
        'D,DISPATCH,PRICE,5,"2026/10/30 21:25:00",1,NSW1,20261030209,0,300.00,0,97.62',
    )
    intervention = rrp_edited + 'D,DISPATCH,PRICE,5,"2026/10/30 21:25:00",1,NSW1,20261030209,1,300.00,0,555.55\n'
    assert _dispatch(intervention).rop["NSW1"] == 97.62


def test_dispatch_explicit_zero_is_published_not_absent():
    text = DISPATCH + 'D,DISPATCH,LOCAL_PRICE,1,"2026/10/30 21:25:00",GSTONE1,0,0\n'
    prices = _dispatch(text)
    assert prices.rows["GSTONE1"] == _lp.LocalPriceRow(0.0, 0)
    assert _lp.dispatch_status(prices, "GSTONE1", _at(21, 26), timedelta(minutes=15)) == _lp.STATUS_PUBLISHED


def test_dispatch_statuses_zero_by_omission_missing_stale_and_no_data():
    age = timedelta(minutes=15)
    prices = _dispatch()
    assert _lp.dispatch_status(prices, "GSTONE1", _at(21, 26), age) == _lp.STATUS_ZERO
    assert _lp.dispatch_status(prices, "GUTHEGA", _at(21, 41), age) == _lp.STATUS_STALE
    assert _lp.dispatch_status(None, "GUTHEGA", _at(21, 26), age) == _lp.STATUS_NO_DATA
    no_table = _dispatch(_without(DISPATCH, "LOCAL_PRICE"))
    assert not no_table.table_present and no_table.rows == {}
    assert _lp.dispatch_status(no_table, "GUTHEGA", _at(21, 26), age) == _lp.STATUS_TABLE_MISSING


def test_dispatch_malformed_values_and_other_intervals_are_skipped():
    text = (
        DISPATCH.replace(",GUTHEGA,88.27,2", ",GUTHEGA,n/a,2")
        + 'D,DISPATCH,LOCAL_PRICE,1,"2026/10/30 21:20:00",GSTONE1,-5,1\n'
        + 'D,DISPATCH,LOCAL_PRICE,1,"not a date",GSTONE2,-5,1\n'
    )
    prices = _dispatch(text)
    assert "GUTHEGA" not in prices.rows and "GSTONE1" not in prices.rows and "GSTONE2" not in prices.rows
    assert "CATHROCK" in prices.rows


def test_dispatch_without_a_price_table_is_none():
    assert _dispatch(_without(DISPATCH, "DISPATCH,PRICE")) is None


def test_constraint_client_carries_the_local_prices_on_the_dispatch_snapshot(monkeypatch):
    monkeypatch.setattr(_cc, "now_nem", lambda: _at(21, 25, 40))
    snapshot = run_async(_cc.ConstraintClient(None)._parse_off_loop(DISPATCH, DISPATCH_NAME))
    assert snapshot.interval_end == _at(21, 25) and snapshot.evaluated == 6
    assert snapshot.local_prices.rows["POR03"].adjustment_mwh == -33.92
    assert snapshot.local_prices.retrieved_at == _at(21, 25, 40)
    # parse_constraints alone, as the existing callers use it, is unchanged.
    assert _cc.parse_constraints(DISPATCH, DISPATCH_NAME).local_prices is None


# ── P5MIN ────────────────────────────────────────────────────────────────────

def test_p5min_real_run_rows_rops_and_intervals():
    run = _p5()
    assert run.run_at == _at(21, 25) and run.table_present and not run.conflicts
    assert run.interval_ends == tuple(_at(21, 25) + timedelta(minutes=5 * i) for i in range(12))
    assert len(run.rows) == 7 and all(len(series) == 12 for series in run.rows.values())
    assert run.rows["CATHROCK"][_at(21, 30)] == _lp.LocalPriceRow(-30.36, 2)
    assert run.rop["NSW1"][_at(21, 40)] == pytest.approx(102.87982)


def test_p5min_only_reads_its_own_run_and_pricing_run():
    text = (
        P5
        + 'D,P5MIN,LOCAL_PRICE,1,"2026/10/30 21:20:00",GSTONE1,"2026/10/30 21:30:00",-9,1,"2026/10/30 21:15:30"\n'
    )
    assert "GSTONE1" not in _p5(text).rows


def test_p5min_conflicting_duplicates_are_dropped_and_reported():
    """Two different rows for one DUID and interval: neither is used (#275)."""
    dup = 'D,P5MIN,LOCAL_PRICE,1,"2026/10/30 21:25:00",GUTHEGA,"2026/10/30 21:40:00",10.00,2,"2026/10/30 21:20:32"\n'
    same = 'D,P5MIN,LOCAL_PRICE,1,"2026/10/30 21:25:00",GUTHEGA,"2026/10/30 21:35:00",87.66,2,"2026/10/30 21:20:32"\n'
    run = _p5(P5 + dup + same)
    assert run.conflicts == frozenset({("GUTHEGA", _at(21, 40))})
    assert _at(21, 40) not in run.rows["GUTHEGA"]
    assert run.rows["GUTHEGA"][_at(21, 35)].adjustment_mwh == 87.66  # an identical repeat is no conflict
    series = _lp.forecast_series(run, "GUTHEGA", _at(21, 27), timedelta(minutes=15))
    assert series.status == _lp.STATUS_CONFLICT
    assert [p["time"] for p in series.points] == [
        "2026-10-30T21:25:00+10:00", "2026-10-30T21:30:00+10:00",
    ]


def test_p5min_without_a_region_table_is_none():
    assert _p5(_without(P5, "REGIONSOLUTION")) is None


def test_latest_p5_file_picks_the_newest_run():
    html = (
        '<a href="/x/PUBLIC_P5MIN_202610302120_20261030211530.zip">a</a>'
        '<a href="/x/PUBLIC_P5MIN_202610302125_20261030212034.zip">b</a>'
        '<a href="/x/PUBLIC_P5MIN_202610302125_20261030212101.zip">c</a>'
    )
    assert _lp.latest_p5_file(html) == "PUBLIC_P5MIN_202610302125_20261030212101.zip"
    assert _lp.latest_p5_file("nothing") is None


# ── Forecast series ──────────────────────────────────────────────────────────

AGE = timedelta(minutes=15)


def test_forecast_times_are_interval_starts_in_aest_and_ends_run_parallel():
    series = _lp.forecast_series(_p5(), "CATHROCK", _at(21, 22), AGE)
    assert series.status == _lp.STATUS_PUBLISHED
    assert series.points[0] == {"time": "2026-10-30T21:20:00+10:00", "value": -0.03408}
    assert series.interval_ends[0] == "2026-10-30T21:25:00+10:00"
    assert len(series.points) == len(series.interval_ends) == 12
    assert series.points[-1]["time"] == "2026-10-30T22:15:00+10:00"
    assert series.forecast_end == series.horizon_end == "2026-10-30T22:20:00+10:00"
    assert all(p["time"].endswith("+10:00") for p in series.points)


def test_forecast_drops_ended_intervals_and_expires_with_the_horizon():
    """Interval ends are exclusive: at 21:30 the interval ending 21:30 has ended."""
    run = _p5()
    series = _lp.forecast_series(run, "CATHROCK", _at(21, 30), timedelta(hours=2))
    assert series.points[0]["time"] == "2026-10-30T21:30:00+10:00" and series.intervals == 10
    expired = _lp.forecast_series(run, "CATHROCK", _at(22, 20), timedelta(hours=2))
    assert expired.status == _lp.STATUS_EXPIRED and expired.points == []


def test_an_interval_without_a_row_is_an_inferred_zero_not_a_gap():
    """AEMO publishes only non-zero rows: a missing interval in a run with the
    table is 0, flagged, and the forecast runs on to the run's last interval."""
    text = _without(P5, "GUTHEGA", '"2026/10/30 21:45:00"')
    series = _lp.forecast_series(_p5(text), "GUTHEGA", _at(21, 22), AGE)
    values = [p["value"] for p in series.points]
    assert values[:4] == [0.08828, 0.08543, 0.08766, 0.09265] and values[4] == 0.0 and len(values) == 12
    assert series.inferred == ["2026-10-30T21:40:00+10:00"]
    assert series.forecast_end == series.horizon_end == "2026-10-30T22:20:00+10:00"
    assert series.later == [] and series.status == _lp.STATUS_PUBLISHED
    assert series.published == 11 and series.intervals == 12


def test_a_duid_with_no_rows_in_the_run_is_zero_throughout():
    series = _lp.forecast_series(_p5(), "GSTONE1", _at(21, 22), AGE)
    assert series.status == _lp.STATUS_ZERO and series.published == 0
    assert [p["value"] for p in series.points] == [0.0] * 12 and len(series.inferred) == 12


def test_stale_missing_table_and_no_run_are_never_filled():
    run = _p5()
    for series, status in [
        (_lp.forecast_series(run, "CATHROCK", _at(21, 41), AGE), _lp.STATUS_STALE),
        (_lp.forecast_series(None, "CATHROCK", _at(21, 22), AGE), _lp.STATUS_NO_DATA),
        (_lp.forecast_series(_p5(_without(P5, "P5MIN,LOCAL_PRICE")), "CATHROCK", _at(21, 22), AGE), _lp.STATUS_TABLE_MISSING),
    ]:
        assert series.status == status and series.points == [] and series.inferred == []


# ── Price limits ─────────────────────────────────────────────────────────────

def test_price_limits_follow_the_financial_year_of_the_interval_start():
    # The interval ending 00:00 on 1 July is the last of June.
    assert _lp.market_price_limits(_at(0, 0, day=1, month=7)) == (-1000.0, 20300.0)
    assert _lp.market_price_limits(_at(0, 5, day=1, month=7)) == (-1000.0, 23200.0)
    # Beyond the table, the last cap listed; before it, the first.
    assert _lp.market_price_limits(_at(12, year=2031))[1] == 23200.0
    assert _lp.market_price_limits(_at(12, year=2020))[1] == 16600.0


def test_local_price_clips_the_sum_not_the_adjustment():
    end = _at(21, 25)
    assert _lp.local_price_mwh(0.01, -1000.01, end) == -1000.0
    assert _lp.local_price_mwh(-900.0, -400.0, end) == -1000.0
    assert _lp.local_price_mwh(23000.0, 500.0, end) == 23200.0
    assert _lp.local_price_mwh(97.62, 88.27, end) == pytest.approx(185.89)


# ── Client and coordinator ───────────────────────────────────────────────────

class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status, self.headers = body, status, {}

    async def text(self):
        return self.body

    async def read(self):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append(url)
        return _Resp(*self.routes[url])


async def _inline(func, *args):
    return func(*args)


def _zip(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name.replace(".zip", ".CSV"), text)
    return buf.getvalue()


def test_p5min_client_fetches_the_newest_run_once(monkeypatch):
    monkeypatch.setattr(_coord, "now_nem", lambda: _at(21, 25, 45))
    base = _const.P5MIN_BASE_URL
    listing = f'<a href="PUBLIC_P5MIN_202610302120_20261030211530.zip">a</a><a href="{P5_NAME}">b</a>'
    session = _Session({base: (listing,), base + P5_NAME: (_zip(P5_NAME, P5),)})
    client = _coord.P5MinClient(session, executor_job=_inline)
    run = run_async(client.fetch_latest())
    assert run.run_at == _at(21, 25) and run.source == P5_NAME and run.retrieved_at == _at(21, 25, 45)
    assert run_async(client.fetch_latest()) is None  # the same file is not read twice
    assert session.calls == [base, base + P5_NAME, base]


class _Dispatch:
    def __init__(self):
        self.listeners = []

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)


class _Hass:
    def __init__(self):
        self.data = {_const.DOMAIN: {}}
        self.tasks = []

    def async_create_background_task(self, coro, name):
        self.tasks.append(name)
        coro.close()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _Client:
    def __init__(self, run):
        self.run, self.calls = run, 0

    async def fetch_latest(self):
        self.calls += 1
        return self.run


def test_coordinator_fetches_once_the_run_held_reaches_its_first_interval_end(monkeypatch):
    run = _p5()
    coord = _coord.P5MinCoordinator(_Hass(), None, _Client(run))
    now = {"t": _at(21, 25, 45)}
    monkeypatch.setattr(_coord, "now_nem", lambda: now["t"])
    assert run_async(coord._async_update_data()) is run and coord.client.calls == 1
    coord.latest = replace(run, run_at=_at(21, 30))
    now["t"] = _at(21, 26)
    run_async(coord._async_update_data())
    assert coord.client.calls == 1  # the 21:30 run is held: nothing newer before 21:30
    now["t"] = _at(21, 30, 30)
    assert run_async(coord._async_update_data()).run_at == _at(21, 30)  # an older run never replaces it
    assert coord.client.calls == 2


def test_shared_p5min_created_once_and_cleaned_up():
    hass, dispatch = _Hass(), _Dispatch()
    first = _coord.shared_p5min(hass, dispatch)
    assert _coord.shared_p5min(hass, dispatch) is first
    # No first fetch on creation: the first enabled forecast sensor asks for it.
    assert hass.tasks == [] and len(dispatch.listeners) == 1
    for cleanup in hass.data[_const.DOMAIN][_const.DISPATCH_UNSUBS_KEY]:
        cleanup()
    assert _const.SHARED_P5MIN_KEY not in hass.data[_const.DOMAIN]
    assert dispatch.listeners == []


# ── Sensors ──────────────────────────────────────────────────────────────────

ENTRY = SimpleNamespace(entry_id="e1")


def _constraints(prices):
    snapshot = SimpleNamespace(local_prices=prices)
    return SimpleNamespace(tracker=SimpleNamespace(latest=snapshot))


def _duid(constraints, duid, region, p5=None, info=None):
    return _sensor.NemPd7dayDuidLocalPriceSensor(
        constraints, p5 if p5 is not None else SimpleNamespace(latest=None), ENTRY, region, duid, info,
    )


def test_adjustment_sensor_publishes_dollars_per_kwh_and_keeps_mwh(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(21, 26))
    sensor = _duid(_constraints(_dispatch()), "GUTHEGA", "NSW1")
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == pytest.approx(0.08827)
    assert sensor._attr_native_unit_of_measurement == "$/kWh"
    assert sensor._attr_unique_id == "e1_NSW1_GUTHEGA_local_price_adjustment"
    assert sensor._attr_name == "GUTHEGA Local Price Adjustment"
    assert attrs["adjustment_mwh"] == 88.27 and attrs["locally_constrained"] == 2
    assert attrs["row_status"] == "published" and attrs["source"] == "DISPATCH_LOCAL_PRICE"
    assert (attrs["interval_start"], attrs["interval_end"]) == (
        "2026-10-30T21:20:00+10:00", "2026-10-30T21:25:00+10:00",
    )
    # Age runs from the interval's start, 21:20: AEMO publishes the file
    # while the interval runs, so measured from its end it read negative.
    assert attrs["source_file"] == DISPATCH_NAME and attrs["age_seconds"] == 360
    assert sensor._attr_entity_category == _sensor.EntityCategory.DIAGNOSTIC
    assert attrs["retrieved_at"] == "2026-10-30T21:25:40+10:00"


def test_no_row_in_a_fresh_file_is_zero_and_flagged(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(21, 26))
    absent = _duid(_constraints(_dispatch()), "GSTONE1", "QLD1")
    attrs = absent.extra_state_attributes
    assert absent.native_value == 0.0 and attrs["row_status"] == "zero_by_omission"
    assert attrs["adjustment_mwh"] == 0.0 and attrs["locally_constrained"] is None
    assert attrs["local_price"] == pytest.approx(0.08525)  # the QLD1 ROP, 85.25 $/MWh


def test_stale_no_data_and_a_missing_table_stay_unknown(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(21, 45))
    stale = _duid(_constraints(_dispatch()), "GSTONE1", "QLD1")
    assert stale.native_value is None and stale.extra_state_attributes["row_status"] == "stale"
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(21, 26))
    empty = _duid(SimpleNamespace(tracker=SimpleNamespace(latest=None)), "GSTONE1", "QLD1")
    assert empty.native_value is None and empty.extra_state_attributes["row_status"] == "no_data"
    no_table = _duid(_constraints(_dispatch(_without(DISPATCH, "LOCAL_PRICE"))), "GSTONE1", "QLD1")
    assert no_table.native_value is None and no_table.extra_state_attributes["local_price"] is None


def test_the_local_price_attributes_use_rop_and_clip_the_sum(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(21, 26))
    prices = _dispatch()
    attrs = _duid(_constraints(prices), "GUTHEGA", "NSW1").extra_state_attributes
    assert attrs["local_price"] == pytest.approx(0.18589)
    assert attrs["rop_mwh"] == 97.62 and attrs["local_price_mwh"] == pytest.approx(185.89)
    assert (attrs["price_floor_mwh"], attrs["price_cap_mwh"]) == (-1000.0, 23200.0)
    vic = _duid(_constraints(prices), "DUNDWF1", "VIC1").extra_state_attributes
    assert vic["local_price"] == -1.0  # 0.01 - 1000.01 clipped to the floor
    no_rop = _duid(_constraints(replace(prices, rop={})), "GUTHEGA", "NSW1")
    assert no_rop.extra_state_attributes["local_price"] is None  # never RRP in its place
    assert no_rop.native_value == pytest.approx(0.08827)  # the adjustment itself still stands


def test_the_forecast_rides_as_attributes_with_its_own_status(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(21, 27))
    sensor = _duid(_constraints(_dispatch()), "CATHROCK", "SA1", p5=SimpleNamespace(latest=_p5()))
    attrs = sensor.extra_state_attributes
    assert sensor.native_value == pytest.approx(-0.03392)  # dispatch, not the forecast's -0.03036
    assert attrs["forecast"][0] == {"time": "2026-10-30T21:25:00+10:00", "value": -0.03036}
    assert attrs["forecast_run_at"] == "2026-10-30T21:25:00+10:00"
    assert attrs["forecast_source"] == "P5MIN_LOCAL_PRICE" and attrs["forecast_status"] == "published"
    assert (attrs["interval_minutes"], attrs["interpolation_mode"]) == (5, "previous")
    assert attrs["forecast_end"] == "2026-10-30T22:20:00+10:00" and attrs["intervals_ahead"] == 11
    assert attrs["forecast_age_seconds"] == 420  # from 21:20, the start of the run's first interval
    assert len(attrs["forecast_interval_ends"]) == len(attrs["forecast"])
    assert attrs["intervals_inferred_zero"] == 0 and attrs["inferred_zero_times"] == []
    assert sensor._unrecorded_attributes == frozenset(
        {"forecast", "forecast_interval_ends", "later_observations", "inferred_zero_times", "predispatch_forecast"}
    )


def test_a_missing_interval_is_zero_and_no_run_leaves_the_state(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(21, 27))
    gap = _without(P5, "CATHROCK", '"2026/10/30 21:30:00",-30.36')
    attrs = _duid(_constraints(_dispatch()), "CATHROCK", "SA1", p5=SimpleNamespace(latest=_p5(gap))).extra_state_attributes
    assert attrs["forecast"][0] == {"time": "2026-10-30T21:25:00+10:00", "value": 0.0}
    assert attrs["inferred_zero_times"] == ["2026-10-30T21:25:00+10:00"] and attrs["intervals_inferred_zero"] == 1
    empty = _duid(_constraints(_dispatch()), "CATHROCK", "SA1")
    assert empty.extra_state_attributes["forecast_status"] == "no_data"
    assert empty.extra_state_attributes["row_status"] == "published"
    assert empty.native_value == pytest.approx(-0.03392)


def test_one_sensor_per_duid():
    constraints, p5 = _constraints(None), SimpleNamespace(latest=None)
    entities = _sensor.local_price_entities(constraints, p5, ENTRY, "QLD1", ["GSTONE1", "CATHROCK"])
    assert [e._attr_unique_id for e in entities] == [
        "e1_QLD1_GSTONE1_local_price_adjustment", "e1_QLD1_CATHROCK_local_price_adjustment",
    ]
    assert {e._attr_entity_category for e in entities} == {_sensor.EntityCategory.DIAGNOSTIC}


def test_age_is_not_negative_while_the_interval_runs(monkeypatch):
    """Live, 31 Oct 2026: the file for the interval ending 06:40 was read at
    06:36:16 and published an age of -224 seconds; from the start it is 76."""
    prices = replace(_dispatch(), interval_end=_at(6, 40, day=31))
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(6, 36, 16, day=31))
    sensor = _duid(_constraints(prices), "CATHROCK", "SA1")
    assert sensor.extra_state_attributes["age_seconds"] == 76
