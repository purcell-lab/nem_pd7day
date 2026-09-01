"""
Tests for the shared NEMWEB bounded retry and for TradingIS using it.

Background
----------
In a 20 hour sample (2026-08-31 10:31 to 2026-09-01 06:32 NEM time) the
TradingIS directory listing failed 19 times and logged the same useless line
every time:

    TradingIS: failed to fetch directory listing

The except clause bound nothing, so the exception type, the status code and the
URL were all discarded, and there was no retry anywhere in the integration, so
each failure permanently dropped one 30 minute actual settlement price.

These tests pin the properties that fix has to hold:

  * the warning names the exception and the URL
  * a transient failure is retried and can then succeed
  * an exhausted retry returns None and never raises into the coordinator
  * a Retry-After header is honoured in place of the computed backoff
  * "not published yet" does not warn, and is not retried

No test touches the network and no test sleeps: the sleep coroutine is injected
and only records the delays it was asked for.

Run with:  python -m pytest tests/test_nemweb_retry.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone
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


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Stub HA imports ──────────────────────────────────────────────────────────

for _mod in [
    "homeassistant", "homeassistant.core", "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.helpers", "homeassistant.helpers.storage",
    "homeassistant.helpers.event", "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.update_coordinator", "homeassistant.util",
    "homeassistant.util.dt", "aiohttp",
]:
    sys.modules.setdefault(_mod, MagicMock())

_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_retry = _load(
    "custom_components.nem_pd7day.nemweb_retry",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nemweb_retry.py"),
)
_tradingis_mod = _load(
    "custom_components.nem_pd7day.tradingis_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "tradingis_client.py"),
)

TradingISClient = _tradingis_mod.TradingISClient
BASE_URL = _const_mod.TRADINGIS_BASE_URL


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeResponse:
    """Minimal aiohttp-shaped response. No raise_for_status: the client reads
    resp.status itself so it can tell a 404 apart from a 403."""

    def __init__(self, status=200, text="", body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._body = body

    async def text(self):
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
    """Serves a queued script of responses per URL.

    The last entry for a URL repeats forever, so a test can say "fails, then
    succeeds" or "always fails" without counting attempts.
    """

    def __init__(self, script):
        self._script = {url: list(items) for url, items in script.items()}
        self.request_log = []

    def get(self, url, **kwargs):
        self.request_log.append(url)
        items = self._script.get(url)
        if not items:
            return FakeResponse(status=404)
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, _Boom):
            raise item.exc
        return item


class RecordingSleep:
    """Injected in place of asyncio.sleep so the suite never actually waits."""

    def __init__(self):
        self.delays = []

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


def _trading_csv(settlement_str, region, rrp_mwh):
    return (
        "C,NEMP.WORLD,TRADINGIS,v3\n"
        "I,TRADING,PRICE,3,SETTLEMENTDATE,RUNNO,REGIONID,PERIODID,RRP,EEP,"
        "INVALIDFLAG,LASTCHANGED,PRICE_STATUS\n"
        f'D,TRADING,PRICE,3,"{settlement_str}",1,{region},223,{rrp_mwh},0,0,'
        f'"2026/09/01 06:31:00",FIRM\n'
        'C,"END OF REPORT",3\n'
    )


def _zip_bytes(csv_content):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("PUBLIC_TRADINGIS.csv", csv_content)
    return buf.getvalue()


def _interval(hour=17, minute=0):
    return datetime(2026, 9, 1, hour, minute, tzinfo=NEM_TZ)


def _zip_url(interval_start):
    end = interval_start + timedelta(minutes=30)
    return BASE_URL + f"PUBLIC_TRADINGIS_{end.strftime('%Y%m%d%H%M')}_1.zip"


def _dir_html(interval_start):
    end = interval_start + timedelta(minutes=30)
    fn = f"PUBLIC_TRADINGIS_{end.strftime('%Y%m%d%H%M')}_1.zip"
    return f'<html><body><a href="{fn}">{fn}</a></body></html>'


def _good_zip(interval_start, region="QLD1", rrp_mwh=95.69):
    end = interval_start + timedelta(minutes=30)
    return FakeResponse(
        body=_zip_bytes(
            _trading_csv(end.strftime("%Y/%m/%d %H:%M:00"), region, rrp_mwh)
        )
    )


# ── Helper: Retry-After parsing ──────────────────────────────────────────────

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


# ── Helper: status classification ────────────────────────────────────────────

def test_classify_status_ok():
    assert _retry.classify_status(200, url="u") is None


def test_classify_status_not_published_is_its_own_signal():
    """404 on a dated filename is "not out yet", a distinct type from failure."""
    with pytest.raises(_retry.NemwebNotPublished):
        _retry.classify_status(404, url="u", not_published_statuses=(404,))


def test_classify_status_404_without_optin_is_a_failure():
    with pytest.raises(_retry.NemwebFetchError) as excinfo:
        _retry.classify_status(404, url="u")
    assert excinfo.value.retryable is False


@pytest.mark.parametrize("status", [403, 408, 429, 500, 502, 503])
def test_classify_status_transient_is_retryable(status):
    """403 counts as transient: NEMWEB answers 403, not 429, under burst load."""
    with pytest.raises(_retry.NemwebFetchError) as excinfo:
        _retry.classify_status(status, url="u")
    assert excinfo.value.retryable is True
    assert excinfo.value.status == status


def test_classify_status_carries_retry_after():
    with pytest.raises(_retry.NemwebFetchError) as excinfo:
        _retry.classify_status(429, url="u", headers={"Retry-After": "3"})
    assert excinfo.value.retry_after == 3.0


# ── Helper: backoff ──────────────────────────────────────────────────────────

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


# ── Helper: fetch_with_retry ─────────────────────────────────────────────────

def test_fetch_with_retry_success_does_not_sleep():
    sleeper = RecordingSleep()

    async def op():
        return "ok"

    result = run_async(
        _retry.fetch_with_retry(op, url="u", label="L", sleep=sleeper)
    )
    assert result == "ok"
    assert sleeper.delays == []


def test_fetch_with_retry_stops_at_first_non_retryable(caplog):
    calls = []
    sleeper = RecordingSleep()

    async def op():
        calls.append(1)
        raise _retry.NemwebFetchError("HTTP 404", retryable=False)

    with caplog.at_level(logging.DEBUG):
        result = run_async(
            _retry.fetch_with_retry(op, url="u", label="L", sleep=sleeper)
        )
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

    run_async(
        _retry.fetch_with_retry(
            op, url="u", label="L", sleep=sleeper, semaphore=sem
        )
    )
    assert len(attempts) == _retry.DEFAULT_MAX_ATTEMPTS
    assert sem.acquired == _retry.DEFAULT_MAX_ATTEMPTS
    assert sem.max_held == 1


# ── TradingIS: the warning has to be diagnosable ─────────────────────────────

def test_directory_failure_warning_names_exception_and_url(caplog):
    """Regression for issue #36.

    The old line was "TradingIS: failed to fetch directory listing" with the
    exception unbound, so 19 warnings in 20 hours said nothing. The warning now
    has to carry the URL and the exception, otherwise this fails.

    Deliberately built with the default constructor and a non-retryable status,
    so it exercises exactly the code path the old version had and fails on the
    assertions rather than on a signature change if the fix is reverted.
    """
    session = QueueSession({BASE_URL: [FakeResponse(status=400)]})
    client = TradingISClient(session)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", _interval()))

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected one warning, got {len(warnings)}"
    message = warnings[0].getMessage()
    assert BASE_URL in message, f"URL missing from warning: {message}"
    assert "400" in message, f"status missing from warning: {message}"
    assert "NemwebFetchError" in message, f"exception type missing: {message}"


def test_directory_403_warns_after_exhausting_the_budget(caplog):
    """403 is transient on NEMWEB, so it retries, but a persistent one warns."""
    sleeper = RecordingSleep()
    session = QueueSession({BASE_URL: [FakeResponse(status=403)]})
    client = TradingISClient(session, sleep=sleeper)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", _interval()))

    assert result is None
    assert session.request_log.count(BASE_URL) == _retry.DEFAULT_MAX_ATTEMPTS
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "403" in warnings[0].getMessage()
    assert BASE_URL in warnings[0].getMessage()


def test_tradingis_client_has_no_unbound_except_exception():
    """Guard against reintroducing the defect this PR fixes.

    `except Exception:` with nothing bound is what discarded the cause in the
    first place. Every handler in this client must name its exception.
    """
    path = os.path.join(
        _ROOT, "custom_components", "nem_pd7day", "tradingis_client.py"
    )
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    assert "except Exception:" not in source
    assert "failed to fetch directory listing" not in source


# ── TradingIS: retry behaviour ───────────────────────────────────────────────

def test_directory_retried_then_succeeds(caplog):
    """A single transient 503 no longer costs the interval its actual price."""
    interval_start = _interval()
    sleeper = RecordingSleep()
    session = QueueSession({
        BASE_URL: [
            FakeResponse(status=503),
            FakeResponse(text=_dir_html(interval_start)),
        ],
        _zip_url(interval_start): [_good_zip(interval_start)],
    })
    client = TradingISClient(session, sleep=sleeper)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result == pytest.approx(0.09569)
    assert session.request_log.count(BASE_URL) == 2, "listing was not retried"
    assert len(sleeper.delays) == 1
    assert 0 < sleeper.delays[0] <= _retry.DEFAULT_BASE_DELAY_S
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == [], (
        "a failure that the retry absorbed must not warn"
    )


def test_directory_retry_exhausted_gives_up_quietly_once(caplog):
    """Exhausting the budget returns None, warns once, and does not raise."""
    sleeper = RecordingSleep()
    session = QueueSession({BASE_URL: [_Boom(OSError("read timeout"))]})
    client = TradingISClient(session, sleep=sleeper)

    with caplog.at_level(logging.WARNING):
        result = run_async(client.fetch_interval_price("QLD1", _interval()))

    assert result is None, "missing data must surface as None, never as 0"
    assert not isinstance(result, (int, float))
    attempts = session.request_log.count(BASE_URL)
    assert attempts == _retry.DEFAULT_MAX_ATTEMPTS
    assert len(sleeper.delays) == _retry.DEFAULT_MAX_ATTEMPTS - 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "read timeout" in message and BASE_URL in message
    assert f"{_retry.DEFAULT_MAX_ATTEMPTS} attempt" in message


def test_retry_budget_is_small():
    """Guard on the budget itself: this runs on a polling cycle at HH:02 and
    HH:32, so a ladder that outlives the cycle would pile requests up."""
    assert _retry.DEFAULT_MAX_ATTEMPTS <= 3
    assert _retry.DEFAULT_MAX_DELAY_S <= 5.0


def test_retry_after_header_is_honoured():
    """A 429 with Retry-After: 2 sleeps exactly 2 s, not the jittered ladder."""
    interval_start = _interval()
    sleeper = RecordingSleep()
    session = QueueSession({
        BASE_URL: [
            FakeResponse(status=429, headers={"Retry-After": "2"}),
            FakeResponse(text=_dir_html(interval_start)),
        ],
        _zip_url(interval_start): [_good_zip(interval_start)],
    })
    client = TradingISClient(session, sleep=sleeper)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result == pytest.approx(0.09569)
    assert sleeper.delays == [2.0], (
        f"Retry-After ignored, slept {sleeper.delays} instead of [2.0]"
    )


def test_zip_retried_then_succeeds():
    """The zip download gets the same treatment as the listing."""
    interval_start = _interval()
    sleeper = RecordingSleep()
    url = _zip_url(interval_start)
    session = QueueSession({
        BASE_URL: [FakeResponse(text=_dir_html(interval_start))],
        url: [FakeResponse(status=503), _good_zip(interval_start)],
    })
    client = TradingISClient(session, sleep=sleeper)

    result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result == pytest.approx(0.09569)
    assert session.request_log.count(url) == 2


# ── TradingIS: not published yet is not a failure ────────────────────────────

def test_zip_not_published_does_not_warn(caplog):
    """A 404 on a dated filename seconds after the interval closed is normal.

    This is half of why the log was noisy: the old code could not tell it from
    a 403 or a timeout.
    """
    interval_start = _interval()
    url = _zip_url(interval_start)
    session = QueueSession({
        BASE_URL: [FakeResponse(text=_dir_html(interval_start))],
        url: [FakeResponse(status=404)],
    })
    # Default constructor on purpose: nothing here should ever sleep, so the
    # test does not need an injected clock to stay fast.
    client = TradingISClient(session)

    with caplog.at_level(logging.DEBUG):
        result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == [], (
        "an unpublished file must not warn"
    )
    assert session.request_log.count(url) == 1, "not published must not retry"
    assert any(
        "not published yet" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.DEBUG
    )


def test_missing_interval_in_listing_does_not_warn(caplog):
    """The interval is simply absent from the listing near a boundary."""
    interval_start = _interval()
    other = _interval(hour=16)
    session = QueueSession({BASE_URL: [FakeResponse(text=_dir_html(other))]})
    client = TradingISClient(session)

    with caplog.at_level(logging.DEBUG):
        result = run_async(client.fetch_interval_price("QLD1", interval_start))

    assert result is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ── TradingIS: concurrency cap ───────────────────────────────────────────────

def test_fetches_hold_the_shared_semaphore():
    """Retries must count against NEMWEB_MAX_CONCURRENT_REQUESTS, not bypass it."""
    interval_start = _interval()

    async def scenario():
        sem = CountingSemaphore(_const_mod.NEMWEB_MAX_CONCURRENT_REQUESTS)
        session = QueueSession({
            BASE_URL: [
                FakeResponse(status=503),
                FakeResponse(text=_dir_html(interval_start)),
            ],
            _zip_url(interval_start): [_good_zip(interval_start)],
        })
        client = TradingISClient(
            session, semaphore=sem, sleep=RecordingSleep()
        )
        price = await client.fetch_interval_price("QLD1", interval_start)
        return price, sem

    price, sem = run_async(scenario())
    assert price == pytest.approx(0.09569)
    # Two listing attempts plus one zip fetch.
    assert sem.acquired == 3
    assert sem.max_held <= _const_mod.NEMWEB_MAX_CONCURRENT_REQUESTS


# ── warn_on_exhausted lets a fan-out caller aggregate (issue #44) ───────────

def test_warn_on_exhausted_false_drops_the_give_up_line_to_debug(caplog):
    """
    The market notice client fans out to up to forty file fetches per cycle and
    summarises the failures itself, so each individual give-up must not warn.
    The line still has to exist at debug, carrying the URL and the exception,
    or the per-file cause becomes unrecoverable. Issue #44.
    """
    async def always_fails():
        raise _retry.NemwebFetchError("HTTP 503", retryable=True, status=503)

    async def scenario(warn: bool):
        return await _retry.fetch_with_retry(
            always_fails,
            url="https://example.invalid/notice.txt",
            label="Notice 1234",
            logger=logging.getLogger("nemweb_retry_test"),
            max_attempts=2,
            sleep=_noop_sleep,
            warn_on_exhausted=warn,
        )

    with caplog.at_level(logging.DEBUG, logger="nemweb_retry_test"):
        assert run_async(scenario(False)) is None

    levels = {r.levelno for r in caplog.records}
    assert logging.WARNING not in levels, (
        "suppressed give-up must not warn: "
        f"{[r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]}"
    )
    give_up = [r for r in caplog.records if "giving up" in r.getMessage()]
    assert give_up, "the give-up line must still be emitted at debug"
    assert give_up[0].levelno == logging.DEBUG
    assert "example.invalid/notice.txt" in give_up[0].getMessage()
    assert "NemwebFetchError" in give_up[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="nemweb_retry_test"):
        assert run_async(scenario(True)) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records), (
        "the default must still warn"
    )


async def _noop_sleep(_delay: float) -> None:
    """Skip the backoff so the test does not spend real time sleeping."""
    return None
