"""
Tests for dispatch_client.py — ELEC_NEM_SUMMARY primary + DispatchIS fallback.

Covers:
  - ELEC_NEM_SUMMARY JSON parsing (all regions, $/MWh → $/kWh, FIRM filter)
  - Stale-data detection falling through to DispatchIS fallback
  - DispatchIS CSV parsing (D,DISPATCH,PRICE, INTERVENTION=0 filter)
  - Primary failure falls back to DispatchIS
  - _settlement_age_seconds helper

Run with: python -m pytest tests/test_dispatch_client.py -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import importlib.util
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NEM_TZ = timezone(timedelta(hours=10))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Stub HA modules before loading integration code
for _mod in [
    "homeassistant", "homeassistant.core", "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.helpers", "homeassistant.helpers.storage",
    "homeassistant.helpers.event", "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.update_coordinator", "homeassistant.util",
    "homeassistant.util.dt", "aiohttp",
]:
    sys.modules.setdefault(_mod, MagicMock())

_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_dispatch_mod = _load(
    "custom_components.nem_pd7day.dispatch_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "dispatch_client.py"),
)

from custom_components.nem_pd7day.dispatch_client import (
    DispatchPrice,
    _fetch_nem_summary,
    _fetch_dispatchis,
    _settlement_age_seconds,
    fetch_dispatch_prices,
    parse_settlement,
    settlement_iso,
)


# ── Sample data ──────────────────────────────────────────────────────────────

def _now_nem_str() -> str:
    """Current NEM time as ELEC_NEM_SUMMARY SETTLEMENTDATE string (no tz suffix)."""
    now_nem = datetime.now(timezone.utc).astimezone(NEM_TZ)
    # Round down to nearest 5 min (interval end = rounded up, but close enough for freshness test)
    return now_nem.strftime("%Y-%m-%dT%H:%M:%S")


def _make_nem_summary_json(settlement: str | None = None) -> bytes:
    """Create a minimal ELEC_NEM_SUMMARY JSON response."""
    ts = settlement or _now_nem_str()
    rows = [
        {"SETTLEMENTDATE": ts, "REGIONID": "QLD1", "PRICE": 89.5,  "PRICE_STATUS": "FIRM"},
        {"SETTLEMENTDATE": ts, "REGIONID": "NSW1", "PRICE": 75.2,  "PRICE_STATUS": "FIRM"},
        {"SETTLEMENTDATE": ts, "REGIONID": "VIC1", "PRICE": 120.0, "PRICE_STATUS": "FIRM"},
        {"SETTLEMENTDATE": ts, "REGIONID": "SA1",  "PRICE": -5.0,  "PRICE_STATUS": "FIRM"},
        {"SETTLEMENTDATE": ts, "REGIONID": "TAS1", "PRICE": 88.1,  "PRICE_STATUS": "CALCULATED"},
    ]
    return json.dumps({"ELEC_NEM_SUMMARY": rows}).encode()


def _make_dispatchis_csv(settlement: str = "2026/05/29 11:10:00") -> str:
    """Minimal DispatchIS CSV with D,DISPATCH,PRICE rows for all 5 regions."""
    rows = [
        # col[0]=D [1]=DISPATCH [2]=PRICE [3]=5 [4]=SETTLEMENTDATE [5]=RUNNO
        # [6]=REGIONID [7]=DISPATCHINTERVAL [8]=INTERVENTION [9]=RRP
        f'D,DISPATCH,PRICE,5,"{settlement}",1,QLD1,100,0,89.5,0,89.5',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,NSW1,100,0,75.2,0,75.2',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,VIC1,100,0,120.0,0,120.0',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,SA1,100,0,-5.0,0,-5.0',
        f'D,DISPATCH,PRICE,5,"{settlement}",1,TAS1,100,0,88.1,0,88.1',
        # Intervention row — must be filtered out
        f'D,DISPATCH,PRICE,5,"{settlement}",2,QLD1,100,1,9999.0,0,9999.0',
    ]
    return "\n".join(rows) + "\n"


def _make_dispatchis_zip(csv_content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("PUBLIC_DISPATCHIS.csv", csv_content)
    return buf.getvalue()


# ── ELEC_NEM_SUMMARY tests ───────────────────────────────────────────────────

def test_nem_summary_parses_all_regions():
    """All 5 FIRM/CALCULATED regions parsed correctly."""
    payload = _make_nem_summary_json()

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_nem_summary()

    assert set(results.keys()) == {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}


def test_nem_summary_rrp_conversion():
    """89.5 $/MWh → 0.0895 $/kWh."""
    payload = _make_nem_summary_json()

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_nem_summary()

    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6


def test_nem_summary_negative_price():
    """-5.0 $/MWh → -0.005 $/kWh (negative prices are valid)."""
    payload = _make_nem_summary_json()

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_nem_summary()

    assert abs(results["SA1"].rrp - (-0.005)) < 1e-6


def test_nem_summary_calculated_accepted():
    """PRICE_STATUS=CALCULATED is accepted (TAS1 in sample)."""
    payload = _make_nem_summary_json()

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_nem_summary()

    assert "TAS1" in results


def test_nem_summary_non_firm_excluded():
    """Rows with PRICE_STATUS not in FIRM/CALCULATED must be excluded."""
    ts = _now_nem_str()
    rows = [
        {"SETTLEMENTDATE": ts, "REGIONID": "QLD1", "PRICE": 89.5,  "PRICE_STATUS": "FIRM"},
        {"SETTLEMENTDATE": ts, "REGIONID": "NSW1", "PRICE": 75.2,  "PRICE_STATUS": "INVALID"},
        {"SETTLEMENTDATE": ts, "REGIONID": "VIC1", "PRICE": 120.0, "PRICE_STATUS": "PRELIMINARY"},
    ]
    payload = json.dumps({"ELEC_NEM_SUMMARY": rows}).encode()

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_nem_summary()

    assert set(results.keys()) == {"QLD1"}


def test_nem_summary_empty_raises():
    """Empty ELEC_NEM_SUMMARY list raises ValueError."""
    payload = json.dumps({"ELEC_NEM_SUMMARY": []}).encode()

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        try:
            _fetch_nem_summary()
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


# ── _settlement_age_seconds tests ────────────────────────────────────────────

def test_settlement_age_fresh():
    """A settlement 30s ago should have age ~30s."""
    now_nem = datetime.now(NEM_TZ)
    thirty_ago = now_nem - timedelta(seconds=30)
    ts = thirty_ago.strftime("%Y-%m-%dT%H:%M:%S")
    age = _settlement_age_seconds(ts)
    assert 25 < age < 60, f"Expected ~30s, got {age:.1f}s"


def test_settlement_age_stale():
    """A settlement 15 minutes ago should have age > 600s."""
    now_nem = datetime.now(NEM_TZ)
    old = now_nem - timedelta(minutes=15)
    ts = old.strftime("%Y-%m-%dT%H:%M:%S")
    age = _settlement_age_seconds(ts)
    assert age > 600


def test_settlement_age_bad_format_raises():
    """A string in neither source format raises rather than returning a
    sentinel that would be read as stale data (issue #104)."""
    import pytest
    with pytest.raises(ValueError):
        _settlement_age_seconds("not-a-date")


def test_settlement_age_accepts_dispatchis_slash_form():
    """The DispatchIS path fills interval_datetime with "YYYY/MM/DD HH:MM:SS";
    the age helper must read that too, pinned against an injected clock."""
    now = datetime(2026, 5, 29, 1, 10, 30, tzinfo=timezone.utc)   # 11:10:30 NEM
    age = _settlement_age_seconds("2026/05/29 11:10:00", now=now)
    assert age == 30.0


def test_settlement_age_iso_form_pinned():
    now = datetime(2026, 9, 3, 20, 50, 0, tzinfo=timezone.utc)   # 06:50 NEM
    age = _settlement_age_seconds("2026-09-04T06:45:00", now=now)
    assert age == 300.0


def test_parse_settlement_both_formats_agree():
    assert parse_settlement("2026-05-29T11:10:00") == parse_settlement("2026/05/29 11:10:00")
    assert settlement_iso("2026/05/29 11:10:00") == "2026-05-29T11:10"
    assert settlement_iso("2026-05-29T11:10:00") == "2026-05-29T11:10"


def test_unparseable_summary_settlement_falls_back_with_parse_reason(caplog):
    """A format change at AEMO must be logged as a parse failure, not as
    "data appears stale"."""
    import logging
    payload = _make_nem_summary_json(settlement="29 May 2026 11:10")
    csv = _make_dispatchis_csv()
    fake_urlopen = _make_dispatchis_urlopen(csv)
    calls = [0]

    def routed(url_or_req, timeout=None):
        calls[0] += 1
        if calls[0] == 1:
            return io.BytesIO(payload)
        return fake_urlopen(url_or_req, timeout)

    with caplog.at_level(logging.DEBUG):
        with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=routed):
            results = fetch_dispatch_prices()

    assert set(results) == {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}
    assert "appears stale" not in caplog.text
    assert "unrecognised SETTLEMENTDATE format" in caplog.text


# ── DispatchIS fallback tests ────────────────────────────────────────────────

def _make_dispatchis_urlopen(csv_content: str):
    """Return a fake urlopen that serves directory listing then zip."""
    zip_bytes = _make_dispatchis_zip(csv_content)
    index_html = (
        '<a href="PUBLIC_DISPATCHIS_202605291110_0000001.zip">'
        'PUBLIC_DISPATCHIS_202605291110_0000001.zip</a>'
    )
    call_count = [0]

    def fake_urlopen(url_or_req, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(index_html.encode())
        return io.BytesIO(zip_bytes)

    return fake_urlopen


def test_dispatchis_parses_all_regions():
    csv = _make_dispatchis_csv()
    fake_urlopen = _make_dispatchis_urlopen(csv)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_dispatchis()

    assert set(results.keys()) == {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}


def test_dispatchis_intervention_excluded():
    """INTERVENTION=1 rows must be excluded."""
    csv = _make_dispatchis_csv()
    fake_urlopen = _make_dispatchis_urlopen(csv)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_dispatchis()

    # Intervention QLD1 row had rrp=9999; INTERVENTION=0 QLD1 has 89.5
    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6


def test_dispatchis_rrp_conversion():
    """120.0 $/MWh → 0.12 $/kWh for VIC1."""
    csv = _make_dispatchis_csv()
    fake_urlopen = _make_dispatchis_urlopen(csv)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = _fetch_dispatchis()

    assert abs(results["VIC1"].rrp - 0.12) < 1e-6


def test_dispatchis_no_files_raises():
    """Empty directory raises ValueError."""
    def fake_urlopen(url_or_req, timeout=None):
        return io.BytesIO(b"<html>no files</html>")

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        try:
            _fetch_dispatchis()
            assert False, "Should have raised"
        except ValueError as e:
            assert "No DispatchIS files" in str(e)


# ── fetch_dispatch_prices integration tests ──────────────────────────────────

def test_fetch_uses_primary_when_fresh():
    """fetch_dispatch_prices returns ELEC_NEM_SUMMARY result when data is fresh."""
    payload = _make_nem_summary_json()  # uses current time → fresh

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = fetch_dispatch_prices()

    assert "QLD1" in results
    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6


def test_fetch_falls_back_on_primary_failure():
    """When ELEC_NEM_SUMMARY fails, DispatchIS result is returned."""
    csv = _make_dispatchis_csv()
    zip_bytes = _make_dispatchis_zip(csv)
    index_html = (
        '<a href="PUBLIC_DISPATCHIS_202605291110_0000001.zip">'
        'PUBLIC_DISPATCHIS_202605291110_0000001.zip</a>'
    )
    call_count = [0]

    def fake_urlopen(url_or_req, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # Primary ELEC_NEM_SUMMARY → network error
            raise ConnectionError("ELEC_NEM_SUMMARY down")
        elif call_count[0] == 2:
            # DispatchIS directory listing
            return io.BytesIO(index_html.encode())
        else:
            # DispatchIS zip
            return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = fetch_dispatch_prices()

    assert "QLD1" in results
    assert abs(results["QLD1"].rrp - 0.0895) < 1e-6


def test_fetch_falls_back_on_stale_primary():
    """When ELEC_NEM_SUMMARY data is stale (>10 min), DispatchIS is used."""
    stale_ts = (datetime.now(NEM_TZ) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S")
    payload = _make_nem_summary_json(settlement=stale_ts)

    csv = _make_dispatchis_csv()
    zip_bytes = _make_dispatchis_zip(csv)
    index_html = (
        '<a href="PUBLIC_DISPATCHIS_202605291110_0000001.zip">'
        'PUBLIC_DISPATCHIS_202605291110_0000001.zip</a>'
    )
    call_count = [0]

    def fake_urlopen(url_or_req, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(payload)           # stale primary
        elif call_count[0] == 2:
            return io.BytesIO(index_html.encode())  # DispatchIS dir
        else:
            return io.BytesIO(zip_bytes)            # DispatchIS zip

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        results = fetch_dispatch_prices()

    # Should have come from DispatchIS, not the stale primary
    assert "QLD1" in results
