"""
NEMWEB Market Notice poller and parser.

Polls https://www.nemweb.com.au/REPORTS/CURRENT/Market_Notice/ directory
for new RESERVE NOTICE (LOR) and MINIMUM SYSTEM LOAD (MSL) notices.
Tracks last-seen notice ID to avoid re-fetching old files.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

NEM_TZ = timezone(timedelta(hours=10))
NEMWEB_MARKET_NOTICE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Market_Notice/"

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
            "forecast_mw": self.forecast_mw,
            "reserve_req_mw": self.reserve_req_mw,
            "surplus_mw": self.surplus_mw,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GridNoticeAnnotation":
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
            forecast_mw=d.get("forecast_mw"),
            reserve_req_mw=d.get("reserve_req_mw"),
            surplus_mw=d.get("surplus_mw"),
        )


def _parse_directory_listing(html: str) -> list[tuple[int, str]]:
    """
    Parse NEMWEB directory listing HTML.
    Returns list of (notice_id, filename) sorted ascending by notice_id.
    Only includes MKTNOTICE files.
    """
    pattern = re.compile(r'(NEMITWEB1_MKTNOTICE_\d{8}\.R(\d+))')
    matches = pattern.findall(html)
    result = [(int(notice_id), filename) for filename, notice_id in matches]
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
    if is_cancelled:
        ref_match = re.search(r'[Rr]efer to Market Notice (\d+)', text)
        if ref_match:
            cancels_notice_id = int(ref_match.group(1))

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
        # Extract "From HHMM hrs to HHMM hrs DD/MM/YYYY"
        period_match = re.search(
            r'[Ff]rom\s+(\d{4})\s*hrs?\s+to\s+(\d{4})\s*hrs?\s+(\d{2}/\d{2}/\d{4})',
            text
        )
        if period_match:
            hfrom = period_match.group(1)
            hto = period_match.group(2)
            date_str = period_match.group(3)
            try:
                base_date = datetime.strptime(date_str, "%d/%m/%Y")
                period_from = base_date.replace(
                    hour=int(hfrom[:2]), minute=int(hfrom[2:]), tzinfo=NEM_TZ
                )
                to_h = int(hto[:2])
                to_m = int(hto[2:])
                if to_h == 0 and to_m == 0:
                    # "0000 hrs" on same date means midnight = next day start
                    period_to = (base_date + timedelta(days=1)).replace(tzinfo=NEM_TZ)
                elif int(hto) < int(hfrom):
                    # spans midnight
                    period_to = (base_date + timedelta(days=1)).replace(
                        hour=to_h, minute=to_m, tzinfo=NEM_TZ
                    )
                else:
                    period_to = base_date.replace(hour=to_h, minute=to_m, tzinfo=NEM_TZ)
            except (ValueError, IndexError):
                pass

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
                NEMWEB_MARKET_NOTICE_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except Exception as exc:
            _LOGGER.warning("Failed to fetch Market_Notice directory: %s", exc)
            return []

        files = _parse_directory_listing(html)
        if not files:
            return []

        # First-run bootstrap: initialise cursor to highest current notice ID
        # without fetching any files. Only notices issued after this point matter.
        if self.last_seen_notice_id == 0:
            self.last_seen_notice_id = files[-1][0]
            _LOGGER.info(
                "Market notice client initialised at notice_id=%d (no backfill)",
                self.last_seen_notice_id,
            )
            return []

        new_files = [(nid, fname) for nid, fname in files if nid > self.last_seen_notice_id]

        if not new_files:
            return []

        notices = []
        for notice_id, filename in new_files:
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
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                resp.raise_for_status()
                text = await resp.text()
        except Exception as exc:
            _LOGGER.debug("Failed to fetch notice %d: %s", notice_id, exc)
            return None
        return _parse_notice_body(text, notice_id)
