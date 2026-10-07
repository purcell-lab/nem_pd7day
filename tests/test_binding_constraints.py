"""
Tests for binding constraints: constraint_client.py (ID classifier, DispatchIS
parser, client), constraint_coordinator.py (run tracker, shared creation) and
constraint_sensor.py.

The fixtures are five consecutive real DispatchIS files, intervals ending
10:30 to 10:50 NEM time on 7 Oct 2026, unchanged from NEMWEB.

Run with:  python -m pytest tests/test_binding_constraints.py -v
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const, _client, _coord, _sensor = load_chain(
    "const", "constraint_client", "constraint_coordinator", "constraint_sensor"
)

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "dispatchis").glob("PUBLIC_DISPATCHIS_*.zip"))
describe = _client.describe_constraint


def _snapshot(path: Path):
    return _client.parse_constraints(_client.unzip_csv(path.read_bytes()), path.name)


# ── Constraint IDs ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "cid, regions, category, cause, co_opt, nil",
    [
        # Every pattern below is binding or present in the fixtures.
        ("N>NIL_969", ("NSW1",), "network", "thermal", False, True),
        ("N>>6CGH_060_051", ("NSW1",), "network", "thermal", True, False),
        ("V^^V_NIL_KGTS", ("VIC1",), "network", "voltage_stability", True, True),
        ("Q:NIL_CS", ("QLD1",), "network", "stability", False, True),
        ("T::T_NIL_1", ("TAS1",), "network", "stability", True, True),
        ("SVML^NIL_MH-CAP_ON", ("VIC1", "SA1"), "interconnector", "voltage_stability", False, True),
        ("TVBL_NIL_CONT_478", ("VIC1", "TAS1"), "interconnector", None, False, True),
        ("NQTE_ROC", ("QLD1", "NSW1"), "interconnector", None, False, False),
        ("NS_150_DYN_TEST", ("NSW1", "SA1"), "interconnector", None, False, False),
        ("N_V_HV1700_OSC_AUTO", ("NSW1", "VIC1"), "interconnector", None, False, False),
        ("NRM_VIC1_SA1", ("VIC1", "SA1"), "negative_residue", None, False, False),
        ("F_MAIN++APD_TL_L1", ("QLD1", "NSW1", "VIC1", "SA1"), "fcas", "frequency_control", True, False),
        ("F_I+BIP_ML_L6", ("QLD1", "NSW1", "VIC1", "SA1", "TAS1"), "fcas", "frequency_control", False, False),
        ("F_T++NIL_MG_R6", ("TAS1",), "fcas", "frequency_control", True, True),
        ("F_TASCAP_RREG_0220", ("TAS1",), "fcas", None, False, False),
        ("F_Q++NIL_R1", ("QLD1",), "fcas", "frequency_control", True, True),
        ("#QLD1_E_20261003", ("QLD1",), "quick", None, False, False),
        ("DATASNAP_DFS_LS", (), "data_snapshot", None, False, False),
        # No documented convention: published as unassigned, not guessed.
        ("L_PEC_X_6C_6G_6H", (), "other", None, False, False),
        ("C_N_NESBESS_150_G", (), "other", None, False, False),
    ],
)
def test_describe_constraint(cid, regions, category, cause, co_opt, nil):
    info = describe(cid)
    assert (info.regions, info.category, info.cause, info.co_optimised, info.system_normal) == (
        regions, category, cause, co_opt, nil,
    )


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_parse_real_file_counts_and_order():
    snap = _snapshot(FIXTURES[-1])
    assert snap.interval_end == datetime(2026, 10, 7, 10, 50, tzinfo=NEM_TZ)
    assert snap.run_no == 1
    assert snap.evaluated == 1105
    assert len(snap.constraints) == 25
    # Largest cost first, AEMO's sign kept.
    assert snap.constraints[0].constraint_id == "Q>NIL_CPWU_CLWU"
    assert snap.constraints[0].marginal_value == pytest.approx(-1198.5135)
    costs = [abs(c.marginal_value) for c in snap.constraints]
    assert costs == sorted(costs, reverse=True)


def test_parse_reads_columns_by_header_and_skips_intervention():
    csv = "\n".join([
        "C,NEMP.WORLD,DISPATCHIS",
        # Columns reordered against the real file: positions must not matter.
        "I,DISPATCH,CONSTRAINT,5,CONSTRAINTID,INTERVENTION,SETTLEMENTDATE,RUNNO,MARGINALVALUE,VIOLATIONDEGREE,RHS,LHS",
        'D,DISPATCH,CONSTRAINT,5,N>NIL_1,0,"2026/10/07 10:50:00",1,-12.5,0,100,100',
        'D,DISPATCH,CONSTRAINT,5,N>NIL_2,0,"2026/10/07 10:50:00",1,0,0,50,20',
        'D,DISPATCH,CONSTRAINT,5,N>NIL_3,1,"2026/10/07 10:50:00",1,-99,0,50,50',
        'D,DISPATCH,CONSTRAINT,5,V>NIL_4,0,"2026/10/07 10:50:00",1,0,3.5,10,13.5',
    ])
    snap = _client.parse_constraints(csv)
    assert [c.constraint_id for c in snap.constraints] == ["N>NIL_1", "V>NIL_4"]
    assert snap.evaluated == 3
    assert snap.constraints[1].violation_degree == 3.5


def test_parse_without_constraint_rows_is_none():
    assert _client.parse_constraints("C,NEMP.WORLD\nI,DISPATCH,PRICE,5,X\n") is None


def test_latest_file_picks_newest_interval():
    html = " ".join(f'<a href="/x/{p.name}">{p.name}</a>' for p in reversed(FIXTURES))
    assert _client.latest_file(html) == FIXTURES[-1].name
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


def test_client_fetches_newest_then_skips_it():
    base = _const.DISPATCHIS_BASE_URL
    name = FIXTURES[-1].name
    session = _Session({
        base: (f'<a href="{name}">{name}</a> <a href="{FIXTURES[0].name}">x</a>',),
        base + name: (FIXTURES[-1].read_bytes(),),
    })
    client = _client.ConstraintClient(session, sleep=_no_sleep)
    snap = run_async(client.fetch_latest())
    assert snap.source == name and len(snap.constraints) == 25
    assert session.calls == [base, base + name]
    # Same newest file: one listing request, nothing new.
    assert run_async(client.fetch_latest()) is None
    assert session.calls == [base, base + name, base]


def test_client_zip_not_published_is_none_and_retried_next_time():
    base = _const.DISPATCHIS_BASE_URL
    name = FIXTURES[-1].name
    session = _Session({base: (f'<a href="{name}">{name}</a>',), base + name: (b"", 404)})
    client = _client.ConstraintClient(session, sleep=_no_sleep)
    assert run_async(client.fetch_latest()) is None
    assert client.last_file is None


# ── Tracker ──────────────────────────────────────────────────────────────────

def test_tracker_runs_previous_value_and_minutes():
    tracker = _coord.ConstraintTracker()
    for path in FIXTURES:
        assert tracker.add(_snapshot(path))
    assert tracker.tracked_since == datetime(2026, 10, 7, 10, 25, tzinfo=NEM_TZ)
    latest = {c.constraint_id: c for c in tracker.latest.constraints}
    whole = tracker.row(latest["N>NIL_969"])
    assert whole["first_bound"] == "2026-10-07T10:25:00+10:00"
    assert whole["bound_minutes_today"] == 25
    assert whole["previous_marginal_value_mwh"] == pytest.approx(-1064.30258)
    # Bound at 10:30, released for three intervals, bound again at 10:50: a
    # new run with no previous value, but both intervals count for the day.
    again = tracker.row(latest["V^^V_NIL_KGTS"])
    assert again["first_bound"] == "2026-10-07T10:45:00+10:00"
    assert (again["bound_minutes_today"], again["previous_marginal_value_mwh"]) == (10, None)


def test_tracker_ignores_repeat_and_older_interval():
    tracker = _coord.ConstraintTracker()
    tracker.add(_snapshot(FIXTURES[1]))
    assert not tracker.add(_snapshot(FIXTURES[1]))
    assert not tracker.add(_snapshot(FIXTURES[0]))
    assert tracker.runs["N>NIL_969"].intervals_today == 1


def test_tracker_gap_restarts_run_but_keeps_day_total():
    tracker = _coord.ConstraintTracker()
    tracker.add(_snapshot(FIXTURES[0]))
    tracker.add(_snapshot(FIXTURES[2]))               # 10:35 missed
    row = tracker.row({c.constraint_id: c for c in tracker.latest.constraints}["N>NIL_969"])
    assert row["first_bound"] == "2026-10-07T10:35:00+10:00"
    assert row["previous_marginal_value_mwh"] is None
    assert row["bound_minutes_today"] == 10


def test_tracker_resets_minutes_at_nem_midnight():
    tracker = _coord.ConstraintTracker()
    snap = _snapshot(FIXTURES[0])
    last = replace(snap, interval_end=datetime(2026, 10, 8, 0, 0, tzinfo=NEM_TZ))
    first = replace(snap, interval_end=datetime(2026, 10, 8, 0, 5, tzinfo=NEM_TZ))
    tracker.add(last)                                  # 23:55 to 00:00 belongs to 7 Oct
    tracker.add(first)
    run = tracker.runs["N>NIL_969"]
    assert (run.intervals_today, run.first_start) == (1, datetime(2026, 10, 7, 23, 55, tzinfo=NEM_TZ))


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


class _ListClient:
    def __init__(self, paths):
        self.paths = list(paths)

    async def fetch_latest(self):
        return _snapshot(self.paths.pop(0)) if self.paths else None


def test_coordinator_keeps_last_interval_when_nothing_new():
    coord = _coord.ConstraintCoordinator(_Hass(), None, _ListClient(FIXTURES[:1]))
    first = run_async(coord._async_update_data())
    assert run_async(coord._async_update_data()) is first


def test_dispatch_notification_starts_one_refresh_at_a_time():
    hass, dispatch = _Hass(), _Dispatch()
    coord = _coord.ConstraintCoordinator(hass, dispatch, _ListClient([]))
    dispatch.listeners[0]()
    dispatch.listeners[0]()                            # still running: dropped
    assert hass.tasks == ["nem_pd7day_binding_constraints_refresh"]
    coord.async_shutdown_listener()
    assert dispatch.listeners == []


def test_shared_constraints_created_once_and_cleaned_up():
    hass, dispatch = _Hass(), _Dispatch()
    first = _coord.shared_constraints(hass, dispatch)
    assert _coord.shared_constraints(hass, dispatch) is first
    assert len(dispatch.listeners) == 1
    assert hass.tasks == ["nem_pd7day_binding_constraints_first"]
    for cleanup in hass.data[_const.DOMAIN][_const.DISPATCH_UNSUBS_KEY]:
        cleanup()
    assert _const.SHARED_CONSTRAINTS_KEY not in hass.data[_const.DOMAIN]
    assert dispatch.listeners == []


# ── Sensor ───────────────────────────────────────────────────────────────────

def _sensor_for(region, paths):
    coord = _coord.ConstraintCoordinator(_Hass(), None, _ListClient(paths))
    for _ in paths:
        run_async(coord._async_update_data())
    return _sensor.NemPd7dayBindingConstraintsSensor(coord, SimpleNamespace(entry_id="e1"), region)


def test_sensor_splits_region_fcas_and_unassigned(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: datetime(2026, 10, 7, 10, 52, tzinfo=NEM_TZ))
    sensor = _sensor_for("SA1", FIXTURES)
    attrs = sensor.extra_state_attributes
    ids = [c["constraint_id"] for c in attrs["constraints"]]
    assert ids == ["S>NIL_MHNW1_MHNW2", "NRM_VIC1_SA1"]
    assert sensor.native_value == 2 and sensor.available
    assert all("SA1" in c["regions"] for c in attrs["fcas_constraints"])
    assert [c["constraint_id"] for c in attrs["unassigned_constraints"]] == ["L_PEC_X_6C_6G_6H"]
    assert (attrs["binding_nem"], attrs["evaluated_nem"]) == (25, 1105)
    assert sensor._unrecorded_attributes == frozenset(
        {"constraints", "fcas_constraints", "unassigned_constraints"}
    )


def test_sensor_unavailable_when_stale_or_empty(monkeypatch):
    monkeypatch.setattr(_sensor, "now_nem", lambda: datetime(2026, 10, 7, 11, 6, tzinfo=NEM_TZ))
    stale = _sensor_for("NSW1", FIXTURES)
    assert not stale.available and stale.native_value is None
    assert "constraints" not in stale.extra_state_attributes
    empty = _sensor_for("NSW1", [])
    assert not empty.available and empty.native_value is None
    assert empty.extra_state_attributes["tracked_since"] is None


def test_interval_constant_is_five_minutes():
    assert _client.INTERVAL == timedelta(minutes=5)
