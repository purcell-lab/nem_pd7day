"""Tests for current-notice filtering, cursor advance, and startup tracing.

Covers the regression where the notice cursor only advanced when an LOR or MSL
notice was stored. Because those notices are rare, the cursor stayed parked while
NEMWEB kept publishing, so every cycle re-examined every notice issued since the
last relevant one, serially, with a delay before each request, once per region.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nem_pd7day.market_notice_client import (
    MarketNoticeClient,
    _NOTICE_MAX_FILES_PER_CYCLE,
)
from custom_components.nem_pd7day.startup_trace import StartupTrace

NEM_TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 6, 15, 14, 0, tzinfo=NEM_TZ)


def _listing(*entries: tuple[int, str]) -> str:
    """Build a NEMWEB-style directory listing from (notice_id, YYYYMMDD) pairs."""
    lines = [
        f"15/06/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_{day}.R{nid}"
        for nid, day in entries
    ]
    return "<pre>\n" + "\n".join(lines) + "\n</pre>"


def _client(listing: str, *, body: str = "NOT A RELEVANT NOTICE", clock=None):
    """A client wired to a mock session: first GET is the listing, rest are files."""
    calls = {"n": 0, "urls": []}

    def _get(url, *args, **kwargs):
        calls["n"] += 1
        calls["urls"].append(url)
        resp = AsyncMock()
        # status and headers are read directly now that the client classifies
        # the response itself instead of calling raise_for_status.
        resp.status = 200
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        resp.text = AsyncMock(return_value=listing if calls["n"] == 1 else body)
        return AsyncMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    client = MarketNoticeClient(session, clock=clock or (lambda: NOW))
    return client, calls


def test_only_current_and_previous_day_are_fetched():
    """Files older than the previous NEM day are never requested."""
    listing = _listing(
        (1000, "20260601"),  # stale
        (1001, "20260610"),  # stale
        (1002, "20260614"),  # previous day, current
        (1003, "20260615"),  # today, current
    )
    client, calls = _client(listing)

    asyncio.run(client.fetch_new_notices())

    # One listing request plus the two current files.
    assert calls["n"] == 3
    fetched = [u for u in calls["urls"][1:]]
    assert any("R1002" in u for u in fetched)
    assert any("R1003" in u for u in fetched)
    assert not any("R1000" in u or "R1001" in u for u in fetched)


def test_cursor_advances_past_stale_files():
    """Stale files are skipped once, not reconsidered on every later cycle."""
    listing = _listing((1000, "20260601"), (1001, "20260602"))
    client, calls = _client(listing)

    asyncio.run(client.fetch_new_notices())

    # Nothing fetched, but the cursor moved past both stale files.
    assert calls["n"] == 1
    assert client.last_seen_notice_id == 1001

    # A second cycle over the same listing does no work at all.
    client2, calls2 = _client(listing)
    client2.last_seen_notice_id = 1001
    asyncio.run(client2.fetch_new_notices())
    assert calls2["n"] == 1


def test_cursor_advances_when_no_relevant_notice_found():
    """A file that is neither LOR nor MSL still advances the cursor."""
    listing = _listing((2000, "20260615"), (2001, "20260615"))
    client, _ = _client(listing, body="GENERAL NOTICE, nothing to see")

    notices = asyncio.run(client.fetch_new_notices())

    assert notices == []
    assert client.last_seen_notice_id == 2001


def test_cursor_advances_when_file_fetch_fails():
    """A failed file fetch does not pin the cursor and force a permanent retry."""
    listing = _listing((3000, "20260615"), (3001, "20260615"))
    calls = {"n": 0}

    def _get(url, *args, **kwargs):
        calls["n"] += 1
        resp = AsyncMock()
        resp.headers = {}
        if calls["n"] == 1:
            resp.status = 200
            resp.raise_for_status = MagicMock()
            resp.text = AsyncMock(return_value=listing)
        else:
            # Mirrors the 403s NEMWEB returned when the backlog was re-read.
            resp.status = 403
            resp.raise_for_status = MagicMock(side_effect=Exception("403 Forbidden"))
            resp.text = AsyncMock(return_value="")
        return AsyncMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    client = MarketNoticeClient(session, clock=lambda: NOW)

    notices = asyncio.run(client.fetch_new_notices())

    assert notices == []
    assert client.last_seen_notice_id == 3001


def test_per_cycle_file_cap_defers_remainder_contiguously():
    """The cap truncates the newest files, so the cursor leaves no gap behind."""
    total = _NOTICE_MAX_FILES_PER_CYCLE + 7
    listing = _listing(*[(5000 + i, "20260615") for i in range(total)])
    client, calls = _client(listing)

    asyncio.run(client.fetch_new_notices())

    assert calls["n"] == 1 + _NOTICE_MAX_FILES_PER_CYCLE
    # Cursor sits at the last fetched file, not the highest listed one, so the
    # deferred remainder is still picked up next cycle.
    assert client.last_seen_notice_id == 5000 + _NOTICE_MAX_FILES_PER_CYCLE - 1
    assert client.highest_listed_notice_id == 5000 + total - 1


def test_fetches_respect_shared_concurrency_limit():
    """File fetches are bounded by the shared NEMWEB semaphore."""
    listing = _listing(*[(6000 + i, "20260615") for i in range(12)])
    in_flight = 0
    peak = 0

    async def _slow_text():
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return "GENERAL NOTICE"

    calls = {"n": 0}

    def _get(url, *args, **kwargs):
        calls["n"] += 1
        resp = AsyncMock()
        resp.status = 200
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        if calls["n"] == 1:
            resp.text = AsyncMock(return_value=listing)
        else:
            resp.text = AsyncMock(side_effect=_slow_text)
        return AsyncMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    async def _run():
        session = MagicMock()
        session.get = MagicMock(side_effect=_get)
        client = MarketNoticeClient(
            session, semaphore=asyncio.Semaphore(2), clock=lambda: NOW
        )
        await client.fetch_new_notices()

    asyncio.run(_run())

    assert peak <= 2, f"expected at most 2 concurrent fetches, saw {peak}"


def test_empty_listing_is_handled():
    """An unparseable or empty listing yields nothing and does not raise."""
    client, calls = _client("<pre>\n</pre>")
    assert asyncio.run(client.fetch_new_notices()) == []
    assert calls["n"] == 1


# ── Startup trace ─────────────────────────────────────────────────────────────


def test_startup_trace_records_phases_in_order():
    trace = StartupTrace("QLD1")
    trace.checkpoint("first")
    trace.checkpoint("second")
    assert [name for name, _ in trace.phases] == ["first", "second"]
    assert trace.total >= 0


def test_startup_trace_phase_records_even_when_block_raises():
    """A failed setup still reports where the time went."""
    trace = StartupTrace("QLD1")
    with pytest.raises(ValueError):
        with trace.phase("blocking fetch"):
            raise ValueError("NEMWEB down")
    assert [name for name, _ in trace.phases] == ["blocking fetch"]


def test_startup_trace_summary_orders_slowest_first():
    trace = StartupTrace("QLD1")
    trace.phases = [("fast", 0.002), ("slowest", 1.5), ("middle", 0.4)]
    summary = trace.summary()
    assert summary.index("slowest") < summary.index("middle") < summary.index("fast")


def test_startup_trace_logs_slow_phase_at_info(caplog):
    """Slow phases surface without needing debug logging enabled."""
    logger = logging.getLogger("test_startup_trace_slow")
    trace = StartupTrace("QLD1", logger)
    trace._last -= 2.0  # pretend the previous phase started 2 s ago
    with caplog.at_level(logging.INFO, logger=logger.name):
        trace.checkpoint("slow thing")
    assert any(r.levelno == logging.INFO for r in caplog.records)
