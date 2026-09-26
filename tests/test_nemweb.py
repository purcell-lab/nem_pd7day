"""
NEMWEB access: the shared bounded retry, 403 resilience in every client that
uses it, the request gate, and the User-Agent invariant.

Sections
--------
nemweb_retry (issue #36)
  In a 20 hour sample (2026-08-31 10:31 to 2026-09-01 06:32 NEM time) the
  TradingIS directory listing failed 19 times and logged the same useless
  line every time, "TradingIS: failed to fetch directory listing". The except
  clause bound nothing, so the exception type, status and URL were discarded,
  and nothing retried, so each failure permanently dropped one 30 minute
  settlement price. The retry helper and the TradingIS tests here pin: the
  warning names the exception and the URL; a transient failure is retried and
  can then succeed; an exhausted retry returns None and never raises; a
  Retry-After header replaces the computed backoff; "not published yet" does
  not warn and is not retried. ``warn_on_exhausted=False`` (issue #44) lets a
  fan-out caller aggregate the give-up lines itself.

403 resilience (issue #22)
  NEMWEB sits behind Akamai and answers 403 Forbidden, not 429, when a caller
  asks too often. A single scattered 403 used to drop a whole refresh cycle:
  STPASA's fetch_all_regions returned {} for all five regions, and PD7DAY's
  only protection was a coordinator wrapper that re-ran the entire fetch after
  a flat five second sleep. Pinned here: a transient 403 must not cost a
  cycle; a sustained 403 degrades to the stale cache and says so once; a 403
  and a 429 must be distinguishable in the log because a 429 is a rate limit
  to back off from while an Akamai 403 may mean the IP is blocked.

NemwebGate (issue #22)
  Replaces the plain asyncio.Semaphore under NEMWEB_SEMAPHORE_KEY. Concurrency
  was already capped at two; what was missing was a bound on request
  frequency, which is what NEMWEB answers with a 403. The clock and sleep are
  injected so pacing is asserted exactly rather than by measuring wall time.

NEMWEB_HEADERS invariant (issue #102)
  const.py defines one browser-like User-Agent for the whole integration.
  Two clients were found still sending a hardcoded "nem_pd7day/2.3" and the
  DispatchIS urllib fallback sending no User-Agent at all.

No test here touches the network and no test sleeps: the retry's sleep is
injected (or its keyword default patched by ``no_sleep``) and only records
the delays it was asked for.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import types
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import pytest

from support import NEM_TZ, PKG_DIR, install_ha_stubs, load_chain, make_zip, run_async

install_ha_stubs()

(
    _const,
    _nem_time,
    _executor,
    _retry,
    _gate_mod,
    _tradingis_mod,
    _stpasa_mod,
    _pd7day_mod,
    _diag_mod,
) = load_chain(
    "const",
    "nem_time",
    "executor",
    "nemweb_retry",
    "nemweb_gate",
    "tradingis_client",
    "stpasa_client",
    "pd7day_client",
    "diagnostics",
)

NemwebGate = _gate_mod.NemwebGate
TradingISClient = _tradingis_mod.TradingISClient
StpasaClient = _stpasa_mod.StpasaClient
PD7DayClient = _pd7day_mod.PD7DayClient
NemwebFetchError = _retry.NemwebFetchError

TRADINGIS_BASE_URL = _const.TRADINGIS_BASE_URL
STPASA_LISTING_URL = _stpasa_mod.STPASA_CURRENT_URL
PD7DAY_LISTING_URL = _const.NEMWEB_BASE_URL


# ── Fakes ────────────────────────────────────────────────────────────────────
# Candidates for support.py: every NEMWEB client test needs the same shapes.

class FakeResponse:
    """Minimal aiohttp-shaped response, usable as its own context manager.

    No raise_for_status: the clients read resp.status through classify_status
    so they can tell a 404 apart from a 403.
    """

    def __init__(self, status=200, text="", body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
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
    """Marker for "this attempt raises instead of answering"."""

    def __init__(self, exc):
        self.exc = exc


class QueueSession:
    """Serves a queued script of responses per URL and logs every request.

    The last entry for a URL repeats forever, so a test can say "fails, then
    succeeds" or "always fails" without counting attempts. An unknown URL
    answers 404.
    """

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

    def count(self, url) -> int:
        return self.request_log.count(url)


class RecordingSleep:
    """Injected in place of asyncio.sleep so the suite never actually waits."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay):
        self.delays.append(delay)


class CountingSemaphore:
    """Wraps a real semaphore and counts acquisitions."""

    def __init__(self, value=2):
        self._sem = asyncio.Semaphore(value)
        self.acquired = 0
        self.max_held = 0
        self._held = 0

    async def __aenter__(self):
        await self._sem.acquire()
        self.acquired += 1
        self._held += 1
        self.max_held = max(self.max_held, self._held)
        return self

    async def __aexit__(self, *exc):
        self._held -= 1
        self._sem.release()
        return False


@pytest.fixture
def no_sleep(monkeypatch) -> list[float]:
    """Collapse the retry backoff and return the list of delays it asked for.

    fetch_with_retry binds asyncio.sleep as a keyword-only default at import
    time, and the STPASA and PD7DAY clients do not pass sleep= through, so the
    default itself is what has to be swapped. Patching the asyncio module
    attribute would be a no-op here. Jitter is fixed so the recorded delays
    are deterministic.
    """
    sleeper = RecordingSleep()
    patched = dict(_retry.fetch_with_retry.__kwdefaults__)
    patched["sleep"] = sleeper
    patched["jitter"] = lambda: 0.0
    monkeypatch.setattr(_retry.fetch_with_retry, "__kwdefaults__", patched)
    return sleeper.delays


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def _give_ups(caplog):
    return [r for r in caplog.records if "giving up" in r.getMessage()]


# ── Scripted sessions per report type ────────────────────────────────────────

def _scripted(url, statuses, *, ok, headers=None):
    """A per-URL script: ``ok`` (text or body kwargs) on 200, empty otherwise."""
    return [
        FakeResponse(status=s, headers=headers, **(ok if s == 200 else {}))
        for s in statuses
    ]


# TradingIS: a dated zip per interval, resolved from a directory listing.

def _trading_csv(settlement_str, region, rrp_mwh):
    return (
        "C,NEMP.WORLD,TRADINGIS,v3\n"
        "I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,"
        "INVALIDFLAG,LASTCHANGED,PRICE_STATUS\n"
        f'D,TRADING,PRICE,3,"{settlement_str}",1,{region},223,{rrp_mwh},0,0,'
        f'"2026/09/01 06:31:00",FIRM\n'
        'C,"END OF REPORT",3\n'
    )


def _interval(hour=17, minute=0):
    return datetime(2026, 9, 1, hour, minute, tzinfo=NEM_TZ)


def _tradingis_zip_url(interval_start):
    end = interval_start + timedelta(minutes=30)
    return TRADINGIS_BASE_URL + f"PUBLIC_TRADINGIS_{end.strftime('%Y%m%d%H%M')}_1.zip"


def _tradingis_dir_html(interval_start):
    end = interval_start + timedelta(minutes=30)
    fn = f"PUBLIC_TRADINGIS_{end.strftime('%Y%m%d%H%M')}_1.zip"
    return f'<html><body><a href="{fn}">{fn}</a></body></html>'


def _tradingis_good_zip(interval_start, region="QLD1", rrp_mwh=95.69):
    end = interval_start + timedelta(minutes=30)
    csv = _trading_csv(end.strftime("%Y/%m/%d %H:%M:00"), region, rrp_mwh)
    return FakeResponse(body=make_zip(csv.encode(), member="PUBLIC_TRADINGIS.csv"))


# STPASA: one nested ZIP holding every region.

_STPASA_FILE = "PUBLIC_STPASA_20260415_072507_1"
STPASA_ZIP_URL = urljoin(STPASA_LISTING_URL, f"{_STPASA_FILE}.ZIP")
_STPASA_LISTING_HTML = f'<a href="{_STPASA_FILE}.ZIP">{_STPASA_FILE}.ZIP</a>'
_STPASA_CSV = (
    "C,NEMP.WORLD,STPASA,AEMO,PUBLIC,2026/04/15,07:25:07,1,,\n"
    "I,STPASA,REGIONSOLUTION,1,INTERVAL_DATETIME,RUNTYPE,REGIONID,"
    "DEMAND10,DEMAND50,DEMAND90,SURPLUSCAPACITY,SS_SOLAR_UIGF,"
    "SS_WIND_UIGF,LASTCHANGED\n"
    "D,STPASA,REGIONSOLUTION,1,2026/04/16 08:00:00,1,QLD1,"
    "5900,6000,6100,900,120,300,2026/04/15 07:25:07\n"
    "C,END OF REPORT,4\n"
).encode()


def make_stpasa_zip(csv_bytes: bytes = _STPASA_CSV, name: str = _STPASA_FILE) -> bytes:
    """Wrap csv_bytes in the outer/inner ZIP layout STPASA publishes.

    Candidate for support.py: test_stpasa_client.py carries the same helper.
    """
    inner = make_zip(csv_bytes, member=f"{name}.CSV")
    return make_zip(inner, member=f"{name}.ZIP")


def stpasa_session(listing=(200,), zip_=(200,), headers=None) -> QueueSession:
    return QueueSession({
        STPASA_LISTING_URL: _scripted(
            STPASA_LISTING_URL, listing, ok={"text": _STPASA_LISTING_HTML}, headers=headers
        ),
        STPASA_ZIP_URL: _scripted(
            STPASA_ZIP_URL, zip_, ok={"body": make_stpasa_zip()}, headers=headers
        ),
    })


# PD7DAY: a flat ZIP; the CSV is built relative to now because the client
# discards intervals that have already passed.

_PD7DAY_HREF = "/Reports/Current/PD7Day/PUBLIC_PD7DAY_1.zip"
PD7DAY_ZIP_URL = urljoin(PD7DAY_LISTING_URL, _PD7DAY_HREF)
_PD7DAY_LISTING_HTML = f'<a href="{_PD7DAY_HREF}">z</a>'


def _pd7day_zip() -> bytes:
    run = _nem_time.now_nem().replace(minute=0, second=0, microsecond=0)
    run_s = run.strftime("%Y/%m/%d %H:%M:%S")
    period_s = (run + timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S")
    tail = ",0,0,0,0,0,0,0,0,0,0,0"
    rows = [
        "C,NEOD,PD7DAY,1,PUBLIC_PD7DAY_X.zip",
        f"D,PD7DAY,CASESOLUTION,1,{run_s},0,{run_s}",
        f"D,PD7DAY,PRICESOLUTION,1,{run_s},1,{period_s},QLD1,95.50{tail}",
    ]
    return make_zip("\n".join(rows).encode())


def pd7day_session(listing=(200,), zip_=(200,)) -> QueueSession:
    return QueueSession({
        PD7DAY_LISTING_URL: _scripted(
            PD7DAY_LISTING_URL, listing, ok={"text": _PD7DAY_LISTING_HTML}
        ),
        PD7DAY_ZIP_URL: _scripted(PD7DAY_ZIP_URL, zip_, ok={"body": _pd7day_zip()}),
    })


# ═════════════════════════════════════════════════════════════════════════════
# nemweb_retry helpers
# ═════════════════════════════════════════════════════════════════════════════

# ── Retry-After parsing ──────────────────────────────────────────────────────

def test_parse_retry_after_seconds():
    assert _retry.parse_retry_after("7") == 7.0
    assert _retry.parse_retry_after(" 2 ") == 2.0


def test_parse_retry_after_http_date():
    now = datetime(2026, 9, 1, 6, 30, 0, tzinfo=timezone.utc)
    raw = "Tue, 01 Sep 2026 06:30:30 GMT"
    assert _retry.parse_retry_after(raw, now=now) == pytest.approx(30.0)


def test_parse_retry_after_junk_and_absent():
    """Unparseable headers fall back to the computed backoff, not an exception."""
    assert _retry.parse_retry_after(None) is None
    assert _retry.parse_retry_after("") is None
    assert _retry.parse_retry_after("soon") is None


def test_parse_retry_after_past_date_is_zero():
    now = datetime(2026, 9, 1, 6, 30, 0, tzinfo=timezone.utc)
    assert _retry.parse_retry_after("Tue, 01 Sep 2026 06:00:00 GMT", now=now) == 0.0


# ── Status classification ────────────────────────────────────────────────────

def test_classify_status_ok():
    assert _retry.classify_status(200, url="u") is None


def test_classify_status_not_published_is_its_own_signal():
    """404 on a dated filename is "not out yet", a distinct type from failure."""
    with pytest.raises(_retry.NemwebNotPublished):
        _retry.classify_status(404, url="u", not_published_statuses=(404,))


def test_classify_status_404_without_optin_is_a_failure():
    with pytest.raises(NemwebFetchError) as excinfo:
        _retry.classify_status(404, url="u")
    assert excinfo.value.retryable is False


@pytest.mark.parametrize("status", [403, 408, 429, 500, 502, 503])
def test_classify_status_transient_is_retryable(status):
    """403 counts as transient: NEMWEB answers 403, not 429, under burst load."""
    with pytest.raises(NemwebFetchError) as excinfo:
        _retry.classify_status(status, url="u")
    assert excinfo.value.retryable is True
    assert excinfo.value.status == status


def test_classify_status_carries_retry_after():
    with pytest.raises(NemwebFetchError) as excinfo:
        _retry.classify_status(429, url="u", headers={"Retry-After": "3"})
    assert excinfo.value.retry_after == 3.0


def test_describe_status_names_the_statuses_that_matter():
    """The helper is the single place a 403 and a 429 are told apart."""
    describe_status = _retry.describe_status
    assert "bot or rate block" in describe_status(403)
    assert "explicit rate limit" in describe_status(429)
    assert "408" in describe_status(408)
    assert "server side" in describe_status(503)
    # Unknown and absent statuses must not fabricate meaning.
    assert describe_status(200) == "HTTP 200"
    assert describe_status(None) == ""


# ── Backoff ──────────────────────────────────────────────────────────────────

def test_backoff_grows_and_stays_bounded():
    """Exponential, jittered into the top half, and capped."""
    lo = _retry.backoff_delay(1, jitter=lambda: 0.0)
    hi = _retry.backoff_delay(1, jitter=lambda: 1.0)
    assert lo == pytest.approx(0.25)
    assert hi == pytest.approx(0.5)
    assert _retry.backoff_delay(2, jitter=lambda: 1.0) == pytest.approx(1.0)
    # The whole three attempt ladder cannot outlive a polling cycle.
    worst = sum(
        _retry.backoff_delay(n, jitter=lambda: 1.0)
        for n in range(1, _retry.DEFAULT_MAX_ATTEMPTS)
    )
    assert worst < 2.0


def test_backoff_prefers_retry_after_and_clamps_it():
    assert _retry.backoff_delay(1, retry_after=3.0) == 3.0
    assert _retry.backoff_delay(1, retry_after=600.0) == _retry.MAX_RETRY_AFTER_S


def test_retry_budget_is_small():
    """Guard on the budget itself: this runs on a polling cycle at HH:02 and
    HH:32, so a ladder that outlives the cycle would pile requests up."""
    assert _retry.DEFAULT_MAX_ATTEMPTS <= 3
    assert _retry.DEFAULT_MAX_DELAY_S <= 5.0


# ── fetch_with_retry ─────────────────────────────────────────────────────────

def test_fetch_with_retry_success_does_not_sleep():
    sleeper = RecordingSleep()

    async def op():
        return "ok"

    result = run_async(_retry.fetch_with_retry(op, url="u", label="L", sleep=sleeper))
    assert result == "ok"
    assert sleeper.delays == []


def test_fetch_with_retry_stops_at_first_non_retryable():
    calls = []
    sleeper = RecordingSleep()

    async def op():
        calls.append(1)
        raise NemwebFetchError("HTTP 404", retryable=False)

    result = run_async(_retry.fetch_with_retry(op, url="u", label="L", sleep=sleeper))
    assert result is None
    assert len(calls) == 1, "a non-retryable failure must not be retried"
    assert sleeper.delays == []


def test_fetch_with_retry_releases_semaphore_between_attempts():
    """The gate is held per attempt, so a backing-off retry does not sit on a
    NEMWEB slot while it sleeps."""
    sem = CountingSemaphore(2)
    sleeper = RecordingSleep()
    attempts = []

    async def op():
        attempts.append(1)
        assert sem.max_held == 1
        raise OSError("connection reset")

    run_async(_retry.fetch_with_retry(op, url="u", label="L", sleep=sleeper, semaphore=sem))
    assert len(attempts) == _retry.DEFAULT_MAX_ATTEMPTS
    assert sem.acquired == _retry.DEFAULT_MAX_ATTEMPTS
    assert sem.max_held == 1


def test_warn_on_exhausted_false_drops_the_give_up_line_to_debug(caplog):
    """The market notice client fans out to up to forty file fetches per cycle
    and summarises the failures itself, so each individual give-up must not
    warn. The line still has to exist at debug, carrying the URL and the
    exception, or the per-file cause becomes unrecoverable. Issue #44."""
    async def always_fails():
        raise NemwebFetchError("HTTP 503", retryable=True, status=503)

    async def scenario(warn: bool):
        return await _retry.fetch_with_retry(
            always_fails,
            url="https://example.invalid/notice.txt",
            label="Notice 1234",
            logger=logging.getLogger("nemweb_retry_test"),
            max_attempts=2,
            sleep=RecordingSleep(),
            warn_on_exhausted=warn,
        )

    with caplog.at_level(logging.DEBUG, logger="nemweb_retry_test"):
        assert run_async(scenario(False)) is None

    assert _warnings(caplog) == [], (
        "suppressed give-up must not warn: "
        f"{[r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]}"
    )
    give_up = _give_ups(caplog)
    assert give_up, "the give-up line must still be emitted at debug"
    assert give_up[0].levelno == logging.DEBUG
    assert "example.invalid/notice.txt" in give_up[0].getMessage()
    assert "NemwebFetchError" in give_up[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="nemweb_retry_test"):
        assert run_async(scenario(True)) is None
    assert _warnings(caplog), "the default must still warn"


# ═════════════════════════════════════════════════════════════════════════════
# TradingIS through the retry (issue #36)
# ═════════════════════════════════════════════════════════════════════════════

def test_directory_failure_warning_names_exception_and_url(caplog):
    """The old line was "TradingIS: failed to fetch directory listing" with the
    exception unbound, so 19 warnings in 20 hours said nothing. Built with the
    default constructor and a non-retryable status so it exercises exactly the
    code path the old version had and fails on the assertions rather than on a
    signature change if the fix is reverted."""
    session = QueueSession({TRADINGIS_BASE_URL: [FakeResponse(status=400)]})
    client = TradingISClient(session)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", _interval()))

    assert result is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"
    message = warnings[0].getMessage()
    assert TRADINGIS_BASE_URL in message, f"URL missing from warning: {message}"
    assert "400" in message, f"status missing from warning: {message}"
    assert "NemwebFetchError" in message, f"exception type missing: {message}"


def test_directory_403_warns_after_exhausting_the_budget(caplog):
    """403 is transient on NEMWEB, so it retries, but a persistent one warns."""
    sleeper = RecordingSleep()
    session = QueueSession({TRADINGIS_BASE_URL: [FakeResponse(status=403)]})
    client = TradingISClient(session, sleep=sleeper)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", _interval()))

    assert result is None
    assert session.count(TRADINGIS_BASE_URL) == _retry.DEFAULT_MAX_ATTEMPTS
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "403" in warnings[0].getMessage()
    assert TRADINGIS_BASE_URL in warnings[0].getMessage()


def test_tradingis_client_has_no_unbound_except_exception():
    """`except Exception:` with nothing bound is what discarded the cause in
    the first place. Every handler in this client must name its exception."""
    with open(os.path.join(PKG_DIR, "tradingis_client.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert "except Exception:" not in source
    assert "failed to fetch directory listing" not in source


def test_directory_retried_then_succeeds(caplog):
    """A single transient 503 no longer costs the interval its actual price."""
    interval_start = _interval()
    sleeper = RecordingSleep()
    session = QueueSession({
        TRADINGIS_BASE_URL: [
            FakeResponse(status=503),
            FakeResponse(text=_tradingis_dir_html(interval_start)),
        ],
        _tradingis_zip_url(interval_start): [_tradingis_good_zip(interval_start)],
    })
    client = TradingISClient(session, sleep=sleeper)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result == pytest.approx(0.09569)
    assert session.count(TRADINGIS_BASE_URL) == 2, "listing was not retried"
    assert len(sleeper.delays) == 1
    assert 0 < sleeper.delays[0] <= _retry.DEFAULT_BASE_DELAY_S
    assert _warnings(caplog) == [], "a failure that the retry absorbed must not warn"


def test_directory_retry_exhausted_gives_up_quietly_once(caplog):
    """Exhausting the budget returns None, warns once, and does not raise."""
    sleeper = RecordingSleep()
    session = QueueSession({TRADINGIS_BASE_URL: [_Boom(OSError("read timeout"))]})
    client = TradingISClient(session, sleep=sleeper)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", _interval()))

    assert result is None, "missing data must surface as None, never as 0"
    assert not isinstance(result, (int, float))
    assert session.count(TRADINGIS_BASE_URL) == _retry.DEFAULT_MAX_ATTEMPTS
    assert len(sleeper.delays) == _retry.DEFAULT_MAX_ATTEMPTS - 1
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "read timeout" in message and TRADINGIS_BASE_URL in message
    assert f"{_retry.DEFAULT_MAX_ATTEMPTS} attempt" in message


def test_tradingis_retry_after_header_is_honoured():
    """A 429 with Retry-After: 2 sleeps exactly 2 s, not the jittered ladder."""
    interval_start = _interval()
    sleeper = RecordingSleep()
    session = QueueSession({
        TRADINGIS_BASE_URL: [
            FakeResponse(status=429, headers={"Retry-After": "2"}),
            FakeResponse(text=_tradingis_dir_html(interval_start)),
        ],
        _tradingis_zip_url(interval_start): [_tradingis_good_zip(interval_start)],
    })
    client = TradingISClient(session, sleep=sleeper)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result == pytest.approx(0.09569)
    assert sleeper.delays == [2.0], f"Retry-After ignored, slept {sleeper.delays} instead of [2.0]"


def test_zip_retried_then_succeeds():
    """The zip download gets the same treatment as the listing."""
    interval_start = _interval()
    sleeper = RecordingSleep()
    url = _tradingis_zip_url(interval_start)
    session = QueueSession({
        TRADINGIS_BASE_URL: [FakeResponse(text=_tradingis_dir_html(interval_start))],
        url: [FakeResponse(status=503), _tradingis_good_zip(interval_start)],
    })
    client = TradingISClient(session, sleep=sleeper)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result == pytest.approx(0.09569)
    assert session.count(url) == 2


def test_zip_not_published_does_not_warn(caplog):
    """A 404 on a dated filename seconds after the interval closed is normal.
    This is half of why the log was noisy: the old code could not tell it
    from a 403 or a timeout."""
    interval_start = _interval()
    url = _tradingis_zip_url(interval_start)
    session = QueueSession({
        TRADINGIS_BASE_URL: [FakeResponse(text=_tradingis_dir_html(interval_start))],
        url: [FakeResponse(status=404)],
    })
    # Default constructor on purpose: nothing here should ever sleep.
    client = TradingISClient(session)

    with caplog.at_level(logging.DEBUG):
        result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result is None
    assert _warnings(caplog) == [], "an unpublished file must not warn"
    assert session.count(url) == 1, "not published must not retry"
    assert any(
        "not published yet" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG
    )


def test_missing_interval_in_listing_does_not_warn(caplog):
    """The interval is simply absent from the listing near a boundary."""
    interval_start = _interval()
    other = _interval(hour=16)
    session = QueueSession({TRADINGIS_BASE_URL: [FakeResponse(text=_tradingis_dir_html(other))]})
    client = TradingISClient(session)

    with caplog.at_level(logging.DEBUG):
        result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result is None
    assert _warnings(caplog) == []


def test_fetches_hold_the_shared_semaphore():
    """Retries must count against NEMWEB_MAX_CONCURRENT_REQUESTS, not bypass it."""
    interval_start = _interval()

    async def scenario():
        sem = CountingSemaphore(_const.NEMWEB_MAX_CONCURRENT_REQUESTS)
        session = QueueSession({
            TRADINGIS_BASE_URL: [
                FakeResponse(status=503),
                FakeResponse(text=_tradingis_dir_html(interval_start)),
            ],
            _tradingis_zip_url(interval_start): [_tradingis_good_zip(interval_start)],
        })
        client = TradingISClient(session, semaphore=sem, sleep=RecordingSleep())
        price = await client.fetch_interval_price("QLD1", interval_start)
        return price, sem

    price, sem = run_async(scenario())
    assert price == pytest.approx(0.09569)
    # Two listing attempts plus one zip fetch.
    assert sem.acquired == 3
    assert sem.max_held <= _const.NEMWEB_MAX_CONCURRENT_REQUESTS


# ═════════════════════════════════════════════════════════════════════════════
# STPASA 403 resilience (issue #22)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    ("listing", "zip_", "expect_data", "listing_requests", "zip_requests", "backoffs"),
    [
        pytest.param([403, 200], [200], True, 2, 1, 1, id="listing-403-then-200"),
        pytest.param([200], [403, 200], True, 1, 2, 1, id="zip-403-then-200"),
        pytest.param([403, 403, 200], [200], True, 3, 1, 2, id="two-403s-inside-the-budget"),
        pytest.param([404], [200], False, 1, 0, 0, id="listing-404-not-retried"),
        pytest.param([200], [404], False, 1, 1, 0, id="zip-404-rotated-out-not-retried"),
    ],
)
def test_stpasa_retries_a_403_but_not_a_404(
    no_sleep, listing, zip_, expect_data, listing_requests, zip_requests, backoffs
):
    """One 403 on the listing or the file used to blank all five regions; the
    default budget of three attempts makes two failures survivable, and every
    retry backs off rather than hammering. A 404 is different: on the CURRENT
    directory it means the report path moved, and on a filename the listing
    named seconds earlier it means NEMWEB rotated the file out mid-cycle.
    Retrying cannot fix either; the next cycle resolves the new newest file."""
    session = stpasa_session(listing=listing, zip_=zip_)
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert ("QLD1" in results) is expect_data
    assert session.count(STPASA_LISTING_URL) == listing_requests
    assert session.count(STPASA_ZIP_URL) == zip_requests
    assert len(no_sleep) == backoffs


def test_stpasa_sustained_403_gives_up_after_the_budget_and_stays_non_fatal(no_sleep, caplog):
    """fetch_all_regions is called from a shared refresh path that keeps the
    previous STPASA values when nothing new arrives, so returning {} is the
    stale-cache fallback. What must not happen is an unhandled exception or a
    request storm, and what must happen is exactly one warning naming the
    cause."""
    session = stpasa_session(listing=[403])
    client = StpasaClient(session)

    with caplog.at_level(logging.DEBUG):
        results = run_async(client.fetch_all_regions())

    assert results == {}, "a sustained 403 must degrade, not raise"
    assert session.count(STPASA_LISTING_URL) == _retry.DEFAULT_MAX_ATTEMPTS, "must stop at the retry budget"
    give_up = [r for r in _warnings(caplog) if "giving up" in r.getMessage()]
    assert len(give_up) == 1, "exactly one give-up warning per failed fetch"
    assert "403" in give_up[0].getMessage()


def test_stpasa_listing_failure_surfaces_as_a_nemweb_fetch_error(no_sleep):
    """Callers below fetch_all_regions get a typed error, not a bare status.
    The PD7DAY coordinator branches on NemwebFetchError to serve stale data,
    so the type is part of the contract."""
    client = StpasaClient(stpasa_session(listing=[403]))

    with pytest.raises(NemwebFetchError):
        run_async(client._list_files())


def test_403_and_429_are_distinguishable_in_the_log(no_sleep, caplog):
    """Same shape of failure, different operational meaning: a 429 clears if
    we back off, an Akamai 403 may mean the source IP is blocked. Before this
    change both logged as an indistinguishable failure line."""
    messages = {}
    for status in (403, 429):
        client = StpasaClient(stpasa_session(listing=[status]))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            run_async(client.fetch_all_regions())
        give_up = _give_ups(caplog)
        assert len(give_up) == 1
        messages[status] = give_up[0].getMessage()

    assert messages[403] != messages[429]
    assert "bot or rate block" in messages[403]
    assert "explicit rate limit" in messages[429]


def test_stpasa_retry_after_on_a_429_is_honoured(no_sleep):
    """NEMWEB rarely sends Retry-After, but when it does it must win: ignoring
    it is what turns a soft rate limit into a hard block."""
    session = stpasa_session(listing=[429, 200], headers={"Retry-After": "3"})
    client = StpasaClient(session)

    results = run_async(client.fetch_all_regions())

    assert "QLD1" in results
    assert no_sleep == pytest.approx([3.0]), f"expected a 3 s Retry-After wait, got {no_sleep}"


# ═════════════════════════════════════════════════════════════════════════════
# PD7DAY 403 resilience (issue #22)
# ═════════════════════════════════════════════════════════════════════════════
# PD7DAY was the one report type that already had a 403 retry, but it lived
# in the coordinator and re-ran the whole fetch, listing plus every file,
# after a flat five second sleep. It is now handled per request in the client.

@pytest.mark.parametrize(
    ("listing", "zip_", "listing_requests", "zip_requests"),
    [
        pytest.param([403, 200], [200], 2, 1, id="listing-403-then-200"),
        pytest.param([200], [403, 200], 1, 2, id="zip-403-then-200"),
    ],
)
def test_pd7day_retries_only_the_failing_request(
    no_sleep, listing, zip_, listing_requests, zip_requests
):
    """A transient 403 must not drop the cycle, and only the failing request
    is retried: a file-level 403 must not force a second directory listing,
    which made the burst worse at the moment NEMWEB was already objecting."""
    session = pd7day_session(listing=listing, zip_=zip_)
    client = PD7DayClient(session)

    result = run_async(client.fetch_all(["QLD1"], []))

    assert "QLD1" in result.prices
    assert session.count(PD7DAY_LISTING_URL) == listing_requests
    assert session.count(PD7DAY_ZIP_URL) == zip_requests


def test_pd7day_sustained_403_raises_a_typed_error(no_sleep):
    """The coordinator needs a type to branch on to serve stale data."""
    session = pd7day_session(listing=[403])
    client = PD7DayClient(session)

    with pytest.raises(NemwebFetchError):
        run_async(client.fetch_all(["QLD1"], []))

    assert session.count(PD7DAY_LISTING_URL) == _retry.DEFAULT_MAX_ATTEMPTS


# ═════════════════════════════════════════════════════════════════════════════
# NemwebGate (issue #22)
# ═════════════════════════════════════════════════════════════════════════════

class FakeClock:
    """Monotonic clock advanced only by the fake sleep, never by real time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        # Yield so other tasks can run, matching asyncio.sleep's behaviour.
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gate(max_concurrent: int = 2, min_gap_s: float = 0.25):
    clock = FakeClock()
    gate = NemwebGate(max_concurrent, min_gap_s, clock=clock, sleep=clock.sleep)
    return gate, clock


async def _acquire(gate, times: int = 1) -> None:
    for _ in range(times):
        async with gate:
            pass


def test_first_request_is_not_delayed():
    """A cold gate must not add latency to the first request of a cycle."""
    gate, clock = make_gate()

    asyncio.run(_acquire(gate))

    assert clock.sleeps == [], "first acquisition should not sleep"
    assert gate.paced_waits == 0
    assert gate.acquisitions == 1


def test_successive_requests_are_spaced_by_the_minimum_gap():
    """Three immediate acquisitions produce two waits of the full gap. This is
    the burst case, a directory listing followed by file fetches, which is
    what used to draw the 403. The counters reach diagnostics() so a future
    403 investigation can tell our own throttling from NEMWEB's."""
    gate, clock = make_gate(min_gap_s=0.25)

    asyncio.run(_acquire(gate, 3))

    assert clock.sleeps == pytest.approx([0.25, 0.25])
    assert gate.diagnostics() == {
        "min_gap_s": pytest.approx(0.25),
        "acquisitions": 3,
        "paced_waits": 2,
        "total_paced_wait_s": pytest.approx(0.5),
    }


def test_idle_time_neither_delays_the_next_request_nor_banks_credit_for_a_burst():
    """Steady state is a fetch every few hours, far wider than the gap, and
    must be a no-op. But a long idle period must not buy a free burst
    afterwards: without the max() in _pace, _next_allowed would trail far
    behind the clock and the next several requests would all pass unpaced,
    defeating the gate at exactly the moment a burst starts."""
    gate, clock = make_gate(min_gap_s=0.25)

    async def scenario():
        async with gate:
            pass
        clock.advance(3600.0)
        async with gate:
            pass
        assert clock.sleeps == [], "an idle gate must not delay the next request"
        assert gate.paced_waits == 0
        async with gate:
            pass

    asyncio.run(scenario())

    # The first post-idle request was free, the second is paced.
    assert clock.sleeps == pytest.approx([0.25])
    assert gate.paced_waits == 1


def test_partial_gap_waits_only_the_remainder():
    """The gate waits the remaining gap, not the whole gap again."""
    gate, clock = make_gate(min_gap_s=1.0)

    async def scenario():
        async with gate:
            pass
        clock.advance(0.4)
        async with gate:
            pass

    asyncio.run(scenario())

    assert clock.sleeps == pytest.approx([0.6])


@pytest.mark.parametrize("gap", [0.0, -5.0], ids=["zero", "negative"])
def test_non_positive_gap_is_a_pure_semaphore(gap):
    """min_gap_s=0 must short-circuit so the gate can be disabled outright,
    and a misconfigured negative gap must clamp rather than sleep negatively."""
    gate, clock = make_gate(min_gap_s=gap)

    asyncio.run(_acquire(gate, 5))

    assert gate.min_gap_s == 0.0
    assert clock.sleeps == []
    assert gate.paced_waits == 0
    assert gate.acquisitions == 5


def test_concurrency_is_still_capped():
    """The gate must not regress the concurrency cap it replaces."""
    gate, clock = make_gate(max_concurrent=2, min_gap_s=0.0)
    in_flight = 0
    peak = 0

    async def one_request():
        nonlocal in_flight, peak
        async with gate:
            in_flight += 1
            peak = max(peak, in_flight)
            # Force a suspension so overlap is observable.
            await asyncio.sleep(0)
            in_flight -= 1

    async def scenario():
        await asyncio.gather(*(one_request() for _ in range(10)))

    asyncio.run(scenario())

    assert peak <= 2, f"concurrency cap breached, peak was {peak}"
    assert gate.acquisitions == 10


def test_max_concurrent_below_one_is_rejected():
    """Zero slots would deadlock every fetch, so it must fail loudly."""
    with pytest.raises(ValueError):
        NemwebGate(0, 0.25)


def test_cancellation_during_pacing_does_not_leak_a_slot():
    """The gate acquires its semaphore slot before pacing. If a task is
    cancelled while waiting out the gap and the slot is not released, every
    cancelled refresh would shrink the effective concurrency until nothing
    could fetch at all. That failure mode is silent and cumulative."""
    clock = FakeClock()
    started = asyncio.Event()

    async def blocking_sleep(seconds: float) -> None:
        started.set()
        # Never returns, so the cancellation lands inside the pacing wait.
        await asyncio.Event().wait()

    gate = NemwebGate(1, 0.25, clock=clock, sleep=blocking_sleep)

    async def scenario():
        # Prime the gate so the next acquisition has to wait out the gap.
        async with gate:
            pass

        async def paced():
            async with gate:
                pass

        task = asyncio.create_task(paced())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The slot must be back. With a leak this times out instead.
        await asyncio.wait_for(gate._semaphore.acquire(), timeout=1.0)
        gate._semaphore.release()

    asyncio.run(scenario())


def test_gate_is_reusable_and_reentrant_across_tasks():
    """Paced requests across concurrent tasks still respect the gap. The
    pacing lock is held across the wait on purpose: released before sleeping,
    every waiting task would compute the same target time and wake together,
    which is the thundering herd the gap exists to prevent."""
    clock = FakeClock()
    gate = NemwebGate(2, 0.25, clock=clock, sleep=clock.sleep)
    start_times: list[float] = []

    async def one_request():
        async with gate:
            start_times.append(clock.now)

    async def scenario():
        await asyncio.gather(*(one_request() for _ in range(6)))

    asyncio.run(scenario())

    assert len(start_times) == 6
    ordered = sorted(start_times)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    assert all(g >= 0.25 - 1e-9 for g in gaps), f"gaps too tight: {gaps}"


# ── diagnostics exposure ─────────────────────────────────────────────────────

def _hass_with(domain_data):
    return types.SimpleNamespace(data=domain_data)


def test_diagnostics_payload_includes_the_gate_counters():
    """The counters are only useful if they reach a downloadable diagnostic."""
    gate, _ = make_gate(min_gap_s=0.25)
    asyncio.run(_acquire(gate, 2))

    hass = _hass_with({_const.DOMAIN: {_const.NEMWEB_SEMAPHORE_KEY: gate}})
    payload = _diag_mod._nemweb_gate(hass)

    assert payload is not None
    assert payload["acquisitions"] == 2
    assert payload["paced_waits"] == 1
    assert payload["min_gap_s"] == pytest.approx(0.25)


def test_diagnostics_tolerates_a_missing_or_plain_semaphore():
    """Diagnostics must never raise, including mid-upgrade: an install that
    has not yet reloaded still has a bare asyncio.Semaphore under this key,
    and a config entry can be diagnosed before the shared objects exist."""
    domain, key = _const.DOMAIN, _const.NEMWEB_SEMAPHORE_KEY
    assert _diag_mod._nemweb_gate(_hass_with({})) is None
    assert _diag_mod._nemweb_gate(_hass_with({domain: {}})) is None
    assert _diag_mod._nemweb_gate(_hass_with({domain: {key: asyncio.Semaphore(2)}})) is None


# ═════════════════════════════════════════════════════════════════════════════
# NEMWEB_HEADERS invariant over the package source (issue #102)
# ═════════════════════════════════════════════════════════════════════════════

# A literal User-Agent key in a headers dict or a UA-looking value.
_HARDCODED_UA = re.compile(r"""["']User-Agent["']\s*:|nem_pd7day/\d""")


def test_no_hardcoded_user_agent_outside_const():
    """Every module but const.py must take its headers from NEMWEB_HEADERS."""
    offenders = []
    for module_name in sorted(os.listdir(PKG_DIR)):
        if not module_name.endswith(".py") or module_name == "const.py":
            continue
        with open(os.path.join(PKG_DIR, module_name), encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                if line.strip().startswith("#"):
                    continue
                if _HARDCODED_UA.search(line):
                    offenders.append(f"{module_name}:{lineno}")
    assert offenders == [], (
        f"hardcoded User-Agent in {offenders}; use NEMWEB_HEADERS from const.py"
    )


def test_urllib_requests_in_dispatch_client_carry_headers():
    """The DispatchIS fallback uses urllib, so it cannot inherit headers from
    a session: every urlopen there must take a Request built with
    NEMWEB_HEADERS rather than a bare URL string."""
    with open(os.path.join(PKG_DIR, "dispatch_client.py"), encoding="utf-8") as handle:
        src = handle.read()
    bare = re.findall(r"urlopen\(\s*(?:DISPATCHIS_BASE|NEM_SUMMARY_URL|url)\b", src)
    assert not bare, f"bare urlopen(url) calls in dispatch_client.py: {bare}"
    assert src.count("headers=NEMWEB_HEADERS") == 2
    assert "{**NEMWEB_HEADERS" in src
