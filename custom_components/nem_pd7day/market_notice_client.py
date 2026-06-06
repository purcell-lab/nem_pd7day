"""
NEMWEB Market Notice poller and parser.

Polls https://www.nemweb.com.au/REPORTS/CURRENT/Market_Notice/ directory
for new RESERVE NOTICE (LOR) and MINIMUM SYSTEM LOAD (MSL) notices.
Tracks last-seen notice ID to avoid re-fetching old files.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

NEM_TZ = timezone(timedelta(hours=10))
NEMWEB_MARKET_NOTICE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Market_Notice/"

# On first run (cold start) backfill only this many hours of notices instead of
# the full directory, to limit the startup request burst to NEMWEB.
_NOTICE_BACKFILL_HOURS = 24

# Delay between individual notice file fetches to avoid bursting NEMWEB.
_NOTICE_FETCH_DELAY_S = 0.5

# Relevant notice type codes in the file body
NOTICE_TYPE_LOR = "RESERVE NOTICE"
NOTICE_TYPE_MSL = "MINIMUM SYSTEM LOAD"

# Region name normalisation: notice text uses "SA Region", "VIC region", "QLD1" etc.
REGION_ALIASES = {
    "SA": "SA1", "VIC": "VIC1", "NSW": "NSW1", "QLD": "QLD1", "TAS": "TAS1",
    "SA1": "SA1", "VIC1": "VIC1", "NSW1": "NSW1", "QLD1": "QLD1", "TAS1": "TAS1",
}


@dataclass
class GridNoticeAnnotation:
    """A parsed MSL or LOR market notice."""
    notice_id: int
    notice_type: str          # "LOR" or "MSL"
    level: int                # 1, 2, or 3
    region: str               # normalised: "QLD1", "SA1" etc
    period_from: datetime     # NEM time (tz-aware)
    period_to: datetime       # NEM time (tz-aware)
    issued_at: datetime       # NEM time (tz-aware)
    is_cancelled: bool = False
    cancels_notice_id: Optional[int] = None   # notice ID this cancels
    cancellation_date: Optional[date] = None  # date of LOR/MSL period being cancelled
    forecast_mw: Optional[float] = None       # MSL: forecast minimum demand
    reserve_req_mw: Optional[float] = None    # LOR: reserve requirement
    surplus_mw: Optional[float] = None        # LOR: min capacity available

    def to_dict(self) -> dict:
        return {
            "notice_id": self.notice_id,
            "notice_type": self.notice_type,
            "level": self.level,
            "region": self.region,
            "period_from": self.period_from.isoformat(),
            "period_to": self.period_to.isoformat(),
            "issued_at": self.issued_at.isoformat(),
            "is_cancelled": self.is_cancelled,
            "cancels_notice_id": self.cancels_notice_id,
            "cancellation_date": self.cancellation_date.isoformat() if self.cancellation_date else None,
            "forecast_mw": self.forecast_mw,
            "reserve_req_mw": self.reserve_req_mw,
            "surplus_mw": self.surplus_mw,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GridNoticeAnnotation":
        cancel_date_raw = d.get("cancellation_date")
        cancellation_date = date.fromisoformat(cancel_date_raw) if cancel_date_raw else None
        return cls(
            notice_id=d["notice_id"],
            notice_type=d["notice_type"],
            level=d["level"],
            region=d["region"],
            period_from=datetime.fromisoformat(d["period_from"]),
            period_to=datetime.fromisoformat(d["period_to"]),
            issued_at=datetime.fromisoformat(d["issued_at"]),
            is_cancelled=d.get("is_cancelled", False),
            cancels_notice_id=d.get("cancels_notice_id"),
            cancellation_date=cancellation_date,
            forecast_mw=d.get("forecast_mw"),
            reserve_req_mw=d.get("reserve_req_mw"),
            surplus_mw=d.get("surplus_mw"),
        )


def _parse_directory_listing(html: str) -> list[tuple[int, str]]:
    """
    Parse NEMWEB directory listing HTML.
    Returns deduplicated list of (notice_id, filename) sorted ascending by notice_id.
    Only includes MKTNOTICE files.
    """
    pattern = re.compile(r'(NEMITWEB1_MKTNOTICE_\d{8}\.R(\d+))')
    matches = pattern.findall(html)
    seen: set[int] = set()
    result = []
    for filename, notice_id_str in matches:
        notice_id = int(notice_id_str)
        if notice_id not in seen:
            seen.add(notice_id)
            result.append((notice_id, filename))
    return sorted(result, key=lambda x: x[0])


def _parse_notice_body(text: str, notice_id: int) -> Optional[GridNoticeAnnotation]:
    """
    Parse a market notice plain-text body.
    Returns GridNoticeAnnotation or None if not an LOR/MSL notice.
    """
    # Determine notice type
    if NOTICE_TYPE_LOR in text:
        notice_type = "LOR"
    elif NOTICE_TYPE_MSL in text:
        notice_type = "MSL"
    else:
        return None

    # Extract issue datetime
    # Pattern: e.g. "24/02/2026 12:39:50" or "11/08/2025 03:25:07 PM"
    issued_at = None
    dt_match = re.search(r'(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2}(?:\s*[AP]M)?)', text)
    if dt_match:
        try:
            dt_str = f"{dt_match.group(1)} {dt_match.group(2).strip()}"
            fmt = "%d/%m/%Y %I:%M:%S %p" if "AM" in dt_str.upper() or "PM" in dt_str.upper() else "%d/%m/%Y %H:%M:%S"
            issued_at = datetime.strptime(dt_str, fmt).replace(tzinfo=NEM_TZ)
        except ValueError:
            issued_at = datetime.now(NEM_TZ)
    else:
        issued_at = datetime.now(NEM_TZ)

    # Check for cancellation
    is_cancelled = bool(re.search(r'\bCancell?ation\b', text, re.IGNORECASE))
    cancels_notice_id = None
    cancellation_date = None
    if is_cancelled:
        ref_match = re.search(r'[Rr]efer to Market Notice (\d+)', text)
        if ref_match:
            cancels_notice_id = int(ref_match.group(1))
        # Extract cancellation effective date from "on DD/MM/YYYY" or "at HHMM hrs DD/MM/YYYY"
        cdate_match = re.search(r'(?:on|at\s+\d{4}\s+hrs)\s+(\d{2}/\d{2}/\d{4})', text)
        if cdate_match:
            try:
                cancellation_date = datetime.strptime(cdate_match.group(1), "%d/%m/%Y").date()
            except ValueError:
                pass

    # Extract level (LOR1/LOR2/LOR3 or MSL1/MSL2/MSL3)
    level = 1
    prefix = "LOR" if notice_type == "LOR" else "MSL"
    level_match = re.search(rf'{prefix}(\d)', text)
    if level_match:
        level = int(level_match.group(1))

    # Extract region
    region = None
    # Try explicit region IDs first
    region_match = re.search(r'\b(QLD1|NSW1|VIC1|SA1|TAS1)\b', text)
    if region_match:
        region = region_match.group(1)
    else:
        # Fall back to region names: "SA region", "VIC Region", "Queensland region" etc
        region_name_match = re.search(
            r'\b(SA|VIC|NSW|QLD|Queensland|Victoria|New South Wales|South Australia|Tasmania)\b\s+[Rr]egion',
            text
        )
        if region_name_match:
            name = region_name_match.group(1).upper()
            mapping = {
                "QUEENSLAND": "QLD1", "VICTORIA": "VIC1",
                "NEW SOUTH WALES": "NSW1", "SOUTH AUSTRALIA": "SA1",
                "TASMANIA": "TAS1",
            }
            region = mapping.get(name) or REGION_ALIASES.get(name)
    if not region:
        _LOGGER.debug("Could not extract region from notice %d", notice_id)
        return None

    # For cancellation notices, use issued_at as period placeholder
    period_from = issued_at
    period_to = issued_at

    if not is_cancelled:
        # AEMO format: "From HHMM hrs DD/MM/YYYY to HHMM hrs DD/MM/YYYY"
        # Notices may have multiple numbered periods [1.] [2.] etc.
        # We use the widest window: earliest period_from to latest period_to.
        period_pattern = re.compile(
            r'[Ff]rom\s+(\d{4})\s+hrs\s+(\d{2}/\d{2}/\d{4})\s+to\s+(\d{4})\s+hrs\s+(\d{2}/\d{2}/\d{4})'
        )
        all_periods = period_pattern.findall(text)
        parsed_periods = []
        for hfrom, date_from_str, hto, date_to_str in all_periods:
            try:
                date_from = datetime.strptime(date_from_str, "%d/%m/%Y")
                date_to = datetime.strptime(date_to_str, "%d/%m/%Y")
                pf = date_from.replace(
                    hour=int(hfrom[:2]), minute=int(hfrom[2:]), tzinfo=NEM_TZ
                )
                to_h, to_m = int(hto[:2]), int(hto[2:])
                if to_h == 0 and to_m == 0:
                    pt = (date_to + timedelta(days=1)).replace(tzinfo=NEM_TZ)
                else:
                    pt = date_to.replace(hour=to_h, minute=to_m, tzinfo=NEM_TZ)
                parsed_periods.append((pf, pt))
            except (ValueError, IndexError):
                continue
        if parsed_periods:
            period_from = min(p[0] for p in parsed_periods)
            period_to = max(p[1] for p in parsed_periods)

    # Extract LOR-specific fields
    reserve_req_mw = None
    surplus_mw = None
    if notice_type == "LOR" and not is_cancelled:
        req_match = re.search(r'[Ff]orecast capacity reserve requirement is ([\d,]+)\s*MW', text)
        if req_match:
            reserve_req_mw = float(req_match.group(1).replace(",", ""))
        avail_match = re.search(r'minimum capacity reserve available is ([\d,]+)\s*MW', text, re.IGNORECASE)
        if avail_match:
            surplus_mw = float(avail_match.group(1).replace(",", ""))

    # Extract MSL-specific fields
    forecast_mw = None
    if notice_type == "MSL" and not is_cancelled:
        msl_match = re.search(r'[Mm]inimum regional demand is forecast to be ([\d,]+)\s*MW', text)
        if msl_match:
            forecast_mw = float(msl_match.group(1).replace(",", ""))

    return GridNoticeAnnotation(
        notice_id=notice_id,
        notice_type=notice_type,
        level=level,
        region=region,
        period_from=period_from,
        period_to=period_to,
        issued_at=issued_at,
        is_cancelled=is_cancelled,
        cancels_notice_id=cancels_notice_id,
        cancellation_date=cancellation_date,
        forecast_mw=forecast_mw,
        reserve_req_mw=reserve_req_mw,
        surplus_mw=surplus_mw,
    )


class MarketNoticeClient:
    """
    Polls NEMWEB Market_Notice directory for new LOR and MSL notices.
    Tracks last_seen_notice_id to avoid re-fetching.
    Called 3x per day at the same fetch schedule as pd7day_client.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.last_seen_notice_id: int = 0

    async def fetch_new_notices(self) -> list[GridNoticeAnnotation]:
        """
        Fetch and parse any new notices since last_seen_notice_id.

        On first run (last_seen_notice_id == 0): initialises cursor to the
        highest current notice ID without fetching any files, then returns [].
        Subsequent calls fetch only new notices incrementally.
        """
        try:
            async with self._session.get(
                NEMWEB_MARKET_NOTICE_URL,
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "nem_pd7day/2.3"},
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientResponseError as exc:
            if exc.status == 403:
                # NEMWEB throttled the directory listing — skip this whole
                # cycle rather than hammering it with per-file requests.
                _LOGGER.debug(
                    "[DEBUG] Market_Notice directory returned 403 — skipping cycle"
                )
                return []
            _LOGGER.warning("Failed to fetch Market_Notice directory: %s", exc)
            return []
        except Exception as exc:
            _LOGGER.warning("Failed to fetch Market_Notice directory: %s", exc)
            return []

        files = _parse_directory_listing(html)
        if not files:
            return []

        # First-run bootstrap: fetch notices from the last _NOTICE_BACKFILL_HOURS.
        # The directory filename encodes the date (YYYYMMDD), so we filter by
        # filename date rather than notice_id to avoid fetching thousands of old files.
        if self.last_seen_notice_id == 0:
            # The filename encodes only a date (YYYYMMDD), not a time, so the
            # tightest cap we can apply from the listing alone is whole-day
            # granularity: keep files whose date is >= the cutoff date.
            cutoff = datetime.now(NEM_TZ) - timedelta(hours=_NOTICE_BACKFILL_HOURS)
            cutoff_str = cutoff.strftime("%Y%m%d")  # e.g. "20260510"
            new_files = []
            for nid, fname in files:
                date_match = re.search(r'_(\d{8})\.', fname)
                if date_match and date_match.group(1) >= cutoff_str:
                    new_files.append((nid, fname))
            _LOGGER.info(
                "Market notice client first run: backfilling %d files since %s",
                len(new_files), cutoff_str,
            )
        else:
            new_files = [(nid, fname) for nid, fname in files if nid > self.last_seen_notice_id]

        if not new_files:
            return []

        notices = []
        for notice_id, filename in new_files:
            # Throttle BEFORE every individual notice file GET so there is a
            # genuine _NOTICE_FETCH_DELAY_S gap between each HTTP request.
            await asyncio.sleep(_NOTICE_FETCH_DELAY_S)
            notice = await self._fetch_and_parse(notice_id, filename)
            if notice is not None:
                notices.append(notice)
            # Always advance last_seen even if notice type is not LOR/MSL
            self.last_seen_notice_id = max(self.last_seen_notice_id, notice_id)

        _LOGGER.debug(
            "Market notices: checked %d new files, found %d LOR/MSL notices",
            len(new_files), len(notices)
        )
        return notices

    async def _fetch_and_parse(self, notice_id: int, filename: str) -> Optional[GridNoticeAnnotation]:
        url = NEMWEB_MARKET_NOTICE_URL + filename
        try:
            async with self._session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "nem_pd7day/2.3"},
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()
        except Exception as exc:
            _LOGGER.debug("Failed to fetch notice %d: %s", notice_id, exc)
            return None
        return _parse_notice_body(text, notice_id)
