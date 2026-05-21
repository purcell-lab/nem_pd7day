"""
Tests for dispatch_client.py — AEMO TradingIS real-time price parsing.

Covers:
  - Parsing TRADING,PRICE rows from TradingIS CSV content
  - Correct RRP conversion from $/MWh to $/kWh
  - PRICE_STATUS filtering (only FIRM/CALCULATED rows)
  - Network/parse failure raises

Run with: python -m pytest tests/test_dispatch_client.py -v
"""
from __future__ import annotations

import io
import os
import sys
import importlib.util
import zipfile
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load the dispatch_client module
_dispatch_mod = _load(
    "custom_components.nem_pd7day.dispatch_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "dispatch_client.py"),
)

from custom_components.nem_pd7day.dispatch_client import (
    DispatchPrice,
    fetch_dispatch_prices,
)


# Sample TradingIS CSV content (simplified)
SAMPLE_CSV = """\
C,NEMP.WORLD,TRADING,ARCHIVE,TRADINGIS,PRICE,PUBLIC
I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,INVALIDFLAG,LASTCHANGED,ROP,APCFLAG,MARKETSUSPENDEDFLAG,TOTALDEMAND,AVAILABLEGENERATION,AVAILABLELOAD,PRICE_STATUS
D,TRADING,PRICE,3,"2026/05/21 09:30:00",1,QLD1,141,89.5,0,0,0,0,0,0,5000,6000,500,FIRM
D,TRADING,PRICE,3,"2026/05/21 09:30:00",1,NSW1,141,75.2,0,0,0,0,0,0,8000,9000,700,FIRM
D,TRADING,PRICE,3,"2026/05/21 09:30:00",1,SA1,141,-5.0,0,0,0,0,0,0,2000,3000,200,FIRM
D,TRADING,PRICE,3,"2026/05/21 09:30:00",1,VIC1,141,120.0,0,0,0,0,0,0,6000,7000,600,INVALID
C,END OF REPORT
"""


def _make_zip_bytes(csv_content: str) -> bytes:
    """Create a zip file in memory containing the CSV content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PUBLIC_TRADINGIS_202605210930.CSV", csv_content)
    return buf.getvalue()


def test_fetch_dispatch_prices_parses_qld1():
    """fetch_dispatch_prices returns correct RRP for QLD1 (FIRM) from mocked HTTP."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_TRADINGIS_202605210930_0000000123456.zip">link</a>'

    call_count = [0]

    def fake_urlopen(url, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # Directory listing
            return io.BytesIO(index_html.encode())
        else:
            # Zip file
            return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = fetch_dispatch_prices()

    assert "QLD1" in prices
    qld = prices["QLD1"]
    assert isinstance(qld, DispatchPrice)
    assert qld.region == "QLD1"
    assert qld.interval_datetime == "2026/05/21 09:30:00"
    # 89.5 $/MWh = 0.0895 $/kWh
    assert abs(qld.rrp - 0.0895) < 1e-6


def test_fetch_dispatch_prices_nsw1_conversion():
    """Verify $/MWh → $/kWh conversion for NSW1."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_TRADINGIS_202605210930_0000000123456.zip">link</a>'

    call_count = [0]

    def fake_urlopen(url, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(index_html.encode())
        return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = fetch_dispatch_prices()

    assert "NSW1" in prices
    # 75.2 $/MWh = 0.0752 $/kWh
    assert abs(prices["NSW1"].rrp - 0.0752) < 1e-6


def test_fetch_dispatch_prices_sa1_negative():
    """Negative prices are valid and should be included when FIRM."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_TRADINGIS_202605210930_0000000123456.zip">link</a>'

    call_count = [0]

    def fake_urlopen(url, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(index_html.encode())
        return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = fetch_dispatch_prices()

    assert "SA1" in prices
    # -5.0 $/MWh = -0.005 $/kWh
    assert abs(prices["SA1"].rrp - (-0.005)) < 1e-6


def test_invalid_price_status_excluded():
    """Rows with PRICE_STATUS==INVALID must be excluded."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_TRADINGIS_202605210930_0000000123456.zip">link</a>'

    call_count = [0]

    def fake_urlopen(url, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(index_html.encode())
        return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = fetch_dispatch_prices()

    # VIC1 has PRICE_STATUS=INVALID, must be excluded
    assert "VIC1" not in prices
    # Only QLD1, NSW1, SA1 should be present (all FIRM)
    assert set(prices.keys()) == {"QLD1", "NSW1", "SA1"}


def test_no_files_raises():
    """Empty directory listing must raise ValueError."""
    index_html = "<html><body>No files</body></html>"

    def fake_urlopen(url, timeout=None):
        return io.BytesIO(index_html.encode())

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        try:
            fetch_dispatch_prices()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "No TradingIS files found" in str(e)


def test_network_error_raises():
    """Network failure must propagate."""
    def fake_urlopen(url, timeout=None):
        raise ConnectionError("Network down")

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        try:
            fetch_dispatch_prices()
            assert False, "Should have raised"
        except ConnectionError:
            pass
