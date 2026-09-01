"""
Tests for stpasa_client — REGIONSOLUTION CSV parsing, region filtering,
nested-ZIP extraction, and best-effort fetch() error handling.

Run with:  python -m pytest tests/test_stpasa_client.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import sys
import zipfile
from unittest.mock import MagicMock

# ── Module loader ─────────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# stpasa_client imports aiohttp at top level — stub it before loading.
sys.modules.setdefault("aiohttp", MagicMock())

_load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_client_mod = _load(
    "custom_components.nem_pd7day.stpasa_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "stpasa_client.py"),
)

from custom_components.nem_pd7day.stpasa_client import (  # noqa: E402
    StpasaClient,
    StpasaResult,
    _extract_csv_bytes,
    _parse_all_regions,
    _parse_regionsolution,
)


def run_async(coro):
    return asyncio.run(coro)


# ── Synthetic STPASA CSV builders ─────────────────────────────────────────────

# REGIONSOLUTION column order used by the synthetic CSV. The parser builds a
# name→index map from the "I" header row, so the exact order is arbitrary as
# long as header and data rows agree.
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


# ── Parsing tests ─────────────────────────────────────────────────────────────

def test_parse_stpasa_csv_qld1():
    """Parse a minimal REGIONSOLUTION CSV — all fields and ISO timestamps."""
    raw = _csv(
        _header_row(),
        _data_row(interval="2026/04/16 08:00:00", d10="5500", d50="6000",
                  d90="6500", surplus="1200", solar="800", wind="400"),
        _data_row(interval="2026/04/16 08:30:00", d10="5400", d50="5900",
                  d90="6400", surplus="1300", solar="900", wind="450"),
    )
    result = _parse_regionsolution(raw, "QLD1")
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
    print("  PASS: parse stpasa csv qld1")


def test_region_filter():
    """_parse_all_regions buckets a mixed CSV into one result per region."""
    raw = _csv(
        _header_row(),
        _data_row(region="QLD1", interval="2026/04/16 08:00:00", d50="6000"),
        _data_row(region="NSW1", interval="2026/04/16 08:00:00", d50="8000"),
        _data_row(region="QLD1", interval="2026/04/16 08:30:00", d50="6100"),
    )
    results = _parse_all_regions(raw)
    assert set(results) == {"QLD1", "NSW1"}

    qld = results["QLD1"]
    assert len(qld.intervals) == 2, "only QLD1 rows expected"
    assert all(i.demand50 in (6000.0, 6100.0) for i in qld.intervals)

    nsw = results["NSW1"]
    assert len(nsw.intervals) == 1
    assert nsw.intervals[0].demand50 == 8000.0
    print("  PASS: region filter")


def test_extract_nested_zip():
    """_extract_csv_bytes walks outer→inner ZIP layers to the CSV."""
    csv_bytes = _csv(_header_row(), _data_row())
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("PUBLIC_STPASA_20260415_072507_1.CSV", csv_bytes)
    inner_bytes = inner.getvalue()
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("PUBLIC_STPASA_20260415_072507_1.ZIP", inner_bytes)
    extracted = _extract_csv_bytes(outer.getvalue())
    assert b"REGIONSOLUTION" in extracted
    result = _parse_regionsolution(extracted, "QLD1")
    assert result is not None and len(result.intervals) == 1
    print("  PASS: extract nested zip")


# ── fetch_all_regions() happy path ────────────────────────────────────────────

class _StubResponse:
    """Async response stub yielding canned text/bytes."""

    def __init__(self, *, text="", data=b"", status: int = 200) -> None:
        self._text = text
        self._data = data
        # The NEMWEB clients inspect resp.status via classify_status now
        # instead of calling raise_for_status, so the stub must carry a
        # status and headers. raise_for_status is kept as a no-op so any
        # remaining caller still works.
        self.status = status
        self.headers = {}

    def raise_for_status(self):
        return None

    async def text(self, *args, **kwargs):
        return self._text

    async def read(self):
        return self._data


class _StubSession:
    """Async session: first .get() returns the listing HTML, rest the ZIP."""

    def __init__(self, *, html, zip_bytes) -> None:
        self._html = html
        self._zip_bytes = zip_bytes

    def get(self, url, *args, **kwargs):
        is_listing = url.endswith("/")
        resp = (
            _StubResponse(text=self._html)
            if is_listing
            else _StubResponse(data=self._zip_bytes)
        )

        class _Ctx:
            async def __aenter__(self_inner):
                return resp

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


def _build_stpasa_zip(csv_bytes: bytes) -> bytes:
    """Wrap csv_bytes in the inner/outer ZIP layout STPASA uses."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("PUBLIC_STPASA_20260415_072507_1.CSV", csv_bytes)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("PUBLIC_STPASA_20260415_072507_1.ZIP", inner.getvalue())
    return outer.getvalue()


def test_fetch_all_regions():
    """fetch_all_regions downloads once and returns every region in the CSV."""
    csv_bytes = _csv(
        _header_row(),
        _data_row(region="QLD1", interval="2026/04/16 08:00:00", d50="6000"),
        _data_row(region="NSW1", interval="2026/04/16 08:00:00", d50="8000"),
        _data_row(region="QLD1", interval="2026/04/16 08:30:00", d50="6100"),
    )
    zip_bytes = _build_stpasa_zip(csv_bytes)
    html = (
        '<a href="PUBLIC_STPASA_20260415_072507_1.ZIP">'
        "PUBLIC_STPASA_20260415_072507_1.ZIP</a>"
    )
    session = _StubSession(html=html, zip_bytes=zip_bytes)
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())
    assert set(results) == {"QLD1", "NSW1"}
    assert len(results["QLD1"].intervals) == 2
    assert len(results["NSW1"].intervals) == 1
    assert results["NSW1"].intervals[0].demand50 == 8000.0
    # fetch(region) now delegates to fetch_all_regions.
    qld = run_async(client.fetch("QLD1"))
    assert qld is not None and qld.region == "QLD1"
    print("  PASS: fetch all regions")


# ── fetch() best-effort error handling ────────────────────────────────────────

class _RaisingSession:
    """Async session stub whose .get(...) context manager raises on enter."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get(self, *args, **kwargs):
        exc = self._exc

        class _Ctx:
            async def __aenter__(self_inner):
                raise exc

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


def test_fetch_returns_none_on_404():
    """A 404 (or any HTTP error) during listing must yield None, not raise."""
    session = _RaisingSession(RuntimeError("404 Not Found"))
    client = StpasaClient(session)
    result = run_async(client.fetch("QLD1"))
    assert result is None
    print("  PASS: fetch returns none on 404")


def test_fetch_returns_none_on_timeout():
    """A timeout during listing must yield None, not raise."""
    session = _RaisingSession(asyncio.TimeoutError())
    client = StpasaClient(session)
    result = run_async(client.fetch("QLD1"))
    assert result is None
    print("  PASS: fetch returns none on timeout")


# ── Runner ────────────────────────────────────────────────────────────────────

TESTS = [
    test_parse_stpasa_csv_qld1,
    test_region_filter,
    test_extract_nested_zip,
    test_fetch_all_regions,
    test_fetch_returns_none_on_404,
    test_fetch_returns_none_on_timeout,
]


def run_all():
    passed = failed = 0
    print(f"\nRunning {len(TESTS)} stpasa_client tests\n{'=' * 50}")
    for test in TESTS:
        try:
            test()
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {test.__name__}\n        {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {test.__name__}\n        {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{'=' * 50}\nResults: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)


# ── Missing numerics stay unavailable, not zero (issue #43) ─────────────────

def test_flt_opt_returns_none_rather_than_a_substituted_zero():
    """
    The MW fields parse through _flt_opt, which reports missing data as None.
    _flt is retained for fields where a zero default is correct. Issue #43.
    """
    from custom_components.nem_pd7day.stpasa_client import _flt, _flt_opt

    assert _flt_opt("6000.0") == 6000.0
    assert _flt_opt("0") == 0.0
    assert _flt_opt("") is None
    assert _flt_opt("n/a") is None
    assert _flt_opt(None) is None
    # The original helper is unchanged for callers that want a zero default.
    assert _flt("") == 0.0
