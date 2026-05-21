"""AEMO TradingIS real-time price client."""
from __future__ import annotations

import io
import logging
import re
import zipfile
import urllib.request
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

DISPATCH_BASE = "https://www.nemweb.com.au/Reports/Current/TradingIS_Reports/"
FILE_PATTERN = r'PUBLIC_TRADINGIS_[^"\'<>\s]+\.zip'


@dataclass
class DispatchPrice:
    region: str
    interval_datetime: str  # "2026/05/21 09:30:00"
    rrp: float  # $/kWh (converted from $/MWh)


def fetch_dispatch_prices() -> dict[str, DispatchPrice]:
    """
    Fetch the latest TradingIS zip (~0.7KB) and parse the TRADING,PRICE table.
    Returns dict keyed by region → DispatchPrice.
    Only includes FIRM prices (filters INVALID/preliminary).
    """
    index = urllib.request.urlopen(DISPATCH_BASE, timeout=15).read().decode(
        "utf-8", errors="ignore"
    )
    files = sorted(
        set(re.findall(FILE_PATTERN, index, re.IGNORECASE))
    )
    if not files:
        raise ValueError("No TradingIS files found")
    url = DISPATCH_BASE + files[-1]
    raw = urllib.request.urlopen(url, timeout=20).read()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    content = zf.read(zf.namelist()[0]).decode("utf-8", errors="ignore")

    results: dict[str, DispatchPrice] = {}
    for line in content.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 9:
            continue
        if parts[0] != "D" or parts[1] != "TRADING" or parts[2] != "PRICE":
            continue
        # Filter: only FIRM prices
        price_status = parts[-1] if parts else ""
        if price_status not in ("FIRM", "CALCULATED"):
            continue
        region = parts[6]
        settlement = parts[4]
        try:
            rrp_mwh = float(parts[8])
        except (ValueError, IndexError):
            continue
        rrp_kwh = round(rrp_mwh / 1000.0, 6)
        results[region] = DispatchPrice(
            region=region,
            interval_datetime=settlement,
            rrp=rrp_kwh,
        )
    return results
