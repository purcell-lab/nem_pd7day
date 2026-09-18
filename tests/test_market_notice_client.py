"""Tests for market_notice_client: the listing and notice parsers and the
MarketNoticeClient fetch cycle.

History kept from the files merged here:

* Current-window filtering and cursor advance. The cursor only advanced when
  an LOR or MSL notice was stored. Those are rare, so it stayed parked while
  NEMWEB kept publishing, and every cycle re-examined every notice issued
  since the last relevant one, serially, with a delay before each request,
  once per region: 145 file requests per region per cycle at its worst.

* Fetch failure visibility (issue #44). Every fetch failure was logged at
  debug only. The module logger sits at INFO by default, so a sustained notice
  outage produced no log output at all: the grid notices sensor stopped
  updating and nothing explained why. The fix has to keep: a genuine directory
  failure warns once after being retried, per-file failures produce one
  aggregated warning per cycle rather than up to forty, a not-published file
  stays at debug, and a healthy cycle stays silent at INFO.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()
_const, _retry, mnc = load_chain("const", "nemweb_retry", "market_notice_client")

MarketNoticeClient = mnc.MarketNoticeClient
GridNoticeAnnotation = mnc.GridNoticeAnnotation
_parse_directory_listing = mnc._parse_directory_listing
_parse_notice_body = mnc._parse_notice_body

NOW = datetime(2026, 6, 15, 14, 0, tzinfo=NEM_TZ)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    """Make the client's pacing sleep and the retry backoff return at once.

    The client sleeps _NOTICE_FETCH_DELAY_S (0.1 s) before each file fetch
    under a gate of two, and fetch_with_retry sleeps a jittered 0.25 to 1 s
    before each retry, holding a gate slot while a per-file fetch does so.
    Nothing here asserts on elapsed time: a retry is proven by the request
    count. Before this fixture the 40-file cap test spent 2 s and the
    twelve-file retry test 2.8 s waiting for nothing. The replacement still
    yields to the loop, so the concurrency behaviour under test is unchanged.
    """
    async def _yield_only(_delay):
        await asyncio.sleep(0)

    monkeypatch.setattr(mnc, "_NOTICE_FETCH_DELAY_S", 0)
    monkeypatch.setattr(
        mnc,
        "fetch_with_retry",
        functools.partial(_retry.fetch_with_retry, sleep=_yield_only),
    )


# ── Notice fixtures ──────────────────────────────────────────────────────────

LOR_NOTICE_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 128465 RESERVE NOTICE 11/08/2025 03:25:07 PM

STPASA - Forecast Lack Of Reserve Level 1 (LOR1) in the SA Region on 19/08/2025

AEMO declares a Forecast LOR1 condition for the SA region for the following period:
[1.] From 0000 hrs 19/08/2025 to 0300 hrs 19/08/2025.
The forecast capacity reserve requirement is 411 MW.
The minimum capacity reserve available is 407 MW.

AEMO Operations
END OF REPORT
"""

MSL_NOTICE_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 124467 MINIMUM SYSTEM LOAD 11/02/2025 02:43:06 PM

Forecast Minimum System Load (MSL1) condition in the VIC region on 16/02/2025

The regional demand is forecast to be below the MSL1 threshold for the following period:
[1.] From 1230 hrs 16/02/2025 to 1430 hrs 16/02/2025. Minimum regional demand is forecast to be 2012 MW at 1330 hrs.

AEMO Operations
END OF REPORT
"""

LOR_MULTI_PERIOD_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 144109 RESERVE NOTICE 17/05/2026 14:34:41

ST PASA - Update of the Forecast Lack Of Reserve Level 1 (LOR1) in the QLD Region on 19/05/2026

The Forecast LOR1 condition in the QLD region has been updated to the following:
[1.] From 0600 hrs 19/05/2026 to 1930 hrs 19/05/2026.
The forecast capacity reserve requirement is 1197 MW.
The minimum capacity reserve available is 1012 MW.

[2.] From 2130 hrs 19/05/2026 to 2200 hrs 19/05/2026.
The forecast capacity reserve requirement is 1199 MW.

Manager NEM Real Time Operations
END OF REPORT
"""


def _lor_level_notice(level: int, notice_id: int) -> str:
    """The boxed NEMITWEB1 form of an LOR notice, whose header line lists
    every level ("LRC/LOR1/LOR2/LOR3") before the body states the real one."""
    return f"""
-------------------------------------------------------------------
                           MARKET NOTICE
-------------------------------------------------------------------

From :              AEMO
To   :              NEMITWEB1
Creation Date :     07/06/2026     10:50:08

-------------------------------------------------------------------

Notice ID               :         {notice_id}
Notice Type ID          :         RESERVE NOTICE
Notice Type Description :         LRC/LOR1/LOR2/LOR3
Issue Date              :         07/06/2026
External Reference      :         STPASA - Forecast Lack Of Reserve Level {level} (LOR{level}) in the SA Region on 10/06/2026

-------------------------------------------------------------------

Reason :

AEMO ELECTRICITY MARKET NOTICE

AEMO declares a Forecast LOR{level} condition under clause 4.8.4(b) of the National Electricity Rules for the SA region for the following period:

[1.] From 0800 hrs 10/06/2026 to 1000 hrs 10/06/2026.
The forecast capacity reserve requirement is 744 MW.
The minimum capacity reserve available is 542 MW.

AEMO is seeking a market response.

AEMO has not yet estimated the latest time at which it would need to intervene through an AEMO intervention event.

Manager NEM Real Time Operations

-------------------------------------------------------------------
END OF REPORT
-------------------------------------------------------------------
"""


CANCELLATION_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 124560 MINIMUM SYSTEM LOAD 12/02/2025 03:04:06 PM

Cancellation of Forecast Minimum System Load (MSL) MSL1 event in the VIC Region.
Cancellation - Forecast MSL1 - VIC Region at 1400 hrs 13/02/2025.
Refer to Market Notice 124467 for MSL1.

AEMO Operations
END OF REPORT
"""

LOR_CANCELLATION_BY_DATE_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 144114 RESERVE NOTICE 18/05/2026 16:16:00

PDPASA - Cancellation of the Forecast Lack Of Reserve Level 1 (LOR1) in the QLD Region on 18/05/2026

Manager NEM Real Time Operations
END OF REPORT
"""

MSL_CANCELLATION_BY_DATE_TEXT = """
MARKET NOTICE
AEMO ELECTRICITY MARKET NOTICE 144200 MINIMUM SYSTEM LOAD 18/05/2026 10:00:00

PDPASA - Cancellation of the Forecast Minimum System Load Level 2 (MSL2) in the SA Region on 20/05/2026

AEMO Operations
END OF REPORT
"""

DIRECTORY_HTML = """
<pre>
01/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133900
01/01/2026 01:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133901
02/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260102.R133910
</pre>
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _listing(*entries: tuple[int, str]) -> str:
    """A NEMWEB-style directory listing from (notice_id, YYYYMMDD) pairs."""
    lines = [
        f"15/06/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_{day}.R{nid}"
        for nid, day in entries
    ]
    return "<pre>\n" + "\n".join(lines) + "\n</pre>"


def _client(
    listing: str,
    *,
    body="NOT A RELEVANT NOTICE",
    status=None,
    clock=None,
    semaphore=None,
):
    """A client on a mock session: request 1 is the listing, the rest are files.

    ``status(n, url)`` decides each request's HTTP status, ``n`` being 1-based
    over every request the client makes; the default answers 200 throughout.
    ``body`` is the text of every file response, or an async callable that
    produces it. Returns the client and a dict counting requests and recording
    their URLs.
    """
    calls = {"n": 0, "urls": []}

    def _get(url, *args, **kwargs):
        calls["n"] += 1
        calls["urls"].append(url)
        resp = AsyncMock()
        resp.status = status(calls["n"], url) if status else 200
        resp.headers = {}
        if calls["n"] == 1:
            resp.text = AsyncMock(return_value=listing)
        elif callable(body):
            resp.text = AsyncMock(side_effect=body)
        else:
            resp.text = AsyncMock(return_value=body)
        return AsyncMock(
            __aenter__=AsyncMock(return_value=resp),
            __aexit__=AsyncMock(return_value=False),
        )

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    client = MarketNoticeClient(
        session, semaphore=semaphore, clock=clock or (lambda: NOW)
    )
    return client, calls


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def _debug_text(caplog) -> str:
    return "\n".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    )


# ── Directory listing parser ─────────────────────────────────────────────────

def test_parse_directory_listing():
    files = _parse_directory_listing(DIRECTORY_HTML)
    assert len(files) == 3
    assert files[0] == (133900, "NEMITWEB1_MKTNOTICE_20260101.R133900")
    assert files[2][0] == 133910


def test_parse_directory_listing_deduplicates():
    """NEMWEB returns each file twice; _parse_directory_listing deduplicates."""
    duplicated_html = """
<pre>
01/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133900
01/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260101.R133900
02/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260102.R133910
02/01/2026 12:00 PM  1234 NEMITWEB1_MKTNOTICE_20260102.R133910
</pre>
"""
    files = _parse_directory_listing(duplicated_html)
    assert len(files) == 2
    assert files[0] == (133900, "NEMITWEB1_MKTNOTICE_20260101.R133900")
    assert files[1] == (133910, "NEMITWEB1_MKTNOTICE_20260102.R133910")


# ── Notice body parser ───────────────────────────────────────────────────────

def test_parse_lor_notice():
    notice = _parse_notice_body(LOR_NOTICE_TEXT, 128465)
    assert notice is not None
    assert notice.notice_type == "LOR"
    assert notice.level == 1
    assert notice.region == "SA1"
    assert not notice.is_cancelled
    assert notice.reserve_req_mw == 411.0
    assert notice.surplus_mw == 407.0
    assert notice.period_from.hour == 0
    assert notice.period_to.hour == 3


def test_parse_msl_notice():
    notice = _parse_notice_body(MSL_NOTICE_TEXT, 124467)
    assert notice is not None
    assert notice.notice_type == "MSL"
    assert notice.level == 1
    assert notice.region == "VIC1"
    assert not notice.is_cancelled
    assert notice.forecast_mw == 2012.0
    assert notice.period_from.hour == 12
    assert notice.period_from.minute == 30
    assert notice.period_to.hour == 14
    assert notice.period_to.minute == 30


def test_parse_multi_period_lor_notice_uses_widest_window():
    """Multi-period notice should use earliest period_from and latest period_to."""
    notice = _parse_notice_body(LOR_MULTI_PERIOD_TEXT, 144109)
    assert notice is not None
    assert notice.notice_type == "LOR"
    assert notice.level == 1
    assert notice.region == "QLD1"
    assert not notice.is_cancelled
    # Period 1: 0600-1930 19/05/2026, Period 2: 2130-2200 19/05/2026.
    assert notice.period_from.hour == 6
    assert notice.period_from.minute == 0
    assert notice.period_to.hour == 22
    assert notice.period_to.minute == 0


@pytest.mark.parametrize("level, notice_id", [(2, 144205), (3, 144206)])
def test_parse_lor_level_from_body_not_header(level, notice_id):
    """LOR2 and LOR3 must not parse as level 1 from the "LRC/LOR1/LOR2/LOR3"
    header line, which always lists LOR1 first. This is the defect behind
    notice store schema v2."""
    notice = _parse_notice_body(_lor_level_notice(level, notice_id), notice_id)
    assert notice is not None
    assert notice.notice_type == "LOR"
    assert notice.level == level
    assert notice.region == "SA1"
    assert not notice.is_cancelled


@pytest.mark.parametrize(
    "text, notice_id, notice_type, level, region, cancels_notice_id, cancellation_date",
    [
        (CANCELLATION_TEXT, 124560, "MSL", 1, "VIC1", 124467, date(2025, 2, 13)),
        (LOR_CANCELLATION_BY_DATE_TEXT, 144114, "LOR", 1, "QLD1", None, date(2026, 5, 18)),
        (MSL_CANCELLATION_BY_DATE_TEXT, 144200, "MSL", 2, "SA1", None, date(2026, 5, 20)),
    ],
    ids=["refer-to-notice-id-and-date", "lor-by-date-only", "msl-by-date-only"],
)
def test_parse_cancellation_notice(
    text, notice_id, notice_type, level, region, cancels_notice_id, cancellation_date
):
    """A cancellation carries the referenced notice ID when the text names one,
    and the date of the cancelled period either way. AEMO's PDPASA
    cancellations name no notice ID at all, so the date is what the store
    matches on."""
    notice = _parse_notice_body(text, notice_id)
    assert notice is not None
    assert notice.notice_type == notice_type
    assert notice.level == level
    assert notice.region == region
    assert notice.is_cancelled is True
    assert notice.cancels_notice_id == cancels_notice_id
    assert notice.cancellation_date == cancellation_date


def test_non_lor_msl_returns_none():
    assert _parse_notice_body("RECLASSIFY CONTINGENCY some other notice text", 99999) is None


def test_to_dict_roundtrip():
    """GridNoticeAnnotation round-trips through to_dict/from_dict."""
    notice = _parse_notice_body(LOR_NOTICE_TEXT, 128465)
    assert notice is not None
    restored = GridNoticeAnnotation.from_dict(notice.to_dict())
    assert restored.notice_id == notice.notice_id
    assert restored.notice_type == notice.notice_type
    assert restored.level == notice.level
    assert restored.region == notice.region
    assert restored.period_from == notice.period_from
    assert restored.period_to == notice.period_to
    assert restored.reserve_req_mw == notice.reserve_req_mw
    assert restored.surplus_mw == notice.surplus_mw


# ── Current-notice window and cursor advance ─────────────────────────────────

def test_only_current_and_previous_day_are_fetched():
    """Files older than the previous NEM day are never requested."""
    listing = _listing(
        (1000, "20260601"),  # stale
        (1001, "20260610"),  # stale
        (1002, "20260614"),  # previous day, current
        (1003, "20260615"),  # today, current
    )
    client, calls = _client(listing)

    run_async(client.fetch_new_notices())

    # One listing request plus the two current files.
    assert calls["n"] == 3
    fetched = calls["urls"][1:]
    assert any("R1002" in u for u in fetched)
    assert any("R1003" in u for u in fetched)
    assert not any("R1000" in u or "R1001" in u for u in fetched)


def test_cursor_advances_past_stale_files():
    """Stale files are skipped once, not reconsidered on every later cycle."""
    listing = _listing((1000, "20260601"), (1001, "20260602"))
    client, calls = _client(listing)

    assert run_async(client.fetch_new_notices()) == []

    # Nothing fetched, but the cursor moved past both stale files.
    assert calls["n"] == 1
    assert client.last_seen_notice_id == 1001

    # A second cycle over the same listing does no work at all.
    client2, calls2 = _client(listing)
    client2.last_seen_notice_id = 1001
    run_async(client2.fetch_new_notices())
    assert calls2["n"] == 1


def test_files_at_or_below_cursor_are_not_fetched():
    """With last_seen set, only the newer current files are requested."""
    listing = _listing((3000, "20260614"), (3001, "20260615"), (3002, "20260615"))
    client, calls = _client(listing)
    client.last_seen_notice_id = 3001  # one behind the latest

    run_async(client.fetch_new_notices())

    assert calls["n"] == 2
    assert "R3002" in calls["urls"][1]
    assert client.last_seen_notice_id == 3002


def test_current_files_are_fetched_and_parsed():
    """Current files come back parsed, and the cursor lands on the highest."""
    listing = _listing((200100, "20260615"), (200101, "20260615"))
    client, calls = _client(listing, body=LOR_NOTICE_TEXT)
    assert client.last_seen_notice_id == 0

    result = run_async(client.fetch_new_notices())

    assert calls["n"] == 3
    assert [n.notice_id for n in result] == [200100, 200101]
    assert all(n.notice_type == "LOR" for n in result)
    assert client.last_seen_notice_id == 200101


def test_cursor_advances_when_no_relevant_notice_found():
    """A file that is neither LOR nor MSL still advances the cursor."""
    listing = _listing((2000, "20260615"), (2001, "20260615"))
    client, _ = _client(listing, body="GENERAL NOTICE, nothing to see")

    assert run_async(client.fetch_new_notices()) == []
    assert client.last_seen_notice_id == 2001


def test_cursor_advances_when_file_fetch_fails():
    """A failed file fetch does not pin the cursor and force a permanent retry.

    Mirrors the 403s NEMWEB returned when the backlog was re-read.
    """
    listing = _listing((3000, "20260615"), (3001, "20260615"))
    client, _ = _client(listing, status=lambda n, url: 200 if n == 1 else 403)

    assert run_async(client.fetch_new_notices()) == []
    assert client.last_seen_notice_id == 3001


def test_per_cycle_file_cap_defers_remainder_contiguously():
    """The cap truncates the newest files, so the cursor leaves no gap behind."""
    cap = mnc._NOTICE_MAX_FILES_PER_CYCLE
    total = cap + 7
    listing = _listing(*[(5000 + i, "20260615") for i in range(total)])
    client, calls = _client(listing)

    run_async(client.fetch_new_notices())

    assert calls["n"] == 1 + cap
    # Cursor sits at the last fetched file, not the highest listed one, so the
    # deferred remainder is still picked up next cycle.
    assert client.last_seen_notice_id == 5000 + cap - 1
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

    client, _ = _client(listing, body=_slow_text, semaphore=asyncio.Semaphore(2))
    run_async(client.fetch_new_notices())

    assert peak <= 2, f"expected at most 2 concurrent fetches, saw {peak}"


def test_empty_listing_is_handled():
    """An unparseable or empty listing yields nothing and does not raise."""
    client, calls = _client("<pre>\n</pre>")
    assert run_async(client.fetch_new_notices()) == []
    assert calls["n"] == 1


# ── Fetch failure visibility (issue #44) ─────────────────────────────────────

def test_directory_403_warns_once_after_retrying(caplog):
    """A throttled or failing directory listing must be visible at the default
    log level. The old code returned an empty list on a 403 with a single
    debug line, so a sustained outage was completely silent."""
    client, calls = _client(_listing((3000, "20260615")), status=lambda n, url: 403)

    with caplog.at_level(logging.DEBUG):
        assert run_async(client.fetch_new_notices()) == []

    # Retried rather than abandoned on the first 403.
    assert calls["n"] > 1, "the listing must be retried before giving up"

    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert "403" in warnings[0]
    assert "Market_Notice" in warnings[0]


def test_directory_404_warns_without_retrying(caplog):
    """The Market_Notice directory always exists, so a 404 means the report
    path moved. That warns, and retrying it is pointless."""
    client, calls = _client(_listing((3000, "20260615")), status=lambda n, url: 404)

    with caplog.at_level(logging.DEBUG):
        assert run_async(client.fetch_new_notices()) == []

    assert calls["n"] == 1, "a 404 on the directory must not be retried"
    assert len(_warnings(caplog)) == 1


def test_per_file_failures_produce_one_aggregated_warning(caplog):
    """Twelve failing notice files must produce one warning, not twelve.

    Per-file give-up lines are suppressed to debug so the cycle can summarise
    them, which is why the aggregate line has to exist: without it the
    suppression would recreate the silence in issue #44.
    """
    listing = _listing(*[(4000 + i, "20260615") for i in range(12)])
    # Call 1 is the listing and succeeds; every file request 500s.
    client, calls = _client(listing, status=lambda n, url: 200 if n == 1 else 500)

    with caplog.at_level(logging.DEBUG):
        assert run_async(client.fetch_new_notices()) == []

    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"expected one aggregated warning, got {warnings}"
    assert "12 of 12" in warnings[0], warnings[0]

    # Each file was retried within its own budget, and no further.
    expected_requests = 1 + 12 * mnc._NOTICE_FILE_MAX_ATTEMPTS
    assert calls["n"] == expected_requests, (
        f"expected {expected_requests} requests, got {calls['n']}"
    )

    # The detail is still available, just one level down.
    assert "giving up" in _debug_text(caplog)


def test_partial_file_failure_reports_the_ratio(caplog):
    """The aggregate line has to say how many of how many failed."""
    listing = _listing(*[(5000 + i, "20260615") for i in range(4)])

    def _status(n, url):
        if n == 1:
            return 200
        return 503 if "R5002" in url else 200

    client, _ = _client(listing, status=_status)

    with caplog.at_level(logging.DEBUG):
        run_async(client.fetch_new_notices())

    warnings = _warnings(caplog)
    assert len(warnings) == 1, warnings
    assert "1 of 4" in warnings[0], warnings[0]


def test_not_published_notice_file_stays_at_debug(caplog):
    """A withdrawn or not-yet-readable notice file is not an outage. It must
    log at debug and must not count towards the cycle warning, or a routine
    404 would raise an alarm on every cycle."""
    listing = _listing(*[(6000 + i, "20260615") for i in range(3)])
    client, calls = _client(listing, status=lambda n, url: 200 if n == 1 else 404)

    with caplog.at_level(logging.DEBUG):
        assert run_async(client.fetch_new_notices()) == []

    assert _warnings(caplog) == [], "a not-published file must not warn"
    # Not retried either: retrying cannot make AEMO republish a withdrawn file.
    assert calls["n"] == 1 + 3
    assert "not published" in _debug_text(caplog)


def test_healthy_cycle_stays_silent_at_info(caplog):
    """A cycle with nothing wrong must not warn. The fix must not add noise."""
    listing = _listing(*[(7000 + i, "20260615") for i in range(5)])
    client, _ = _client(listing)

    with caplog.at_level(logging.INFO):
        run_async(client.fetch_new_notices())

    assert caplog.records == [], (
        f"a healthy cycle must be silent at INFO, got "
        f"{[r.getMessage() for r in caplog.records]}"
    )
