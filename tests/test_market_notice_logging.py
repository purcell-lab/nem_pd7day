"""Tests for market notice fetch failure visibility (issue #44).

The notice client logged every fetch failure at debug only. The integration's
module logger sits at INFO by default, so a sustained notice outage produced no
log output at all: the grid notices sensor simply stopped updating and nothing
explained why. Diagnosing it meant turning debug on and waiting for the failure
to happen again.

These tests pin the behaviour the fix has to keep:

  - a genuine directory listing failure warns once, having been retried
  - per-file failures produce one aggregated warning per cycle, not up to forty
  - a not-published notice file stays at debug and raises no warning
  - a healthy cycle stays silent at INFO
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.nem_pd7day.market_notice_client import (
    MarketNoticeClient,
    _NOTICE_FILE_MAX_ATTEMPTS,
)

NEM_TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 6, 15, 14, 0, tzinfo=NEM_TZ)
CLIENT_LOGGER = "custom_components.nem_pd7day.market_notice_client"


def _listing(*entries: tuple[int, str]) -> str:
    lines = [
        f"15/06/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_{day}.R{nid}"
        for nid, day in entries
    ]
    return "<pre>\n" + "\n".join(lines) + "\n</pre>"


def _client(status_for_call, *, listing: str, body: str = "NOT A RELEVANT NOTICE"):
    """Client whose per-request status is decided by ``status_for_call(n, url)``.

    ``n`` is 1-based over every request the client makes, so call 1 is the
    directory listing. Sleeps are patched out at the client level by giving the
    retry helper nothing to wait on: the helper's real asyncio.sleep is left in
    place but the delays are sub-second, which keeps the test honest about the
    retry actually happening.
    """
    calls = {"n": 0, "urls": []}

    def _get(url, *args, **kwargs):
        calls["n"] += 1
        calls["urls"].append(url)
        resp = AsyncMock()
        resp.status = status_for_call(calls["n"], url)
        resp.headers = {}
        resp.text = AsyncMock(return_value=listing if calls["n"] == 1 else body)
        return AsyncMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return MarketNoticeClient(session, clock=lambda: NOW), calls


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    ]


def test_directory_403_warns_once_after_retrying(caplog):
    """
    A throttled or failing directory listing must be visible at the default
    log level. The old code returned an empty list on a 403 with a single
    debug line, so a sustained outage was completely silent.
    """
    listing = _listing((3000, "20260615"))
    client, calls = _client(lambda n, url: 403, listing=listing)

    with caplog.at_level(logging.DEBUG):
        notices = asyncio.run(client.fetch_new_notices())

    assert notices == []
    # Retried rather than abandoned on the first 403.
    assert calls["n"] > 1, "the listing must be retried before giving up"

    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert "403" in warnings[0]
    assert "Market_Notice" in warnings[0]


def test_directory_404_warns_without_retrying(caplog):
    """
    The Market_Notice directory always exists, so a 404 means the report path
    moved. That warns, and retrying it is pointless.
    """
    listing = _listing((3000, "20260615"))
    client, calls = _client(lambda n, url: 404, listing=listing)

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(client.fetch_new_notices()) == []

    assert calls["n"] == 1, "a 404 on the directory must not be retried"
    assert len(_warnings(caplog)) == 1


def test_per_file_failures_produce_one_aggregated_warning(caplog):
    """
    Twelve failing notice files must produce one warning, not twelve.

    Per-file give-up lines are suppressed to debug so the cycle can summarise
    them, which is why the aggregate line has to exist: without it the
    suppression would recreate the silence in issue #44.
    """
    entries = [(4000 + i, "20260615") for i in range(12)]
    listing = _listing(*entries)
    # Call 1 is the listing and succeeds; every file request 500s.
    client, calls = _client(
        lambda n, url: 200 if n == 1 else 500, listing=listing
    )

    with caplog.at_level(logging.DEBUG):
        notices = asyncio.run(client.fetch_new_notices())

    assert notices == []

    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"expected one aggregated warning, got {warnings}"
    assert "12 of 12" in warnings[0], warnings[0]

    # Each file was retried within its own budget, and no further.
    expected_requests = 1 + 12 * _NOTICE_FILE_MAX_ATTEMPTS
    assert calls["n"] == expected_requests, (
        f"expected {expected_requests} requests, got {calls['n']}"
    )

    # The detail is still available, just one level down.
    debug_text = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    )
    assert "giving up" in debug_text


def test_partial_file_failure_reports_the_ratio(caplog):
    """The aggregate line has to say how many of how many failed."""
    entries = [(5000 + i, "20260615") for i in range(4)]
    listing = _listing(*entries)
    failing_url_suffix = "R5002"

    def _status(n, url):
        if n == 1:
            return 200
        return 503 if failing_url_suffix in url else 200

    client, _ = _client(_status, listing=listing)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(client.fetch_new_notices())

    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "1 of 4" in warnings[0], warnings[0]


def test_not_published_notice_file_stays_at_debug(caplog):
    """
    A withdrawn or not-yet-readable notice file is not an outage. It must log
    at debug and must not count towards the cycle warning, or a routine 404
    would raise an alarm on every cycle.
    """
    entries = [(6000 + i, "20260615") for i in range(3)]
    listing = _listing(*entries)
    client, calls = _client(
        lambda n, url: 200 if n == 1 else 404, listing=listing
    )

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(client.fetch_new_notices()) == []

    assert _warnings(caplog) == [], "a not-published file must not warn"
    # Not retried either: retrying cannot make AEMO republish a withdrawn file.
    assert calls["n"] == 1 + 3

    debug_text = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    )
    assert "not published" in debug_text


def test_healthy_cycle_stays_silent_at_info(caplog):
    """A cycle with nothing wrong must not warn. The fix must not add noise."""
    entries = [(7000 + i, "20260615") for i in range(5)]
    listing = _listing(*entries)
    client, _ = _client(lambda n, url: 200, listing=listing)

    with caplog.at_level(logging.INFO):
        asyncio.run(client.fetch_new_notices())

    assert caplog.records == [], (
        f"a healthy cycle must be silent at INFO, got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
