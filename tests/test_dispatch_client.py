"""
Tests for dispatch_client.py: ELEC_NEM_SUMMARY primary + DispatchIS fallback.

Covers:
  - ELEC_NEM_SUMMARY JSON parsing (all regions, $/MWh -> $/kWh, FIRM filter)
  - _settlement_age_seconds and parse_settlement across both source formats (#104)
  - DispatchIS CSV parsing (D,DISPATCH,PRICE, INTERVENTION=0 filter)
  - fetch_dispatch_prices: primary when fresh, DispatchIS on failure or staleness
  - the one DEBUG line the primary path emits per poll (#33)

Run with: python -m pytest tests/test_dispatch_client.py -v
"""
from __future__ import annotations

import inspect
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, make_zip

install_ha_stubs()

_const_mod, _dispatch_mod = load_chain("const", "dispatch_client")

_fetch_nem_summary = _dispatch_mod._fetch_nem_summary
_fetch_dispatchis = _dispatch_mod._fetch_dispatchis
_settlement_age_seconds = _dispatch_mod._settlement_age_seconds
fetch_dispatch_prices = _dispatch_mod.fetch_dispatch_prices
parse_settlement = _dispatch_mod.parse_settlement
settlement_iso = _dispatch_mod.settlement_iso

REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]
DISPATCHIS_SETTLEMENT = "2026/05/29 11:10:00"
DISPATCHIS_INDEX_HTML = (
    '<a href="PUBLIC_DISPATCHIS_202605291110_0000001.zip">PUBLIC_DISPATCHIS_202605291110_0000001.zip</a>'
)


# ── Sample data ──────────────────────────────────────────────────────────────

def _now_nem_str() -> str:
    """Current NEM time as an ELEC_NEM_SUMMARY SETTLEMENTDATE string (no tz suffix)."""
    return datetime.now(timezone.utc).astimezone(NEM_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _make_nem_summary_json(settlement: str | None = None, rows: list | None = None) -> bytes:
    """A minimal ELEC_NEM_SUMMARY JSON response; the default rows carry all five regions."""
    ts = settlement or _now_nem_str()
    if rows is None:
        rows = [
            {"SETTLEMENTDATE": ts, "REGIONID": "QLD1", "PRICE": 89.5, "PRICE_STATUS": "FIRM"},
            {"SETTLEMENTDATE": ts, "REGIONID": "NSW1", "PRICE": 75.2, "PRICE_STATUS": "FIRM"},
            {"SETTLEMENTDATE": ts, "REGIONID": "VIC1", "PRICE": 120.0, "PRICE_STATUS": "FIRM"},
            {"SETTLEMENTDATE": ts, "REGIONID": "SA1", "PRICE": -5.0, "PRICE_STATUS": "FIRM"},
            {"SETTLEMENTDATE": ts, "REGIONID": "TAS1", "PRICE": 88.1, "PRICE_STATUS": "CALCULATED"},
        ]
    return json.dumps({"ELEC_NEM_SUMMARY": rows}).encode()


def _make_dispatchis_csv(settlement: str = DISPATCHIS_SETTLEMENT) -> str:
    """Minimal DispatchIS CSV with D,DISPATCH,PRICE rows for all 5 regions plus one intervention row."""
    rows = [
        # col[0]=D [1]=DISPATCH [2]=PRICE [3]=5 [4]=SETTLEMENTDATE [5]=RUNNO
        # [6]=REGIONID [7]=DISPATCHINTERVAL [8]=INTERVENTION [9]=RRP
        f'D,DISPATCH,PRICE,5,"{settlement}",1,QLD1,100,0,89.5,0,89.5',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,NSW1,100,0,75.2,0,75.2',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,VIC1,100,0,120.0,0,120.0',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,SA1,100,0,-5.0,0,-5.0',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,TAS1,100,0,88.1,0,88.1',
        # Intervention row: must be filtered out
        f'D,DISPATCH,PRICE,5,"{settlement}",2,QLD1,100,1,9999.0,0,9999.0',
    ]
    return "\n".join(rows) + "\n"


def _dispatchis_responses(csv_content: str | None = None) -> list[bytes]:
    """The two responses the DispatchIS path reads: directory listing, then zip."""
    zip_bytes = make_zip((csv_content or _make_dispatchis_csv()).encode(), "PUBLIC_DISPATCHIS.csv")
    return [DISPATCHIS_INDEX_HTML.encode(), zip_bytes]


def _serving(*items):
    """A fake urlopen serving ``items`` in order: bytes as a response body, an exception raised."""
    queue = list(items)

    def fake_urlopen(url_or_req, timeout=None):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return io.BytesIO(item)

    return fake_urlopen


def _urlopen(*items):
    return patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=_serving(*items))


# ── ELEC_NEM_SUMMARY ─────────────────────────────────────────────────────────

def test_nem_summary_parses_all_regions_in_kwh():
    """All five regions parsed, FIRM and CALCULATED both accepted, $/MWh -> $/kWh,
    negative prices intact, and the settlement carried through."""
    ts = _now_nem_str()
    with _urlopen(_make_nem_summary_json(settlement=ts)):
        results = _fetch_nem_summary()

    assert set(results) == set(REGIONS)
    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6
    assert abs(results["SA1"].rrp - (-0.005)) < 1e-6
    assert results["TAS1"].rrp > 0, "PRICE_STATUS=CALCULATED must be accepted"
    assert results["QLD1"].interval_datetime == ts


def test_nem_summary_non_firm_excluded():
    """Rows with PRICE_STATUS not in FIRM/CALCULATED must be excluded."""
    ts = _now_nem_str()
    rows = [
        {"SETTLEMENTDATE": ts, "REGIONID": "QLD1", "PRICE": 89.5, "PRICE_STATUS": "FIRM"},
        {"SETTLEMENTDATE": ts, "REGIONID": "NSW1", "PRICE": 75.2, "PRICE_STATUS": "INVALID"},
        {"SETTLEMENTDATE": ts, "REGIONID": "VIC1", "PRICE": 120.0, "PRICE_STATUS": "PRELIMINARY"},
    ]
    with _urlopen(_make_nem_summary_json(rows=rows)):
        results = _fetch_nem_summary()

    assert set(results) == {"QLD1"}


def test_nem_summary_empty_raises():
    """An empty ELEC_NEM_SUMMARY list raises ValueError."""
    with _urlopen(_make_nem_summary_json(rows=[])):
        with pytest.raises(ValueError):
            _fetch_nem_summary()


# ── _settlement_age_seconds / parse_settlement ───────────────────────────────

@pytest.mark.parametrize(
    "age_ago, low, high",
    [(timedelta(seconds=30), 25, 60), (timedelta(minutes=15), 600, 1000)],
    ids=["fresh_30s", "stale_15min"],
)
def test_settlement_age_against_the_real_clock(age_ago, low, high):
    ts = (datetime.now(NEM_TZ) - age_ago).strftime("%Y-%m-%dT%H:%M:%S")
    age = _settlement_age_seconds(ts)
    assert low < age < high, f"Expected ~{age_ago.total_seconds():.0f}s, got {age:.1f}s"


@pytest.mark.parametrize(
    "settlement, now_utc, expected",
    [
        # The DispatchIS path fills interval_datetime with "YYYY/MM/DD HH:MM:SS";
        # the age helper must read that too.
        ("2026/05/29 11:10:00", datetime(2026, 5, 29, 1, 10, 30, tzinfo=timezone.utc), 30.0),   # 11:10:30 NEM
        ("2026-09-04T06:45:00", datetime(2026, 9, 3, 20, 50, 0, tzinfo=timezone.utc), 300.0),   # 06:50 NEM
    ],
    ids=["dispatchis_slash_form", "summary_iso_form"],
)
def test_settlement_age_pinned_against_an_injected_clock(settlement, now_utc, expected):
    assert _settlement_age_seconds(settlement, now=now_utc) == expected


def test_settlement_age_bad_format_raises():
    """A string in neither source format raises rather than returning a
    sentinel that would be read as stale data (issue #104)."""
    with pytest.raises(ValueError):
        _settlement_age_seconds("not-a-date")


def test_parse_settlement_both_formats_agree():
    assert parse_settlement("2026-05-29T11:10:00") == parse_settlement("2026/05/29 11:10:00")
    assert settlement_iso("2026/05/29 11:10:00") == "2026-05-29T11:10"
    assert settlement_iso("2026-05-29T11:10:00") == "2026-05-29T11:10"


def test_unparseable_summary_settlement_falls_back_with_parse_reason(caplog):
    """A format change at AEMO must be logged as a parse failure, not as
    "data appears stale" (issue #104)."""
    payload = _make_nem_summary_json(settlement="29 May 2026 11:10")

    with caplog.at_level(logging.DEBUG):
        with _urlopen(payload, *_dispatchis_responses()):
            results = fetch_dispatch_prices()

    assert set(results) == set(REGIONS)
    assert "appears stale" not in caplog.text
    assert "unrecognised SETTLEMENTDATE format" in caplog.text


# ── DispatchIS fallback ──────────────────────────────────────────────────────

def test_dispatchis_parses_all_regions_excluding_intervention():
    """All five regions in $/kWh; the INTERVENTION=1 QLD1 row (rrp 9999) is dropped."""
    with _urlopen(*_dispatchis_responses()):
        results = _fetch_dispatchis()

    assert set(results) == set(REGIONS)
    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6, "intervention row must not win"
    assert abs(results["VIC1"].rrp - 0.12) < 1e-6
    assert results["QLD1"].interval_datetime == DISPATCHIS_SETTLEMENT


def test_dispatchis_no_files_raises():
    """An empty directory raises ValueError."""
    with _urlopen(b"<html>no files</html>"):
        with pytest.raises(ValueError, match="No DispatchIS files"):
            _fetch_dispatchis()


# ── fetch_dispatch_prices ────────────────────────────────────────────────────

def test_fetch_uses_primary_when_fresh():
    """fetch_dispatch_prices returns the ELEC_NEM_SUMMARY result when data is fresh."""
    ts = _now_nem_str()
    with _urlopen(_make_nem_summary_json(settlement=ts)):
        results = fetch_dispatch_prices()

    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6
    assert results["QLD1"].interval_datetime == ts, "should have come from the primary"


@pytest.mark.parametrize(
    "primary",
    [
        lambda: ConnectionError("ELEC_NEM_SUMMARY down"),
        lambda: _make_nem_summary_json(
            settlement=(datetime.now(NEM_TZ) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S")
        ),
    ],
    ids=["primary_fails", "primary_stale"],
)
def test_fetch_falls_back_to_dispatchis(primary):
    """When ELEC_NEM_SUMMARY fails, or its data is stale (>10 min), the DispatchIS result is returned."""
    with _urlopen(primary(), *_dispatchis_responses()):
        results = fetch_dispatch_prices()

    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6
    # The slash form is what the DispatchIS path fills in, so this proves the source.
    assert results["QLD1"].interval_datetime == DISPATCHIS_SETTLEMENT


# ── DEBUG volume (issue #33) ─────────────────────────────────────────────────
# With five regions the dispatch path emitted nine DEBUG records per 5-minute
# poll; the client's "Dispatch: N regions fetched" line restated the all-region
# summary that precedes it. The per-cycle count is pinned in test_coordinator.py;
# this guards the client's own success path so a re-added line cannot hide
# behind a relaxed count.

def test_dispatch_client_summary_path_logs_once():
    """The ELEC_NEM_SUMMARY success path must emit exactly one DEBUG line.

    The DispatchIS fallback keeps its own line, so scope the check to the
    primary path, which is the one that runs every five minutes.
    """
    src = inspect.getsource(_dispatch_mod.fetch_dispatch_prices)
    primary = src.split("# Fallback: DispatchIS_Reports zip")[0]
    # Drop comment lines: the removed statement is quoted in a comment there
    # explaining why it went, and that should not trip the guard.
    primary = "\n".join(line for line in primary.splitlines() if not line.lstrip().startswith("#"))
    assert primary.count("Dispatch: %d regions fetched") == 0, (
        "dispatch_client must not re-add the duplicate region count on the ELEC_NEM_SUMMARY success path"
    )
    assert primary.count("ELEC_NEM_SUMMARY fetched") == 1
