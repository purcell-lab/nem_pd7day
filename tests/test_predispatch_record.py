"""Stage 3a (#337): pre-dispatch forecasts recorded against TradingIS.

predispatch_history turns a PredispatchIS run into forecast history periods,
thinned by horizon; PredispatchRecord keeps a region's history and its own
observation log under ``pd_`` keys, and the shared TradingIS service records
each settled half hour into it beside the PD7DAY store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()

(_const, _nem_time, _engine, _recorder, _history, _obslog, _pdc, _pdh, _pdr, _store_mod, _tis, _service) = load_chain(
    "const", "nem_time", "calibration_engine", "actual_recorder", "forecast_history", "observation_log",
    "predispatch_client", "predispatch_history", "predispatch_record", "calibration_store",
    "tradingis_client", "actual_price_service",
)

_DIR = Path(__file__).parent / "fixtures" / "local_price"
PD_NAME = "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.zip"
PD = (_DIR / "PUBLIC_PREDISPATCHIS_202610311330_20261031130240.CSV").read_text()


def _at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=NEM_TZ)


class _Stores:
    """Dict-backed HA stores, one dict per test, recording the keys used."""

    def __init__(self):
        self.data: dict = {}

    def factory(self, key):
        stores = self

        class _S:
            async def async_load(self):
                return stores.data.get(key)

            async def async_save(self, data):
                stores.data[key] = data

            async def async_remove(self):
                stores.data.pop(key, None)

        return _S()


def _record(region="QLD1", stores=None):
    stores = stores or _Stores()
    log = _obslog.ObservationLog(None, region, store_factory=stores.factory, key_prefix=_const.PD_KEY_PREFIX)
    record = _pdr.PredispatchRecord(
        MagicMock(), region, history_store=stores.factory(_const.pd_forecast_history_key(region)), log=log,
    )
    run_async(record.async_load())
    return record, stores


def _snapshot(run_at, prices, source="pd.zip"):
    return SimpleNamespace(run_at=run_at, source=source, prices=prices)


# ── Thinning ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("horizon_h, every", [(0.0, 1), (5.99, 1), (6.0, 2), (11.9, 2), (12.0, 4), (23.9, 4), (24.0, 8), (39.0, 8)])
def test_runs_are_kept_more_sparsely_further_ahead(horizon_h, every):
    kept = [slot for slot in range(64) if _pdh.keep(horizon_h, slot)]
    assert kept == list(range(0, 64, every))


def test_a_negative_horizon_is_never_kept():
    assert not any(_pdh.keep(-0.01, slot) for slot in range(16))


def test_an_interval_forecast_to_40_hours_ahead_keeps_28_runs():
    """12 within 6 h, 6 of 12 from 6 to 12 h, 6 of 24 from 12 to 24 h, 4 of 32 from 24 to 40 h."""
    target = _at(31, 12)
    kept = 0
    for k in range(80):  # runs every half hour, from 40 h before the interval up to it
        run_at = target - timedelta(minutes=30 * (k + 1)) + timedelta(seconds=103)
        horizon = (target - run_at).total_seconds() / 3600
        kept += _pdh.keep(horizon, _pdh.run_slot(run_at))
    assert kept == 28


def test_the_slot_is_the_run_time_floored_to_the_half_hour():
    assert _pdh.run_slot(_at(31, 13, 2)) == _pdh.run_slot(_at(31, 13, 29)) == _pdh.run_slot(_at(31, 13))
    assert _pdh.run_slot(_at(31, 13, 30)) == _pdh.run_slot(_at(31, 13)) + 1


# ── A real run ───────────────────────────────────────────────────────────────

def test_a_real_run_becomes_periods_at_their_start_in_kwh():
    snapshot = _pdc.parse_predispatch(PD, PD_NAME)
    prices = snapshot.prices["QLD1"]
    forecast = _pdh.run_forecast(snapshot, "QLD1")
    slot = _pdh.run_slot(snapshot.run_at)
    want = []
    for end, rrp in sorted(prices.items()):
        start = end - timedelta(minutes=30)
        h = (start - snapshot.run_at).total_seconds() / 3600
        if h >= 0 and _pdh.keep(h, slot):
            want.append((_nem_time.to_nem_iso(start), round(rrp / 1000, 6)))
    assert [(p.time, p.value) for p in forecast.forecast] == want
    first_start = min(prices) - timedelta(minutes=30)
    assert first_start < snapshot.run_at, "the run is solved after its first period starts"
    assert _nem_time.to_nem_iso(first_start) not in {p.time for p in forecast.forecast}
    assert all(p.time.endswith("+10:00") for p in forecast.forecast)


# ── The record ───────────────────────────────────────────────────────────────

def test_a_run_is_ingested_once_and_kept_under_pd_keys():
    record, stores = _record()
    run = _snapshot(_at(31, 13, 2), {"QLD1": {_at(31, 14): 90.0, _at(31, 14, 30): 110.0}, "NSW1": {_at(31, 14): 1.0}})
    assert run_async(record.async_ingest(run, now=_at(31, 13, 5))) == 2
    assert run_async(record.async_ingest(run, now=_at(31, 13, 5))) == 0
    history = stores.data[_const.pd_forecast_history_key("QLD1")]["forecast_history"]
    assert set(history) == {"2026-10-31T13:30:00+10:00", "2026-10-31T14:00:00+10:00"}
    entry = history["2026-10-31T13:30:00+10:00"][0]
    assert entry["forecast_price"] == 0.09 and entry["region"] == "QLD1"
    assert entry["run_at"] == "2026-10-31T13:02:00+10:00"
    assert _const.pd_forecast_history_key("QLD1") == "nem_pd7day.qld1.pd_forecast_history"


def test_an_actual_pairs_with_each_run_that_forecast_it():
    record, stores = _record()
    for minute in (2, 32):
        run_at = _at(31, 13, minute)
        run_async(record.async_ingest(_snapshot(run_at, {"QLD1": {_at(31, 14, 30): 100.0 + minute}}), now=run_at))
    assert run_async(record.async_record_actual("2026-10-31T14:00:00+10:00", 0.0975, source="tradingis")) == 2
    obs = record._log.observations
    assert [(o["forecast_run_at"], o["pd7day_forecast"], o["horizon_hours"]) for o in obs] == [
        ("2026-10-31T13:02:00+10:00", 0.102, 0.97), ("2026-10-31T13:32:00+10:00", 0.132, 0.47),
    ]
    assert all(o["actual_rrp"] == 0.0975 and o["actual_source"] == "tradingis" for o in obs)
    assert "nem_pd7day.qld1.pd_observations.2026-10-31" in stores.data
    assert "nem_pd7day.qld1.pd_observation_segments" in stores.data
    assert not any(k.startswith("nem_pd7day.qld1.observations") for k in stores.data), "PD7DAY keys untouched"


def test_the_record_survives_a_restart():
    record, stores = _record()
    run_async(record.async_ingest(_snapshot(_at(31, 13, 2), {"QLD1": {_at(31, 14, 30): 100.0}}), now=_at(31, 13, 2)))
    run_async(record.async_record_actual("2026-10-31T14:00:00+10:00", 0.1, source="tradingis"))
    again, _ = _record(stores=stores)
    assert len(again._log.observations) == 1
    # A second reading of the same pair updates it, as for PD7DAY, rather than adding a row.
    assert run_async(again.async_record_actual("2026-10-31T14:00:00+10:00", 0.2, source="tradingis")) == 1
    assert len(again._log.observations) == 1


def test_the_history_is_pruned_to_a_few_days():
    record, stores = _record()
    run_async(record.async_ingest(_snapshot(_at(20, 13, 2), {"QLD1": {_at(20, 14): 1.0}}), now=_at(20, 13, 2)))
    run_async(record.async_ingest(_snapshot(_at(31, 13, 2), {"QLD1": {_at(31, 14): 1.0}}), now=_at(31, 13, 2)))
    assert list(stores.data[_const.pd_forecast_history_key("QLD1")]["forecast_history"]) == ["2026-10-31T13:30:00+10:00"]


def test_the_cap_is_the_pre_dispatch_one():
    record, _ = _record()
    record_actual = MagicMock(return_value=0)
    with patch.object(_pdr, "record_actual", record_actual):
        run_async(record.async_record_actual("2026-10-31T14:00:00+10:00", 0.1))
    assert record_actual.call_args.kwargs["max_total"] == _const.MAX_TOTAL_OBS_PD
    assert record_actual.call_args.kwargs["calibration_region"] == "QLD1"


def test_stats_count_and_date_the_observations():
    record, _ = _record()
    assert record.stats(now=_at(31, 15)) == {"pd_observations": 0, "pd_history_days": None}
    run_async(record.async_ingest(_snapshot(_at(30, 13, 2), {"QLD1": {_at(30, 14, 30): 100.0}}), now=_at(30, 13, 2)))
    run_async(record.async_record_actual("2026-10-30T14:00:00+10:00", 0.1))
    assert record.stats(now=_at(31, 14)) == {"pd_observations": 1, "pd_history_days": 1.0}


def test_it_listens_to_the_coordinator_and_ingests_what_it_holds():
    record, _ = _record()
    tasks = []
    record._hass = SimpleNamespace(async_create_task=lambda coro: tasks.append(coro))
    listeners = []
    coord = SimpleNamespace(latest=_snapshot(_at(31, 13, 2), {}),
                            async_add_listener=lambda cb: listeners.append(cb) or (lambda: None))
    record.listen(coord)
    assert len(tasks) == 1 and len(listeners) == 1
    listeners[0]()
    assert len(tasks) == 2
    for task in tasks:
        task.close()


def test_pd7day_observation_keys_are_unchanged():
    assert _const.observation_manifest_key("QLD1") == "nem_pd7day.qld1.observation_segments"
    assert _const.observation_segment_key("QLD1", "2026-10-31") == "nem_pd7day.qld1.observations.2026-10-31"


# ── The TradingIS service records into both ──────────────────────────────────

def test_the_tradingis_service_records_into_each_store_of_an_entry():
    service = _service.ActualPriceService(MagicMock(), MagicMock())
    service._client = MagicMock()
    service._client.fetch_interval_prices = AsyncMock(return_value={"QLD1": 0.0975})
    pd7day, pd = MagicMock(), MagicMock()
    pd7day.async_record_actual = AsyncMock(return_value=1)
    pd.async_record_actual = AsyncMock(return_value=1)
    with patch.object(_service, "async_track_utc_time_change", MagicMock()):
        service.register("e1", "QLD1", pd7day, pd, None)
    run_async(service._on_tradingis_tick(datetime(2026, 10, 31, 4, 32, tzinfo=timezone.utc)))
    for store in (pd7day, pd):
        store.async_record_actual.assert_awaited_once_with(
            "2026-10-31T14:00:00+10:00", 0.0975, calibration_region="QLD1", source="tradingis",
        )


def test_setup_builds_the_record_before_the_platforms_subscribe_it():
    """v3.42.0 built it after _forward_platforms, so the sensor setup saw None:
    no run was ingested and the constraints sensor showed no pd_ attributes."""
    src = (Path(__file__).parents[1] / "custom_components" / "nem_pd7day" / "__init__.py").read_text()
    built = src.index("pd_record = PredispatchRecord(hass, region)")
    assert built < src.index("entry.runtime_data = NemPd7dayEntryData(") < src.index("await _forward_platforms(hass, entry, region)")
    assert "pd_record=pd_record," in src
