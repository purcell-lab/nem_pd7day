"""
Tests for the pre-dispatch feed: predispatch_client.py (parser, file listing,
client), predispatch_coordinator.py (when a run is fetched, shared creation)
and predispatch_sensor.py.

The fixture is the real PredispatchIS run written at 06:01 NEM time on 13 Oct
2026, trimmed to three periods (ending 11:00, 13:00 and 18:00) and to the four
tables the integration reads; headers and rows are unchanged from NEMWEB. At
11:00 QNI (NSW1-QLD1) is held at its import limit, flowing 612 MW to NSW, and
QLD is priced below NSW; at 18:00 it is not at a limit and QLD follows NSW.

Run with:  python -m pytest tests/test_predispatch.py -v
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const, _cc, _client, _coord, _sensor = load_chain(
    "const", "constraint_client", "predispatch_client", "predispatch_coordinator", "predispatch_sensor"
)

FIXTURE = next((Path(__file__).parent / "fixtures" / "predispatchis").glob("PUBLIC_PREDISPATCHIS_*.zip"))


def _at(hour, minute=0):
    return datetime(2026, 10, 13, hour, minute, tzinfo=NEM_TZ)


def _snapshot():
    return _client.parse_predispatch(_cc.unzip_csv(FIXTURE.read_bytes()), FIXTURE.name)


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parse_real_run():
    snap = _snapshot()
    assert snap.run_at == datetime(2026, 10, 13, 6, 1, 35, tzinfo=NEM_TZ)
    assert snap.source == FIXTURE.name
    assert snap.period_ends == [_at(11), _at(13), _at(18)]
    assert sorted(snap.interconnectors) == [
        "N-Q-MNSP1", "NSW1-QLD1", "NSW1-SA1", "T-V-MNSP1", "V-S-MNSP1", "V-SA", "VIC1-NSW1",
    ]
    assert snap.evaluated == 3097
    assert snap.prices["QLD1"][_at(18)] == pytest.approx(986.11)
    assert snap.prices["NSW1"][_at(11)] == pytest.approx(166.67)


def test_qni_bound_at_import_limit_midday_only():
    qni = {p.interval_end: p for p in _snapshot().interconnectors["NSW1-QLD1"]}
    midday, evening = qni[_at(11)], qni[_at(18)]
    assert midday.mwflow == pytest.approx(-612.05611)
    assert (midday.bound, midday.limit_constraint) == ("import", "F_STHN++86_R60")
    assert (evening.bound, evening.limit_constraint) == (None, None)
    assert evening.importlimit == pytest.approx(-1179)


def test_constraints_per_period_largest_cost_first():
    cons = _snapshot().constraints
    evening = cons[_at(18)]
    assert [c.constraint_id for c in evening[:2]] == ["SN_ZERO", "N::N_CNCW_2"]
    assert evening[1].marginal_value == pytest.approx(-1044.87)
    costs = [abs(c.marginal_value) for c in cons[_at(11)]]
    assert costs == sorted(costs, reverse=True)


@pytest.mark.parametrize(
    "flow, export, imp, bound",
    [
        (100.0, 100.4, -500.0, "export"),
        (-499.0, 300.0, -500.0, "import"),       # within 0.5 % of 500 MW
        (-490.0, 300.0, -500.0, None),
        (0.5, 1.0, -1.0, "export"),               # the 1 MW floor
        (10.0, None, None, None),
        (0.0, 0.0, 0.0, "closed"),               # NSW1-SA1 held by NS_ZERO / SN_ZERO
        (0.4, 0.5, -0.5, "closed"),              # meeting within the 1 MW floor of zero
        (71.53924, 71.53924, 71.53922, "pinned"),  # Murraylink held by V^SML_BUDP_3 (#243)
        (-120.0, -120.0, -120.0, "pinned"),     # pinned in the import direction
    ],
)
def test_bound_tolerance(flow, export, imp, bound):
    p = _client.InterconnectorPeriod(_at(11), flow, export, imp, None, None, "E", "I")
    assert p.bound == bound
    assert p.limit_constraint == {"export": "E", "import": "I", "closed": "E", "pinned": "E", None: None}[bound]


def test_parse_reads_headers_and_skips_intervention():
    csv = "\n".join([
        "C,NEMP.WORLD,PREDISPATCHIS",
        "I,PREDISPATCH,CASE_SOLUTION,1,LASTCHANGED,INTERVENTION",
        'D,PREDISPATCH,CASE_SOLUTION,1,"2026/10/13 06:01:35",0',
        "I,PREDISPATCH,INTERCONNECTOR_SOLN,3,INTERVENTION,INTERCONNECTORID,DATETIME,MWFLOW,EXPORTLIMIT,IMPORTLIMIT",
        'D,PREDISPATCH,INTERCONNECTOR_SOLN,3,0,NSW1-QLD1,"2026/10/13 11:00:00",-10,50,-100',
        'D,PREDISPATCH,INTERCONNECTOR_SOLN,3,1,NSW1-QLD1,"2026/10/13 11:30:00",-99,50,-100',
        "I,PREDISPATCH,CONSTRAINT_SOLUTION,5,CONSTRAINTID,DATETIME,MARGINALVALUE,VIOLATIONDEGREE,INTERVENTION",
        'D,PREDISPATCH,CONSTRAINT_SOLUTION,5,N>NIL_1,"2026/10/13 11:00:00",-12.5,0,0',
        'D,PREDISPATCH,CONSTRAINT_SOLUTION,5,N>NIL_2,"2026/10/13 11:00:00",0,0,0',
        'D,PREDISPATCH,CONSTRAINT_SOLUTION,5,N>NIL_3,"2026/10/13 11:00:00",-99,0,1',
    ])
    snap = _client.parse_predispatch(csv)
    assert [p.interval_end for p in snap.interconnectors["NSW1-QLD1"]] == [_at(11)]
    assert [c.constraint_id for c in snap.constraints[_at(11)]] == ["N>NIL_1"]
    assert snap.evaluated == 2


def test_parse_without_interconnectors_is_none():
    assert _client.parse_predispatch("C,NEMP.WORLD\nI,PREDISPATCH,REGION_PRICES,2,X\n") is None


def test_run_time_from_file_name_without_case_solution():
    csv = (
        "I,PREDISPATCH,INTERCONNECTOR_SOLN,3,INTERCONNECTORID,DATETIME,MWFLOW\n"
        'D,PREDISPATCH,INTERCONNECTOR_SOLN,3,V-SA,"2026/10/13 11:00:00",5\n'
    )
    assert _client.parse_predispatch(csv, FIXTURE.name).run_at == datetime(2026, 10, 13, 6, 1, 42, tzinfo=NEM_TZ)
    assert _client.parse_predispatch(csv, "unnamed") is None


def test_latest_file_picks_newest_run():
    older = "PUBLIC_PREDISPATCHIS_202610130600_20261013053142.zip"
    html = f'<a href="/x/{FIXTURE.name}">x</a> <a href="/x/{older}">y</a>'
    assert _client.latest_file(html) == FIXTURE.name
    assert _client.latest_file("<html></html>") is None


# ── Client ───────────────────────────────────────────────────────────────────

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


async def _no_sleep(_):
    return None


async def _inline(func, *args):
    return func(*args)


def test_client_fetches_newest_then_skips_it():
    base = _const.PREDISPATCHIS_BASE_URL
    session = _Session({
        base: (f'<a href="{FIXTURE.name}">{FIXTURE.name}</a>',),
        base + FIXTURE.name: (FIXTURE.read_bytes(),),
    })
    client = _client.PredispatchClient(session, executor_job=_inline, sleep=_no_sleep)
    snap = run_async(client.fetch_latest())
    assert snap.source == FIXTURE.name and len(snap.interconnectors) == 7
    assert run_async(client.fetch_latest()) is None
    assert session.calls == [base, base + FIXTURE.name, base]


def test_client_without_interconnector_rows_is_none():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.CSV", "C,NEMP.WORLD\n")
    base = _const.PREDISPATCHIS_BASE_URL
    session = _Session({base: (f'<a href="{FIXTURE.name}">x</a>',), base + FIXTURE.name: (buf.getvalue(),)})
    client = _client.PredispatchClient(session, executor_job=_inline, sleep=_no_sleep)
    assert run_async(client.fetch_latest()) is None
    assert client.last_file is None


def test_dispatch_client_hooks_unchanged():
    client = _cc.ConstraintClient(None)
    assert (client.LABEL, client.BASE_URL) == ("DispatchIS", _const.DISPATCHIS_BASE_URL)
    assert client._latest('<a href="PUBLIC_DISPATCHIS_202610071050_1.zip">') == "PUBLIC_DISPATCHIS_202610071050_1.zip"


# ── Coordinator and shared creation ──────────────────────────────────────────

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


class _CountingClient:
    def __init__(self, snapshot):
        self.snapshot, self.calls = snapshot, 0

    async def fetch_latest(self):
        self.calls += 1
        return self.snapshot


def test_coordinator_fetches_only_when_a_new_run_is_due(monkeypatch):
    client = _CountingClient(_snapshot())
    coord = _coord.PredispatchCoordinator(_Hass(), None, client)
    monkeypatch.setattr(_coord, "now_nem", lambda: _at(6, 10))
    first = run_async(coord._async_update_data())
    assert first is client.snapshot and client.calls == 1
    run_async(coord._async_update_data())               # 06:10: the 06:01 run is current
    assert client.calls == 1
    monkeypatch.setattr(_coord, "now_nem", lambda: _at(6, 36))
    assert run_async(coord._async_update_data()) is first
    assert client.calls == 2                             # due, but nothing newer: kept


def test_dispatch_notification_starts_one_refresh_at_a_time():
    hass, dispatch = _Hass(), _Dispatch()
    coord = _coord.PredispatchCoordinator(hass, dispatch, _CountingClient(None))
    dispatch.listeners[0]()
    dispatch.listeners[0]()
    assert hass.tasks == ["nem_pd7day_predispatch_refresh"]
    coord.async_shutdown_listener()
    assert dispatch.listeners == []


def test_shared_predispatch_created_once_and_cleaned_up():
    hass, dispatch = _Hass(), _Dispatch()
    first = _coord.shared_predispatch(hass, dispatch)
    assert _coord.shared_predispatch(hass, dispatch) is first
    assert hass.tasks == ["nem_pd7day_predispatch_first"]
    for cleanup in hass.data[_const.DOMAIN][_const.DISPATCH_UNSUBS_KEY]:
        cleanup()
    assert _const.SHARED_PREDISPATCH_KEY not in hass.data[_const.DOMAIN]
    assert dispatch.listeners == []


# ── Sensors ──────────────────────────────────────────────────────────────────

def _coordinator(snapshot, run_at=None):
    """A coordinator holding the fixture; run_at moves its solve time so a
    later hour still reads as a fresh run."""
    coord = _coord.PredispatchCoordinator(_Hass(), None, _CountingClient(None))
    coord.latest = replace(snapshot, run_at=run_at) if snapshot is not None and run_at else snapshot
    return coord


def test_interconnector_sensor_current_period_and_forecast(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(7))
    sensor = _sensor.NemPd7dayPredispatchInterconnectorSensor(
        _coordinator(_snapshot()), SimpleNamespace(entry_id="e1"), "QLD1", "NSW1-QLD1"
    )
    attrs = sensor.extra_state_attributes
    assert sensor.available and sensor.native_value == pytest.approx(-612.05611)
    assert (attrs["from_region"], attrs["to_region"]) == ("NSW1", "QLD1")
    assert (attrs["time"], attrs["nemtime"]) == ("2026-10-13T10:30:00+10:00", "2026-10-13T11:00:00+10:00")
    assert (attrs["bound"], attrs["limit_constraint"]) == ("import", "F_STHN++86_R60")
    assert attrs["run_at"] == "2026-10-13T06:01:35+10:00"
    evening = attrs["forecast"][-1]
    assert evening["bound"] is None
    assert (evening["rrp_from"], evening["rrp_to"]) == (pytest.approx(1079.32653), pytest.approx(986.11))
    assert sensor._attr_unique_id == "nem_pd7day_qld1_predispatch_ic_nsw1_qld1"
    assert sensor._unrecorded_attributes == frozenset({"forecast"})


def test_interconnector_sensor_drops_elapsed_periods(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(12))
    sensor = _sensor.NemPd7dayPredispatchInterconnectorSensor(
        _coordinator(_snapshot(), _at(11, 31)), SimpleNamespace(entry_id="e1"), "QLD1", "NSW1-QLD1"
    )
    assert [p["nemtime"] for p in sensor.extra_state_attributes["forecast"]] == [
        "2026-10-13T13:00:00+10:00", "2026-10-13T18:00:00+10:00",
    ]


def test_constraints_sensor_counts_region_and_skips_artefacts(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(17, 45))
    sensor = _sensor.NemPd7dayPredispatchConstraintsSensor(
        _coordinator(_snapshot(), _at(17, 31)), SimpleNamespace(entry_id="e1"), "NSW1"
    )
    attrs = sensor.extra_state_attributes
    ids = [c["constraint_id"] for c in attrs["constraints"]]
    assert ids[:2] == ["SN_ZERO", "N::N_CNCW_2"]
    assert sensor.native_value == len(ids) == attrs["count"]
    # SN_ZERO's $8.35M penalty is listed, but the largest priced value is N::N_CNCW_2.
    assert attrs["max_marginal_value_mwh"] == pytest.approx(1044.87)
    assert attrs["rrp"] == pytest.approx(1079.32653)
    assert attrs["forecast"][0]["top"][0] == "N::N_CNCW_2"
    assert sensor._unrecorded_attributes == frozenset({"constraints", "unassigned_constraints", "forecast"})


def _with_ca_bris(snapshot, end):
    """The fixture run with the 16 Oct 2026 CA_BRIS constraint added to one
    period: binding at about half its dispatch RHS, at -$32,000/MWh (#255)."""
    ca = _cc.BindingConstraint("CA_BRIS_59E3F2B6_1", rhs=1039.0, lhs=1039.0, marginal_value=-32000.0, violation_degree=0.0)
    constraints = dict(snapshot.constraints)
    constraints[end] = constraints.get(end, ()) + (ca,)
    return replace(snapshot, constraints=constraints)


def _dispatch_holding(rhs, interval_end):
    """A stand-in for the shared dispatch ConstraintCoordinator."""
    ca = _cc.BindingConstraint("CA_BRIS_59E3F2B6_1", rhs=rhs, lhs=rhs, marginal_value=-1200.0, violation_degree=0.0)
    latest = SimpleNamespace(interval_end=interval_end, constraints=(ca,))
    return SimpleNamespace(tracker=SimpleNamespace(latest=latest))


def test_constraint_automation_is_listed_as_unassigned(monkeypatch):
    """CA_BRIS names no region, so no region's list showed it while it priced
    QLD1 at the cap; it is now published as unassigned, as dispatch does (#255)."""
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(10, 40))
    snapshot = _with_ca_bris(_snapshot(), _at(11))
    plain = _sensor.NemPd7dayPredispatchConstraintsSensor(
        _coordinator(_snapshot(), _at(10, 1)), SimpleNamespace(entry_id="e1"), "QLD1"
    )
    sensor = _sensor.NemPd7dayPredispatchConstraintsSensor(
        _coordinator(snapshot, _at(10, 1)), SimpleNamespace(entry_id="e1"), "QLD1",
        _dispatch_holding(2557.0, _at(10, 35)),
    )
    attrs = sensor.extra_state_attributes
    # Not guessed into the region: the count and the region list are unchanged.
    assert sensor.native_value == plain.native_value
    assert "CA_BRIS_59E3F2B6_1" not in [c["constraint_id"] for c in attrs["constraints"]]
    (ca,) = attrs["unassigned_constraints"]
    assert (ca["constraint_id"], ca["category"], ca["regions"]) == ("CA_BRIS_59E3F2B6_1", "constraint_automation", [])
    assert ca["rhs_vs_dispatch"] == pytest.approx(0.406)
    assert attrs["max_unassigned_marginal_value_mwh"] == 32000.0
    first = attrs["forecast"][0]
    assert first["max_unassigned_marginal_value_mwh"] == 32000.0
    assert first["unassigned"] == [{
        "constraint_id": "CA_BRIS_59E3F2B6_1", "marginal_value_mwh": -32000.0, "rhs": 1039.0,
        "violation_degree": 0.0, "rhs_vs_dispatch": pytest.approx(0.406),
    }]
    # Periods without it carry an empty list and a zero, not a missing key.
    assert attrs["forecast"][1]["unassigned"] == [] and attrs["forecast"][1]["max_unassigned_marginal_value_mwh"] == 0.0
    assert "unassigned" not in attrs


def test_rhs_vs_dispatch_needs_a_recent_dispatch_interval(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(10, 40))
    snapshot = _with_ca_bris(_snapshot(), _at(11))
    for dispatch in (None, _dispatch_holding(2557.0, _at(10, 20)), _dispatch_holding(0.0, _at(10, 35))):
        sensor = _sensor.NemPd7dayPredispatchConstraintsSensor(
            _coordinator(snapshot, _at(10, 1)), SimpleNamespace(entry_id="e1"), "QLD1", dispatch,
        )
        assert sensor.extra_state_attributes["unassigned_constraints"][0]["rhs_vs_dispatch"] is None


def test_constraints_sensor_qld_midday(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(10, 40))
    sensor = _sensor.NemPd7dayPredispatchConstraintsSensor(
        _coordinator(_snapshot(), _at(10, 1)), SimpleNamespace(entry_id="e1"), "QLD1"
    )
    first = sensor.extra_state_attributes["forecast"][0]
    assert first["top"][:2] == ["Q>NIL_WOPW_WOSP", "Q>NIL_CPWU_CLWU"]
    assert first["max_marginal_value_mwh"] == pytest.approx(182.36961)


def test_sensors_unavailable_when_stale_or_empty(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: _at(7, 20))     # run solved 06:01, 79 min ago
    entry = SimpleNamespace(entry_id="e1")
    for coord in (_coordinator(_snapshot()), _coordinator(None)):
        ic = _sensor.NemPd7dayPredispatchInterconnectorSensor(coord, entry, "QLD1", "NSW1-QLD1")
        cons = _sensor.NemPd7dayPredispatchConstraintsSensor(coord, entry, "QLD1")
        assert not ic.available and ic.native_value is None and "forecast" not in ic.extra_state_attributes
        assert not cons.available and cons.native_value is None and "forecast" not in cons.extra_state_attributes


def test_interconnector_ends_cover_region_map():
    mapped = set().union(*_const.REGION_INTERCONNECTORS.values())
    assert set(_const.INTERCONNECTOR_ENDS) == mapped
    for region, signs in _const.REGION_INTERCONNECTOR_SIGN.items():
        for ic, sign in signs.items():
            assert _const.INTERCONNECTOR_ENDS[ic][1 if sign > 0 else 0] == region


def test_energyconnect_reads_closed_in_the_real_run():
    pec = _snapshot().interconnectors["NSW1-SA1"]
    assert {(p.mwflow, p.bound, p.limit_constraint, p.import_constraint) for p in pec} == {
        (0.0, "closed", "NS_ZERO", "SN_ZERO")
    }


def test_murraylink_held_at_a_flow_reads_pinned_not_closed_in_the_real_run():
    """#243: the limits meet at a non-zero flow, so the link carries exactly
    that flow. "closed" is kept for limits meeting at 0 MW."""
    murraylink = _snapshot().interconnectors["V-S-MNSP1"]
    pinned = [p for p in murraylink if p.bound == "pinned"]
    assert pinned, "the fixture has Murraylink pinned by V^SML_BUDP_3"
    for p in pinned:
        assert p.mwflow == pytest.approx(p.exportlimit, abs=1.0) and abs(p.mwflow) > 1.0
        assert p.limit_constraint == "V^SML_BUDP_3"
    assert all(p.bound != "closed" for p in murraylink)


def test_period_is_thirty_minutes():
    assert _client.PERIOD == timedelta(minutes=30)
