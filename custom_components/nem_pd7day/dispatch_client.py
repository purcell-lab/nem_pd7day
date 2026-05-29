"""AEMO 5-minute dispatch price client.

Primary source: AEMO ELEC_NEM_SUMMARY JSON API (all 5 regions, ~13 KB, no auth).
Fallback source: NEMWeb DispatchIS_Reports zip files (canonical MMS CSV).

The DispatchCoordinator calls fetch_dispatch_prices() synchronously via
hass.async_add_executor_job — this module uses urllib/zipfile only (no aiohttp).
"""
from __future__ import annotations

import io
import json
import logging
import re
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

from .const import DISPATCHIS_BASE_URL, ELEC_NEM_SUMMARY_URL

_LOGGER = logging.getLogger(__name__)

# Primary: AEMO visualisation API — returns all regions in a single JSON response.
# Undocumented but stable; used by multiple open-source HA integrations.
NEM_SUMMARY_URL = ELEC_NEM_SUMMARY_URL

# Fallback: NEMWeb DispatchIS — canonical 5-minute MMS price files.
DISPATCHIS_BASE = DISPATCHIS_BASE_URL
_DISPATCHIS_FILE_RE = re.compile(
    r"PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip", re.IGNORECASE
)

# Price status values accepted from both sources.
_FIRM_STATUSES = {"FIRM", "CALCULATED"}


@dataclass
class DispatchPrice:
    region: str
    interval_datetime: str  # "2026/05/29 11:05:00" — interval END (NEM time)
    rrp: float              # $/kWh (converted from $/MWh)


# ── Primary: ELEC_NEM_SUMMARY JSON ───────────────────────────────────────────

def _fetch_nem_summary() -> dict[str, DispatchPrice]:
    """Fetch all regions from the AEMO ELEC_NEM_SUMMARY JSON API.

    Returns a dict keyed by REGIONID.  Raises on any network or parse error.
    """
    req = urllib.request.Request(
        NEM_SUMMARY_URL,
        headers={"Accept": "application/json", "User-Agent": "nem_pd7day/2.3"},
    )
    raw = urllib.request.urlopen(req, timeout=15).read()
    payload = json.loads(raw)

    rows = payload.get("ELEC_NEM_SUMMARY", [])
    if not rows:
        raise ValueError("ELEC_NEM_SUMMARY key missing or empty in response")

    results: dict[str, DispatchPrice] = {}
    for row in rows:
        region = row.get("REGIONID", "")
        if not region:
            continue
        price_status = row.get("PRICE_STATUS", "")
        if price_status not in _FIRM_STATUSES:
            _LOGGER.debug(
                "ELEC_NEM_SUMMARY: skipping %s — PRICE_STATUS=%s", region, price_status
            )
            continue
        try:
            rrp_mwh = float(row["PRICE"])
        except (KeyError, ValueError, TypeError):
            continue
        # SETTLEMENTDATE is interval END in NEM time, no tz suffix: "2026/05/29 11:10:00"
        settlement = row.get("SETTLEMENTDATE", "")
        results[region] = DispatchPrice(
            region=region,
            interval_datetime=settlement,
            rrp=round(rrp_mwh / 1000.0, 6),
        )

    if not results:
        raise ValueError("ELEC_NEM_SUMMARY: no FIRM prices found in response")

    return results


def _settlement_age_seconds(settlement_str: str) -> float:
    """Return how many seconds ago the settlement interval ended.

    settlement_str is NEM time without tz suffix e.g. '2026/05/29 11:10:00'.
    Returns a large number on parse failure so stale-data checks fail safely.
    """
    try:
        # Parse as NEM time (UTC+10) and compare against UTC now
        dt = datetime.strptime(settlement_str, "%Y-%m-%dT%H:%M:%S")
        # ELEC_NEM_SUMMARY uses ISO format without offset
        from datetime import timedelta
        nem_utc_offset = timedelta(hours=10)
        dt_utc = dt.replace(tzinfo=timezone.utc) - nem_utc_offset
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        # dt_utc is naive UTC; compare as naive
        return (now_utc - dt_utc.replace(tzinfo=None)).total_seconds()
    except Exception:  # noqa: BLE001
        return 9999.0


# ── Fallback: DispatchIS_Reports zip ─────────────────────────────────────────

def _fetch_dispatchis() -> dict[str, DispatchPrice]:
    """Fetch the latest DispatchIS zip from NEMWeb and parse D,DISPATCH,PRICE rows.

    Returns a dict keyed by REGIONID.  Raises on any network or parse error.
    """
    index = urllib.request.urlopen(DISPATCHIS_BASE, timeout=15).read().decode(
        "utf-8", errors="ignore"
    )
    files = sorted(set(_DISPATCHIS_FILE_RE.findall(index)))
    if not files:
        raise ValueError("No DispatchIS files found in directory listing")

    url = DISPATCHIS_BASE + files[-1]
    raw = urllib.request.urlopen(url, timeout=20).read()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    content = zf.read(zf.namelist()[0]).decode("utf-8", errors="ignore")

    results: dict[str, DispatchPrice] = {}
    for line in content.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 10:
            continue
        if parts[0] != "D" or parts[1] != "DISPATCH" or parts[2] != "PRICE":
            continue
        # INTERVENTION col [8]: 0 = market price, 1 = intervention price
        try:
            if int(parts[8]) != 0:
                continue
        except (ValueError, IndexError):
            continue
        region = parts[6]
        settlement = parts[4]   # "2026/05/29 11:10:00"
        try:
            rrp_mwh = float(parts[9])
        except (ValueError, IndexError):
            continue
        results[region] = DispatchPrice(
            region=region,
            interval_datetime=settlement,
            rrp=round(rrp_mwh / 1000.0, 6),
        )

    if not results:
        raise ValueError("DispatchIS: no D,DISPATCH,PRICE rows with INTERVENTION=0 found")

    return results


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_dispatch_prices() -> dict[str, DispatchPrice]:
    """Return the latest 5-minute dispatch RRP for all NEM regions.

    Tries ELEC_NEM_SUMMARY first (fast JSON, all regions); falls back to
    DispatchIS_Reports zip (canonical MMS CSV) on any failure.

    Called synchronously via hass.async_add_executor_job.
    """
    # Primary: ELEC_NEM_SUMMARY JSON
    try:
        results = _fetch_nem_summary()
        # Sanity-check freshness: settlement should be within the last 10 minutes
        sample = next(iter(results.values()), None)
        if sample:
            age = _settlement_age_seconds(sample.interval_datetime)
            if age > 600:
                _LOGGER.warning(
                    "ELEC_NEM_SUMMARY data appears stale (age=%.0fs) — trying DispatchIS",
                    age,
                )
                raise ValueError(f"Stale ELEC_NEM_SUMMARY data: age={age:.0f}s")
        _LOGGER.debug(
            "Dispatch prices from ELEC_NEM_SUMMARY: %d regions, settlement=%s",
            len(results),
            sample.interval_datetime if sample else "?",
        )
        return results
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("ELEC_NEM_SUMMARY failed (%s) — falling back to DispatchIS", exc)

    # Fallback: DispatchIS_Reports zip
    results = _fetch_dispatchis()
    sample = next(iter(results.values()), None)
    _LOGGER.debug(
        "Dispatch prices from DispatchIS fallback: %d regions, settlement=%s",
        len(results),
        sample.interval_datetime if sample else "?",
    )
    return results
