"""
Tests for tradingis_client.py: TradingISClient, 30-minute actual settlement
price fetching.

One TradingIS zip is fetched per 30-min interval, keyed by interval END
(interval_start + 30 min), and one D,TRADING,PRICE row per region is parsed.
The directory listing is cached for _DIR_CACHE_TTL seconds.

Run with:  python -m pytest tests/test_tradingis_client.py -v
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, make_zip, run_async

install_ha_stubs()

_const_mod, _nem_time, _tradingis_mod = load_chain("const", "nem_time", "tradingis_client")

TradingISClient = _tradingis_mod.TradingISClient
BASE_URL = _const_mod.TRADINGIS_BASE_URL
REGION_PRICES = [("QLD1", 89.5), ("NSW1", 75.2), ("VIC1", 120.0), ("SA1", -5.0), ("TAS1", 88.1)]
INTERVAL_START = datetime(2026, 4, 18, 17, 0, tzinfo=NEM_TZ)
INTERVAL_END = INTERVAL_START + timedelta(minutes=30)


# ── Sample CSV helpers ───────────────────────────────────────────────────────

def _trading_csv(settlement_str: str, rows: list[tuple[str, float]]) -> str:
    """A TradingIS CSV with one D,TRADING,PRICE row per (region, rrp_mwh)."""
    lines = [
        "C,NEMP.WORLD,TRADINGIS,v3",
        "I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,INVALIDFLAG,LASTCHANGED,PRICE_STATUS",
    ]
    for region, rrp in rows:
        lines.append(f'D,TRADING,PRICE,3,"{settlement_str}",1,{region},223,{rrp},0,0,"2026/04/18 17:01:00",FIRM')
    lines.append('C,"END OF REPORT",3')
    return "\n".join(lines) + "\n"


def _zip(csv_content: str) -> bytes:
    return make_zip(csv_content.encode(), "PUBLIC_TRADINGIS.csv")


def _directory_html(filenames: list[str]) -> str:
    links = "\n".join(f'<a href="{fn}">{fn}</a>' for fn in filenames)
    return f"<html><body>{links}</body></html>"


def _filename_for(interval_end: datetime) -> str:
    return f"PUBLIC_TRADINGIS_{interval_end.strftime('%Y%m%d%H%M')}_1.zip"


# ── Mock session ─────────────────────────────────────────────────────────────

class MockResponse:
    def __init__(self, text_content: str = "", byte_content: bytes = b"", status: int = 200):
        self._text = text_content
        self._bytes = byte_content
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise Exception(f"HTTP {self.status}")

    async def text(self):
        return self._text

    async def read(self):
        return self._bytes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class MockSession:
    def __init__(self, responses: dict[str, MockResponse]):
        self._responses = responses
        self.request_log: list[str] = []

    def get(self, url, **kwargs):
        self.request_log.append(url)
        return self._responses.get(url, MockResponse(status=404))


def _client_serving(filenames: list[str], payload: bytes | None = None, filename: str | None = None):
    """A client whose directory lists ``filenames``; ``payload`` is served for ``filename``."""
    responses = {BASE_URL: MockResponse(text_content=_directory_html(filenames))}
    if payload is not None:
        responses[BASE_URL + filename] = MockResponse(byte_content=payload)
    session = MockSession(responses)
    return TradingISClient(session), session


# ── _parse_all_rrp ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("region, rrp_mwh", [("QLD1", 89.5), ("NSW1", 75.2), ("SA1", -5.0)])
def test_parse_reads_each_region_in_kwh(region, rrp_mwh):
    """Each region's row, and $/MWh becomes $/kWh (negatives included)."""
    csv = _trading_csv("2026/04/18 17:05:00", REGION_PRICES)
    result = TradingISClient._parse_all_rrp(csv).get(region)
    assert result is not None
    assert abs(result - rrp_mwh / 1000) < 1e-9


def test_parse_omits_a_region_not_in_the_file_and_reads_nothing_without_rows():
    csv = _trading_csv("2026/04/18 17:05:00", [("QLD1", 95.69)])
    assert "SA1" not in TradingISClient._parse_all_rrp(csv), "region not in CSV"
    assert TradingISClient._parse_all_rrp("C,NEMP.WORLD\nI,TRADING,PRICE,3,HEADER\nC,END\n") == {}, (
        "no D,TRADING,PRICE rows"
    )


# ── fetch_interval_prices: the interval end, and failures ────────────────────

def test_fetch_interval_price_returns_rrp_for_the_interval_end():
    """Fetches the zip for interval_end = interval_start + 30 min and returns $/kWh."""
    filename = _filename_for(INTERVAL_END)
    csv = _trading_csv(INTERVAL_END.strftime("%Y/%m/%d %H:%M:00"), [("QLD1", 95.69)])
    client, session = _client_serving([filename], _zip(csv), filename)

    result = run_async(client.fetch_interval_prices(INTERVAL_START)).get("QLD1")

    assert result is not None
    assert abs(result - 95.69 / 1000) < 1e-9
    assert BASE_URL + filename in session.request_log


def test_fetch_interval_price_file_not_in_directory():
    """If no file exists for the target interval end, returns None."""
    client, _ = _client_serving(["PUBLIC_TRADINGIS_202604181600_1.zip"])   # a different interval
    assert run_async(client.fetch_interval_prices(INTERVAL_START)).get("QLD1") is None


def test_fetch_interval_price_bad_zip():
    """Malformed zip bytes: returns None, does not raise."""
    filename = _filename_for(INTERVAL_END)
    client, _ = _client_serving([filename], b"not a zip", filename)
    assert run_async(client.fetch_interval_prices(INTERVAL_START)).get("QLD1") is None


def test_directory_http_failure_returns_none_from_fetch():
    """If the directory fetch fails (404), there is no price."""
    client = TradingISClient(MockSession({BASE_URL: MockResponse(status=404)}))
    assert run_async(client.fetch_interval_prices(INTERVAL_START)).get("QLD1") is None


# ── Directory cache ───────────────────────────────────────────────────────────

def test_directory_cache_hit():
    """A second call within the TTL uses the cache: one HTTP request to the directory."""
    client, session = _client_serving(["PUBLIC_TRADINGIS_202604181705_1.zip"])

    run_async(client._fetch_directory())
    run_async(client._fetch_directory())

    dir_requests = [u for u in session.request_log if u == BASE_URL]
    assert len(dir_requests) == 1, f"Expected 1 (cached), got {len(dir_requests)}"


def test_directory_cache_expiry():
    """After the TTL expires, a fresh request is made."""
    client, session = _client_serving(["PUBLIC_TRADINGIS_202604181705_1.zip"])

    run_async(client._fetch_directory())
    client._dir_cache_ts = time.monotonic() - 100   # expire the cache
    run_async(client._fetch_directory())

    dir_requests = [u for u in session.request_log if u == BASE_URL]
    assert len(dir_requests) == 2, f"Expected 2 (expired), got {len(dir_requests)}"


# ── fetch_interval_prices: every region from one zip (#335) ───────────────────

def test_fetch_interval_prices_reads_every_region_from_one_listing_and_one_zip():
    filename = _filename_for(INTERVAL_END)
    csv = _trading_csv(INTERVAL_END.strftime("%Y/%m/%d %H:%M:00"), REGION_PRICES)
    client, session = _client_serving([filename], _zip(csv), filename)

    prices = run_async(client.fetch_interval_prices(INTERVAL_START))

    assert prices == {region: round(rrp / 1000.0, 6) for region, rrp in REGION_PRICES}
    assert session.request_log == [BASE_URL, BASE_URL + filename]


def test_all_region_parse_agrees_with_the_per_region_parse():
    """The shared fetch must record exactly what each entry recorded before: the
    first valid row per region, an unparseable RRP skipped, other tables ignored."""
    csv = _trading_csv("2026/04/18 17:30:00", [("QLD1", "bad"), ("QLD1", 91.25), ("NSW1", 75.2), ("QLD1", 99.0)])
    csv += 'D,TRADING,INTERCONNECTORRES,3,"2026/04/18 17:30:00",1,QLD1,223,1234,0\n'
    every = TradingISClient._parse_all_rrp(csv)
    assert every == {"QLD1": 0.09125, "NSW1": 0.0752}
    for region in ("QLD1", "NSW1", "VIC1"):
        assert every.get(region) == _per_region_parse_before_335(csv, region)


def _per_region_parse_before_335(content, region):
    """TradingISClient._parse_rrp as it was on main before #335, kept as the reference."""
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


@pytest.mark.parametrize("case", ["not_listed", "bad_zip", "listing_fails"])
def test_fetch_interval_prices_is_empty_on_any_failure(case):
    filename = _filename_for(INTERVAL_END)
    if case == "not_listed":
        client, _ = _client_serving(["PUBLIC_TRADINGIS_202604181600_1.zip"])
    elif case == "bad_zip":
        client, _ = _client_serving([filename], b"not a zip", filename)
    else:
        client = TradingISClient(MockSession({BASE_URL: MockResponse(status=404)}))
    assert run_async(client.fetch_interval_prices(INTERVAL_START)) == {}
