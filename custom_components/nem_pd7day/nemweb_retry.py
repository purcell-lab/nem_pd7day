"""
Shared bounded retry for NEMWEB fetches.

Why this exists
---------------
NEMWEB is intermittently unreachable rather than persistently down. In a
20 hour sample (2026-08-31 10:31 to 2026-09-01 06:32 NEM time) the TradingIS
directory listing failed 19 times, while the adjacent dispatch path logged 62
read timeouts in the same window. Failures scattered like that are exactly the
case a short retry absorbs. No fetcher in this integration retried, so each of
those 19 failures permanently dropped one 30 minute actual settlement price
from the calibration store.

The retry budget is deliberately tiny. Every caller sits on a polling cycle:
TradingIS fires at HH:02 and HH:32, the coordinators refresh on their own
timers. A generous retry ladder would still be sleeping when the next tick
arrives, and overlapping ticks pile requests onto a NEMWEB that is already
refusing them. With the defaults below, three attempts add at most about
1.5 s of sleep, or the server's own Retry-After when it sends one, capped.

Failures never propagate. Every helper here converts a failed fetch into
``None`` so a caller records missing data as unavailable rather than as a
zero price, and a coordinator refresh is never taken down by NEMWEB.

Retries run inside the shared NEMWEB semaphore
(``NEMWEB_MAX_CONCURRENT_REQUESTS = 2``), acquired per attempt rather than
held across the whole ladder, so a backing-off retry does not occupy a slot
that another fetcher could use while it sleeps.

This module deliberately imports nothing from ``homeassistant`` and nothing
from ``aiohttp``, so it can be unit tested directly, in the same spirit as
``pd7day_shared.py``. Callers pass their own transport exception types in
through ``retryable_exceptions``.

Issue #22 tracks adopting this helper in ``stpasa_client.py`` and
``pd7day_client.py``; only ``tradingis_client.py`` uses it today.
"""
from __future__ import annotations

import asyncio
import email.utils
import logging
import random
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping

# Three attempts means two retries. Two retries covered every scattered
# failure in the sampled window without a fetch outliving its polling cycle.
DEFAULT_MAX_ATTEMPTS = 3

# 0.5 s then 1.0 s nominal, halved to full-jitter lower bounds, so the worst
# case sleep total across a whole exhausted ladder is about 1.5 s.
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 4.0

# A server asking us to wait longer than this is telling us to give up for
# this cycle, not to hold a polling tick open. Honour it up to the cap only.
MAX_RETRY_AFTER_S = 10.0

# 403 is in this set on purpose. NEMWEB answers 403, not 429, when it decides
# a caller is requesting too fast, which is why the integration caps
# concurrency at 2 in the first place. Those 403s clear on their own.
RETRYABLE_STATUSES = frozenset({403, 408, 425, 429})

# Transport level failures that need no third party import to recognise.
# builtins.TimeoutError and asyncio.TimeoutError are the same class on the
# Python versions Home Assistant supports, and ConnectionError is an OSError.
RETRYABLE_BUILTIN_ERRORS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    OSError,
)

_LOGGER = logging.getLogger(__name__)


class NemwebNotPublished(Exception):
    """The requested file is not on NEMWEB yet.

    Normal near an interval boundary: a caller often asks for a file within
    seconds of the interval it covers closing. This is not a failure, must not
    warn, and must not be retried, because retrying inside one polling cycle
    cannot make AEMO publish faster.
    """


class NemwebFetchError(Exception):
    """A genuine fetch failure: a bad status, or a transport error.

    ``retryable`` says whether another attempt could plausibly succeed.
    ``retry_after`` carries the server's own Retry-After in seconds when it
    sent one, which overrides the computed backoff.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.status = status


# NEMWEB answers 403 where a well behaved origin would answer 429, so the two
# look identical in a log line unless they are spelled out. Knowing which one
# arrived decides the response: a 429 means back off on our own schedule, a 403
# from Akamai may mean the IP is blocked and no amount of backing off helps.
_STATUS_MEANINGS = {
    403: "403 Forbidden, Akamai bot or rate block rather than an explicit "
         "rate limit",
    408: "408 Request Timeout",
    425: "425 Too Early",
    429: "429 Too Many Requests, an explicit rate limit",
}


def describe_status(status: int | None) -> str:
    """Human readable status for a log line, or an empty string if unknown."""
    if status is None:
        return ""
    described = _STATUS_MEANINGS.get(status)
    if described is not None:
        return described
    if status >= 500:
        return f"HTTP {status}, server side"
    return f"HTTP {status}"


def parse_retry_after(
    raw: Any,
    *,
    now: datetime | None = None,
) -> float | None:
    """Return Retry-After as seconds, or None if absent or unparseable.

    HTTP allows either a delta in seconds or an HTTP-date. NEMWEB has been
    seen sending neither, so anything unrecognised falls back to the computed
    backoff rather than raising.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (when - reference).total_seconds())


def classify_status(
    status: int,
    *,
    url: str,
    headers: Mapping[str, Any] | None = None,
    not_published_statuses: Iterable[int] = (),
) -> None:
    """Raise for a bad HTTP status, or return None if the status is fine.

    ``not_published_statuses`` lets a caller declare which statuses mean "not
    on NEMWEB yet" for that particular URL. A 404 on a dated zip filename
    means the file is not out yet; a 404 on a directory listing that always
    exists means something is genuinely wrong, so the caller decides.
    """
    if status in set(not_published_statuses):
        raise NemwebNotPublished(f"HTTP {status} for {url}")
    if status < 400:
        return
    retry_after = None
    if headers is not None:
        try:
            retry_after = parse_retry_after(headers.get("Retry-After"))
        except AttributeError:
            retry_after = None
    raise NemwebFetchError(
        f"HTTP {status}",
        retryable=status in RETRYABLE_STATUSES or status >= 500,
        retry_after=retry_after,
        status=status,
    )


def _status_suffix(exc: BaseException | None) -> str:
    """``" [<meaning>]"`` when the exception carries an HTTP status."""
    status = getattr(exc, "status", None)
    if not isinstance(status, int):
        return ""
    described = describe_status(status)
    return f" [{described}]" if described else ""


def is_retryable(
    exc: BaseException,
    retryable_exceptions: tuple[type[BaseException], ...] = (),
) -> bool:
    """Whether another attempt at the same fetch could plausibly succeed."""
    if isinstance(exc, NemwebFetchError):
        return exc.retryable
    if isinstance(exc, RETRYABLE_BUILTIN_ERRORS):
        return True
    if retryable_exceptions and isinstance(exc, retryable_exceptions):
        return True
    return False


def backoff_delay(
    attempt: int,
    *,
    retry_after: float | None = None,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before attempt number ``attempt + 1``.

    A server supplied Retry-After wins over the computed ladder, clamped to
    MAX_RETRY_AFTER_S so a hostile or mistaken header cannot stall a tick.
    Otherwise the delay is exponential with jitter in the top half of the
    window, which is enough to stop five region coordinators retrying in
    lockstep without making the total wait unpredictable.
    """
    if retry_after is not None:
        return max(0.0, min(float(retry_after), MAX_RETRY_AFTER_S))
    nominal = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    return nominal * (0.5 + 0.5 * jitter())


async def fetch_with_retry(
    operation: Callable[[], Awaitable[Any]],
    *,
    url: str,
    label: str,
    logger: logging.Logger | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    semaphore: Any | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
    retryable_exceptions: tuple[type[BaseException], ...] = (),
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
) -> Any | None:
    """Run ``operation`` with a bounded, jittered retry. Never raises.

    ``operation`` is an awaitable-returning callable that performs one attempt
    and raises NemwebNotPublished, NemwebFetchError, or a transport error.

    Returns whatever ``operation`` returned, or None once the file is not
    published, the failure is not retryable, or the attempts are exhausted.
    None means unavailable, and callers must surface it as such rather than
    substituting a zero.
    """
    log = logger or _LOGGER
    last_exc: BaseException | None = None
    max_attempts = max(1, max_attempts)
    attempt = 1

    for attempt in range(1, max_attempts + 1):
        try:
            # The semaphore is acquired per attempt, not around the ladder, so
            # the backoff sleep below does not hold a NEMWEB slot idle.
            if semaphore is None:
                return await operation()
            async with semaphore:
                return await operation()
        except NemwebNotPublished as exc:
            # Expected near an interval boundary. Debug only: warning on this
            # is what made the TradingIS log unreadable, 19 warnings in 20 h
            # that said nothing a reader could act on.
            log.debug("%s: not published yet, url=%s (%s)", label, url, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - converted to None below
            last_exc = exc
            retryable = is_retryable(exc, retryable_exceptions)
            if not retryable or attempt >= max_attempts:
                break
            retry_after = getattr(exc, "retry_after", None)
            delay = backoff_delay(
                attempt,
                retry_after=retry_after,
                base_delay=base_delay,
                max_delay=max_delay,
                jitter=jitter,
            )
            log.debug(
                "%s: attempt %d/%d failed (%s: %s%s), url=%s, retrying in "
                "%.2f s%s",
                label,
                attempt,
                max_attempts,
                type(exc).__name__,
                exc,
                _status_suffix(exc),
                url,
                delay,
                " honouring Retry-After" if retry_after is not None else "",
            )
            await sleep(delay)

    attempts_used = min(attempt, max_attempts)
    # Both the exception and the URL go in the message. The old log line
    # carried neither, so a 403, a DNS failure, a read timeout and a parse
    # error were indistinguishable from each other.
    log.warning(
        "%s: giving up after %d attempt(s), url=%s: %s: %s%s",
        label,
        attempts_used,
        url,
        type(last_exc).__name__,
        last_exc,
        _status_suffix(last_exc),
    )
    # The traceback is one level down so a normal log stays one line per
    # failure while a debug run still shows where it came from.
    log.debug("%s: traceback for failed fetch of %s", label, url, exc_info=last_exc)
    return None
