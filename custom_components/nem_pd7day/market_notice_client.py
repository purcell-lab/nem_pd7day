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
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import aiohttp

from .nemweb_retry import classify_status, fetch_with_retry
from .const import NEM_TZ, NEMWEB_HEADERS

_LOGGER = logging.getLogger(__name__)

# aiohttp transport failures are retryable, but nemweb_retry stays free of the
# aiohttp import so it can be unit tested, so the class is handed to it from
# here. Resolved defensively because unit tests stub the aiohttp module out
# with a MagicMock, where ClientError is not an exception class at all and
# naming it in an except clause would raise TypeError.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = tuple(
    candidate
    for candidate in (getattr(aiohttp, "ClientError", None),)
    if isinstance(candidate, type) and issubclass(candidate, BaseException)
)

# A notice file named in the listing but answering 404 has been withdrawn or
# is not yet readable. Nothing to report and nothing to retry, so it stays at
# debug and is not counted as a failure.
_NOTICE_NOT_PUBLISHED_STATUSES = (404,)

# The Market_Notice directory always exists, so a 404 on it means the report
# path moved or something is intercepting the request. That is worth a warning
# and pointless to retry.
_DIRECTORY_NOT_PUBLISHED_STATUSES: tuple[int, ...] = ()

# One retry per notice file, against three for the directory listing. The
# listing is a single request that gates the whole cycle, so it is worth a
# full ladder. Per-file fetches fan out to _NOTICE_MAX_FILES_PER_CYCLE, and a
# three attempt ladder on each would treble request volume against a NEMWEB
# that answers 403 precisely when it wants less traffic. One retry absorbs a
# scattered failure without that risk.
_NOTICE_FILE_MAX_ATTEMPTS = 2


class _NotPublished:
    """Sentinel: the notice file answered a not-published status.

    Needed because ``fetch_with_retry`` collapses every unsuccessful outcome to
    None, and a withdrawn notice must not be counted towards the cycle failure
    total that raises a warning.
    """


_NOT_PUBLISHED = _NotPublished()

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
        # Per-file fetch failures in the cycle currently running. Each per-file
        # fetch suppresses its own give-up warning so that a bad cycle produces
        # one aggregated warning rather than up to forty. Reset at the top of
        # every fetch_new_notices call.
        self._cycle_fetch_failures: int = 0

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
        self._cycle_fetch_failures = 0

        # The listing gates the whole cycle, so a scattered 403 or read timeout
        # here costs every notice in it. Retried under the shared NEMWEB gate,
        # then warned about once. The previous version returned an empty list on
        # a 403 with only a debug line, so a sustained throttle or outage left
        # the grid notices sensor frozen with nothing in the log at the default
        # level to explain why. See issue #44.
        html = await fetch_with_retry(
            self._get_directory_html,
            url=NEMWEB_MARKET_NOTICE_URL,
            label="Market_Notice directory listing",
            logger=_LOGGER,
            semaphore=self._semaphore,
            retryable_exceptions=_TRANSPORT_ERRORS,
        )
        if html is None:
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

        # One warning per bad cycle. Each per-file fetch ran with its own
        # give-up log suppressed to debug, so without this line a total notice
        # outage would still be silent at the default log level, which is the
        # defect in issue #44. Every failure keeps its detail at debug.
        if self._cycle_fetch_failures:
            _LOGGER.warning(
                "Market notices: %d of %d notice file fetch(es) failed this "
                "cycle after retry, url=%s. Enable debug logging on %s for the "
                "per-file cause.",
                self._cycle_fetch_failures,
                len(candidates),
                NEMWEB_MARKET_NOTICE_URL,
                __name__,
            )

        # Advance past everything just attempted, including files that were not
        # LOR or MSL and files whose fetch failed. A failure gets its bounded
        # retry inside this cycle and is then left behind: notices are immutable
        # once published, so a transient 403 on an irrelevant file is not worth
        # re-reading the whole window for on every subsequent cycle, and that
        # unbounded re-reading is what produced the runaway request volume.
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
        """Fetch one notice, holding the shared NEMWEB concurrency gate.

        The gate is held across the retry ladder rather than acquired per
        attempt, so a backoff sleep does occupy a slot. That is the deliberate
        trade: pacing requests inside the gate is what keeps a large candidate
        batch from arriving as a burst, and with one retry and a gate of two
        the idle time is a few seconds across a whole bad cycle, on a client
        that runs three times a day.
        """
        async with self._semaphore:
            await asyncio.sleep(_NOTICE_FETCH_DELAY_S)
            return await self._fetch_and_parse(notice_id, filename)

    async def _get_directory_html(self) -> str:
        """One attempt at the directory listing. Raises on a bad status."""
        async with self._session.get(
            NEMWEB_MARKET_NOTICE_URL,
            timeout=aiohttp.ClientTimeout(total=30),
            headers=NEMWEB_HEADERS,
        ) as resp:
            classify_status(
                resp.status,
                url=NEMWEB_MARKET_NOTICE_URL,
                headers=getattr(resp, "headers", None),
                not_published_statuses=_DIRECTORY_NOT_PUBLISHED_STATUSES,
            )
            return await resp.text()

    async def _get_notice_text(self, url: str) -> str | _NotPublished:
        """One attempt at a single notice file. Raises on a bad status.

        Returns the _NOT_PUBLISHED sentinel rather than raising for a withdrawn
        file, so the caller can tell it apart from a genuine failure once
        fetch_with_retry has collapsed both to a return value.
        """
        async with self._session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=15),
            headers=NEMWEB_HEADERS,
        ) as resp:
            if resp.status in _NOTICE_NOT_PUBLISHED_STATUSES:
                _LOGGER.debug(
                    "Market notice file not published, HTTP %d, url=%s",
                    resp.status,
                    url,
                )
                return _NOT_PUBLISHED
            classify_status(
                resp.status,
                url=url,
                headers=getattr(resp, "headers", None),
            )
            return await resp.text()

    async def _fetch_and_parse(self, notice_id: int, filename: str) -> Optional[GridNoticeAnnotation]:
        """Fetch and parse one notice, or return None.

        A failure here is counted rather than warned about individually, so the
        caller can emit one line per cycle instead of one per file. The retry
        budget is deliberately shorter than the directory listing's, because
        this runs once per candidate file.
        """
        url = NEMWEB_MARKET_NOTICE_URL + filename
        text = await fetch_with_retry(
            lambda: self._get_notice_text(url),
            url=url,
            label=f"Market notice {notice_id}",
            logger=_LOGGER,
            max_attempts=_NOTICE_FILE_MAX_ATTEMPTS,
            retryable_exceptions=_TRANSPORT_ERRORS,
            warn_on_exhausted=False,
        )
        if text is _NOT_PUBLISHED:
            # A withdrawn or not-yet-readable file is not an outage, so it must
            # not contribute to the cycle warning.
            return None
        if text is None:
            self._cycle_fetch_failures += 1
            return None
        return _parse_notice_body(text, notice_id)
