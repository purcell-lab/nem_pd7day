"""
Tests for TradingISClient — CSV parsing, directory caching,
price fetching with zip mocking.

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
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NEM_TZ = timezone(timedelta(hours=10))

# ── Stub HA imports ──────────────────────────────────────────────────────────

sys.modules.setdefault("aiohttp", MagicMock())
sys.modules.setdefault("homeassistant", MagicMock())
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.storage"] = MagicMock()
sys.modules["homeassistant.helpers.event"] = MagicMock()
sys.modules["homeassistant.helpers.aiohttp_client"] = MagicMock()
sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()
sys.modules["homeassistant.util"] = MagicMock()
sys.modules["homeassistant.util.dt"] = MagicMock()

# Load integration modules
_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)

# Now we need real aiohttp for ClientSession type hints — but we only use mocks
# So we re-import after stubs to get the tradingis_client module
import aiohttp as _real_aiohttp

_tradingis_mod = _load(
    "custom_components.nem_pd7day.tradingis_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "tradingis_client.py"),
)

from custom_components.nem_pd7day.tradingis_client import TradingISClient

# ── Sample CSV data ──────────────────────────────────────────────────────────

SAMPLE_CSV = """\
C,NEMP.WORLD,TRADINGIS,v3
I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,INVALIDFLAG,LASTCHANGED,ROP,RAISE6SECRRP,RAISE6SECROP,RAISE60SECRRP,RAISE60SECROP,RAISE5MINRRP,RAISE5MINROP,RAISEREGRRP,RAISEREGROP,LOWER6SECRRP,LOWER6SECROP,LOWER60SECRRP,LOWER60SECROP,LOWER5MINRRP,LOWER5MINROP,LOWERREGRRP,LOWERREGROP,RAISE1SECRRP,RAISE1SECROP,LOWER1SECRRP,LOWER1SECROP,PRICE_STATUS
D,TRADING,PRICE,3,"2026/04/18 17:05:00",1,QLD1,223,95.69,0,0,"2026/04/18 17:01:00",0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,FIRM
D,TRADING,PRICE,3,"2026/04/18 17:05:00",1,NSW1,223,100.05,0,0,"2026/04/18 17:01:00",0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,FIRM
D,TRADING,PRICE,3,"2026/04/18 17:05:00",1,VIC1,223,100.93,0,0,"2026/04/18 17:01:00",0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,NOT FIRM
C,"END OF REPORT",3
"""


def _make_csv(settlement_str: str, region: str, rrp: float) -> str:
    """Generate a minimal TradingIS CSV for one region/settlement."""
    return (
        f"C,NEMP.WORLD,TRADINGIS,v3\n"
        f"I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,INVALIDFLAG,LASTCHANGED,PRICE_STATUS\n"
        f'D,TRADING,PRICE,3,"{settlement_str}",1,{region},223,{rrp},0,0,"2026/04/18 17:01:00",FIRM\n'
        f'C,"END OF REPORT",3\n'
    )


def _make_zip_bytes(csv_content: str) -> bytes:
    """Create in-memory zip bytes containing the CSV string."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PUBLIC_TRADINGIS.csv", csv_content)
    return buf.getvalue()


def _make_directory_html(filenames: list[str]) -> str:
    """Create a minimal HTML directory listing containing the given filenames."""
    links = "\n".join(f'<a href="{fn}">{fn}</a>' for fn in filenames)
    return f"<html><body>{links}</body></html>"


# ── Mock session helper ──────────────────────────────────────────────────────

class MockResponse:
    """Minimal aiohttp response mock."""

    def __init__(self, text_content: str = "", byte_content: bytes = b"", status: int = 200):
        self._text = text_content
        self._bytes = byte_content
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def text(self):
        return self._text

    async def read(self):
        return self._bytes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class MockSession:
    """Mock aiohttp.ClientSession that returns predefined responses by URL."""

    def __init__(self, responses: dict[str, MockResponse]):
        self._responses = responses
        self.request_log: list[str] = []

    def get(self, url, **kwargs):
        self.request_log.append(url)
        if url in self._responses:
            return self._responses[url]
        return MockResponse(status=404)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_parse_csv_qld1():
    """Parse sample CSV, verify QLD1 RRP=95.69 extracted correctly."""
    client = TradingISClient.__new__(TradingISClient)
    result = client._parse_csv(SAMPLE_CSV, "QLD1")
    assert "2026/04/18 17:05:00" in result
    assert abs(result["2026/04/18 17:05:00"] - 95.69) < 1e-6


def test_parse_csv_region_filter():
    """Verify only QLD1 rows returned when filtering for QLD1."""
    client = TradingISClient.__new__(TradingISClient)
    result = client._parse_csv(SAMPLE_CSV, "QLD1")
    # Only QLD1 row should be present
    assert len(result) == 1
    assert "2026/04/18 17:05:00" in result

    # NSW1 filter should return different result
    result_nsw = client._parse_csv(SAMPLE_CSV, "NSW1")
    assert len(result_nsw) == 1
    assert abs(result_nsw["2026/04/18 17:05:00"] - 100.05) < 1e-6


def test_fetch_interval_price_full_window():
    """Mock 6 HTTP responses (directory + 6 zips), verify average = sum/6/1000."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    region = "QLD1"

    # 6 settlement times: 17:05, 17:10, 17:15, 17:20, 17:25, 17:30
    rrp_values = [90.0, 92.0, 94.0, 96.0, 98.0, 100.0]
    filenames = []
    responses = {}

    base_url = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"

    for i, rrp in enumerate(rrp_values):
        t = interval_start + timedelta(minutes=5 * (i + 1))
        ts_str = t.strftime("%Y%m%d%H%M")
        filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
        filenames.append(filename)
        settlement_str = t.strftime("%Y/%m/%d %H:%M:00")
        csv = _make_csv(settlement_str, region, rrp)
        zip_bytes = _make_zip_bytes(csv)
        responses[base_url + filename] = MockResponse(byte_content=zip_bytes)

    dir_html = _make_directory_html(filenames)
    responses[base_url] = MockResponse(text_content=dir_html)

    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price(region, interval_start))

    expected = sum(rrp_values) / 6 / 1000.0
    assert result is not None
    assert abs(result - expected) < 1e-9, f"Expected {expected}, got {result}"


def test_fetch_interval_price_partial_window():
    """Only 4 of 6 files present -> returns average of 4."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    region = "QLD1"

    # Only provide 4 of the 6 settlement times (skip 17:10 and 17:25)
    present_offsets = [5, 15, 20, 30]  # minutes after interval_start
    rrp_values = [90.0, 94.0, 96.0, 100.0]
    filenames = []
    responses = {}

    base_url = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"

    for offset, rrp in zip(present_offsets, rrp_values):
        t = interval_start + timedelta(minutes=offset)
        ts_str = t.strftime("%Y%m%d%H%M")
        filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
        filenames.append(filename)
        settlement_str = t.strftime("%Y/%m/%d %H:%M:00")
        csv = _make_csv(settlement_str, region, rrp)
        zip_bytes = _make_zip_bytes(csv)
        responses[base_url + filename] = MockResponse(byte_content=zip_bytes)

    dir_html = _make_directory_html(filenames)
    responses[base_url] = MockResponse(text_content=dir_html)

    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price(region, interval_start))

    expected = sum(rrp_values) / 4 / 1000.0
    assert result is not None
    assert abs(result - expected) < 1e-9, f"Expected {expected}, got {result}"


def test_fetch_interval_price_insufficient():
    """Only 3 of 6 files -> returns None."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    region = "QLD1"

    # Only provide 3 files
    present_offsets = [5, 15, 20]
    rrp_values = [90.0, 94.0, 96.0]
    filenames = []
    responses = {}

    base_url = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"

    for offset, rrp in zip(present_offsets, rrp_values):
        t = interval_start + timedelta(minutes=offset)
        ts_str = t.strftime("%Y%m%d%H%M")
        filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
        filenames.append(filename)
        settlement_str = t.strftime("%Y/%m/%d %H:%M:00")
        csv = _make_csv(settlement_str, region, rrp)
        zip_bytes = _make_zip_bytes(csv)
        responses[base_url + filename] = MockResponse(byte_content=zip_bytes)

    dir_html = _make_directory_html(filenames)
    responses[base_url] = MockResponse(text_content=dir_html)

    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price(region, interval_start))
    assert result is None


def test_directory_cache():
    """Two calls within 90s -> only one HTTP request to the directory URL."""
    base_url = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"
    dir_html = _make_directory_html(["PUBLIC_TRADINGIS_202604181705_1.zip"])
    responses = {base_url: MockResponse(text_content=dir_html)}

    session = MockSession(responses)
    client = TradingISClient(session)

    # First call — should fetch
    run_async(client._fetch_directory())
    # Second call — should use cache
    run_async(client._fetch_directory())

    # Count requests to the directory URL
    dir_requests = [url for url in session.request_log if url == base_url]
    assert len(dir_requests) == 1, (
        f"Expected 1 directory request (cached), got {len(dir_requests)}"
    )


def test_directory_cache_expires():
    """After 90s, the cache should expire and a new request should be made."""
    base_url = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"
    dir_html = _make_directory_html(["PUBLIC_TRADINGIS_202604181705_1.zip"])
    responses = {base_url: MockResponse(text_content=dir_html)}

    session = MockSession(responses)
    client = TradingISClient(session)

    # First call
    run_async(client._fetch_directory())

    # Manually expire cache by moving timestamp back
    client._dir_cache_ts = time.monotonic() - 100

    # Second call — should fetch again
    run_async(client._fetch_directory())

    dir_requests = [url for url in session.request_log if url == base_url]
    assert len(dir_requests) == 2, (
        f"Expected 2 directory requests (cache expired), got {len(dir_requests)}"
    )


def test_price_conversion():
    """Verify $/MWh -> $/kWh (divide by 1000)."""
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
    region = "QLD1"

    # All 6 intervals at 100 $/MWh
    rrp_mwh = 100.0
    filenames = []
    responses = {}
    base_url = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"

    for i in range(6):
        t = interval_start + timedelta(minutes=5 * (i + 1))
        ts_str = t.strftime("%Y%m%d%H%M")
        filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
        filenames.append(filename)
        settlement_str = t.strftime("%Y/%m/%d %H:%M:00")
        csv = _make_csv(settlement_str, region, rrp_mwh)
        zip_bytes = _make_zip_bytes(csv)
        responses[base_url + filename] = MockResponse(byte_content=zip_bytes)

    dir_html = _make_directory_html(filenames)
    responses[base_url] = MockResponse(text_content=dir_html)

    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price(region, interval_start))
    assert result is not None
    # 100 $/MWh = 0.1 $/kWh
    assert abs(result - 0.1) < 1e-9, f"Expected 0.1 $/kWh, got {result}"


def test_parse_csv_not_firm_included():
    """NOT FIRM prices should be included in parsing."""
    client = TradingISClient.__new__(TradingISClient)
    result = client._parse_csv(SAMPLE_CSV, "VIC1")
    assert "2026/04/18 17:05:00" in result
    assert abs(result["2026/04/18 17:05:00"] - 100.93) < 1e-6


def test_bad_zip_returns_none():
    """Malformed zip data should return None, not crash."""
    base_url = "https://www.nemweb.com.au/REPORTS/CURRENT/TradingIS_Reports/"
    interval_start = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)

    filenames = []
    responses = {}
    for i in range(6):
        t = interval_start + timedelta(minutes=5 * (i + 1))
        ts_str = t.strftime("%Y%m%d%H%M")
        filename = f"PUBLIC_TRADINGIS_{ts_str}_1.zip"
        filenames.append(filename)
        # Bad zip data
        responses[base_url + filename] = MockResponse(byte_content=b"not a zip file")

    dir_html = _make_directory_html(filenames)
    responses[base_url] = MockResponse(text_content=dir_html)

    session = MockSession(responses)
    client = TradingISClient(session)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))
    assert result is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
