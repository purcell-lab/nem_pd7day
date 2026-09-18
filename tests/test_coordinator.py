"""
Tests for coordinator.py: PD7DayCoordinator and DispatchCoordinator.

Sections
  - fetch -> ingest -> store pipeline, and the CalibrationStore contract the
    coordinator relies on (no duplicate history on restart, intervention flag)
  - market notice polling: cursor advance, the #139 last_fetched stamp, and one
    poll per cycle across region coordinators
  - client wiring: the shared fetcher registered in hass.data, else a private client
  - stale-data fallback when NEMWEB fails (#22: retries live in the client)
  - staleness attributes (#105) and the missed publish slot rule (#128)
  - DispatchCoordinator: stale fallback and the DEBUG volume of one poll (#33)

Run with:  python -m pytest tests/test_coordinator.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import support
from support import NEM_TZ, install_ha_stubs, load, nem_iso, run_async

install_ha_stubs()

_nem_time = load("nem_time")
_engine_mod = load("calibration_engine")
_store_mod = load("calibration_store")
_client_mod = load("pd7day_client")
_const_mod = load("const")
_notice_client_mod = load("market_notice_client")
_notice_store_mod = load("notice_store")
_dispatch_mod = load("dispatch_client")
_coord_mod = load("coordinator")

# coordinator.py catches ``aiohttp.ClientResponseError`` by identity, so the
# class on the aiohttp stub it imported has to be a real exception. support
# seeds one when it creates the stub; a file collected earlier may have left a
# bare MagicMock there, in which case install the same class on it.
if not (
    isinstance(getattr(_coord_mod.aiohttp, "ClientResponseError", None), type)
    and issubclass(_coord_mod.aiohttp.ClientResponseError, BaseException)
):
    _coord_mod.aiohttp.ClientResponseError = support.ClientResponseError
ClientResponseError = _coord_mod.aiohttp.ClientResponseError
UpdateFailed = _coord_mod.UpdateFailed
NemwebFetchError = _coord_mod.NemwebFetchError

PD7DayCoordinator = _coord_mod.PD7DayCoordinator
DispatchCoordinator = _coord_mod.DispatchCoordinator
CaseSolutionData = _client_mod.CaseSolutionData
PD7DayResult = _client_mod.PD7DayResult
GridNoticeStore = _notice_store_mod.GridNoticeStore
DOMAIN = _const_mod.DOMAIN
SHARED_FETCH_KEY = _const_mod.SHARED_FETCH_KEY
PACKAGE_LOGGER = "custom_components.nem_pd7day"

# Bound to this file's module objects, not the last file's (see support.py).
make_price_period = partial(support.make_real_price_period, _client_mod)
make_pd7day_data = partial(support.make_pd7day_data, _client_mod)
make_store = partial(support.make_store, _store_mod)

# Pin _now_nem() close to the test dates so forecast-history pruning keeps the data.
_store_mod._now_nem = lambda: datetime(2026, 4, 15, 19, 0, tzinfo=NEM_TZ)

RUN_AT = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
PERIOD_END = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
PERIOD_KEY = nem_iso(PERIOD_END - timedelta(minutes=30))


# ── Builders ──────────────────────────────────────────────────────────────────

def make_case(intervention: bool = False, run_dt: str = "2026-04-15T07:25:07+10:00"):
    return CaseSolutionData(run_datetime=run_dt, intervention=intervention, last_changed=run_dt)


def make_result(run_at_dt: datetime, periods: list, intervention: bool = False) -> PD7DayResult:
    return PD7DayResult(
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        case=make_case(intervention),
        prices={"QLD1": make_pd7day_data(run_at_dt, periods)},
        market_summary=None,
        interconnectors={},
    )


def make_coordinator(store=None, notice_store=None, notice_client=None) -> PD7DayCoordinator:
    """A PD7DayCoordinator built with __new__, wired for a single QLD1 region."""
    coord = PD7DayCoordinator.__new__(PD7DayCoordinator)
    coord.hass = MagicMock()
    coord.logger = MagicMock()
    coord.name = "nem_pd7day"
    coord.update_interval = None
    coord.last_update_success = True
    coord.data = None
    coord._regions = ["QLD1"]
    coord._interconnector_ids = {"NSW1-QLD1"}
    coord._store = store
    coord._session = None
    coord.notice_store = notice_store
    coord._notice_client = notice_client
    coord._forecast_store = None
    coord._stpasa_store = None
    coord.tod_stats = _coord_mod.TodStats()
    coord._first_refresh_done = False
    return coord


def make_dispatch_coordinator(*, run_fetch_inline: bool = False) -> DispatchCoordinator:
    """A DispatchCoordinator built with __new__.

    ``run_fetch_inline`` makes ``hass.async_add_executor_job`` call the
    function directly so the real ``fetch_dispatch_prices`` runs in-process.
    """
    coord = DispatchCoordinator.__new__(DispatchCoordinator)
    hass = MagicMock()
    if run_fetch_inline:
        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        hass.async_add_executor_job = _inline
    coord.hass = hass
    coord.logger = MagicMock()
    coord.name = "NEM Dispatch"
    coord.update_interval = None
    coord.last_update_success = True
    coord.data = None
    coord.prices = {}
    coord.last_updated = None
    return coord


def make_client(fetch_all) -> MagicMock:
    """A PD7DAY client stand-in; ``fetch_all`` is a return value, exception or coroutine function."""
    client = MagicMock()
    if isinstance(fetch_all, BaseException):
        client.fetch_all = AsyncMock(side_effect=fetch_all)
    elif inspect.iscoroutinefunction(fetch_all):
        client.fetch_all = fetch_all
    else:
        client.fetch_all = AsyncMock(return_value=fetch_all)
    return client


def make_notice_store(notices: dict | None = None, cursor: int = 50000) -> GridNoticeStore:
    store = GridNoticeStore.__new__(GridNoticeStore)
    store._notices = notices or {}
    store._last_seen_notice_id = cursor
    store.last_fetched_at = None
    store._store = MagicMock()
    store._store.async_save = AsyncMock()
    return store


def make_notice_client(cursor: int = 50000, cursor_after_fetch: int | None = None) -> MagicMock:
    """A MarketNoticeClient stand-in that finds nothing relevant.

    ``cursor_after_fetch`` simulates the client having examined files up to
    that id during the poll.
    """
    client = MagicMock()
    client.last_seen_notice_id = cursor

    async def _fetch():
        if cursor_after_fetch is not None:
            client.last_seen_notice_id = cursor_after_fetch
        return []

    client.fetch_new_notices = AsyncMock(side_effect=_fetch)
    return client


def _client_response_error(status=403, message="Forbidden"):
    return ClientResponseError(request_info=MagicMock(), history=(), status=status, message=message)


def _attrs_at(coord, now_utc: datetime) -> dict:
    with patch.object(_coord_mod.dt_util, "utcnow", return_value=now_utc):
        return coord.staleness_attributes()


# ── Fetch -> ingest -> store pipeline ────────────────────────────────────────

def test_update_data_ingests_forecast_into_store_without_stpasa():
    """
    The coordinator no longer fetches STPASA itself: a shared coroutine in
    __init__.py downloads it centrally and populates the per-region stores.
    With no STPASA store wired in, _async_update_data must still complete,
    ingest the PD7DAY forecast for each region with stpasa=None, and return
    the PD7DayResult. STPASA absence is non-fatal.
    """
    store = make_store()
    coord = make_coordinator(store=store)
    coord._first_refresh_done = True   # skip the notice fetch path
    result = make_result(RUN_AT, [make_price_period(PERIOD_END, value=0.085)])

    with patch.object(coord, "_get_client", return_value=make_client(result)):
        out = run_async(coord._async_update_data())

    assert out is result
    # There is no STPASA fetch helper on the coordinator any more.
    assert not hasattr(coord, "_get_stpasa_client")
    assert PERIOD_KEY in store._forecast_history
    entry = store._forecast_history[PERIOD_KEY][0]
    assert entry["run_at"] == nem_iso(RUN_AT)
    assert "stpasa_demand50" not in entry, "No STPASA data -> no stpasa_* annotation expected"


def test_ingest_populates_history_with_raw_forecast_price():
    """
    One ingest of a coordinator result must leave one history entry per
    period, keyed by interval START, carrying run_at and the raw AEMO value.

    The stored forecast_price must be period.value as parsed (float(row[8]) /
    1000), never a calibrated value: OLS trains actual ~ a*raw + b, and
    training on already-corrected data would be circular. Calibration is
    applied later, in sensor.py.
    """
    store = make_store()
    result = make_result(RUN_AT, [make_price_period(PERIOD_END, value=0.085)])

    for region, price_data in result.prices.items():
        run_async(store.ingest_forecast(
            region=region, price_data=price_data,
            interconnectors=result.interconnectors, case=result.case,
        ))

    assert PERIOD_KEY in store._forecast_history, (
        f"{PERIOD_KEY!r} must be in forecast_history; keys: {list(store._forecast_history)[:3]}"
    )
    entries = store._forecast_history[PERIOD_KEY]
    assert len(entries) == 1
    assert entries[0]["run_at"] == nem_iso(RUN_AT)
    assert abs(entries[0]["forecast_price"] - 0.085) < 1e-9, (
        f"forecast_price must be the raw AEMO value 0.085, got {entries[0]['forecast_price']}"
    )


def test_restart_reingest_same_file_no_duplicate():
    """
    BUG (v1.8.0): an HA restart triggers a startup fetch, and the scheduled
    fetch may then see the same AEMO file (same run_at). ingest_forecast must
    skip duplicate run_at entries rather than appending them, or the running
    average is corrupted.
    """
    store = make_store()
    price_data = make_pd7day_data(RUN_AT, [make_price_period(PERIOD_END, value=0.085)])

    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case()))   # startup
    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case()))   # same file again

    entries = store._forecast_history[PERIOD_KEY]
    assert len(entries) == 1, f"Expected 1 entry after double-ingest of one run_at, got {len(entries)}"


def test_new_aemo_publish_adds_second_entry():
    """Two genuine AEMO publishes (different run_at) covering the same interval
    must each produce a separate history entry."""
    store = make_store()
    period_end = datetime(2026, 4, 16, 7, 0, tzinfo=NEM_TZ)
    run1 = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    run2 = datetime(2026, 4, 15, 13, 0, tzinfo=NEM_TZ)

    for run_at in (run1, run2):
        price_data = make_pd7day_data(run_at, [make_price_period(period_end, value=0.10)])
        run_async(store.ingest_forecast("QLD1", price_data, {}, make_case()))

    entries = store._forecast_history[nem_iso(period_end - timedelta(minutes=30))]
    assert len(entries) == 2, f"Two distinct publishes must produce 2 entries, got {len(entries)}"
    assert {e["run_at"] for e in entries} == {nem_iso(run1), nem_iso(run2)}


def test_intervention_flag_flows_to_observation_and_excludes_it_from_ols():
    """
    intervention=True from CASESOLUTION must reach the history entry, then
    the observation recorded against it, and CalibrationEngine.fit() must
    exclude that observation.
    """
    store = make_store()
    price_data = make_pd7day_data(RUN_AT, [make_price_period(PERIOD_END, value=0.10)])

    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case(intervention=True)))
    assert store._forecast_history[PERIOD_KEY][0]["is_intervention"] is True

    run_async(store.async_record_actual(PERIOD_KEY, 0.095))
    assert len(store._observations) == 1
    obs = store._observations[0]
    assert obs["is_intervention"] is True, "Observation must be flagged as intervention"

    obs_list = [_engine_mod.Observation(
        interval_time=obs["interval_time"],
        horizon_hours=obs["horizon_hours"],
        pd7day_forecast=obs["pd7day_forecast"],
        actual_rrp=obs["actual_rrp"],
        forecast_run_at=obs["forecast_run_at"],
        hour_of_day=obs["hour_of_day"],
        day_of_week=obs["day_of_week"],
        month=obs["month"],
        gas_forecast_tj=obs.get("gas_forecast_tj"),
        qni_mwflow=obs.get("qni_mwflow"),
        qni_violation_degree=obs.get("qni_violation_degree"),
        is_intervention=obs["is_intervention"],
    )]
    fit = _engine_mod.CalibrationEngine().fit(obs_list)
    assert fit.total_observations == 0, (
        f"Intervention observation must be excluded from OLS, got total_observations={fit.total_observations}"
    )


# ── Market notice polling ────────────────────────────────────────────────────
# LOR and MSL notices are rare and pruned after 7 days, so an empty store and a
# poll that finds nothing are the ordinary state of a quiet grid. The cursor
# used to move only when a notice was stored, so it parked while NEMWEB kept
# publishing and every cycle re-read the growing backlog; and an empty store
# was read as an incomplete upgrade needing a full backfill on every cycle.

def test_cursor_advances_when_no_relevant_notices_found():
    """The cursor is persisted forward even when a cycle stores nothing."""
    notice_store = make_notice_store()
    notice_client = make_notice_client(cursor_after_fetch=50120)   # examined up to 50120
    coord = make_coordinator(notice_store=notice_store, notice_client=notice_client)

    run_async(coord.async_fetch_notices())

    assert notice_store.last_seen_notice_id == 50120
    # Advancing the cursor is only useful if it survives a restart.
    notice_store._store.async_save.assert_awaited()


def test_notice_poll_stamps_last_fetched_at_even_when_nothing_relevant_found():
    """
    Every completed poll stamps last_fetched_at, notices or not, so the grid
    notices sensor's last_fetched reports the poll rather than the last
    stored notice (issue #139). A poll that raises leaves the stamp alone.
    """
    notice_store = make_notice_store()
    notice_client = make_notice_client()
    coord = make_coordinator(notice_store=notice_store, notice_client=notice_client)

    polled = datetime(2026, 9, 6, 13, 0, tzinfo=NEM_TZ)
    with patch.object(_notice_store_mod, "now_nem", return_value=polled):
        run_async(coord._fetch_notices_once())
    assert notice_store.last_fetched_at == polled
    # Nothing relevant and the cursor did not move, so nothing was written.
    notice_store._store.async_save.assert_not_awaited()

    # A failed poll must not claim success.
    notice_client.fetch_new_notices = AsyncMock(side_effect=RuntimeError("503"))
    with patch.object(_notice_store_mod, "now_nem", return_value=polled + timedelta(hours=1)):
        with pytest.raises(RuntimeError):
            run_async(coord._fetch_notices_once())
    assert notice_store.last_fetched_at == polled


def _one_lor_notice() -> dict:
    now = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)
    return {"QLD1": [_notice_client_mod.GridNoticeAnnotation(
        notice_id=50000, notice_type="LOR", level=1, region="QLD1",
        period_from=now, period_to=now + timedelta(hours=2), issued_at=now,
    )]}


@pytest.mark.parametrize("notices", [{}, _one_lor_notice()], ids=["empty_store", "notices_exist"])
def test_fetch_notices_never_resets_the_cursor(notices):
    """A poll that finds nothing leaves the cursor where it was, whether the
    store is empty (the old backfill trigger) or already holds notices."""
    notice_store = make_notice_store(notices=notices)
    notice_client = make_notice_client()
    coord = make_coordinator(notice_store=notice_store, notice_client=notice_client)

    run_async(coord.async_fetch_notices())

    assert notice_store.last_seen_notice_id == 50000
    assert notice_client.last_seen_notice_id == 50000


def test_notices_fetched_once_per_cycle_across_regions():
    """Notices are global, so five region coordinators sharing a store must
    poll NEMWEB once between them, not once each."""
    notice_store = make_notice_store()
    notice_client = make_notice_client()
    shared_data: dict = {}
    coords = []
    for region in ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]:
        c = make_coordinator(notice_store=notice_store, notice_client=notice_client)
        c._regions = [region]
        c.hass.data = {DOMAIN: shared_data}
        coords.append(c)

    async def _all():
        await asyncio.gather(*(c.async_fetch_notices() for c in coords))

    run_async(_all())

    assert notice_client.fetch_new_notices.await_count == 1


# ── Client wiring ────────────────────────────────────────────────────────────

def test_get_client_prefers_the_registered_shared_fetcher():
    """The shared fetcher could exist and be correct while every coordinator
    quietly kept using its own private client; this pins the wiring. With
    nothing registered the coordinator must still stand alone."""
    coord = make_coordinator()
    fetcher = MagicMock(name="shared_fetcher")
    coord.hass.data = {DOMAIN: {SHARED_FETCH_KEY: fetcher}}
    assert coord._get_client() is fetcher, "coordinator built its own client while a shared fetcher was registered"

    coord.hass.data = {DOMAIN: {}}
    own = coord._get_client()
    assert isinstance(own, _coord_mod.PD7DayClient)


# ── Stale-data fallback ──────────────────────────────────────────────────────
# A transient NEMWEB error (403, 429, timeout) must serve the last good result
# rather than raise UpdateFailed, which marks every sensor unavailable. Only the
# very first fetch, with nothing to serve, raises.

def test_serving_stale_on_http_error_is_flagged_with_reason_and_age():
    """Stale data keeps the entity available, but the attributes must say so
    (issue #105): is_stale True, the failure as stale_reason, and the age of
    the data served, measured from the last success."""
    coord = make_coordinator()
    stale = MagicMock(name="stale_pd7day_result")
    coord.data = stale
    coord.last_success_at = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
    coord._get_client = lambda: make_client(_client_response_error(403, "Forbidden"))

    now = datetime(2026, 9, 3, 6, 30, tzinfo=timezone.utc)
    with patch.object(_coord_mod.dt_util, "utcnow", return_value=now):
        result = run_async(coord._async_update_data())
        attrs = coord.staleness_attributes()

    assert result is stale, "Coordinator must return stale data on HTTP error when stale data exists"
    assert attrs["is_stale"] is True
    assert "403" in attrs["stale_reason"]
    assert attrs["data_age_hours"] == 6.5


def test_returns_stale_on_nemweb_fetch_error_with_one_legible_warning(caplog):
    """The client raises NemwebFetchError once its retries are spent.

    Issue #22 moved retrying out of the coordinator and into the clients, so
    the stale-data fallback has to recognise the client's own exhaustion
    error, not only a raw aiohttp status error. Without this branch a
    sustained 403 would surface as UpdateFailed.
    """
    coord = make_coordinator()
    stale = MagicMock(name="stale_pd7day_result")
    coord.data = stale
    coord._get_client = lambda: make_client(NemwebFetchError(
        "PD7DAY directory listing unavailable after retry", retryable=False, status=403,
    ))

    with caplog.at_level(logging.WARNING):
        result = run_async(coord._async_update_data())

    assert result is stale
    stale_lines = [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "serving stale data" in r.getMessage()
    ]
    assert len(stale_lines) == 1, "one stale-data warning, not a burst"
    # "403" alone does not tell an operator that NEMWEB is rate blocking
    # rather than that the report path is wrong.
    assert "bot or rate block" in stale_lines[0]


@pytest.mark.parametrize(
    "exc",
    [
        _client_response_error(403, "Forbidden"),
        NemwebFetchError("exhausted", retryable=False, status=403),
        RuntimeError("timeout"),
    ],
    ids=["ClientResponseError", "NemwebFetchError", "Exception"],
)
def test_raises_update_failed_when_there_is_no_stale_data(exc):
    """With no previous data there is nothing to serve, so it must fail."""
    coord = make_coordinator()
    coord.data = None
    coord._get_client = lambda: make_client(exc)

    with pytest.raises(UpdateFailed):
        run_async(coord._async_update_data())


def test_fetch_is_attempted_exactly_once_with_no_coordinator_retry():
    """Retrying belongs to the client (issue #22), so the wrapper must be gone.

    Leaving it in place would double the retry budget and re-download every
    file on each attempt, which is what provoked the 403 to begin with.
    """
    coord = make_coordinator()
    assert not hasattr(coord, "_fetch_all_with_retry")
    calls = []

    async def fetch_all(regions, interconnectors):
        calls.append((tuple(regions), tuple(interconnectors)))
        raise NemwebFetchError("exhausted", retryable=False, status=403)

    coord._get_client = lambda: make_client(fetch_all)
    coord.data = MagicMock(name="stale")

    run_async(coord._async_update_data())

    assert len(calls) == 1, "the coordinator must attempt the fetch exactly once"


# ── Staleness attributes (issue #105) ────────────────────────────────────────

def test_success_clears_staleness_and_resets_age():
    coord = make_coordinator()
    coord.serving_stale = True
    coord.stale_reason = "403 Forbidden"
    result = MagicMock(name="fresh_result")
    result.prices = {}
    result.interconnectors = {}
    result.case = None
    result.source_file = "PUBLIC_PD7DAY.zip"
    coord._get_client = lambda: make_client(result)

    now = datetime(2026, 9, 3, 7, 30, tzinfo=timezone.utc)
    with patch.object(_coord_mod.dt_util, "utcnow", return_value=now):
        run_async(coord._async_update_data())
        attrs = coord.staleness_attributes()

    assert attrs == {
        "data_age_hours": 0.0,
        "last_success_at": "2026-09-03T17:30:00+10:00",
        "is_stale": False,
        "stale_reason": None,
    }
    assert coord.last_success_at == now


def test_staleness_attributes_before_first_success():
    """Restored-from-cache startup: nothing fetched yet, so no age and not
    stale rather than a misleading zero."""
    coord = make_coordinator()
    assert coord.staleness_attributes() == {
        "data_age_hours": None, "last_success_at": None,
        "is_stale": False, "stale_reason": None,
    }


def test_staleness_helper_ignores_coordinators_that_do_not_track_it():
    assert _coord_mod.staleness_attributes(MagicMock()) == {}
    assert _coord_mod.staleness_attributes(object()) == {}


# ── A run that never arrives is stale without any failure (issue #128) ───────
# The live failure of 5 Sep 2026: the scheduled fetch died silently, no failure
# was recorded, and is_stale read False while the 07:30 run never arrived.

def _serving_run(coord, generated_at: str) -> None:
    """Make the coordinator serve a PD7DAY result whose run is generated_at."""
    price = MagicMock(name="price_data")
    price.forecast_generated_at = generated_at
    result = MagicMock(name="result")
    result.prices = {"SA1": price}
    coord.data = result


def test_missed_publish_slot_marks_stale_without_a_failure():
    """The 07:30 run has not arrived by 08:05 NEM: stale, and the reason says
    which slot was missed."""
    coord = make_coordinator()
    coord.last_success_at = datetime(2026, 9, 4, 21, 9, tzinfo=timezone.utc)   # 07:09 NEM
    _serving_run(coord, "2026-09-04T18:00:00+10:00")
    attrs = _attrs_at(coord, datetime(2026, 9, 4, 22, 5, tzinfo=timezone.utc))   # 08:05 NEM
    assert attrs["is_stale"] is True
    assert attrs["stale_reason"] == "missed 07:30 run"
    assert attrs["last_success_at"] == "2026-09-05T07:09:00+10:00"
    assert attrs["data_age_hours"] == 0.93


def test_missed_slot_waits_for_the_grace_period():
    """At 07:50 NEM the 07:30 slot is inside STALE_RUN_GRACE_MIN: not stale."""
    coord = make_coordinator()
    coord.last_success_at = datetime(2026, 9, 4, 21, 9, tzinfo=timezone.utc)
    _serving_run(coord, "2026-09-04T18:00:00+10:00")
    attrs = _attrs_at(coord, datetime(2026, 9, 4, 21, 50, tzinfo=timezone.utc))   # 07:50 NEM
    assert attrs["is_stale"] is False
    assert attrs["stale_reason"] is None


def test_current_run_is_not_stale_after_the_slot():
    """Serving the 07:30 run at 08:05 NEM is fresh; the overnight case, the
    18:00 run served at 06:00 NEM, is fresh too because the latest slot that
    has passed is yesterday's 18:00."""
    coord = make_coordinator()
    coord.last_success_at = datetime(2026, 9, 4, 21, 30, tzinfo=timezone.utc)
    _serving_run(coord, "2026-09-05T07:30:00+10:00")
    assert _attrs_at(coord, datetime(2026, 9, 4, 22, 5, tzinfo=timezone.utc))["is_stale"] is False
    _serving_run(coord, "2026-09-04T18:00:00+10:00")
    assert _attrs_at(coord, datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc))["is_stale"] is False   # 06:00 NEM


def test_fetch_failure_reason_wins_over_the_missed_slot():
    """A recorded failure keeps its own reason; the missed slot only fills in
    when nothing else explains the staleness."""
    coord = make_coordinator()
    coord.last_success_at = datetime(2026, 9, 4, 21, 9, tzinfo=timezone.utc)
    coord.serving_stale = True
    coord.stale_reason = "403 Forbidden"
    _serving_run(coord, "2026-09-04T18:00:00+10:00")
    attrs = _attrs_at(coord, datetime(2026, 9, 4, 22, 5, tzinfo=timezone.utc))
    assert attrs["is_stale"] is True and attrs["stale_reason"] == "403 Forbidden"


def test_missed_slot_rule_ignores_results_without_a_run_time():
    """The dispatch coordinator and a bare MagicMock result carry no run time."""
    coord = make_coordinator()
    coord.last_success_at = datetime(2026, 9, 4, 21, 9, tzinfo=timezone.utc)
    coord.data = MagicMock(name="opaque")
    attrs = _attrs_at(coord, datetime(2026, 9, 4, 22, 5, tzinfo=timezone.utc))
    assert attrs["is_stale"] is False and attrs["stale_reason"] is None


# ── DispatchCoordinator: stale fallback ──────────────────────────────────────

def test_dispatch_coordinator_serves_stale_prices_on_error():
    """When fetch_dispatch_prices raises and stale prices exist, the
    coordinator returns them and flags the staleness with the reason."""
    coord = make_dispatch_coordinator()
    stale = MagicMock(name="stale_dispatch_prices")
    coord.data = stale
    coord.hass.async_add_executor_job = AsyncMock(side_effect=Exception("timeout"))

    result = run_async(coord._async_update_data())
    attrs = coord.staleness_attributes()

    assert result is stale, "DispatchCoordinator must return stale data on error when stale data exists"
    assert attrs["is_stale"] is True
    assert attrs["stale_reason"] == "timeout"


# ── DispatchCoordinator: DEBUG volume of one poll cycle (issue #33) ──────────
# With five regions the dispatch path used to emit nine DEBUG records every five
# minutes: the scheduler intent line, the client's all-region summary, the
# client's region count, the coordinator's timing line, and one line per
# region. Six restated something another line carried, so a cycle now emits
# two: the scheduler intent line and the single all-region summary. (The
# separate x5 multiplier reported on #33 was five DispatchCoordinator instances
# being created at setup: issue #34, see test_shared_dispatch.py.) These tests
# pin the count, so a new per-cycle line has to be added deliberately, and pin
# that the survivors still name every region with settlement and price, so a
# future "dedupe" cannot quietly become a loss of diagnostics.

DISPATCH_REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]
DISPATCH_PRICES_MWH = {"QLD1": 89.5, "NSW1": 75.2, "VIC1": 120.0, "SA1": -5.0, "TAS1": 88.1}


def _current_boundary_nem() -> datetime:
    """Current 5-minute boundary in NEM time (UTC+10, no daylight saving).

    This is the settlement the coordinator computes as expected, so serving
    it back keeps the freshness gate satisfied without any sleeping retry.
    """
    nem_now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=10)
    return nem_now.replace(minute=(nem_now.minute // 5) * 5, second=0, microsecond=0)


def _summary_payload(settlement: datetime, regions=DISPATCH_REGIONS) -> bytes:
    ts = settlement.strftime("%Y-%m-%dT%H:%M:%S")
    rows = [
        {"SETTLEMENTDATE": ts, "REGIONID": r, "PRICE": DISPATCH_PRICES_MWH[r], "PRICE_STATUS": "FIRM"}
        for r in regions
    ]
    return json.dumps({"ELEC_NEM_SUMMARY": rows}).encode()


def _run_one_cycle(coord: DispatchCoordinator, payload: bytes) -> dict:
    """One full poll cycle: fetch prices, then schedule the next boundary."""
    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = run_async(coord._async_update_data())
    with patch.object(_coord_mod, "async_track_point_in_utc_time", return_value=MagicMock()):
        coord.schedule_next_poll()
    return prices


def _debug_records(caplog):
    return [r for r in caplog.records if r.levelno == logging.DEBUG and r.name.startswith(PACKAGE_LOGGER)]


@pytest.mark.parametrize("regions", [DISPATCH_REGIONS, ["QLD1"]], ids=["five_regions", "one_region"])
def test_dispatch_cycle_emits_exactly_two_debug_lines(caplog, regions):
    """Two DEBUG records per cycle, down from nine, and the count must not
    scale with the number of regions AEMO returns (the old per-region loop
    made the cost of DEBUG proportional to it)."""
    coord = make_dispatch_coordinator(run_fetch_inline=True)

    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        prices = _run_one_cycle(coord, _summary_payload(_current_boundary_nem(), regions))

    assert set(prices) == set(regions), "test must exercise the regions it claims to"
    records = _debug_records(caplog)
    rendered = "\n".join(r.getMessage() for r in records)
    assert len(records) == 2, f"expected 2 DEBUG records per dispatch cycle, got {len(records)}:\n{rendered}"


def test_surviving_debug_lines_carry_every_region_price_settlement_and_next_boundary(caplog):
    """This is a dedupe, not a loss of diagnostics: everything the removed
    per-region loop printed must still be readable off the one summary
    record, and the other survivor is the scheduler intent line."""
    coord = make_dispatch_coordinator(run_fetch_inline=True)
    settlement = _current_boundary_nem()

    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        _run_one_cycle(coord, _summary_payload(settlement))

    messages = [r.getMessage() for r in _debug_records(caplog)]
    summaries = [m for m in messages if "ELEC_NEM_SUMMARY fetched" in m]
    assert len(summaries) == 1, f"expected one all-region summary, got {summaries}"
    summary = summaries[0]
    for region in DISPATCH_REGIONS:
        assert region in summary, f"{region} missing from summary: {summary}"
        assert f"${DISPATCH_PRICES_MWH[region] / 1000.0:.4f}/kWh" in summary, (
            f"{region} price missing from summary: {summary}"
        )
    assert settlement.strftime("%Y-%m-%dT%H:%M") in summary, f"settlement missing from summary: {summary}"
    # The removed client line reported the region count; it is still
    # recoverable because every region is named.
    assert summary.count("settlement=") == len(DISPATCH_REGIONS)

    boundary_lines = [m for m in messages if "next boundary poll" in m]
    assert len(boundary_lines) == 1, f"expected one scheduler line, got {boundary_lines}"
    assert f"+{_coord_mod._DISPATCH_POLL_DELAY_S}s delay" in boundary_lines[0]


def test_dispatch_success_path_has_no_debug_logging():
    """Guard against the per-region loop and timing line coming back.

    Counting records alone would not catch someone re-adding a line while
    also relaxing the count, so pin the source too. Failure paths are
    excluded: they log at WARNING and are outside this issue.
    """
    src = inspect.getsource(DispatchCoordinator._async_update_data)
    success_path = src.split("except Exception as exc:")[0]
    success_path = "\n".join(
        line for line in success_path.splitlines() if not line.lstrip().startswith("#")
    )
    assert "Finished fetching NEM Dispatch data" not in success_path, (
        "coordinator must not re-add the timing/region-count DEBUG line to the dispatch "
        "success path; HA core's DataUpdateCoordinator already logs elapsed time"
    )
    assert "for region_id, dp in sorted(prices.items())" not in success_path, (
        "coordinator must not re-add the per-region DEBUG loop; the ELEC_NEM_SUMMARY line "
        "already covers every region"
    )
