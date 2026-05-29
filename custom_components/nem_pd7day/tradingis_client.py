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

import io
import logging
import re
import time
import zipfile
from datetime import datetime, timedelta

import aiohttp

from .const import TRADINGIS_BASE_URL
from .nem_time import NEM_TZ

_LOGGER = logging.getLogger(__name__)

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

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._dir_cache: dict[datetime, str] | None = None
        self._dir_cache_ts: float = 0.0

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

        try:
            directory = await self._fetch_directory()
        except Exception:
            _LOGGER.warning("TradingIS: failed to fetch directory listing")
            return None

        url = directory.get(interval_end)
        if url is None:
            _LOGGER.debug(
                "TradingIS: no file for interval end %s (directory has %d entries)",
                interval_end.strftime("%Y-%m-%dT%H:%M"),
                len(directory),
            )
            return None

        price = await self._fetch_price_from_zip(url, region)
        if price is not None:
            _LOGGER.debug(
                "TradingIS: %s interval_end=%s — $%.4f/kWh",
                region,
                interval_end.strftime("%Y-%m-%dT%H:%M"),
                price,
            )
        return price

    async def _fetch_directory(self) -> dict[datetime, str]:
        """Return {interval_end_datetime: full_url} for all TradingIS zip files.

        Caches the result for _DIR_CACHE_TTL seconds.
        """
        now = time.monotonic()
        if (
            self._dir_cache is not None
            and (now - self._dir_cache_ts) < _DIR_CACHE_TTL
        ):
            return self._dir_cache

        async with self._session.get(self.BASE_URL) as resp:
            resp.raise_for_status()
            html = await resp.text()

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

    async def _fetch_price_from_zip(
        self,
        url: str,
        region: str,
    ) -> float | None:
        """Download a TradingIS zip, parse the D,TRADING,PRICE row for region.

        Returns the RRP in $/kWh, or None if region not found or on error.
        """
        try:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.read()
        except aiohttp.ClientError as exc:
            _LOGGER.warning("TradingIS: failed to download %s: %s", url, exc)
            return None

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if not names:
                    return None
                csv_content = zf.read(names[0]).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError, IndexError) as exc:
            _LOGGER.warning("TradingIS: bad zip from %s: %s", url, exc)
            return None

        return self._parse_rrp(csv_content, region)

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
