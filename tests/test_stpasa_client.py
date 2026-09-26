"""
stpasa_client: REGIONSOLUTION CSV parsing, region bucketing, nested-ZIP
extraction, the fetch_all_regions happy path, and best-effort fetch() error
handling.

Missing numerics stay unavailable, not zero (issue #43): the MW fields parse
through _flt_opt, which reports absent or unparseable data as None, because
0 MW is a real reading for demand, availability and reserve.

The 403/404/Retry-After behaviour of the fetch path is in test_nemweb.py.
"""
from __future__ import annotations

import asyncio

import pytest

from support import install_ha_stubs, load_chain, make_zip, run_async

install_ha_stubs()

_nem_time, _executor, _retry, _client_mod = load_chain(
    "nem_time", "executor", "nemweb_retry", "stpasa_client"
)

StpasaClient = _client_mod.StpasaClient
STPASA_LISTING_URL = _client_mod.STPASA_CURRENT_URL
_STPASA_FILE = "PUBLIC_STPASA_20260415_072507_1"
STPASA_ZIP_URL = STPASA_LISTING_URL + f"{_STPASA_FILE}.ZIP"
_LISTING_HTML = f'<a href="{_STPASA_FILE}.ZIP">{_STPASA_FILE}.ZIP</a>'


# ── Synthetic STPASA CSV builders ─────────────────────────────────────────────

# REGIONSOLUTION column order used by the synthetic CSV. The parser builds a
# name-to-index map from the "I" header row, so the exact order is arbitrary
# as long as header and data rows agree.
_COLS = [
    "I", "STPASA", "REGIONSOLUTION", "1",
    "RUN_DATETIME", "INTERVAL_DATETIME", "REGIONID",
    "DEMAND10", "DEMAND50", "DEMAND90",
    "SURPLUSCAPACITY", "SS_SOLAR_UIGF", "SS_WIND_UIGF",
]


def _header_row() -> str:
    return ",".join(_COLS)


def _data_row(
    region="QLD1",
    interval="2026/04/16 08:00:00",
    run="2026/04/15 07:25:07",
    d10="5500", d50="6000", d90="6500",
    surplus="1200", solar="800", wind="400",
) -> str:
    return ",".join([
        "D", "STPASA", "REGIONSOLUTION", "1",
        run, interval, region,
        d10, d50, d90, surplus, solar, wind,
    ])


def _csv(*rows: str) -> bytes:
    return "\n".join(rows).encode("utf-8")


_MIXED_CSV = _csv(
    _header_row(),
    _data_row(region="QLD1", interval="2026/04/16 08:00:00", d50="6000"),
    _data_row(region="NSW1", interval="2026/04/16 08:00:00", d50="8000"),
    _data_row(region="QLD1", interval="2026/04/16 08:30:00", d50="6100"),
)


def make_stpasa_zip(csv_bytes: bytes, name: str = _STPASA_FILE) -> bytes:
    """Wrap csv_bytes in the outer/inner ZIP layout STPASA publishes.

    Candidate for support.py: test_nemweb.py carries the same helper.
    """
    inner = make_zip(csv_bytes, member=f"{name}.CSV")
    return make_zip(inner, member=f"{name}.ZIP")


# ── Fakes (candidates for support.py; test_nemweb.py has the superset) ────────

class FakeResponse:
    def __init__(self, status=200, text="", body=b""):
        self.status = status
        self.headers = {}
        self._text = text
        self._body = body

    async def text(self, *args, **kwargs):
        return self._text

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Boom:
    def __init__(self, exc):
        self.exc = exc


class QueueSession:
    """Serves a queued script of responses per URL; the last entry repeats."""

    def __init__(self, script):
        self._script = {url: list(items) for url, items in script.items()}
        self.request_log: list[str] = []

    def get(self, url, **kwargs):
        self.request_log.append(url)
        items = self._script.get(url)
        if not items:
            return FakeResponse(status=404)
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, _Boom):
            raise item.exc
        return item


@pytest.fixture
def no_sleep(monkeypatch) -> list[float]:
    """Collapse the retry backoff (see test_nemweb.py for why the keyword
    default is what has to be patched) and return the recorded delays."""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    patched = dict(_retry.fetch_with_retry.__kwdefaults__)
    patched["sleep"] = fake_sleep
    monkeypatch.setattr(_retry.fetch_with_retry, "__kwdefaults__", patched)
    return delays


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_parse_regionsolution_reads_every_field_as_nem_iso():
    """Parse a minimal REGIONSOLUTION CSV: all fields and ISO timestamps."""
    raw = _csv(
        _header_row(),
        _data_row(interval="2026/04/16 08:00:00", d10="5500", d50="6000",
                  d90="6500", surplus="1200", solar="800", wind="400"),
        _data_row(interval="2026/04/16 08:30:00", d10="5400", d50="5900",
                  d90="6400", surplus="1300", solar="900", wind="450"),
    )
    result = _client_mod._parse_regionsolution(raw, "QLD1")
    assert result is not None
    assert result.region == "QLD1"
    assert len(result.intervals) == 2
    first = result.intervals[0]
    assert first.interval_datetime == "2026-04-16T08:00:00+10:00", first.interval_datetime
    assert first.run_datetime == "2026-04-15T07:25:07+10:00", first.run_datetime
    assert first.demand10 == 5500.0
    assert first.demand50 == 6000.0
    assert first.demand90 == 6500.0
    assert first.surpluscapacity == 1200.0
    assert first.ss_solar_uigf == 800.0
    assert first.ss_wind_uigf == 400.0
    # fetched_at is a UTC ISO string
    assert result.fetched_at


def test_parse_all_regions_buckets_rows_per_region():
    results = _client_mod._parse_all_regions(_MIXED_CSV)
    assert set(results) == {"QLD1", "NSW1"}

    qld = results["QLD1"]
    assert len(qld.intervals) == 2, "only QLD1 rows expected"
    assert all(i.demand50 in (6000.0, 6100.0) for i in qld.intervals)

    nsw = results["NSW1"]
    assert len(nsw.intervals) == 1
    assert nsw.intervals[0].demand50 == 8000.0


def test_extract_nested_zip():
    """_extract_csv_bytes walks the outer and inner ZIP layers to the CSV."""
    extracted = _client_mod._extract_csv_bytes(make_stpasa_zip(_csv(_header_row(), _data_row())))
    assert b"REGIONSOLUTION" in extracted
    result = _client_mod._parse_regionsolution(extracted, "QLD1")
    assert result is not None and len(result.intervals) == 1


def test_flt_opt_returns_none_rather_than_a_substituted_zero():
    """_flt is retained for fields where a zero default is correct. Issue #43."""
    _flt, _flt_opt = _client_mod._flt, _client_mod._flt_opt
    assert _flt_opt("6000.0") == 6000.0
    assert _flt_opt("0") == 0.0
    assert _flt_opt("") is None
    assert _flt_opt("n/a") is None
    assert _flt_opt(None) is None
    # The original helper is unchanged for callers that want a zero default.
    assert _flt("") == 0.0


# ── fetch_all_regions() happy path ────────────────────────────────────────────

def test_fetch_all_regions_downloads_once_and_returns_every_region():
    session = QueueSession({
        STPASA_LISTING_URL: [FakeResponse(text=_LISTING_HTML)],
        STPASA_ZIP_URL: [FakeResponse(body=make_stpasa_zip(_MIXED_CSV))],
    })
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert set(results) == {"QLD1", "NSW1"}
    assert len(results["QLD1"].intervals) == 2
    assert len(results["NSW1"].intervals) == 1
    assert results["NSW1"].intervals[0].demand50 == 8000.0
    assert session.request_log == [STPASA_LISTING_URL, STPASA_ZIP_URL]
    # fetch(region) delegates to fetch_all_regions.
    qld = run_async(client.fetch("QLD1"))
    assert qld is not None and qld.region == "QLD1"


# ── fetch() best-effort error handling ────────────────────────────────────────

@pytest.mark.parametrize(
    ("exc", "attempts"),
    [
        pytest.param(RuntimeError("404 Not Found"), 1, id="non-retryable-error"),
        pytest.param(asyncio.TimeoutError(), _retry.DEFAULT_MAX_ATTEMPTS, id="timeout"),
    ],
)
def test_fetch_returns_none_when_the_listing_raises(no_sleep, exc, attempts):
    """Any error during the listing must yield None, not raise. A timeout is
    retryable so it uses the whole budget; an unknown error is not."""
    session = QueueSession({STPASA_LISTING_URL: [_Boom(exc)]})
    client = StpasaClient(session)

    result = run_async(client.fetch("QLD1"))

    assert result is None
    assert session.request_log.count(STPASA_LISTING_URL) == attempts
    assert len(no_sleep) == attempts - 1
