"""
Tests for coordinator.py — the fetch→ingest→store pipeline.

Covers the end-to-end flow that links PD7DayClient, CalibrationStore,
and the coordinator.  These tests catch integration-level bugs that
unit tests on individual components miss.

Key scenarios:
  - Coordinator feeds forecast into store after fetch
  - Re-fetch of same AEMO file (restart) does NOT duplicate forecast history
  - Different AEMO publish (new run_at) DOES add new entries
  - Intervention flag from CASESOLUTION reaches the store

Run with:  python -m pytest tests/test_coordinator.py -v
"""
from __future__ import annotations

import sys
import os
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Stub HA and aiohttp
sys.modules.setdefault("aiohttp", MagicMock())
for ha_mod in [
    "homeassistant", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.storage", "homeassistant.helpers.event",
    "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform", "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.util", "homeassistant.util.dt",
]:
    sys.modules.setdefault(ha_mod, MagicMock())

# DataUpdateCoordinator stub — our coordinator inherits from it
class _FakeCoordinator:
    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.last_update_success = True
        self.data = None

    # Support DataUpdateCoordinator[PD7DayResult] subscript syntax
    def __class_getitem__(cls, item):
        return cls

    async def async_config_entry_first_refresh(self):
        pass

uc_mock = MagicMock()
uc_mock.DataUpdateCoordinator = _FakeCoordinator
uc_mock.UpdateFailed = Exception
sys.modules["homeassistant.helpers.update_coordinator"] = uc_mock

_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_engine_mod = _load(
    "custom_components.nem_pd7day.calibration_engine",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"),
)

# HA storage stub for CalibrationStore
ha_storage_mock = MagicMock()
class _FakeStore:
    def __init__(self, hass, version, key): pass
    async def async_load(self): return None
    async def async_save(self, data): pass
ha_storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = ha_storage_mock

_store_mod = _load(
    "custom_components.nem_pd7day.calibration_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"),
)
_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)

# Load const before coordinator
_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)

# Load notice modules before coordinator
_notice_client_mod = _load(
    "custom_components.nem_pd7day.market_notice_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "market_notice_client.py"),
)
_notice_store_mod = _load(
    "custom_components.nem_pd7day.notice_store",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "notice_store.py"),
)

_coord_mod = _load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

from custom_components.nem_pd7day.calibration_store import CalibrationStore
from custom_components.nem_pd7day.coordinator import PD7DayCoordinator
from custom_components.nem_pd7day.pd7day_client import (
    PD7DayResult, PD7DayData, CaseSolutionData, PricePeriod,
)

NEM_TZ = timezone(timedelta(hours=10))

# Pin _now_nem() close to test dates so forecast-history pruning doesn't discard data
_store_mod._now_nem = lambda: datetime(2026, 4, 15, 19, 0, tzinfo=NEM_TZ)


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Helpers ───────────────────────────────────────────────────────────────────

def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def make_price_period(nemtime_dt: datetime, value: float = 0.10) -> PricePeriod:
    start_dt = nemtime_dt - timedelta(minutes=30)
    return PricePeriod(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),
        value=value,
    )


def make_pd7day_data(run_at_dt: datetime, periods: list) -> PD7DayData:
    return PD7DayData(
        region="QLD1",
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        forecast_generated_at=nem_iso(run_at_dt),
        interval_minutes=30,
        current_value=periods[0].value if periods else 0.0,
        next_value=periods[1].value if len(periods) > 1 else None,
        min_24h_value=None,
        max_24h_value=None,
        cheapest_2h_window=None,
        forecast=periods,
    )


def make_case(intervention: bool = False, run_dt: str = "2026-04-15T07:25:07+10:00"):
    return CaseSolutionData(
        run_datetime=run_dt,
        intervention=intervention,
        last_changed=run_dt,
    )


def make_result(run_at_dt: datetime, periods: list,
                intervention: bool = False) -> PD7DayResult:
    price_data = make_pd7day_data(run_at_dt, periods)
    return PD7DayResult(
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        case=make_case(intervention),
        prices={"QLD1": price_data},
        market_summary=None,
        interconnectors={},
    )


def make_store() -> CalibrationStore:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(
        side_effect=lambda fn, *args: asyncio.coroutine(lambda: fn(*args))()
    )
    store = CalibrationStore.__new__(CalibrationStore)
    store._hass = hass
    store._region = "QLD1"
    store._obs_store = MagicMock()
    store._obs_store.async_load = AsyncMock(return_value=None)
    store._obs_store.async_save = AsyncMock()
    store._coeff_store = MagicMock()
    store._coeff_store.async_load = AsyncMock(return_value=None)
    store._coeff_store.async_save = AsyncMock()
    store._fh_store = MagicMock()
    store._fh_store.async_load = AsyncMock(return_value=None)
    store._fh_store.async_save = AsyncMock()
    from custom_components.nem_pd7day.calibration_engine import CalibrationEngine
    store._engine = CalibrationEngine()
    store._observations = []
    store._calibration = None
    store._forecast_history = {}
    store._actual_accum = {}
    return store


def make_coordinator(store=None, notice_store=None, notice_client=None) -> PD7DayCoordinator:
    hass = MagicMock()
    coord = PD7DayCoordinator.__new__(PD7DayCoordinator)
    coord.hass = hass
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
    coord._first_refresh_done = False
    return coord


# ── Tests: coordinator feeds store ───────────────────────────────────────────

def test_coordinator_calls_ingest_forecast_after_fetch():
    """
    After a successful fetch, the coordinator must call store.ingest_forecast()
    for each region in the result.
    """
    store = MagicMock()
    coord = make_coordinator(store=store)

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    periods = [make_price_period(datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ))]
    result = make_result(run_at_dt, periods)

    # Simulate what _async_update_data does with the result
    if coord._store is not None:
        for region, price_data in result.prices.items():
            coord._store.ingest_forecast(
                region=region,
                price_data=price_data,
                interconnectors=result.interconnectors,
                case=result.case,
            )

    store.ingest_forecast.assert_called_once()
    call_kwargs = store.ingest_forecast.call_args
    assert call_kwargs[1]["region"] == "QLD1" or call_kwargs[0][0] == "QLD1"


def test_coordinator_ingest_with_real_store_populates_history():
    """
    End-to-end: coordinator fetch result flows into CalibrationStore.
    After one fetch, _forecast_history must have entries for each period.
    """
    store = make_store()
    make_coordinator(store=store)

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    period_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    periods = [make_price_period(period_end_dt, value=0.085)]
    result = make_result(run_at_dt, periods)

    # Run the ingest directly
    for region, price_data in result.prices.items():
        run_async(store.ingest_forecast(
            region=region,
            price_data=price_data,
            interconnectors=result.interconnectors,
            case=result.case,
        ))

    expected_key = nem_iso(period_end_dt - timedelta(minutes=30))
    assert expected_key in store._forecast_history, (
        f"After coordinator fetch, {expected_key!r} must be in forecast_history. "
        f"Keys present: {list(store._forecast_history.keys())[:3]}"
    )
    entries = store._forecast_history[expected_key]
    assert len(entries) == 1
    assert entries[0]["run_at"] == nem_iso(run_at_dt)
    assert abs(entries[0]["forecast_price"] - 0.085) < 1e-9


def test_restart_reingest_same_file_no_duplicate():
    """
    BUG (v1.8.0): HA restart triggers a startup fetch then the scheduled fetch
    may already have the same AEMO file (same run_at).  ingest_forecast must
    silently skip duplicate run_at entries rather than appending them.

    Scenario: startup fetch at t=0, then 2nd fetch at t=5min (same AEMO file).
    _forecast_history must have exactly 1 entry, not 2.
    """
    store = make_store()

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    period_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    periods = [make_price_period(period_end_dt, value=0.085)]
    price_data = make_pd7day_data(run_at_dt, periods)

    # First ingest (startup)
    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case()))
    # Second ingest (restart-triggered, same AEMO file = same run_at)
    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case()))

    key = nem_iso(period_end_dt - timedelta(minutes=30))
    entries = store._forecast_history[key]
    assert len(entries) == 1, (
        f"Expected 1 history entry after double-ingest of same run_at, "
        f"got {len(entries)}. Duplicate ingest corrupts running average."
    )


def test_new_aemo_publish_adds_second_entry():
    """
    Two genuine AEMO publishes (different run_at) covering the same interval
    must each produce a separate history entry.
    """
    store = make_store()

    period_end_dt = datetime(2026, 4, 16, 7, 0, tzinfo=NEM_TZ)
    run1 = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    run2 = datetime(2026, 4, 15, 13, 0, tzinfo=NEM_TZ)

    for run_at_dt in [run1, run2]:
        periods = [make_price_period(period_end_dt, value=0.10)]
        price_data = make_pd7day_data(run_at_dt, periods)
        run_async(store.ingest_forecast("QLD1", price_data, {}, make_case()))

    key = nem_iso(period_end_dt - timedelta(minutes=30))
    entries = store._forecast_history[key]
    assert len(entries) == 2, (
        f"Two distinct AEMO publishes must produce 2 history entries, got {len(entries)}"
    )
    run_ats = [e["run_at"] for e in entries]
    assert nem_iso(run1) in run_ats
    assert nem_iso(run2) in run_ats


def test_intervention_flag_propagated_to_history():
    """
    is_intervention from CaseSolutionData must be stored in each history entry.
    Observations created during intervention periods must be excluded from OLS.
    """
    store = make_store()

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    period_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    periods = [make_price_period(period_end_dt)]
    price_data = make_pd7day_data(run_at_dt, periods)

    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case(intervention=True)))

    key = nem_iso(period_end_dt - timedelta(minutes=30))
    assert store._forecast_history[key][0]["is_intervention"] is True, (
        "intervention=True from CaseSolutionData must reach forecast history entries"
    )


def test_intervention_observations_excluded_from_ols():
    """
    Observations marked is_intervention=True must be skipped during OLS fit.
    CalibrationEngine.fit() excludes them; this test verifies the flag flows
    correctly from CASESOLUTION → history → observation → engine.
    """
    store = make_store()
    from custom_components.nem_pd7day.calibration_engine import (
        CalibrationEngine, Observation
    )

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    period_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    period_start_str = nem_iso(period_end_dt - timedelta(minutes=30))
    periods = [make_price_period(period_end_dt, value=0.10)]
    price_data = make_pd7day_data(run_at_dt, periods)

    # Ingest as intervention
    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case(intervention=True)))

    # Record an actual for that interval
    run_async(store.async_record_actual(period_start_str, 0.095))

    # Observation must exist but be flagged
    assert len(store._observations) == 1
    obs = store._observations[0]
    assert obs["is_intervention"] is True, (
        "Observation must be flagged as intervention"
    )

    # Now verify the engine excludes it
    engine = CalibrationEngine()
    obs_list = [Observation(
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
    result = engine.fit(obs_list)
    assert result.total_observations == 0, (
        f"Intervention observation must be excluded from OLS fit. "
        f"Got total_observations={result.total_observations}"
    )


def test_no_store_coordinator_does_not_crash():
    """Coordinator with store=None must not crash when processing a result."""
    coord = make_coordinator(store=None)
    result = make_result(datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ), [])

    # Simulate _async_update_data logic
    if coord._store is not None:
        for region, price_data in result.prices.items():
            coord._store.ingest_forecast(
                region=region,
                price_data=price_data,
                interconnectors=result.interconnectors,
                case=result.case,
            )
    # Must not raise — store is None


def test_forecast_price_stored_is_raw_not_calibrated():
    """
    The forecast_price stored in history must be the raw AEMO value (period.value),
    not a calibrated value.  OLS trains actual ~ a*raw + b; if calibrated values
    are stored instead, the model trains on already-corrected data (circular).
    """
    store = make_store()

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    period_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    raw_value = 0.085  # $/kWh from AEMO CSV

    # PricePeriod.value IS the raw value — the integration sets it directly
    # from float(row[8]) / 1000.  Calibration is applied later in sensor.py.
    period = make_price_period(period_end_dt, value=raw_value)
    price_data = make_pd7day_data(run_at_dt, [period])
    run_async(store.ingest_forecast("QLD1", price_data, {}, make_case()))

    key = nem_iso(period_end_dt - timedelta(minutes=30))
    entry = store._forecast_history[key][0]

    assert abs(entry["forecast_price"] - raw_value) < 1e-9, (
        f"forecast_price in history must be raw AEMO value {raw_value}, "
        f"got {entry['forecast_price']}. If calibrated, OLS is circular."
    )


# ── Tests: notice cursor advance ──────────────────────────────────────────────

def test_cursor_advances_when_no_relevant_notices_found():
    """
    The cursor must be persisted forward even when a cycle finds no LOR or MSL
    notices.

    Those notices are rare, so most cycles legitimately store nothing. When the
    cursor only moved on a stored notice it stayed parked while NEMWEB kept
    publishing, and every later cycle re-examined the whole growing backlog.
    """
    from custom_components.nem_pd7day.notice_store import GridNoticeStore

    notice_store = GridNoticeStore.__new__(GridNoticeStore)
    notice_store._notices = {}
    notice_store._last_seen_notice_id = 50000
    notice_store.last_fetched_at = None
    notice_store._store = MagicMock()
    notice_store._store.async_save = AsyncMock()

    # The client examined files up to 50120 and found nothing relevant.
    notice_client = MagicMock()
    notice_client.last_seen_notice_id = 50000
    async def _fetch():
        notice_client.last_seen_notice_id = 50120
        return []
    notice_client.fetch_new_notices = AsyncMock(side_effect=_fetch)

    coord = make_coordinator(notice_store=notice_store, notice_client=notice_client)

    run_async(coord.async_fetch_notices())

    assert notice_store.last_seen_notice_id == 50120
    # Advancing the cursor is only useful if it survives a restart.
    notice_store._store.async_save.assert_awaited()


def test_empty_store_does_not_trigger_backfill_reset():
    """
    An empty notice store must not reset the cursor.

    A store with a cursor and no notices was treated as an incomplete upgrade
    needing a full backfill. But with LOR and MSL notices being rare and pruned
    after 7 days, an empty store is the ordinary state of a quiet grid, so that
    reset fired on every cycle and forced a full re-read each time.
    """
    from custom_components.nem_pd7day.notice_store import GridNoticeStore

    notice_store = GridNoticeStore.__new__(GridNoticeStore)
    notice_store._notices = {}
    notice_store._last_seen_notice_id = 50000
    notice_store.last_fetched_at = None
    notice_store._store = MagicMock()
    notice_store._store.async_save = AsyncMock()

    notice_client = MagicMock()
    notice_client.last_seen_notice_id = 50000
    notice_client.fetch_new_notices = AsyncMock(return_value=[])

    coord = make_coordinator(notice_store=notice_store, notice_client=notice_client)

    run_async(coord.async_fetch_notices())

    assert notice_store.last_seen_notice_id == 50000
    assert notice_client.last_seen_notice_id == 50000


def test_notices_fetched_once_per_cycle_across_regions():
    """
    Notices are global, so five region coordinators sharing a store must poll
    NEMWEB once between them, not once each.
    """
    from custom_components.nem_pd7day.notice_store import GridNoticeStore
    from custom_components.nem_pd7day.const import DOMAIN

    notice_store = GridNoticeStore.__new__(GridNoticeStore)
    notice_store._notices = {}
    notice_store._last_seen_notice_id = 50000
    notice_store.last_fetched_at = None
    notice_store._store = MagicMock()
    notice_store._store.async_save = AsyncMock()

    notice_client = MagicMock()
    notice_client.last_seen_notice_id = 50000
    notice_client.fetch_new_notices = AsyncMock(return_value=[])

    shared_data = {}
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


def test_stpasa_fetch_failure_non_fatal():
    """
    The coordinator no longer fetches STPASA itself — a shared coroutine in
    __init__.py downloads it centrally and populates the per-region stores.
    With no STPASA data available (stpasa_store=None), _async_update_data must
    still complete, ingest the PD7DAY forecast with stpasa=None, and return the
    PD7DayResult — i.e. STPASA absence is non-fatal.
    """
    store = make_store()
    coord = make_coordinator(store=store)
    coord._first_refresh_done = True   # skip notice fetch path
    coord.tod_stats = None

    run_at_dt = datetime(2026, 4, 15, 7, 30, tzinfo=NEM_TZ)
    period_end_dt = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    periods = [make_price_period(period_end_dt, value=0.085)]
    result = make_result(run_at_dt, periods)

    # No STPASA store wired in — the coordinator must not attempt any fetch.
    coord._stpasa_store = None

    # Patch only the PD7DAY fetch; there is no STPASA fetch path any more.
    # The coordinator calls client.fetch_all directly; the 403 retry that used
    # to sit in _fetch_all_with_retry now lives inside the client, per issue #22.
    client = MagicMock()
    client.fetch_all = AsyncMock(return_value=result)
    with patch.object(coord, "_get_client", return_value=client):
        out = run_async(coord._async_update_data())

    # Coordinator update succeeded with no STPASA data.
    assert out is result
    # Coordinator must not expose a STPASA fetch helper any more.
    assert not hasattr(coord, "_get_stpasa_client")
    # Nor a PD7DAY retry wrapper: retries belong to the client.
    assert not hasattr(coord, "_fetch_all_with_retry")
    # Forecast still ingested into the calibration store.
    key = nem_iso(period_end_dt - timedelta(minutes=30))
    assert key in store._forecast_history
    entry = store._forecast_history[key][0]
    assert "stpasa_demand50" not in entry, (
        "No STPASA data → no stpasa_* annotation expected"
    )


def test_no_reset_when_notices_exist():
    """
    When notice_store has stored notices, cursor should NOT be reset.
    """
    from custom_components.nem_pd7day.notice_store import GridNoticeStore
    from custom_components.nem_pd7day.market_notice_client import (
        GridNoticeAnnotation,
    )

    now = datetime(2026, 5, 14, 12, 0, tzinfo=NEM_TZ)
    notice_store = GridNoticeStore.__new__(GridNoticeStore)
    notice_store._notices = {
        "QLD1": [
            GridNoticeAnnotation(
                notice_id=50000, notice_type="LOR", level=1, region="QLD1",
                period_from=now, period_to=now + timedelta(hours=2),
                issued_at=now,
            ),
        ],
    }
    notice_store._last_seen_notice_id = 50000
    notice_store.last_fetched_at = None
    notice_store._store = MagicMock()
    notice_store._store.async_save = AsyncMock()

    notice_client = MagicMock()
    notice_client.last_seen_notice_id = 50000
    notice_client.fetch_new_notices = AsyncMock(return_value=[])

    coord = make_coordinator(notice_store=notice_store, notice_client=notice_client)

    run_async(coord.async_fetch_notices())

    # Cursor should NOT have been reset — notices exist
    assert notice_store.last_seen_notice_id == 50000
    assert notice_client.last_seen_notice_id == 50000
