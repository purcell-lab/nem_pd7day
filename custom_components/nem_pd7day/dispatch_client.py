"""AEMO DispatchIS real-time price client."""
from __future__ import annotations

import io
import logging
import re
import zipfile
import urllib.request
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

DISPATCH_BASE = "https://www.nemweb.com.au/Reports/Current/DispatchIS_Reports/"


@dataclass
class DispatchPrice:
    region: str
    interval_datetime: str  # "2026/05/21 09:30:00"
    rrp: float  # $/kWh (converted from $/MWh)


def fetch_dispatch_prices() -> dict[str, DispatchPrice]:
    """
    Fetch the latest DispatchIS zip and parse the DISPATCH,PRICE table.
    Returns dict keyed by region (e.g. "QLD1") -> DispatchPrice.
    Raises on network/parse failure.
    """
    # List directory and find latest zip
    index = urllib.request.urlopen(DISPATCH_BASE, timeout=15).read().decode(
        "utf-8", errors="ignore"
    )
    files = sorted(
        set(
            re.findall(
                r"PUBLIC_DISPATCHIS_[^\"'<>\s]+\.zip", index, re.IGNORECASE
            )
        )
    )
    if not files:
        raise ValueError("No DispatchIS files found")
    url = DISPATCH_BASE + files[-1]
    raw = urllib.request.urlopen(url, timeout=20).read()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    content = zf.read(zf.namelist()[0]).decode("utf-8", errors="ignore")

    # Parse DISPATCH,PRICE table
    # Column indices (0-based from confirmed sample row):
    # 0=I/D, 1=DISPATCH, 2=PRICE, 3=5, 4=SETTLEMENTDATE, 5=RUNNO,
    # 6=REGIONID, 7=DISPATCHINTERVAL, 8=INTERVENTION, 9=RRP
    results: dict[str, DispatchPrice] = {}

    for line in content.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 10:
            continue
        if parts[0] == "D" and parts[1] == "DISPATCH" and parts[2] == "PRICE":
            # Only use INTERVENTION==0 rows (non-intervention dispatch)
            if parts[8] != "0":
                continue
            region = parts[6]
            settlement = parts[4]
            try:
                rrp_mwh = float(parts[9])
            except (ValueError, IndexError):
                continue
            rrp_kwh = round(rrp_mwh / 1000.0, 6)
            results[region] = DispatchPrice(
                region=region,
                interval_datetime=settlement,
                rrp=rrp_kwh,
            )
    return results
