"""price_sources.py: typed price source snapshots, each with an identity (#335).

Later stages key their caches on ``identity`` = (kind, source, run_at), so it
must be the same for the same data and differ for a different run or file.
The DispatchIS parse also keeps each region's RRP now, for the dispatch
snapshot; P5MIN's RRP is covered in test_local_price.
"""
from __future__ import annotations

import ast
import csv
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from support import NEM_TZ, PKG_DIR, install_ha_stubs, load_chain

install_ha_stubs()

_const, _ps, _lp, _cc, _pdc = load_chain("const", "price_sources", "local_price", "constraint_client", "predispatch_client")

_DIR = Path(__file__).parent / "fixtures" / "local_price"
DISPATCH_NAME = "PUBLIC_DISPATCHIS_202610302125_0000000545750856.zip"
DISPATCH = (_DIR / "PUBLIC_DISPATCHIS_202610302125_0000000545750856.CSV").read_text()
P5_NAME = "PUBLIC_P5MIN_202610302125_20261030212034.zip"
P5 = (_DIR / "PUBLIC_P5MIN_202610302125_20261030212034.CSV").read_text()
PD_NAME = "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.zip"
PD = (_DIR / "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.CSV").read_text()


def _at(hour, minute=0, day=30):
    return datetime(2026, 10, day, hour, minute, tzinfo=NEM_TZ)


def _dispatch():
    return _lp.parse_dispatch_local_prices(DISPATCH, DISPATCH_NAME, _at(21, 25))


def _p5():
    return _lp.parse_p5min_local_prices(P5, P5_NAME, _at(21, 25))


# ── The module stays in the domain layer ─────────────────────────────────────

def test_price_sources_imports_nothing_from_home_assistant_or_the_package():
    tree = ast.parse((Path(PKG_DIR) / "price_sources.py").read_text())
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("homeassistant") for m in imported)
    assert not any(n.level for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)), "a domain leaf"


def test_the_moved_types_are_the_ones_local_price_exports():
    assert _lp.P5MinRun is _ps.P5MinRun and _lp.LocalPriceRow is _ps.LocalPriceRow


# ── Identities ───────────────────────────────────────────────────────────────

def test_a_p5min_run_is_identified_by_its_file_and_run():
    run = _p5()
    assert run.identity == ("p5min", P5_NAME, _at(21, 25))
    assert run.identity.kind == _ps.KIND_P5MIN
    assert replace(run, retrieved_at=_at(21, 26)).identity == run.identity, "fetch time is not identity"
    assert replace(run, run_at=_at(21, 30)).identity != run.identity


def test_a_predispatch_snapshot_is_identified_by_its_file_and_run():
    snapshot = _pdc.parse_predispatch(PD, PD_NAME)
    assert snapshot.identity == ("predispatch", PD_NAME, snapshot.run_at)
    assert snapshot.run_at.tzinfo is not None


def test_the_dispatch_snapshot_keeps_each_regions_rrp_and_rop():
    """Read independently from the fixture's DISPATCH,PRICE rows, pricing run only."""
    header = None
    want_rrp, want_rop = {}, {}
    for row in csv.reader(DISPATCH.splitlines()):
        if row[:3] == ["I", "DISPATCH", "PRICE"]:
            header = row
        elif row[:3] == ["D", "DISPATCH", "PRICE"]:
            rec = dict(zip(header, row))
            if rec["INTERVENTION"] == "0":
                want_rrp[rec["REGIONID"]] = float(rec["RRP"])
                want_rop[rec["REGIONID"]] = float(rec["ROP"])
    prices = _dispatch()
    assert prices.rrp == want_rrp and prices.rop == want_rop
    snap = _ps.DispatchSnapshot.of(prices)
    assert snap.rrp == want_rrp and snap.rop == want_rop
    assert snap.identity == ("dispatch", DISPATCH_NAME, _at(21, 25))
    assert _ps.DispatchSnapshot.of(None) is None


def test_dispatch_rrp_is_read_from_its_own_column():
    """In the fixture RRP equals ROP, so make them differ for one region."""
    lines = DISPATCH.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("D,DISPATCH,PRICE") and ",QLD1," in line)
    fields = lines[i].split(",")
    fields[9] = "321.5"  # RRP; ROP (field 11) as published
    lines[i] = ",".join(fields)
    prices = _lp.parse_dispatch_local_prices("\n".join(lines) + "\n", DISPATCH_NAME, _at(21, 25))
    assert prices.rrp["QLD1"] == 321.5 and prices.rop["QLD1"] == float(fields[11])


def test_the_fallbacks_carry_rrp_too():
    run = _p5()
    filled = _lp.p5_fallback(run, _at(21, 30), timedelta(minutes=15))
    assert filled.rrp == {region: series[_at(21, 30)] for region, series in run.rrp.items()}
    end = _at(21, 30)
    pd = SimpleNamespace(run_at=_at(21, 2), source="pd.zip", period_ends=[end],
                         prices={"QLD1": {end: 77.0}}, local_prices={})
    from_pd = _lp.predispatch_fallback(pd, _at(21, 25))
    assert from_pd.rrp == {"QLD1": 77.0} == from_pd.rop  # pre-dispatch publishes RRP only


def test_a_pd7day_run_is_identified_by_its_file_and_case_run():
    result = SimpleNamespace(source_file="PUBLIC_PD7DAY_202611011245.zip",
                             case=SimpleNamespace(run_datetime="2026-11-01T13:00:00+10:00"))
    run = _ps.Pd7dayRun.of(result)
    assert run.identity == ("pd7day", "PUBLIC_PD7DAY_202611011245.zip", datetime(2026, 11, 1, 13, tzinfo=NEM_TZ))
    assert run.result is result
    no_case = _ps.Pd7dayRun.of(SimpleNamespace(source_file="f.zip", case=None))
    assert no_case.identity == ("pd7day", "f.zip", None)
    bad = _ps.Pd7dayRun.of(SimpleNamespace(source_file="f.zip", case=SimpleNamespace(run_datetime="garbage")))
    assert bad.run_at is None
    assert _ps.Pd7dayRun.of(None) is None
