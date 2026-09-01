"""Guards that CPU-bound decompression and CSV parsing stay off the event loop.

The PD7DAY archive expands to ~47 MB across ~339,000 lines and costs roughly
800 ms to unzip and parse; STPASA adds ~350 ms across five regions. Performing
that inside an ``async def`` blocked the Home Assistant event loop on every
coordinator refresh, and once per region coordinator during startup.

These tests assert the *property* rather than the implementation: the parse
must execute on a thread other than the one running the event loop, and the
loop must stay responsive while it happens. They fail if anyone moves the work
back inline, even via a different mechanism.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
import time
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEM_TZ = timezone(timedelta(hours=10))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.modules.setdefault("aiohttp", MagicMock())

_load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_load(
    "custom_components.nem_pd7day.executor",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "executor.py"),
)
_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)

from custom_components.nem_pd7day.executor import run_in_executor  # noqa: E402
from custom_components.nem_pd7day.pd7day_client import PD7DayClient  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_zip(csv_bytes: bytes, member: str = "PUBLIC_PD7DAY_X.CSV") -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, csv_bytes)
    return buf.getvalue()


# Mirrors the PRICESOLUTION/CASESOLUTION column layout asserted in
# test_pd7day_client.py. Dates are relative so the fixture never goes stale.
def _minimal_csv() -> bytes:
    from custom_components.nem_pd7day import nem_time

    run = nem_time.now_nem().replace(minute=0, second=0, microsecond=0)
    run_s = run.strftime("%Y/%m/%d %H:%M:%S")
    period_s = (run + timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S")
    tail = ",0,0,0,0,0,0,0,0,0,0,0"
    rows = [
        "C,NEOD,PD7DAY,1,PUBLIC_PD7DAY_X.zip",
        f"D,PD7DAY,CASESOLUTION,1,{run_s},0,{run_s}",
        f"D,PD7DAY,PRICESOLUTION,1,{run_s},1,{period_s},QLD1,85000.00{tail}",
    ]
    return "\n".join(rows).encode("utf-8")


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        # The NEMWEB clients inspect resp.status via classify_status now
        # instead of calling raise_for_status, so the stub must carry a
        # status and headers. raise_for_status is kept as a no-op so any
        # remaining caller still works.
        self.status = status
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def read(self):
        return self._payload

    async def text(self, *args, **kwargs):
        return self._payload.decode("utf-8", "replace")


class _FakeSession:
    """Serves a directory listing then the ZIP payload."""

    def __init__(self, listing: bytes, payload: bytes):
        self._listing = listing
        self._payload = payload

    def get(self, url, **kwargs):
        if url.endswith("/"):
            return _FakeResp(self._listing)
        return _FakeResp(self._payload)


# ── run_in_executor contract ──────────────────────────────────────────────────


def test_run_in_executor_uses_a_worker_thread():
    """Without a hass job, work still leaves the loop thread."""

    async def scenario():
        loop_thread = threading.get_ident()
        where = await run_in_executor(None, threading.get_ident)
        return loop_thread, where

    loop_thread, work_thread = asyncio.run(scenario())
    assert work_thread != loop_thread, (
        "run_in_executor ran the callable on the event loop thread"
    )


def test_run_in_executor_prefers_the_supplied_hass_job():
    """hass.async_add_executor_job is used when provided, so HA tracks the job."""
    calls = []

    async def fake_job(func, *args):
        calls.append(func)
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def scenario():
        return await run_in_executor(fake_job, threading.get_ident)

    result = asyncio.run(scenario())
    assert calls, "supplied executor_job was bypassed"
    assert result != threading.get_ident()


# ── PD7DAY client ─────────────────────────────────────────────────────────────


def test_pd7day_unzip_and_parse_run_off_the_loop():
    """_unzip and _parse_all_tables must both execute on a worker thread."""
    listing = b'<a href="/Reports/Current/PD7Day/PUBLIC_PD7DAY_1.zip">z</a>'
    payload = _make_zip(_minimal_csv())

    seen: dict[str, int] = {}
    real_parse = _client_mod._parse_all_tables

    def spy_parse(csv_bytes, regions, ic_ids):
        seen["parse"] = threading.get_ident()
        return real_parse(csv_bytes, regions, ic_ids)

    real_unzip = PD7DayClient._unzip

    def spy_unzip(raw, name):
        seen["unzip"] = threading.get_ident()
        return real_unzip(raw, name)

    async def scenario():
        seen["loop"] = threading.get_ident()
        client = PD7DayClient(_FakeSession(listing, payload))
        _client_mod._parse_all_tables = spy_parse
        PD7DayClient._unzip = staticmethod(spy_unzip)
        try:
            await client.fetch_all(["QLD1"])
        finally:
            _client_mod._parse_all_tables = real_parse
            PD7DayClient._unzip = staticmethod(real_unzip)

    asyncio.run(scenario())

    assert "unzip" in seen, "_unzip was never called"
    assert "parse" in seen, "_parse_all_tables was never called"
    assert seen["unzip"] != seen["loop"], (
        "ZIP decompression ran on the event loop thread"
    )
    assert seen["parse"] != seen["loop"], (
        "CSV parsing ran on the event loop thread"
    )


def test_event_loop_stays_responsive_during_parse():
    """A concurrent heartbeat keeps ticking while a slow parse runs.

    This is the user-visible property: Home Assistant startup and every other
    integration must not stall while PD7DAY is decompressed and parsed.
    """
    listing = b'<a href="/Reports/Current/PD7Day/PUBLIC_PD7DAY_1.zip">z</a>'
    payload = _make_zip(_minimal_csv())

    real_parse = _client_mod._parse_all_tables
    BLOCK_S = 0.30

    def slow_parse(csv_bytes, regions, ic_ids):
        time.sleep(BLOCK_S)  # stand-in for ~700 ms of real CPU
        return real_parse(csv_bytes, regions, ic_ids)

    async def scenario():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            try:
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1
            except asyncio.CancelledError:
                raise

        client = PD7DayClient(_FakeSession(listing, payload))
        _client_mod._parse_all_tables = slow_parse
        beat = asyncio.create_task(heartbeat())
        try:
            await client.fetch_all(["QLD1"])
        finally:
            beat.cancel()
            _client_mod._parse_all_tables = real_parse
            try:
                await beat
            except asyncio.CancelledError:
                pass
        return ticks

    ticks = asyncio.run(scenario())

    # With the parse inline, the loop is frozen for BLOCK_S and records ~0
    # ticks. Off the loop, it should manage most of BLOCK_S / 0.01.
    assert ticks >= 10, (
        f"event loop only ticked {ticks} times during a {BLOCK_S}s parse — "
        "the work appears to be running on the loop"
    )


def test_parse_offload_is_awaited_not_fire_and_forget():
    """fetch_all must still return fully parsed data, not an empty result."""
    listing = b'<a href="/Reports/Current/PD7Day/PUBLIC_PD7DAY_1.zip">z</a>'
    payload = _make_zip(_minimal_csv())

    async def scenario():
        client = PD7DayClient(_FakeSession(listing, payload))
        return await client.fetch_all(["QLD1"])

    result = asyncio.run(scenario())
    assert result.case is not None, "case solution lost by the executor hand-off"
    assert result.source_file.upper().endswith(".ZIP")


# ── STPASA client ─────────────────────────────────────────────────────────────


def test_stpasa_extract_and_parse_run_off_the_loop():
    stpasa_mod = _load(
        "custom_components.nem_pd7day.stpasa_client",
        os.path.join(_ROOT, "custom_components", "nem_pd7day", "stpasa_client.py"),
    )

    seen: dict[str, int] = {}
    real = stpasa_mod._extract_and_parse_all_regions

    def spy(raw):
        seen["work"] = threading.get_ident()
        return real(raw)

    inner = _make_zip(b"C,NEMP.WORLD,STPASA,AEMO\n", "PUBLIC_STPASA_X.CSV")
    payload = _make_zip(inner, "PUBLIC_STPASA_X.zip")
    listing = (
        b'<a href="/Reports/Current/Short_Term_PASA_Reports/'
        b'PUBLIC_STPASA_1.zip">z</a>'
    )

    async def scenario():
        seen["loop"] = threading.get_ident()
        client = stpasa_mod.StpasaClient(_FakeSession(listing, payload))
        stpasa_mod._extract_and_parse_all_regions = spy
        try:
            await client.fetch_all_regions()
        finally:
            stpasa_mod._extract_and_parse_all_regions = real

    asyncio.run(scenario())

    assert "work" in seen, "_extract_and_parse_all_regions was never called"
    assert seen["work"] != seen["loop"], (
        "STPASA extraction/parsing ran on the event loop thread"
    )


# ── Static guard ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "module_name",
    ["pd7day_client.py", "stpasa_client.py", "tradingis_client.py"],
)
def test_no_zipfile_construction_inside_async_functions(module_name):
    """zipfile.ZipFile must not be constructed inside an async def.

    Catches a regression reintroduced anywhere in these clients, including in
    code paths the runtime tests above do not reach.
    """
    import ast

    path = os.path.join(_ROOT, "custom_components", "nem_pd7day", module_name)
    tree = ast.parse(open(path, encoding="utf-8").read())

    offenders = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.async_depth = 0

        def visit_AsyncFunctionDef(self, node):
            self.async_depth += 1
            self.generic_visit(node)
            self.async_depth -= 1

        def visit_FunctionDef(self, node):
            # A sync def nested in an async def is executor-bound; reset.
            saved, self.async_depth = self.async_depth, 0
            self.generic_visit(node)
            self.async_depth = saved

        def visit_Call(self, node):
            if self.async_depth > 0:
                f = node.func
                name = getattr(f, "attr", getattr(f, "id", ""))
                if name in {"ZipFile", "is_zipfile"}:
                    offenders.append(node.lineno)
            self.generic_visit(node)

    V().visit(tree)

    assert not offenders, (
        f"{module_name}: zipfile used directly inside an async function at "
        f"line(s) {offenders} — move it to a sync helper run via "
        f"run_in_executor()"
    )
