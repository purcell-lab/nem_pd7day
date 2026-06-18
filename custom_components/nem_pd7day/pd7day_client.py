"""
NEMWeb PD7DAY data client.

Fetches, extracts, and parses the latest PUBLIC_PD7DAY ZIP/CSV from NEMWeb.
Parses all five table types:
  - PRICESOLUTION         → regional spot price forecasts
  - CASESOLUTION          → run metadata + intervention flag
  - MARKET_SUMMARY        → NEM-wide gas generation forecast (TJ/day)
  - INTERCONNECTORSOLUTION → interconnector MW flow + constraint forecasts
  - CONSTRAINTSOLUTION    → (parsed but not exposed as sensors by default)

Timezone policy
---------------
All datetime values from the CSV are in NEM time (AEST, UTC+10:00, no DST).
Every timestamp stored in a dataclass or returned by this module is a
timezone-aware ISO-8601 string with an explicit +10:00 suffix, e.g.:
    "2026-04-14T07:30:00+10:00"

See nem_time.py for the authoritative timezone helpers.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .const import FILE_PATTERN, NEMWEB_BASE_URL, NEMWEB_HEADERS, QLD1_INTERCONNECTORS

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class PricePeriod:
    nemtime: str     # ISO-8601 NEM-aware interval-END timestamp (AEMO convention)
    time: str        # ISO-8601 NEM-aware interval-START timestamp (nemtime − 30 min)
    value: float     # $/kWh


@dataclass
class CheapestWindow:
    start: str       # ISO-8601 NEM-aware interval-START of first period in window
    end: str         # ISO-8601 NEM-aware interval-START of last period in window
    nemtime_start: str  # interval-END of first period
    nemtime_end: str    # interval-END of last period
    avg_value: float
    points: int


@dataclass
class PD7DayData:
    """PRICESOLUTION — regional spot price forecast."""
    region: str
    source_file: str
    forecast_generated_at: str | None   # ISO-8601 NEM-aware
    interval_minutes: int
    current_value: float
    next_value: float | None
    min_24h_value: float | None
    max_24h_value: float | None
    cheapest_2h_window: CheapestWindow | None
    forecast: list[PricePeriod] = field(default_factory=list)

    def as_state(self) -> float:
        return self.current_value

    def as_attributes(self) -> dict[str, Any]:
        return {
            "friendly_name": f"{self.region} PD7DAY Forecast",
            "icon": "mdi:transmission-tower",
            "unit_of_measurement": "$/kWh",
            "region": self.region,
            "forecast_generated_at": self.forecast_generated_at,
            "interval_minutes": self.interval_minutes,
            "next_value": self.next_value,
            "min_24h_value": self.min_24h_value,
            "max_24h_value": self.max_24h_value,
            "cheapest_2h_window": (
                {
                    "start": self.cheapest_2h_window.start,
                    "end": self.cheapest_2h_window.end,
                    "avg_value": self.cheapest_2h_window.avg_value,
                    "points": self.cheapest_2h_window.points,
                }
                if self.cheapest_2h_window
                else None
            ),
            "forecast": [{"time": p.time, "value": p.value} for p in self.forecast],
            "source_file": self.source_file,
        }


@dataclass
class CaseSolutionData:
    """
    CASESOLUTION — one row per run.

    intervention=True means the market is operating under AEMO-directed
    conditions. The RRP does not reflect normal supply/demand pricing and
    PD7DAY calibration observations from this period are excluded.
    """
    run_datetime: str   # ISO-8601 NEM-aware
    intervention: bool
    last_changed: str   # ISO-8601 NEM-aware


@dataclass
class GasForecastPeriod:
    """One day's NEM-wide gas generation forecast."""
    nemtime: str        # ISO-8601 NEM-aware interval-END (AEMO convention)
    time: str           # ISO-8601 NEM-aware interval-START (nemtime − 30 min)
    value_tj: float     # GPG_FUEL_FORECAST_TJ


@dataclass
class MarketSummaryData:
    """MARKET_SUMMARY — daily gas generation pressure forecast."""
    run_datetime: str   # ISO-8601 NEM-aware
    forecast: list[GasForecastPeriod] = field(default_factory=list)

    @property
    def current_tj(self) -> float | None:
        return self.forecast[0].value_tj if self.forecast else None

    @property
    def max_7d_tj(self) -> float | None:
        if not self.forecast:
            return None
        return round(max(p.value_tj for p in self.forecast), 3)


@dataclass
class InterconnectorPeriod:
    """One 30-min interval of interconnector data."""
    nemtime: str        # ISO-8601 NEM-aware interval-END (AEMO convention)
    time: str           # ISO-8601 NEM-aware interval-START (nemtime − 30 min)
    mwflow: float
    meteredmwflow: float
    mwlosses: float
    marginalvalue: float
    violationdegree: float
    exportlimit: float
    importlimit: float
    marginalloss: float


@dataclass
class InterconnectorData:
    """INTERCONNECTORSOLUTION — MW flow and constraint forecast."""
    interconnector_id: str
    source_file: str
    run_datetime: str   # ISO-8601 NEM-aware
    forecast: list[InterconnectorPeriod] = field(default_factory=list)

    @property
    def current_mwflow(self) -> float | None:
        return self.forecast[0].mwflow if self.forecast else None

    @property
    def current_violationdegree(self) -> float | None:
        return self.forecast[0].violationdegree if self.forecast else None

    @property
    def is_constrained(self) -> bool:
        v = self.current_violationdegree
        return v is not None and v > 0.0

    @property
    def max_violation_7d(self) -> float | None:
        if not self.forecast:
            return None
        return round(max(p.violationdegree for p in self.forecast), 3)


@dataclass
class PD7DayResult:
    """All parsed tables from one ZIP download."""
    source_file: str
    case: CaseSolutionData | None
    prices: dict[str, PD7DayData]
    market_summary: MarketSummaryData | None
    interconnectors: dict[str, InterconnectorData]
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# HTML link parser
# ---------------------------------------------------------------------------

class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flt(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _avg(prices: list[PricePeriod]) -> float | None:
    if not prices:
        return None
    return round(sum(p.value for p in prices) / len(prices), 6)


def _find_cheapest_window(
    prices: list[PricePeriod], hours: int = 2, interval_minutes: int = 30
) -> CheapestWindow | None:
    n = int(hours * 60 / interval_minutes)
    if len(prices) < n:
        return None
    best: CheapestWindow | None = None
    for i in range(len(prices) - n + 1):
        window = prices[i : i + n]
        avg = _avg(window)
        if avg is None:
            continue
        if best is None or avg < best.avg_value:
            best = CheapestWindow(
                start=window[0].time,
                end=window[-1].time,
                nemtime_start=window[0].nemtime,
                nemtime_end=window[-1].nemtime,
                avg_value=avg,
                points=n,
            )
    return best


def _min_max_24h(
    prices: list[PricePeriod], hours: int = 24
) -> tuple[float | None, float | None]:
    subset = prices[: hours * 2]
    if not subset:
        return None, None
    vals = [p.value for p in subset]
    return round(min(vals), 6), round(max(vals), 6)


# ---------------------------------------------------------------------------
# CSV parser — single pass, all tables, NEM-aware timestamps
# ---------------------------------------------------------------------------

def _parse_all_tables(
    raw: bytes,
    regions: list[str],
    interconnector_ids: set[str],
) -> tuple[
    None,
    CaseSolutionData | None,
    dict[str, tuple[str | None, list[PricePeriod]]],
    MarketSummaryData | None,
    dict[str, list[InterconnectorPeriod]],
]:
    from .nem_time import interval_start, parse_nem_csv, to_nem_iso  # noqa: E402

    text = raw.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))

    case: CaseSolutionData | None = None
    # Value: (run_datetime_iso_str | None, list[PricePeriod])
    price_rows: dict[str, tuple[str | None, list[PricePeriod]]] = {
        r: (None, []) for r in regions
    }
    gas_rows: list[GasForecastPeriod] = []
    gas_run_dt: str | None = None
    ic_rows: dict[str, list[InterconnectorPeriod]] = {}

    for row in reader:
        if not row or row[0] != "D" or len(row) < 5:
            continue
        if row[1] != "PD7DAY":
            continue

        table = row[2]

        # ── CASESOLUTION ──────────────────────────────────────────────────
        if table == "CASESOLUTION" and len(row) >= 7:
            case = CaseSolutionData(
                run_datetime=to_nem_iso(parse_nem_csv(row[4])),
                intervention=row[5].strip() == "1",
                last_changed=to_nem_iso(parse_nem_csv(row[6])) if row[6].strip() else "",
            )

        # ── PRICESOLUTION ─────────────────────────────────────────────────
        elif table == "PRICESOLUTION" and len(row) >= 20:
            region = row[7]
            if region not in price_rows:
                continue
            run_dt_str, prices = price_rows[region]
            if run_dt_str is None:
                run_dt_str = to_nem_iso(parse_nem_csv(row[4]))
                price_rows[region] = (run_dt_str, prices)
            nem_ts = to_nem_iso(parse_nem_csv(row[6]))
            prices.append(
                PricePeriod(
                    nemtime=nem_ts,
                    time=interval_start(nem_ts),
                    value=round(float(row[8]) / 1000.0, 6),
                )
            )

        # ── MARKET_SUMMARY ────────────────────────────────────────────────
        elif table == "MARKET_SUMMARY" and len(row) >= 7:
            if gas_run_dt is None:
                gas_run_dt = to_nem_iso(parse_nem_csv(row[4]))
            nem_ts = to_nem_iso(parse_nem_csv(row[5]))
            gas_rows.append(
                GasForecastPeriod(
                    nemtime=nem_ts,
                    time=interval_start(nem_ts),
                    value_tj=round(_flt(row[6]), 3),
                )
            )

        # ── INTERCONNECTORSOLUTION ────────────────────────────────────────
        elif table == "INTERCONNECTORSOLUTION" and len(row) >= 16:
            ic_id = row[7]
            if ic_id not in interconnector_ids:
                continue
            if ic_id not in ic_rows:
                ic_rows[ic_id] = []
            nem_ts = to_nem_iso(parse_nem_csv(row[6]))
            ic_rows[ic_id].append(
                InterconnectorPeriod(
                    nemtime=nem_ts,
                    time=interval_start(nem_ts),
                    mwflow=round(_flt(row[9]), 3),
                    meteredmwflow=round(_flt(row[8]), 3),
                    mwlosses=round(_flt(row[10]), 3),
                    marginalvalue=round(_flt(row[11]), 3),
                    violationdegree=round(_flt(row[12]), 3),
                    exportlimit=round(_flt(row[13]), 3),
                    importlimit=round(_flt(row[14]), 3),
                    marginalloss=round(_flt(row[15]), 6),
                )
            )

    # Sort by nemtime (interval-end) — ISO-8601 with fixed offset sorts correctly
    for region in price_rows:
        run_dt_str, prices = price_rows[region]
        price_rows[region] = (run_dt_str, sorted(prices, key=lambda p: p.nemtime))

    gas_rows.sort(key=lambda p: p.nemtime)

    for ic_id in ic_rows:
        ic_rows[ic_id].sort(key=lambda p: p.nemtime)

    market_summary = (
        MarketSummaryData(run_datetime=gas_run_dt, forecast=gas_rows)
        if gas_run_dt
        else None
    )

    return None, case, price_rows, market_summary, ic_rows


# ---------------------------------------------------------------------------
# Async network client
# ---------------------------------------------------------------------------

class PD7DayClient:
    """Async client for NEMWeb PD7DAY data."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str = NEMWEB_BASE_URL,
        interconnector_ids: set[str] | None = None,
        semaphore: "asyncio.Semaphore | None" = None,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._interconnector_ids = interconnector_ids or QLD1_INTERCONNECTORS
        # Shared across all region coordinators to cap concurrent NEMWEB
        # requests. nullcontext when absent (e.g. unit tests).
        self._semaphore = semaphore

    def _gate(self):
        """Return the semaphore context manager (or a no-op if unset)."""
        if self._semaphore is None:
            return contextlib.nullcontext()
        return self._semaphore

    async def _list_files(self) -> list[dict[str, str]]:
        async with self._gate():
            async with self._session.get(
                self._base_url,
                headers=NEMWEB_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                html = await resp.text(errors="ignore")

        parser = _LinkExtractor()
        parser.feed(html)

        files = []
        for href in parser.links:
            name = href.split("/")[-1]
            if FILE_PATTERN.search(name):
                files.append({"name": name, "url": urljoin(self._base_url, href)})
        return files

    async def _newest_file(self) -> dict[str, str]:
        files = await self._list_files()
        if not files:
            raise FileNotFoundError("No PUBLIC_PD7DAY ZIP/CSV files found at NEMWeb")
        return sorted(files, key=lambda x: x["name"])[-1]

    async def _fetch_bytes(self, url: str) -> bytes:
        async with self._gate():
            async with self._session.get(
                url,
                headers=NEMWEB_HEADERS,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                resp.raise_for_status()
                return await resp.read()

    async def _get_csv_bytes(self, file_meta: dict[str, str]) -> tuple[str, bytes]:
        raw = await self._fetch_bytes(file_meta["url"])
        name = file_meta["name"]
        if name.upper().endswith(".ZIP"):
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                members = [m for m in zf.namelist() if m.upper().endswith(".CSV")]
                if not members:
                    raise FileNotFoundError(f"No CSV inside ZIP: {name}")
                with zf.open(sorted(members)[0]) as f:
                    return name, f.read()
        return name, raw

    async def fetch_all(self, regions: list[str]) -> PD7DayResult:
        """
        Download the latest PD7DAY ZIP and parse all tables in one pass.
        All timestamps in the returned PD7DayResult are ISO-8601 strings
        with an explicit +10:00 (NEM time) offset.
        """
        from .nem_time import now_nem, to_nem_iso  # noqa: E402

        file_meta = await self._newest_file()
        source_name, csv_bytes = await self._get_csv_bytes(file_meta)

        _, case, price_rows, market_summary, ic_rows = _parse_all_tables(
            csv_bytes, regions, self._interconnector_ids
        )

        prices: dict[str, PD7DayData] = {}
        for region in regions:
            run_dt_str, forecast = price_rows.get(region, (None, []))
            if not forecast:
                _LOGGER.warning(
                    "No PRICESOLUTION rows for region %s in %s", region, source_name
                )
                continue

            min_24h, max_24h = _min_max_24h(forecast)
            cheapest = _find_cheapest_window(forecast)

            prices[region] = PD7DayData(
                region=region,
                source_file=source_name,
                forecast_generated_at=run_dt_str,
                interval_minutes=30,
                current_value=forecast[0].value,
                next_value=forecast[1].value if len(forecast) > 1 else None,
                min_24h_value=min_24h,
                max_24h_value=max_24h,
                cheapest_2h_window=cheapest,
                forecast=forecast,
            )

        interconnectors: dict[str, InterconnectorData] = {}
        run_dt_str_case = case.run_datetime if case else ""
        for ic_id, periods in ic_rows.items():
            interconnectors[ic_id] = InterconnectorData(
                interconnector_id=ic_id,
                source_file=source_name,
                run_datetime=run_dt_str_case,
                forecast=periods,
            )

        return PD7DayResult(
            source_file=source_name,
            case=case,
            prices=prices,
            market_summary=market_summary,
            interconnectors=interconnectors,
            updated_at=to_nem_iso(now_nem()),
        )
