"""
Tests for TradingISClient — 30-minute actual settlement price fetching.

The new implementation fetches a single TradingIS zip per 30-min interval
(keyed by interval END = interval_start + 30 min) and parses one
D,TRADING,PRICE row per region.

Run with:  python -m pytest tests/test_tradingis_client.py -v
"""
from __future__ import annotations

import io
import os
import sys
import asyncio
import importlib.util
import time
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NEM_TZ = timezone(timedelta(hours=10))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Stub HA imports ──────────────────────────────────────────────────────────

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
_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_tradingis_mod = _load(
    "custom_components.nem_pd7day.tradingis_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "tradingis_client.py"),
)

from custom_components.nem_pd7day.tradingis_client import TradingISClient


# ── Sample CSV helpers ───────────────────────────────────────────────────────

def _make_trading_csv(settlement_str: str, region: str, rrp_mwh: float) -> str:
    """Minimal TradingIS CSV with one D,TRADING,PRICE row."""
    return (
        "C,NEMP.WORLD,TRADINGIS,v3\n"
        "I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,INVALIDFLAG,"
        "LASTCHANGED,PRICE_STATUS\n"
        f'D,TRADING,PRICE,3,"{settlement_str}",1,{region},223,{rrp_mwh},0,0,'
        f'"2026/04/18 17:01:00",FIRM\n'
        'C,"END OF REPORT",3\n'
    )


def _make_multi_region_csv(settlement_str: str) -> str:
    """TradingIS CSV with all 5 regions."""
    regions = [
        ("QLD1", 89.5),
        ("NSW1", 75.2),
        ("VIC1", 120.0),
        ("SA1", -5.0),
        ("TAS1", 88.1),
    ]
    lines = [
        "C,NEMP.WORLD,TRADINGIS,v3",
        "I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,INVALIDFLAG,"
        "LASTCHANGED,PRICE_STATUS",
    ]
    for region, rrp in regions:
        lines.append(
            f'D,TRADING,PRICE,3,"{settlement_str}",1,{region},223,{rrp},0,0,'
            f'"2026/04/18 17:01:00",FIRM'
        )
    lines.append('C,"END OF REPORT",3')
    return "\n".join(lines) + "\n"


def _make_zip_bytes(csv_content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PUBLIC_TRADINGIS.csv", csv_content)
    return buf.getvalue()


def _make_directory_html(filenames: list[str], base_url: str = "") -> str:
    links = "\n".join(f'<a href="{fn}">{fn}</a>' for fn in filenames)
    return f"<html><body>{links}</body></html>"


# ── Mock session ─────────────────────────────────────────────────────────────

class MockResponse:
    def __init__(self, text_content: str = "", byte_content: bytes = b"", status: int = 200):
        self._text = text_content
        self._bytes = byte_content
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def text(self): return self._text
    async def read(self): return self._bytes
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass


class MockSession:
    def __init__(self, responses: dict[str, MockResponse]):
        self._responses = responses
        self.request_log: list[str] = []

    def get(self, url, **kwargs):
        self.request_log.append(url)
        return self._responses.get(url, MockResponse(status=404))


# ── Tests: _parse_rrp ─────────────────────────────────────────────────────────

def test_parse_rrp_qld1():
    """Parse a single-region CSV, return $/kWh."""
    csv = _make_trading_csv("2026/04/18 17:05:00", "QLD1", 95.69)
    result = TradingISClient._parse_rrp(csv, "QLD1")
    assert result is not None
    assert abs(result - 95.69 / 1000) < 1e-9


def test_parse_rrp_region_filter():
    """Only the requested region is returned."""
    csv = _make_multi_region_csv("2026/04/18 17:05:00")
    qld = TradingISClient._parse_rrp(csv, "QLD1")
    nsw = TradingISClient._parse_rrp(csv, "NSW1")
    assert qld is not None and abs(qld - 0.0895) < 1e-9
    assert nsw is not None and abs(nsw - 0.0752) < 1e-9


def test_parse_rrp_missing_region():
    """Region not in CSV returns None."""
    csv = _make_trading_csv("2026/04/18 17:05:00", "QLD1", 95.69)
    assert TradingISClient._parse_rrp(csv, "SA1") is None


def test_parse_rrp_negative_price():
    """-5.0 $/MWh → -0.005 $/kWh."""
    csv = _make_trading_csv("2026/04/18 17:05:00", "SA1", -5.0)
    result = TradingISClient._parse_rrp(csv, "SA1")
    assert result is not None
    assert abs(result - (-0.005)) < 1e-9


def test_parse_rrp_empty_csv():
    """No D,TRADING,PRICE rows → None."""
    csv = "C,NEMP.WORLD\nI,TRADING,PRICE,3,HEADER\nC,END\n"
    assert TradingISClient._parse_rrp(csv, "QLD1") is None


# ── Tests: fetch_interval_price ───────────────────────────────────────────────

def test_fetch_interval_price_returns_rrp():
    """Fetches the zip for interval_end = interval_start + 30 min, returns $/kWh."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    interval_end = interval_start + timedelta(minutes=30)
    region = "QLD1"
    rrp_mwh = 95.69
    settlement_str = interval_end.strftime("%Y/%m/%d %H:%M:00")

    ts_str = interval_end.strftime("%Y%m%d%H%M")
    filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
    base_url = _const_mod.TRADINGIS_BASE_URL

    csv = _make_trading_csv(settlement_str, region, rrp_mwh)
    zip_bytes = _make_zip_bytes(csv)
    dir_html = _make_directory_html([filename])

    responses = {
        base_url: MockResponse(text_content=dir_html),
        base_url + filename: MockResponse(byte_content=zip_bytes),
    }
    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price(region, interval_start))
    assert result is not None
    assert abs(result - rrp_mwh / 1000) < 1e-9


def test_fetch_interval_price_file_not_in_directory():
    """If no file exists for the target interval end, returns None."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    base_url = _const_mod.TRADINGIS_BASE_URL
    # Directory has a file for a different interval
    dir_html = _make_directory_html(["PUBLIC_TRADINGIS_202604181600_1.zip"])
    responses = {base_url: MockResponse(text_content=dir_html)}
    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))
    assert result is None


def test_fetch_interval_price_bad_zip():
    """Malformed zip bytes → returns None, does not raise."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    interval_end = interval_start + timedelta(minutes=30)
    ts_str = interval_end.strftime("%Y%m%d%H%M")
    filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
    base_url = _const_mod.TRADINGIS_BASE_URL

    dir_html = _make_directory_html([filename])
    responses = {
        base_url: MockResponse(text_content=dir_html),
        base_url + filename: MockResponse(byte_content=b"not a zip"),
    }
    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))
    assert result is None


def test_fetch_interval_price_rrp_conversion():
    """100 $/MWh → 0.1 $/kWh."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    interval_end = interval_start + timedelta(minutes=30)
    region = "QLD1"
    ts_str = interval_end.strftime("%Y%m%d%H%M")
    filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
    base_url = _const_mod.TRADINGIS_BASE_URL
    settlement_str = interval_end.strftime("%Y/%m/%d %H:%M:00")

    csv = _make_trading_csv(settlement_str, region, 100.0)
    zip_bytes = _make_zip_bytes(csv)
    dir_html = _make_directory_html([filename])
    responses = {
        base_url: MockResponse(text_content=dir_html),
        base_url + filename: MockResponse(byte_content=zip_bytes),
    }
    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price(region, interval_start))
    assert result is not None
    assert abs(result - 0.1) < 1e-9


# ── Tests: directory cache ────────────────────────────────────────────────────

def test_directory_cache_hit():
    """Second call within TTL uses cache (only one HTTP request to directory)."""
    base_url = _const_mod.TRADINGIS_BASE_URL
    dir_html = _make_directory_html(["PUBLIC_TRADINGIS_202604181705_1.zip"])
    responses = {base_url: MockResponse(text_content=dir_html)}
    session = MockSession(responses)
    client = TradingISClient(session)

    run_async(client._fetch_directory())
    run_async(client._fetch_directory())

    dir_requests = [u for u in session.request_log if u == base_url]
    assert len(dir_requests) == 1, f"Expected 1 (cached), got {len(dir_requests)}"


def test_directory_cache_expiry():
    """After TTL expires, a fresh request is made."""
    base_url = _const_mod.TRADINGIS_BASE_URL
    dir_html = _make_directory_html(["PUBLIC_TRADINGIS_202604181705_1.zip"])
    responses = {base_url: MockResponse(text_content=dir_html)}
    session = MockSession(responses)
    client = TradingISClient(session)

    run_async(client._fetch_directory())
    client._dir_cache_ts = time.monotonic() - 100   # expire cache

    run_async(client._fetch_directory())

    dir_requests = [u for u in session.request_log if u == base_url]
    assert len(dir_requests) == 2, f"Expected 2 (expired), got {len(dir_requests)}"


def test_directory_http_failure_returns_none_from_fetch():
    """If directory fetch raises, fetch_interval_price returns None."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    base_url = _const_mod.TRADINGIS_BASE_URL

    # 404 for directory
    responses = {base_url: MockResponse(status=404)}
    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))
    assert result is None
