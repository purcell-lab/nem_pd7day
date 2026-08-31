"""
TradingIS Client — fetches 30-minute actual NEM settlement prices from NEMWeb.

Each TradingIS file covers one 30-minute trading interval and contains a
single D,TRADING,PRICE row per region.  The RRP here is the trading interval
settlement price ($/MWh) — averaged across the six 5-minute dispatch intervals
by AEMO — which is the correct basis for calibration observations.

Directory: https://www.nemweb.com.au/Reports/Current/TradingIS_Reports/
Files:     PUBLIC_TRADINGIS_YYYYMMDDHHMI_<seq>.zip
           where YYYYMMDDHHMI is the trading interval END datetime.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import aiohttp

from .const import NEMWEB_HEADERS, TRADINGIS_BASE_URL
from .executor import ExecutorJob, run_in_executor
from .nem_time import NEM_TZ
from .nemweb_retry import classify_status, fetch_with_retry

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

# A dated TradingIS zip that is absent is the normal state for a few seconds
# after an interval closes: this client is scheduled at HH:02 and HH:32 and
# AEMO publishes on its own schedule. A 404 there means "not published yet",
# so it logs at debug and is not retried, because no amount of retrying inside
# one polling cycle makes AEMO publish sooner.
_ZIP_NOT_PUBLISHED_STATUSES = (404,)

# The directory listing URL always exists, so a 404 on it means the report path
# moved or something is intercepting the request. That warns, and is pointless
# to retry.
_DIRECTORY_NOT_PUBLISHED_STATUSES: tuple[int, ...] = ()

# Regex for TradingIS filenames — YYYYMMDDHHMI is the 30-min interval END
_FILENAME_RE = re.compile(
    r"(PUBLIC_TRADINGIS_(\d{12})_\d+\.zip)", re.IGNORECASE
)

# Directory listing cache TTL (seconds)
_DIR_CACHE_TTL = 90


class TradingISClient:
    """Fetches actual 30-minute NEM settlement prices from AEMO TradingIS reports.

    Each call to fetch_interval_price() downloads a single zip for the requested
    trading interval and extracts the D,TRADING,PRICE row for the given region.
    The returned price is the 30-minute settlement RRP ($/kWh).
    """

    BASE_URL = TRADINGIS_BASE_URL

    def __init__(
        self,
        session: aiohttp.ClientSession,
        executor_job: ExecutorJob | None = None,
        semaphore: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        # hass.async_add_executor_job — see executor.py.
        self._executor_job = executor_job
        self._dir_cache: dict[datetime, str] | None = None
        self._dir_cache_ts: float = 0.0
        # Shared with every other NEMWEB caller in the integration, so retries
        # here cannot push concurrent requests past
        # NEMWEB_MAX_CONCURRENT_REQUESTS. This client previously took no
        # semaphore at all and so sat outside the global cap.
        self._semaphore = semaphore
        # Injectable so retry tests do not spend real seconds asleep.
        self._sleep = sleep or asyncio.sleep

    async def fetch_interval_price(
        self,
        region: str,
        interval_start: datetime,
    ) -> float | None:
        """Return the 30-minute settlement RRP ($/kWh) for the given interval.

        interval_start must be a NEM-aware datetime (UTC+10:00).
        The TradingIS file is keyed by interval END = interval_start + 30 min.

        Returns None if the file is not yet published or the region is absent.
        """
        interval_end = interval_start + timedelta(minutes=30)

        directory = await self._fetch_directory()
        if directory is None:
            # _fetch_directory has already logged the exception and the URL, at
            # warning for a genuine failure and at debug otherwise. Returning
            # None here means the interval is recorded as unavailable, never as
            # a zero price.
            return None

        url = directory.get(interval_end)
        if url is None:
            _LOGGER.debug(
                "TradingIS: no file for interval_end=%s (NEMtime) (directory has %d entries)",
                interval_end.strftime("%Y-%m-%dT%H:%M"),
                len(directory),
            )
            return None

        price = await self._fetch_price_from_zip(url, region)
        if price is not None:
            _LOGGER.debug(
                "  TradingIS: %s interval_end=%s (NEMtime) — $%.4f/kWh",
                region,
                interval_end.strftime("%Y-%m-%dT%H:%M"),
                price,
            )
        return price

    async def _fetch_directory(self) -> dict[datetime, str] | None:
        """Return {interval_end_datetime: full_url} for all TradingIS zip files.

        Caches the result for _DIR_CACHE_TTL seconds. Returns None when the
        listing could not be fetched; fetch_with_retry has logged the exception
        and the URL by then, so the caller does not log again.
        """
        now = time.monotonic()
        if (
            self._dir_cache is not None
            and (now - self._dir_cache_ts) < _DIR_CACHE_TTL
        ):
            return self._dir_cache

        html = await fetch_with_retry(
            self._get_directory_html,
            url=self.BASE_URL,
            label="TradingIS directory listing",
            logger=_LOGGER,
            semaphore=self._semaphore,
            sleep=self._sleep,
            retryable_exceptions=_TRANSPORT_ERRORS,
        )
        if html is None:
            # A failed listing is not cached, so the next tick retries rather
            # than serving an empty directory for _DIR_CACHE_TTL seconds.
            return None

        result: dict[datetime, str] = {}
        for match in _FILENAME_RE.finditer(html):
            filename = match.group(1)
            ts_str = match.group(2)   # YYYYMMDDHHMI (12 digits)
            try:
                dt = datetime.strptime(ts_str, "%Y%m%d%H%M").replace(tzinfo=NEM_TZ)
            except ValueError:
                continue
            result[dt] = self.BASE_URL + filename

        self._dir_cache = result
        self._dir_cache_ts = now
        return result

    async def _get_directory_html(self) -> str:
        """One attempt at the directory listing. Raises on a bad status."""
        async with self._session.get(self.BASE_URL, headers=NEMWEB_HEADERS) as resp:
            classify_status(
                resp.status,
                url=self.BASE_URL,
                headers=getattr(resp, "headers", None),
                not_published_statuses=_DIRECTORY_NOT_PUBLISHED_STATUSES,
            )
            return await resp.text()

    async def _get_zip_bytes(self, url: str) -> bytes:
        """One attempt at a TradingIS zip. Raises on a bad status."""
        async with self._session.get(url, headers=NEMWEB_HEADERS) as resp:
            classify_status(
                resp.status,
                url=url,
                headers=getattr(resp, "headers", None),
                not_published_statuses=_ZIP_NOT_PUBLISHED_STATUSES,
            )
            return await resp.read()

    async def _fetch_price_from_zip(
        self,
        url: str,
        region: str,
    ) -> float | None:
        """Download a TradingIS zip, parse the D,TRADING,PRICE row for region.

        Returns the RRP in $/kWh, or None if region not found or on error.
        """
        data = await fetch_with_retry(
            lambda: self._get_zip_bytes(url),
            url=url,
            label="TradingIS zip",
            logger=_LOGGER,
            semaphore=self._semaphore,
            sleep=self._sleep,
            retryable_exceptions=_TRANSPORT_ERRORS,
        )
        if data is None:
            return None

        # Zip and CSV errors are not retried: the bytes arrived, they are just
        # not what we expected, and asking NEMWEB again for the same file will
        # produce the same bytes.
        try:
            # Small archive, but decompression is still CPU on the loop.
            csv_content = await run_in_executor(
                self._executor_job, self._unzip_csv, data
            )
        except (zipfile.BadZipFile, KeyError, IndexError) as exc:
            _LOGGER.warning("TradingIS: bad zip from %s: %s", url, exc)
            return None
        if csv_content is None:
            return None

        return self._parse_rrp(csv_content, region)

    @staticmethod
    def _unzip_csv(data: bytes) -> str | None:
        """Extract the first CSV member as text. Runs in the executor."""
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            if not names:
                return None
            return zf.read(names[0]).decode("utf-8", errors="replace")

    @staticmethod
    def _parse_rrp(content: str, region: str) -> float | None:
        """Extract the RRP ($/kWh) for region from a TradingIS CSV.

        Looks for D,TRADING,PRICE rows.
        Column layout (0-indexed):
          [0] D  [1] TRADING  [2] PRICE  [4] SETTLEMENTDATE
          [6] REGIONID  [8] RRP
        """
        for line in content.splitlines():
            parts = line.split(",")
            if len(parts) < 9:
                continue
            if parts[0] != "D" or parts[1] != "TRADING" or parts[2] != "PRICE":
                continue
            region_id = parts[6].strip().strip('"')
            if region_id != region:
                continue
            try:
                rrp_mwh = float(parts[8].strip().strip('"'))
                return round(rrp_mwh / 1000.0, 6)
            except (ValueError, IndexError):
                continue
        return None
