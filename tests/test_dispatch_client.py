"""
Tests for dispatch_client.py — AEMO DispatchIS real-time price parsing.

Covers:
  - Parsing DISPATCH,PRICE rows from DispatchIS CSV content
  - Correct RRP conversion from $/MWh to $/kWh
  - INTERVENTION filtering (only rows with INTERVENTION==0)
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


# Sample DispatchIS CSV content (simplified)
SAMPLE_CSV = """\
C,NEMP.WORLD,DISPATCH,ARCHIVE,DISPATCHIS,PRICE,PUBLIC
I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,RRP,EEP,ROP,APCFLAG,MARKETSUSPENDEDFLAG,TOTALDEMAND,AVAILABLEGENERATION,AVAILABLELOAD
D,DISPATCH,PRICE,5,"2026/05/21 09:30:00",1,QLD1,"2026/05/21 09:30:00",0,-1.5,0,0,0,0,5000,6000,500
D,DISPATCH,PRICE,5,"2026/05/21 09:30:00",1,NSW1,"2026/05/21 09:30:00",0,85.2,0,0,0,0,8000,9000,700
D,DISPATCH,PRICE,5,"2026/05/21 09:30:00",1,VIC1,"2026/05/21 09:30:00",0,72.0,0,0,0,0,6000,7000,600
D,DISPATCH,PRICE,5,"2026/05/21 09:30:00",1,SA1,"2026/05/21 09:30:00",0,55.8,0,0,0,0,2000,3000,200
D,DISPATCH,PRICE,5,"2026/05/21 09:30:00",1,TAS1,"2026/05/21 09:30:00",0,90.0,0,0,0,0,1500,2000,100
D,DISPATCH,PRICE,5,"2026/05/21 09:30:00",1,QLD1,"2026/05/21 09:30:00",1,999.0,0,0,0,0,5000,6000,500
C,END OF REPORT
"""


def _make_zip_bytes(csv_content: str) -> bytes:
    """Create a zip file in memory containing the CSV content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PUBLIC_DISPATCHIS_202605210930.CSV", csv_content)
    return buf.getvalue()


def test_fetch_dispatch_prices_parses_qld1():
    """fetch_dispatch_prices returns correct RRP for QLD1 from mocked HTTP."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_DISPATCHIS_202605210930_0000000123456.zip">link</a>'

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
    # -1.5 $/MWh = -0.0015 $/kWh
    assert abs(qld.rrp - (-0.0015)) < 1e-6


def test_fetch_dispatch_prices_nsw1_conversion():
    """Verify $/MWh → $/kWh conversion for NSW1."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_DISPATCHIS_202605210930_0000000123456.zip">link</a>'

    call_count = [0]

    def fake_urlopen(url, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(index_html.encode())
        return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = fetch_dispatch_prices()

    assert "NSW1" in prices
    # 85.2 $/MWh = 0.0852 $/kWh
    assert abs(prices["NSW1"].rrp - 0.0852) < 1e-6


def test_fetch_dispatch_prices_all_regions():
    """All five NEM regions must be parsed."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_DISPATCHIS_202605210930_0000000123456.zip">link</a>'

    call_count = [0]

    def fake_urlopen(url, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(index_html.encode())
        return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = fetch_dispatch_prices()

    assert set(prices.keys()) == {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}


def test_intervention_rows_excluded():
    """Rows with INTERVENTION==1 must be excluded."""
    zip_bytes = _make_zip_bytes(SAMPLE_CSV)
    index_html = '<a href="PUBLIC_DISPATCHIS_202605210930_0000000123456.zip">link</a>'

    call_count = [0]

    def fake_urlopen(url, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return io.BytesIO(index_html.encode())
        return io.BytesIO(zip_bytes)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = fetch_dispatch_prices()

    # QLD1 has two rows: INTERVENTION==0 (rrp=-1.5) and INTERVENTION==1 (rrp=999.0)
    # Only the INTERVENTION==0 row should be used
    assert abs(prices["QLD1"].rrp - (-0.0015)) < 1e-6


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
            assert "No DispatchIS files found" in str(e)


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
