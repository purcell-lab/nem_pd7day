"""
ForecastStore (forecast_store.py): the per region cache of the last
PD7DayResult that lets a restart publish the previous run before the first
fetch completes.

Covers the PD7DayResult serialise -> save -> load round trip over every nested
dataclass, optional None fields, staleness (updated_at older than
_CACHE_MAX_AGE_S loads as None), the first-install and corrupt-cache paths and
per region key isolation.

The two-phase startup branch in __init__.async_setup_entry (cache hit ->
async_set_updated_data plus a staggered background refresh, otherwise
async_config_entry_first_refresh) is not exercised here: __init__ cannot be
loaded without Home Assistant, and the tests that used to claim it drove a
local copy of that branch. What the branch keys on, load() returning None or a
result, is what the staleness tests assert.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from support import (
    NEM_TZ,
    install_ha_stubs,
    load_chain,
    make_pd7day_data,
    make_real_price_period,
    nem_iso,
    run_async,
)

install_ha_stubs()


class _FakeStore:
    """HA Store stand-in backed by a class dict keyed by storage key.

    support.FakeStore loads None; this one lets a second ForecastStore read
    what the first saved, which is the restart the module exists for.
    """

    _backing: dict[str, dict] = {}

    def __init__(self, hass, version, key):
        self._key = key

    async def async_load(self):
        return _FakeStore._backing.get(self._key)

    async def async_save(self, data):
        _FakeStore._backing[self._key] = data


sys.modules["homeassistant.helpers.storage"].Store = _FakeStore

_const_mod, _nem_time, _client_mod, _fs_mod = load_chain(
    "const", "nem_time", "pd7day_client", "forecast_store"
)

ForecastStore = _fs_mod.ForecastStore
_CACHE_MAX_AGE_S = _fs_mod._CACHE_MAX_AGE_S
CaseSolutionData = _client_mod.CaseSolutionData
CheapestWindow = _client_mod.CheapestWindow
GasForecastPeriod = _client_mod.GasForecastPeriod
InterconnectorData = _client_mod.InterconnectorData
InterconnectorPeriod = _client_mod.InterconnectorPeriod
MarketSummaryData = _client_mod.MarketSummaryData
PD7DayResult = _client_mod.PD7DayResult


def _make_result(updated_at: str) -> PD7DayResult:
    """A fully populated PD7DayResult covering every nested dataclass."""
    base = datetime(2026, 4, 15, 14, 0, tzinfo=NEM_TZ)
    periods = [
        make_real_price_period(_client_mod, base + timedelta(minutes=30 * i), round(0.10 + 0.01 * i, 6))
        for i in range(4)
    ]
    price = make_pd7day_data(
        _client_mod, base, periods,
        min_24h_value=0.10,
        max_24h_value=0.13,
        cheapest_2h_window=CheapestWindow(
            start=periods[0].time,
            end=periods[3].time,
            nemtime_start=periods[0].nemtime,
            nemtime_end=periods[3].nemtime,
            avg_value=0.115,
            points=4,
        ),
    )
    market = MarketSummaryData(
        run_datetime=nem_iso(base),
        forecast=[
            GasForecastPeriod(
                nemtime=nem_iso(base + timedelta(days=d)),
                time=nem_iso(base + timedelta(days=d) - timedelta(minutes=30)),
                value_tj=100.0 + d,
            )
            for d in range(2)
        ],
    )
    ic = InterconnectorData(
        interconnector_id="NSW1-QLD1",
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        run_datetime=nem_iso(base),
        forecast=[
            InterconnectorPeriod(
                nemtime=nem_iso(base),
                time=nem_iso(base - timedelta(minutes=30)),
                mwflow=120.0,
                meteredmwflow=119.0,
                mwlosses=2.0,
                marginalvalue=0.0,
                violationdegree=0.0,
                exportlimit=1000.0,
                importlimit=-1000.0,
                marginalloss=0.05,
            )
        ],
    )
    return PD7DayResult(
        source_file="PUBLIC_PD7DAY_20260415.ZIP",
        case=CaseSolutionData(
            run_datetime=nem_iso(base),
            intervention=False,
            last_changed=nem_iso(base),
        ),
        prices={"QLD1": price},
        market_summary=market,
        interconnectors={"NSW1-QLD1": ic},
        updated_at=updated_at,
    )


def _fresh_iso() -> str:
    return nem_iso(datetime.now(NEM_TZ) - timedelta(minutes=5))


def _stale_iso() -> str:
    return nem_iso(datetime.now(NEM_TZ) - timedelta(seconds=_CACHE_MAX_AGE_S + 120))


def _new_store(region="QLD1") -> ForecastStore:
    _FakeStore._backing.clear()
    return ForecastStore(MagicMock(), region)


# ── Round trip ───────────────────────────────────────────────────────────────


def test_save_load_round_trip_preserves_tree():
    """A fresh cache restores every nested dataclass, field for field."""
    store = _new_store()
    original = _make_result(_fresh_iso())
    run_async(store.save(original))
    restored = run_async(store.load())

    assert restored is not None
    assert restored.source_file == original.source_file
    assert restored.updated_at == original.updated_at
    # CaseSolutionData
    assert restored.case.intervention is False
    assert restored.case.run_datetime == original.case.run_datetime
    # PD7DayData + nested PricePeriod / CheapestWindow
    rp = restored.prices["QLD1"]
    op = original.prices["QLD1"]
    assert rp.region == "QLD1"
    assert rp.current_value == op.current_value
    assert len(rp.forecast) == len(op.forecast)
    assert rp.forecast[0].value == op.forecast[0].value
    assert rp.forecast[0].nemtime == op.forecast[0].nemtime
    assert rp.cheapest_2h_window.avg_value == op.cheapest_2h_window.avg_value
    assert rp.cheapest_2h_window.points == 4
    # MarketSummaryData + GasForecastPeriod
    assert len(restored.market_summary.forecast) == 2
    assert restored.market_summary.forecast[0].value_tj == 100.0
    # InterconnectorData + InterconnectorPeriod
    ric = restored.interconnectors["NSW1-QLD1"]
    assert ric.interconnector_id == "NSW1-QLD1"
    assert ric.forecast[0].mwflow == 120.0
    assert ric.forecast[0].marginalloss == 0.05


def test_round_trip_handles_optional_none_fields():
    """Optional fields (next_value, cheapest window, case, market_summary) = None."""
    store = _new_store()
    minimal = PD7DayResult(
        source_file="PUBLIC_PD7DAY_X.ZIP",
        case=None,
        prices={
            "QLD1": make_pd7day_data(
                _client_mod, None, [], source_file="PUBLIC_PD7DAY_X.ZIP", current_value=0.05
            )
        },
        market_summary=None,
        interconnectors={},
        updated_at=_fresh_iso(),
    )
    run_async(store.save(minimal))
    restored = run_async(store.load())

    assert restored is not None
    assert restored.case is None
    assert restored.market_summary is None
    assert restored.interconnectors == {}
    assert restored.prices["QLD1"].next_value is None
    assert restored.prices["QLD1"].cheapest_2h_window is None
    assert restored.prices["QLD1"].forecast == []


# ── Staleness and the empty paths ────────────────────────────────────────────


def test_load_returns_none_when_stale():
    """updated_at older than _CACHE_MAX_AGE_S (35 minutes) loads as None."""
    store = _new_store()
    run_async(store.save(_make_result(_stale_iso())))
    assert run_async(store.load()) is None


def test_load_returns_none_when_no_cache():
    """Empty backing store: first install."""
    store = _new_store()
    assert run_async(store.load()) is None


def test_load_returns_none_when_updated_at_missing():
    """A corrupt or legacy payload without updated_at is not served."""
    store = _new_store()
    _FakeStore._backing["nem_pd7day.forecast.qld1"] = {"source_file": "x", "prices": {}}
    assert run_async(store.load()) is None


def test_per_region_keys_are_isolated():
    """Two regions use distinct storage keys."""
    _FakeStore._backing.clear()
    qld = ForecastStore(MagicMock(), "QLD1")
    nsw = ForecastStore(MagicMock(), "NSW1")
    run_async(qld.save(_make_result(_fresh_iso())))
    assert run_async(nsw.load()) is None
    assert run_async(qld.load()) is not None
