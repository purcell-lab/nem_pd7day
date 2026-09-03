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
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .const import DISPATCHIS_BASE_URL, ELEC_NEM_SUMMARY_URL, NEMWEB_HEADERS

_LOGGER = logging.getLogger(__name__)

# Primary: AEMO visualisation API — returns all regions in a single JSON response.
# Undocumented but stable; used by multiple open-source HA integrations.
NEM_SUMMARY_URL = ELEC_NEM_SUMMARY_URL

# Fallback: NEMWeb DispatchIS — canonical 5-minute MMS price files.
DISPATCHIS_BASE = DISPATCHIS_BASE_URL
_DISPATCHIS_FILE_RE = re.compile(
    r"(PUBLIC_DISPATCHIS_\d{12}_\d+\.zip)", re.IGNORECASE
)

# Price status values accepted from both sources.
_FIRM_STATUSES = {"FIRM", "CALCULATED"}


class StaleIntervalError(Exception):
    """Raised when ELEC_NEM_SUMMARY returns an older-than-expected interval."""


@dataclass
class DispatchPrice:
    region: str
    # Interval END in NEM time, no tz suffix. The format depends on which
    # path filled it: ELEC_NEM_SUMMARY gives ISO, "2026-09-04T06:45:00";
    # the DispatchIS fallback gives the MMS CSV form, "2026/05/29 11:10:00".
    # Use parse_settlement() rather than assuming either (issue #104).
    interval_datetime: str
    rrp: float              # $/kWh (converted from $/MWh)


_SETTLEMENT_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S")

# NEM time is UTC+10 with no daylight saving.
_NEM_UTC_OFFSET = timedelta(hours=10)


def parse_settlement(settlement_str: str) -> datetime:
    """Parse a SETTLEMENTDATE string from either source into a naive NEM-time datetime.

    ELEC_NEM_SUMMARY returns ISO without an offset ("2026-09-04T06:45:00");
    DispatchIS CSV returns slash-delimited ("2026/05/29 11:10:00"). Both are
    NEM time. Raises ValueError for anything else, so a format change at AEMO
    surfaces as a parse error rather than masquerading as stale data.
    """
    for fmt in _SETTLEMENT_FORMATS:
        try:
            return datetime.strptime(settlement_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised SETTLEMENTDATE format: {settlement_str!r}")


def settlement_iso(settlement_str: str) -> str:
    """Render a settlement string from either source as ISO to the minute."""
    try:
        return parse_settlement(settlement_str).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return settlement_str


# ── Primary: ELEC_NEM_SUMMARY JSON ───────────────────────────────────────────

def _fetch_nem_summary() -> dict[str, DispatchPrice]:
    """Fetch all regions from the AEMO ELEC_NEM_SUMMARY JSON API.

    Returns a dict keyed by REGIONID.  Raises on any network or parse error.
    """
    req = urllib.request.Request(
        NEM_SUMMARY_URL,
        headers={**NEMWEB_HEADERS, "Accept": "application/json"},
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
        # SETTLEMENTDATE is interval END in NEM time, ISO with no offset:
        # "2026-09-04T06:45:00" (verified against the live API, issue #104).
        settlement = row.get("SETTLEMENTDATE", "")
        results[region] = DispatchPrice(
            region=region,
            interval_datetime=settlement,
            rrp=round(rrp_mwh / 1000.0, 6),
        )

    if not results:
        raise ValueError("ELEC_NEM_SUMMARY: no FIRM prices found in response")

    return results


def _settlement_age_seconds(
    settlement_str: str, now: datetime | None = None
) -> float:
    """Return how many seconds ago the settlement interval ended.

    settlement_str is NEM time without tz suffix, in either source format
    (see parse_settlement). *now* is an aware UTC datetime; tests pass one to
    pin the clock.

    Raises ValueError when the string does not parse. This used to return a
    9999.0 sentinel instead, which tripped the stale branch and blamed AEMO
    for what would have been a parse bug; letting the error out means the
    caller logs the real reason (issue #104).
    """
    dt_nem = parse_settlement(settlement_str)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    dt_utc = (dt_nem - _NEM_UTC_OFFSET).replace(tzinfo=timezone.utc)
    return (now_utc - dt_utc).total_seconds()


# ── Fallback: DispatchIS_Reports zip ─────────────────────────────────────────

def _fetch_dispatchis() -> dict[str, DispatchPrice] | None:
    """Fetch the latest DispatchIS zip from NEMWeb and parse D,DISPATCH,PRICE rows.

    Returns a dict keyed by REGIONID, or None if the zip is not yet published
    (HTTP 404).  Raises on other network or parse errors.
    """
    # Both requests carry NEMWEB_HEADERS. Before issue #102 they carried no
    # User-Agent at all, so they went out as Python-urllib, the exact
    # automated pattern the browser-like UA in const.py exists to avoid.
    index_req = urllib.request.Request(DISPATCHIS_BASE, headers=NEMWEB_HEADERS)
    index = urllib.request.urlopen(index_req, timeout=15).read().decode(
        "utf-8", errors="ignore"
    )
    files = sorted(set(_DISPATCHIS_FILE_RE.findall(index)))
    if not files:
        raise ValueError("No DispatchIS files found in directory listing")

    url = DISPATCHIS_BASE + files[-1]
    _LOGGER.debug("DispatchIS URL: %s", url)
    try:
        zip_req = urllib.request.Request(url, headers=NEMWEB_HEADERS)
        raw = urllib.request.urlopen(zip_req, timeout=20).read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            _LOGGER.warning("DispatchIS 404 — zip not yet published: %s", url)
            return None
        raise
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
        settlement = parts[4]   # MMS CSV form, "2026/05/29 11:10:00"
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

def fetch_dispatch_prices(
    expected_settlement: datetime | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, DispatchPrice]:
    """Return the latest 5-minute dispatch RRP for all NEM regions.

    Tries ELEC_NEM_SUMMARY first (fast JSON, all regions); falls back to
    DispatchIS_Reports zip (canonical MMS CSV) on any failure.

    If *expected_settlement* is provided and the ELEC_NEM_SUMMARY interval is
    behind that timestamp, a StaleIntervalError is raised so the caller can
    retry before falling back to DispatchIS.

    *clock* returns the current aware UTC time; it exists so tests can pin
    the freshness check instead of racing wall-clock time (issue #109). This
    module holds no hass reference, so it cannot use dt_util.

    Called synchronously via hass.async_add_executor_job.
    """
    now_utc = clock() if clock is not None else datetime.now(timezone.utc)
    # Primary: ELEC_NEM_SUMMARY JSON
    try:
        results = _fetch_nem_summary()
        # Sanity-check freshness: settlement should be within the last 10 minutes
        sample = next(iter(results.values()), None)
        if sample:
            age = _settlement_age_seconds(sample.interval_datetime, now=now_utc)
            if age > 600:
                _LOGGER.warning(
                    "ELEC_NEM_SUMMARY data appears stale (age=%.0fs) — trying DispatchIS",
                    age,
                )
                raise ValueError(f"Stale ELEC_NEM_SUMMARY data: age={age:.0f}s")

            # Log settlement + price before gate check
            _LOGGER.debug(
                "ELEC_NEM_SUMMARY fetched: %s",
                ", ".join(
                    f"{r} settlement={results[r].interval_datetime} ${results[r].rrp:.4f}/kWh"
                    for r in sorted(results)
                ),
            )

            # Gate: if caller expects a specific settlement, verify it
            if expected_settlement is not None:
                actual_str = sample.interval_datetime
                actual_dt = parse_settlement(actual_str)
                # Both sides tz-naive NEM time
                if actual_dt < expected_settlement.replace(tzinfo=None):
                    _LOGGER.debug(
                        "ELEC_NEM_SUMMARY: settlement=%s is behind expected %s — will retry",
                        actual_str,
                        expected_settlement.strftime("%Y-%m-%dT%H:%M"),
                    )
                    raise StaleIntervalError(
                        f"ELEC_NEM_SUMMARY: settlement {actual_str} < expected "
                        f"{expected_settlement.strftime('%Y-%m-%dT%H:%M')}"
                    )

        # No second summary line on the success path (issue #33).  The
        # "ELEC_NEM_SUMMARY fetched: ..." line above already names every
        # region with its settlement and price, so a follow-up
        # "Dispatch: %d regions fetched, settlement=..." record restated the
        # count and settlement that line already carried.  The DispatchIS
        # fallback below keeps its own line because that path emits no
        # per-region summary and is rare enough to be worth announcing.
        return results
    except StaleIntervalError:
        raise  # let caller handle retry — don't fall through to DispatchIS
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("ELEC_NEM_SUMMARY failed (%s) — falling back to DispatchIS", exc)

    # Fallback: DispatchIS_Reports zip
    results = _fetch_dispatchis()
    if results is None:
        raise ValueError("DispatchIS fallback unavailable (zip not yet published)")
    sample = next(iter(results.values()), None)
    _LOGGER.debug(
        "Dispatch (DispatchIS fallback): %d regions fetched, settlement=%s (NEMtime)",
        len(results),
        settlement_iso(sample.interval_datetime) if sample else "?",
    )
    return results
