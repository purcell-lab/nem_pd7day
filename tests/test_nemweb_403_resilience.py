"""NEMWEB 403 resilience across the STPASA and PD7DAY clients.

Issue #22. NEMWEB sits behind Akamai and answers 403 Forbidden, not 429, when a
caller asks too often. Before this change a single scattered 403 dropped a whole
refresh cycle: STPASA's fetch_all_regions caught everything and returned {} for
all five regions, and PD7DAY's only protection was a coordinator-level wrapper
that re-ran the entire fetch after a flat five second sleep.

These tests pin the three behaviours that matter operationally:

  1. A transient 403 that clears on retry must not cost a cycle.
  2. A sustained 403 must degrade to the stale cache, and say so once.
  3. A 403 and a 429 must be distinguishable in the log, because they call for
     different responses. A 429 is an explicit rate limit to back off from; a
     403 from Akamai may mean the source IP is blocked, where backing off does
     not help and a human needs to know.

Retry sleeps are stubbed throughout, so nothing here spends real time.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import zipfile
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.nem_pd7day import nemweb_retry  # noqa: E402
from custom_components.nem_pd7day.nemweb_retry import (  # noqa: E402
    NemwebFetchError,
    describe_status,
)
from custom_components.nem_pd7day.nem_time import now_nem  # noqa: E402
from custom_components.nem_pd7day.pd7day_client import PD7DayClient  # noqa: E402
from custom_components.nem_pd7day.stpasa_client import StpasaClient  # noqa: E402

_STPASA_FILE = "PUBLIC_STPASA_20260415_072507_1"
_LISTING_HTML = f'<a href="{_STPASA_FILE}.ZIP">{_STPASA_FILE}.ZIP</a>'


def run_async(coro):
    return asyncio.run(coro)


# ── stubs ─────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, *, status=200, text="", data=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._data = data

    async def text(self, *a, **k):
        return self._text

    async def read(self):
        return self._data


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _ScriptedSession:
    """Session returning a scripted status sequence per URL kind.

    Each of ``listing_statuses`` and ``file_statuses`` is consumed one entry per
    request; once exhausted the last entry repeats, so "403 forever" is written
    as a single-element list.
    """

    def __init__(
        self,
        *,
        zip_bytes: bytes,
        listing_statuses=(200,),
        file_statuses=(200,),
        headers=None,
    ):
        self._zip_bytes = zip_bytes
        self._listing_statuses = list(listing_statuses)
        self._file_statuses = list(file_statuses)
        self._headers = headers or {}
        self.listing_requests = 0
        self.file_requests = 0

    @staticmethod
    def _next(queue):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def get(self, url, *args, **kwargs):
        if url.endswith("/"):
            self.listing_requests += 1
            status = self._next(self._listing_statuses)
            text = _LISTING_HTML if status == 200 else ""
            return _Ctx(_Resp(status=status, text=text, headers=self._headers))
        self.file_requests += 1
        status = self._next(self._file_statuses)
        data = self._zip_bytes if status == 200 else b""
        return _Ctx(_Resp(status=status, data=data, headers=self._headers))


def _stpasa_zip() -> bytes:
    """Minimal but genuinely parseable STPASA payload."""
    header = (
        "D,STPASA,REGIONSOLUTION,1,2026/04/16 08:00:00,1,QLD1,"
        "5900,6000,6100,900,120,300,2026/04/15 07:25:07"
    )
    csv_bytes = (
        "C,NEMP.WORLD,STPASA,AEMO,PUBLIC,2026/04/15,07:25:07,1,,\n"
        "I,STPASA,REGIONSOLUTION,1,INTERVAL_DATETIME,RUNTYPE,REGIONID,"
        "DEMAND10,DEMAND50,DEMAND90,SURPLUSCAPACITY,SS_SOLAR_UIGF,"
        "SS_WIND_UIGF,LASTCHANGED\n"
        f"{header}\n"
        "C,END OF REPORT,4\n"
    ).encode()
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr(f"{_STPASA_FILE}.CSV", csv_bytes)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr(f"{_STPASA_FILE}.ZIP", inner.getvalue())
    return outer.getvalue()


@pytest.fixture
def no_sleep(monkeypatch):
    """Collapse retry backoff, and record what would have been waited.

    fetch_with_retry binds asyncio.sleep as a keyword-only default at import
    time, and the clients do not pass sleep= through, so the default itself is
    what has to be swapped. Patching the asyncio module attribute would be a
    no-op here.
    """
    recorded: list[float] = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    defaults = dict(nemweb_retry.fetch_with_retry.__kwdefaults__)
    patched = dict(defaults)
    patched["sleep"] = fake_sleep
    # Fixed jitter keeps the recorded delays deterministic.
    patched["jitter"] = lambda: 0.0
    monkeypatch.setattr(
        nemweb_retry.fetch_with_retry, "__kwdefaults__", patched
    )
    return recorded


# ── 1. a transient 403 must not cost a cycle ─────────────────────────────────


def test_transient_403_on_the_listing_does_not_drop_the_stpasa_cycle(no_sleep):
    """One 403 on the directory listing used to blank all five regions."""
    session = _ScriptedSession(
        zip_bytes=_stpasa_zip(), listing_statuses=[403, 200]
    )
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert "QLD1" in results, "a retried 403 should still yield data"
    assert session.listing_requests == 2, "the listing must be retried once"
    assert no_sleep, "the retry must back off rather than hammer immediately"


def test_transient_403_on_the_zip_does_not_drop_the_stpasa_cycle(no_sleep):
    """The listing can succeed and the file still draw a 403."""
    session = _ScriptedSession(
        zip_bytes=_stpasa_zip(), file_statuses=[403, 200]
    )
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert "QLD1" in results
    assert session.file_requests == 2


def test_two_consecutive_403s_still_recover_within_the_retry_budget(no_sleep):
    """The default budget is three attempts, so two failures are survivable."""
    session = _ScriptedSession(
        zip_bytes=_stpasa_zip(), listing_statuses=[403, 403, 200]
    )
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert "QLD1" in results
    assert session.listing_requests == 3


# ── 2. a sustained 403 degrades, and says so once ────────────────────────────


def test_sustained_403_gives_up_after_the_budget_and_stays_non_fatal(
    no_sleep, caplog
):
    """Best-effort semantics must survive: an empty result, not an exception.

    fetch_all_regions is called from a shared refresh path that keeps the
    previous STPASA values when nothing new arrives, so returning {} is the
    stale-cache fallback. What must not happen is an unhandled exception, and
    what must happen is exactly one warning naming the cause.
    """
    session = _ScriptedSession(zip_bytes=_stpasa_zip(), listing_statuses=[403])
    client = StpasaClient(session)

    with caplog.at_level(logging.DEBUG):
        results = run_async(client.fetch_all_regions())

    assert results == {}, "a sustained 403 must degrade, not raise"
    assert session.listing_requests == 3, "must stop at the retry budget"

    give_up = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "giving up" in r.getMessage()
    ]
    assert len(give_up) == 1, "exactly one give-up warning per failed fetch"
    assert "403" in give_up[0].getMessage()


def test_a_sustained_403_is_not_retried_forever(no_sleep):
    """The retry budget must be bounded, or a block becomes a request storm."""
    session = _ScriptedSession(zip_bytes=_stpasa_zip(), listing_statuses=[403])
    client = StpasaClient(session)

    run_async(client.fetch_all_regions())

    assert session.listing_requests == 3


def test_404_on_the_listing_is_not_retried(no_sleep):
    """A genuine 404 means the report path moved. Retrying cannot fix that."""
    session = _ScriptedSession(zip_bytes=_stpasa_zip(), listing_statuses=[404])
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert results == {}
    assert session.listing_requests == 1, "a 404 directory must not be retried"


def test_404_on_the_zip_is_treated_as_rotated_out_not_retried(no_sleep):
    """The filename came from the listing seconds earlier.

    A 404 on it means NEMWEB rotated the file out mid-cycle. Retrying the same
    URL cannot succeed; the next cycle resolves the new newest file.
    """
    session = _ScriptedSession(zip_bytes=_stpasa_zip(), file_statuses=[404])
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert results == {}
    assert session.file_requests == 1


def test_the_failure_surfaces_as_a_nemweb_fetch_error(no_sleep):
    """Callers below fetch_all_regions get a typed error, not a bare status.

    The PD7DAY coordinator branches on NemwebFetchError to serve stale data, so
    the type is part of the contract rather than an implementation detail.
    """
    session = _ScriptedSession(zip_bytes=_stpasa_zip(), listing_statuses=[403])
    client = StpasaClient(session)

    with pytest.raises(NemwebFetchError):
        run_async(client._list_files())


# ── 3. 403 and 429 must be distinguishable ───────────────────────────────────


def test_403_and_429_are_distinguishable_in_the_log(no_sleep, caplog):
    """Same shape of failure, different operational meaning.

    A 429 is an explicit rate limit: back off and it clears. A 403 from Akamai
    may mean the source IP is blocked, where backing off does not help. Before
    this change both logged as an indistinguishable failure line.
    """
    messages = {}
    for status in (403, 429):
        session = _ScriptedSession(
            zip_bytes=_stpasa_zip(), listing_statuses=[status]
        )
        client = StpasaClient(session)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            run_async(client.fetch_all_regions())
        give_up = [
            r.getMessage() for r in caplog.records if "giving up" in r.getMessage()
        ]
        assert len(give_up) == 1
        messages[status] = give_up[0]

    assert messages[403] != messages[429]
    assert "bot or rate block" in messages[403]
    assert "explicit rate limit" in messages[429]


def test_describe_status_names_the_statuses_that_matter():
    """The helper is the single place the two are told apart."""
    assert "bot or rate block" in describe_status(403)
    assert "explicit rate limit" in describe_status(429)
    assert "408" in describe_status(408)
    assert "server side" in describe_status(503)
    # Unknown and absent statuses must not fabricate meaning.
    assert describe_status(200) == "HTTP 200"
    assert describe_status(None) == ""


def test_retry_after_on_a_429_is_honoured(no_sleep):
    """NEMWEB rarely sends Retry-After, but when it does it must win.

    Ignoring it and using our own backoff is what turns a soft rate limit into
    a hard block.
    """
    session = _ScriptedSession(
        zip_bytes=_stpasa_zip(),
        listing_statuses=[429, 200],
        headers={"Retry-After": "3"},
    )
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert "QLD1" in results
    assert no_sleep == pytest.approx([3.0]), (
        f"expected a 3 s Retry-After wait, got {no_sleep}"
    )


# ── 4. the PD7DAY path ───────────────────────────────────────────────────────
#
# PD7DAY was the one report type that already had a 403 retry, but it lived in
# the coordinator and re-ran the whole fetch, listing plus every file, after a
# flat five second sleep. It is now handled per request inside the client like
# every other report.


def _pd7day_csv() -> bytes:
    run = now_nem().replace(minute=0, second=0, microsecond=0)
    run_s = run.strftime("%Y/%m/%d %H:%M:%S")
    period_s = (run + timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S")
    tail = ",0,0,0,0,0,0,0,0,0,0,0"
    rows = [
        "C,NEOD,PD7DAY,1,PUBLIC_PD7DAY_X.zip",
        f"D,PD7DAY,CASESOLUTION,1,{run_s},0,{run_s}",
        f"D,PD7DAY,PRICESOLUTION,1,{run_s},1,{period_s},QLD1,95.50{tail}",
    ]
    return "\n".join(rows).encode()


def _pd7day_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PUBLIC_PD7DAY_X.CSV", _pd7day_csv())
    return buf.getvalue()


class _Pd7dayScriptedSession(_ScriptedSession):
    """Same scripting, but a PD7DAY-shaped directory listing."""

    def get(self, url, *args, **kwargs):
        if url.endswith("/"):
            self.listing_requests += 1
            status = self._next(self._listing_statuses)
            text = (
                '<a href="/Reports/Current/PD7Day/PUBLIC_PD7DAY_1.zip">z</a>'
                if status == 200
                else ""
            )
            return _Ctx(_Resp(status=status, text=text, headers=self._headers))
        self.file_requests += 1
        status = self._next(self._file_statuses)
        data = self._zip_bytes if status == 200 else b""
        return _Ctx(_Resp(status=status, data=data, headers=self._headers))


def test_transient_403_does_not_drop_the_pd7day_cycle(no_sleep):
    """The behaviour the removed coordinator wrapper used to provide."""
    session = _Pd7dayScriptedSession(
        zip_bytes=_pd7day_zip(), listing_statuses=[403, 200]
    )
    client = PD7DayClient(session)

    result = run_async(client.fetch_all(["QLD1"], []))

    assert "QLD1" in result.prices
    assert session.listing_requests == 2


def test_transient_403_on_a_pd7day_file_is_retried(no_sleep):
    session = _Pd7dayScriptedSession(
        zip_bytes=_pd7day_zip(), file_statuses=[403, 200]
    )
    client = PD7DayClient(session)

    result = run_async(client.fetch_all(["QLD1"], []))

    assert "QLD1" in result.prices
    assert session.file_requests == 2


def test_sustained_403_on_pd7day_raises_a_typed_error(no_sleep):
    """The coordinator needs a type to branch on to serve stale data."""
    session = _Pd7dayScriptedSession(
        zip_bytes=_pd7day_zip(), listing_statuses=[403]
    )
    client = PD7DayClient(session)

    with pytest.raises(NemwebFetchError):
        run_async(client.fetch_all(["QLD1"], []))

    assert session.listing_requests == 3


def test_the_whole_fetch_is_not_replayed_on_a_403(no_sleep):
    """Only the failing request is retried, not the listing plus every file.

    The old coordinator wrapper re-ran fetch_all wholesale, so a 403 on one file
    cost a second directory listing and a second download of everything. That
    made the burst worse at precisely the moment NEMWEB was already objecting.
    """
    session = _Pd7dayScriptedSession(
        zip_bytes=_pd7day_zip(), file_statuses=[403, 200]
    )
    client = PD7DayClient(session)

    run_async(client.fetch_all(["QLD1"], []))

    assert session.listing_requests == 1, (
        "a file-level 403 must not force a second directory listing"
    )
