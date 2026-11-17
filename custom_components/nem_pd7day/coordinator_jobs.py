"""The jobs PD7DayCoordinator runs besides fetching and serving the run (#366).

The coordinator's own job is to fetch the PD7DAY run and serve it, falling
back to the last good run when NEMWEB fails. Around that it does four more,
which live here so the class reads as the fetch and its fallback:

* feeding each run's forecast history into the calibration store and
  recomputing the time-of-day statistics from it (``feed_calibration_store``);
* polling NEMWEB for market notices, once per cycle across every region
  (``fetch_notices``);
* building the STPASA interval index that calibration looks intervals up in
  (``build_stpasa_index``);
* computing the run features stage 2 calibration applies at serving time,
  once per run (``cached_run_features``, ``compute_run_features``).

The coordinator still holds the state each job reads and writes (the
``tod_stats`` it serves, the STPASA index cache and its run key, the run
feature cache), because entities, calibration_inputs and the tests read it
there. Nothing here imports Home Assistant.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .const import (
    DOMAIN,
    NOTICE_FETCH_LOCK_KEY,
    NOTICE_FETCH_STAMP_KEY,
    NOTICE_FETCH_WINDOW_S,
)
from . import tod_stats as _tod_stats
from .tod_stats import TodStats

if TYPE_CHECKING:
    from .calibration_engine import RunFeatures
    from .calibration_store import CalibrationStore
    from .market_notice_client import MarketNoticeClient
    from .notice_store import GridNoticeStore
    from .pd7day_client import PD7DayResult
    from .stpasa_client import StpasaResult
    from .stpasa_store import StpasaStore

# coordinator.py logged these lines before they moved here, and they keep its
# logger name, so a log level or filter set for it still reaches them.
_LOGGER = logging.getLogger(__name__.rpartition(".")[0] + ".coordinator")


# ── Calibration store feed ────────────────────────────────────────────────────

async def feed_calibration_store(
    store: "CalibrationStore",
    stpasa_store: "StpasaStore | None",
    result: "PD7DayResult",
) -> TodStats:
    """Ingest every region of ``result`` into ``store``; return the new tod_stats.

    STPASA is fetched centrally (once per cycle) in __init__.py and
    distributed to every region store; the coordinator only READS its store
    here. latest() returns None when no fresh data is available, so the
    calibration path falls through to isotonic-only silently.
    """
    stpasa_latest = stpasa_store.latest() if stpasa_store is not None else None
    for region, price_data in result.prices.items():
        await store.ingest_forecast(
            region=region,
            price_data=price_data,
            interconnectors=result.interconnectors,
            case=result.case,
            market_summary=result.market_summary,
            stpasa=stpasa_latest,
        )
    # Recompute time-of-day statistics from updated observations
    return _tod_stats.compute_for_store(store)


# ── Market notices ────────────────────────────────────────────────────────────

async def fetch_notices(
    hass: Any,
    client: "MarketNoticeClient | None",
    store: "GridNoticeStore | None",
    regions: list[str],
) -> None:
    """Fetch new market notices and persist.

    Market notices are not region-specific: the store and client are shared,
    and every region coordinator reads the same notices out of the same
    store. So this runs once per cycle across the whole integration rather
    than once per region, which previously multiplied the directory poll and
    every file fetch by five. The first of ``regions`` labels the debug lines.
    """
    if client is None or store is None:
        return

    domain_data = hass.data.get(DOMAIN) if hass is not None else None
    if not isinstance(domain_data, dict):
        # No shared domain data to coordinate through, so there is no sibling
        # coordinator to deduplicate against. Fetch directly.
        await fetch_notices_once(client, store, regions)
        return

    lock: asyncio.Lock = domain_data.setdefault(
        NOTICE_FETCH_LOCK_KEY, asyncio.Lock()
    )
    # Serialised rather than checked-then-fetched, so five coordinators
    # arriving together queue behind one fetch and then see its timestamp,
    # instead of all passing a staleness check simultaneously.
    async with lock:
        last = domain_data.get(NOTICE_FETCH_STAMP_KEY)
        now = time.monotonic()
        if last is not None and (now - last) < NOTICE_FETCH_WINDOW_S:
            _LOGGER.debug(
                "[%s] Market notices fetched %.1f s ago by another region, "
                "using shared store",
                regions[0],
                now - last,
            )
            return
        await fetch_notices_once(client, store, regions)
        domain_data[NOTICE_FETCH_STAMP_KEY] = time.monotonic()


async def fetch_notices_once(
    client: "MarketNoticeClient", store: "GridNoticeStore", regions: list[str]
) -> None:
    """Poll NEMWEB for current notices and persist any change."""
    started = time.monotonic()
    client.last_seen_notice_id = store.last_seen_notice_id
    new_notices = await client.fetch_new_notices()
    # The poll succeeded: say so whether or not anything relevant came
    # back, so the sensor's last_fetched reads as "the poll is running"
    # rather than "a notice was once stored" (issue #139). A failed fetch
    # raises above this line and leaves the previous stamp in place.
    store.mark_fetched()

    # Persist the cursor even when nothing relevant was found. The client has
    # examined those files and will not examine them again, so the store has
    # to record that or the same window is re-read on every cycle.
    cursor_moved = store.advance_cursor(client.last_seen_notice_id)
    if new_notices:
        store.add_notices(new_notices)
    if new_notices or cursor_moved:
        await store.async_save()
    if new_notices:
        _LOGGER.info("Fetched %d new market notices", len(new_notices))
    _LOGGER.debug(
        "[%s] Notice fetch took %.0f ms, cursor %d",
        regions[0],
        (time.monotonic() - started) * 1000,
        store.last_seen_notice_id,
    )


# ── STPASA index ──────────────────────────────────────────────────────────────

def build_stpasa_index(
    result: "StpasaResult",
) -> tuple[dict[str, Any], list[tuple[float, Any]]]:
    """``(index_map, sorted_intervals)`` for one STPASA run.

    ``index_map`` maps interval-START ISO strings (NEM +10:00) to
    ``StpasaInterval``; ``sorted_intervals`` is ``(epoch_seconds,
    StpasaInterval)`` ascending, for the O(log n) nearest-match fallback in
    calibration_inputs. An interval whose time does not parse is left out.
    PD7DayCoordinator.stpasa_index caches the pair against the run, replacing
    the old O(forecast_intervals x stpasa_intervals) linear scan.
    """
    from .nem_time import interval_start, parse_iso

    index_map: dict[str, Any] = {}
    sorted_intervals: list[tuple[float, Any]] = []
    for si in result.intervals:
        try:
            start_iso = interval_start(si.interval_datetime)
            epoch = parse_iso(start_iso).timestamp()
        except (ValueError, TypeError):
            continue
        index_map[start_iso] = si
        sorted_intervals.append((epoch, si))
    sorted_intervals.sort(key=lambda t: t[0])
    return index_map, sorted_intervals


# ── Run features ──────────────────────────────────────────────────────────────

RunFeaturesCache = tuple[tuple[str, int], Any]


def cached_run_features(
    result: "PD7DayResult | None",
    regions: list[str],
    cached: RunFeaturesCache | None,
) -> tuple["RunFeatures | None", RunFeaturesCache | None]:
    """``(features, cache)`` for the first region's run in ``result``.

    ``cached`` is PD7DayCoordinator.current_run_features' cache, ``(key,
    features)`` with key ``(forecast_generated_at, interval count)``: it is
    returned as it is when it names this run or there is no usable run, and
    replaced by a freshly computed entry when the run has changed.
    """
    if result is None or not result.prices:
        return None, cached
    region = regions[0]
    price_data = result.prices.get(region)
    if price_data is None or not price_data.forecast:
        return None, cached
    if not price_data.forecast_generated_at:
        return None, cached
    key = (price_data.forecast_generated_at, len(price_data.forecast))
    if cached is not None and cached[0] == key:
        return cached[1], cached
    features = compute_run_features(price_data)
    return features, (key, features)


def compute_run_features(price_data: Any) -> "RunFeatures | None":
    """RunFeatures for one region's PD7DAY run, uncached.

    Mirrors calibration_engine._compute_run_features so the values fed to
    OLS apply() at runtime match those used during fitting:
      run_max_h6_rrp : max raw forecast for horizon < 6h
      run_mean_rrp   : mean raw forecast for horizon < 24h
      run_spread     : p90 − p10 of raw forecast for horizon < 24h
    Horizon is interval START (period.time) minus the run datetime
    (forecast_generated_at).  Returns None if no usable data.
    cached_run_features calls it once per run.
    """
    from .calibration_engine import RunFeatures, _p90_minus_p10
    from .nem_time import parse_iso

    try:
        run_dt = parse_iso(price_data.forecast_generated_at)
    except Exception:  # noqa: BLE001
        return None

    near: list[float] = []
    day: list[float] = []
    for period in price_data.forecast:
        try:
            start_dt = parse_iso(period.time)
        except Exception:  # noqa: BLE001
            continue
        horizon_hours = (start_dt - run_dt).total_seconds() / 3600.0
        if horizon_hours < 6:
            near.append(period.value)
        if horizon_hours < 24:
            day.append(period.value)

    if not near and not day:
        return None
    return RunFeatures(
        run_max_h6_rrp=max(near) if near else 0.0,
        run_mean_rrp=(sum(day) / len(day)) if day else 0.0,
        run_spread=_p90_minus_p10(day),
    )
