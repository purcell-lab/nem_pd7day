"""
NEMWEB Market Notice poller and parser.

Polls https://www.nemweb.com.au/REPORTS/CURRENT/Market_Notice/ directory
for new RESERVE NOTICE (LOR) and MINIMUM SYSTEM LOAD (MSL) notices.

Only current notices are fetched, meaning the current and previous NEM day. An
LOR or MSL notice exists to annotate a forecast period that is running or about
to run, so older notices have no use here and the store prunes them anyway.

The last-seen notice ID advances past every file each cycle considers, including
files that turned out not to be LOR or MSL and files whose fetch failed. Only
advancing it when a relevant notice was found leaves it parked below a growing
backlog, and since LOR and MSL notices are rare, that is the normal case rather
than the exception.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import Callable, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

NEM_TZ = timezone(timedelta(hours=10))
NEMWEB_MARKET_NOTICE_URL = "https://www.nemweb.com.au/REPORTS/CURRENT/Market_Notice/"

# Only notices from the current or previous NEM day are of any use here. An LOR
# or MSL notice annotates a forecast period that is either running now or about
# to, so anything older is dead weight. The previous day is kept because a
# notice issued late in the evening can cover a period that crosses midnight.
#
# Without this cap the directory listing is the only bound on how far back a
# fetch reaches, which is how a stalled cursor turned into 145 file requests per
# region per cycle.
_NOTICE_CURRENT_DAYS = 1

# Hard ceiling on file fetches per cycle, applied oldest-first so the cursor
# still advances contiguously and any remainder is picked up next cycle. This is
# a backstop: with the cursor advancing correctly, a normal cycle fetches a
# handful of files.
_NOTICE_MAX_FILES_PER_CYCLE = 40

# Small delay inside the concurrency gate, so requests are paced rather than
# fired as a burst. Fetches are now bounded by the shared NEMWEB semaphore, so
# this no longer needs to carry throttling on its own and is much shorter than
# the 0.5 s serial sleep it replaces.
_NOTICE_FETCH_DELAY_S = 0.1

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
    # The header line "Notice Type Description : LRC/LOR1/LOR2/LOR3" always
    # contains LOR1 first, so a naive search picks up the header rather than
    # the actual level. Use targeted patterns that skip the header line.
    level = 1
    prefix = "LOR" if notice_type == "LOR" else "MSL"
    # 1. "Level N (LORN)" pattern (External Reference and body)
    level_match = re.search(rf'Level\s+(\d)\s+\({prefix}(\d)\)', text)
    if level_match:
        level = int(level_match.group(2))
    else:
        # 2. "Forecast LORN condition" in the body
        cond_match = re.search(rf'Forecast\s+{prefix}(\d)\s+condition', text)
        if cond_match:
            level = int(cond_match.group(1))
        else:
            # 3. "External Reference" line specifically
            ext_match = re.search(rf'External Reference.*{prefix}(\d)', text)
            if ext_match:
                level = int(ext_match.group(1))

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

    def __init__(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        # Injectable so the current-notice window can be tested against fixed
        # directory fixtures. Without this, any test using a hardcoded date in a
        # filename starts failing once that date falls outside the window.
        self._clock = clock or (lambda: datetime.now(NEM_TZ))
        self.last_seen_notice_id: int = 0
        # Shared with every other NEMWEB caller in the integration so notice
        # fetches cannot burst past the global concurrency cap. A private
        # semaphore is used when none is supplied, which keeps the client usable
        # standalone and in tests.
        self._semaphore = semaphore or asyncio.Semaphore(2)
        # Highest notice ID present in the last directory listing, whether or not
        # its file was fetched. Lets a caller distinguish "nothing new" from
        # "nothing relevant", which the cursor alone cannot express.
        self.highest_listed_notice_id: int = 0

    async def fetch_new_notices(self) -> list[GridNoticeAnnotation]:
        """
        Fetch and parse any new current notices since last_seen_notice_id.

        Only files from the current or previous NEM day are considered, capped at
        _NOTICE_MAX_FILES_PER_CYCLE per call and fetched under the shared NEMWEB
        concurrency gate.

        last_seen_notice_id always advances past every file this call decided
        not to fetch, including ones skipped as stale. Leaving the cursor parked
        below skipped files means the same files are reconsidered on every cycle
        forever, which is precisely the failure this replaces.
        """
        started = time.monotonic()
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

        self.highest_listed_notice_id = max(nid for nid, _ in files)

        # Everything below is bounded to current notices, so the first-run and
        # incremental paths differ only in where the cursor starts.
        cutoff_str = (
            self._clock() - timedelta(days=_NOTICE_CURRENT_DAYS)
        ).strftime("%Y%m%d")

        candidates: list[tuple[int, str]] = []
        stale_ids: list[int] = []
        for nid, fname in files:
            if nid <= self.last_seen_notice_id:
                continue
            date_match = re.search(r"_(\d{8})\.", fname)
            if date_match is not None and date_match.group(1) < cutoff_str:
                # Older than the current window. Record it so the cursor can move
                # past it instead of reconsidering it every cycle.
                stale_ids.append(nid)
                continue
            candidates.append((nid, fname))

        # Oldest first, so the cap below truncates the newest and the cursor
        # advances without leaving unfetched gaps behind it.
        candidates.sort(key=lambda item: item[0])
        deferred = 0
        if len(candidates) > _NOTICE_MAX_FILES_PER_CYCLE:
            deferred = len(candidates) - _NOTICE_MAX_FILES_PER_CYCLE
            candidates = candidates[:_NOTICE_MAX_FILES_PER_CYCLE]

        # Move the cursor past stale files, but never past a file still queued
        # for fetching, otherwise the deferred remainder would be lost.
        if stale_ids:
            limit = candidates[0][0] if candidates else None
            passable = [
                nid for nid in stale_ids if limit is None or nid < limit
            ]
            if passable and max(passable) > self.last_seen_notice_id:
                self.last_seen_notice_id = max(passable)
                _LOGGER.debug(
                    "Market notices: skipped %d file(s) dated before %s, cursor "
                    "advanced to %d",
                    len(passable),
                    cutoff_str,
                    self.last_seen_notice_id,
                )

        if not candidates:
            _LOGGER.debug(
                "Market notices: no current notices to fetch, cursor at %d "
                "(listing high-water mark %d), %.0f ms",
                self.last_seen_notice_id,
                self.highest_listed_notice_id,
                (time.monotonic() - started) * 1000,
            )
            return []

        # Fetched concurrently under the shared NEMWEB gate. The previous version
        # slept _NOTICE_FETCH_DELAY_S before each of these sequentially, which on
        # a 145-file backlog was over a minute of deliberate waiting per region
        # per cycle.
        results = await asyncio.gather(
            *(self._fetch_guarded(nid, fname) for nid, fname in candidates)
        )
        notices = [n for n in results if n is not None]

        # Advance past everything just attempted, including files that were not
        # LOR or MSL and files whose fetch failed. A failed fetch is not retried:
        # notices are immutable once published, so a transient 403 on an
        # irrelevant file is not worth re-reading the whole window for, and
        # retrying is what produced the runaway request volume.
        self.last_seen_notice_id = max(
            [self.last_seen_notice_id] + [nid for nid, _ in candidates]
        )

        _LOGGER.debug(
            "Market notices: fetched %d current file(s), found %d LOR/MSL "
            "notice(s), %d deferred to next cycle, cursor now %d, %.0f ms",
            len(candidates),
            len(notices),
            deferred,
            self.last_seen_notice_id,
            (time.monotonic() - started) * 1000,
        )
        return notices

    async def _fetch_guarded(
        self, notice_id: int, filename: str
    ) -> Optional[GridNoticeAnnotation]:
        """Fetch one notice, holding the shared NEMWEB concurrency gate."""
        async with self._semaphore:
            await asyncio.sleep(_NOTICE_FETCH_DELAY_S)
            return await self._fetch_and_parse(notice_id, filename)

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
