"""
TradingIS Client — fetches actual NEM dispatch prices from NEMWeb.

Downloads 5-minute TradingIS reports (zip → CSV) and averages the RRP
values across a 30-minute trading interval for a given region.

The AEMO TradingIS directory listing at BASE_URL contains files named:
  PUBLIC_TRADINGIS_YYYYMMDDHHMI_<seq>.zip
where YYYYMMDDHHMI is the settlement datetime (5-min interval end).
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

# Regex for TradingIS filenames in the directory listing
_FILENAME_RE = re.compile(
    r"PUBLIC_TRADINGIS_(\d{12})_\d+\.zip", re.IGNORECASE
)

# Cache TTL in seconds
_DIR_CACHE_TTL = 90


class TradingISClient:
    """Fetches actual NEM dispatch prices from AEMO TradingIS reports."""

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
        """
        Return the average RRP ($/kWh) for the 30-min trading interval
        starting at interval_start, or None if insufficient data.

        interval_start must be a NEM-aware datetime (UTC+10:00).
        """
        # Compute the 6 x 5-min settlement end times
        targets: list[datetime] = [
            interval_start + timedelta(minutes=5 * i)
            for i in range(1, 7)
        ]

        try:
            directory = await self._fetch_directory()
        except Exception:
            _LOGGER.warning("TradingIS: failed to fetch directory listing")
            return None

        prices: list[float] = []
        for target_dt in targets:
            url = directory.get(target_dt)
            if url is None:
                continue
            price = await self._fetch_price_from_zip(url, region, target_dt)
            if price is not None:
                prices.append(price)

        if len(prices) < 4:
            _LOGGER.debug(
                "TradingIS: only %d of 6 intervals found for %s %s (need >= 4)",
                len(prices), region, interval_start.isoformat(),
            )
            return None

        avg_mwh = sum(prices) / len(prices)
        avg_kwh = avg_mwh / 1000.0
        return avg_kwh

    async def _fetch_directory(self) -> dict[datetime, str]:
        """
        Fetch the BASE_URL directory listing and return
        {settlement_datetime: full_url} for all .zip files.

        Caches the result for 90 seconds (instance-level).
        """
        now = time.monotonic()
        if self._dir_cache is not None and (now - self._dir_cache_ts) < _DIR_CACHE_TTL:
            return self._dir_cache

        async with self._session.get(self.BASE_URL) as resp:
            resp.raise_for_status()
            html = await resp.text()

        result: dict[datetime, str] = {}
        for match in _FILENAME_RE.finditer(html):
            filename = match.group(0)
            ts_str = match.group(1)  # YYYYMMDDHHMI (12 digits)
            try:
                dt = datetime.strptime(ts_str, "%Y%m%d%H%M").replace(tzinfo=NEM_TZ)
            except ValueError:
                continue
            result[dt] = self.BASE_URL + filename

        self._dir_cache = result
        self._dir_cache_ts = time.monotonic()
        return result

    async def _fetch_price_from_zip(
        self, url: str, region: str, target_dt: datetime
    ) -> float | None:
        """Download a single zip, extract CSV, return RRP for the region."""
        try:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.read()
        except aiohttp.ClientError:
            _LOGGER.warning("TradingIS: failed to download %s", url)
            return None

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if not names:
                    return None
                csv_content = zf.read(names[0]).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError, IndexError):
            _LOGGER.warning("TradingIS: bad zip file from %s", url)
            return None

        parsed = self._parse_csv(csv_content, region)
        if not parsed:
            return None

        # Return the first (and typically only) RRP for this region
        # from this single-interval file
        for rrp in parsed.values():
            return rrp
        return None

    def _parse_csv(self, content: str, region: str) -> dict[str, float]:
        """
        Parse TradingIS CSV content.
        Return {settlementdate_str: rrp_float} for the given region.
        Only processes D,TRADING,PRICE rows.
        """
        result: dict[str, float] = {}
        for line in content.splitlines():
            parts = line.split(",")
            if len(parts) < 9:
                continue
            if parts[0] != "D" or parts[1] != "TRADING" or parts[2] != "PRICE":
                continue
            # Column indices: [4]=SETTLEMENTDATE, [6]=REGIONID, [8]=RRP
            settlement = parts[4].strip().strip('"')
            region_id = parts[6].strip().strip('"')
            if region_id != region:
                continue
            try:
                rrp = float(parts[8].strip().strip('"'))
            except (ValueError, IndexError):
                continue
            result[settlement] = rrp
        return result
