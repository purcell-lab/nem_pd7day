"""
ActualPriceService (actual_price_service.py): the TradingIS tick that records
the just-closed 30 minute interval's actual price into the calibration store.

Ticks fire at HH:02 and HH:32 (TRADINGIS_FETCH_MINUTES), two minutes after
each boundary so AEMO has published the last five minute dispatch file, and
the interval recorded is the one that closed at the boundary before the tick.
Home Assistant supplies the tick time in UTC. The recorded observation carries
actual_source so the store can tell TradingIS actuals from other sources.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import partial
from unittest.mock import AsyncMock, MagicMock

import pytest

import support
from support import install_ha_stubs, load_chain, run_async

install_ha_stubs()
_const_mod, _nem_time, _engine_mod, _store_mod, _tradingis_mod, _service_mod = load_chain(
    "const", "nem_time", "calibration_engine", "calibration_store",
    "tradingis_client", "actual_price_service",
)

make_store = partial(support.make_store, _store_mod)

# A forecast history entry for 2026-04-18T17:00 NEM from the 13:00 run.
INTERVAL_ISO = "2026-04-18T17:00:00+10:00"
_HISTORY_ENTRY = {
    "run_at": "2026-04-18T13:00:00+10:00",
    "forecast_price": 0.108,
    "gas_tj": None,
    "qni_mwflow": None,
    "qni_violation": None,
    "is_intervention": False,
    "region": "QLD1",
}


def make_service(store=None, regions=None, fetch_price_return=None):
    """An ActualPriceService with a mocked TradingIS client."""
    hass = MagicMock()
    if store is None:
        store = make_store()
    if regions is None:
        regions = ["QLD1"]
    service = _service_mod.ActualPriceService(hass, store, regions, MagicMock())
    service._client = MagicMock()
    service._client.fetch_interval_price = AsyncMock(return_value=fetch_price_return)
    return service, store


# ── The tick ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "now_utc, interval_iso",
    [
        (datetime(2026, 4, 18, 7, 32, tzinfo=timezone.utc), "2026-04-18T17:00:00+10:00"),  # 17:32 NEM
        (datetime(2026, 4, 18, 8, 2, tzinfo=timezone.utc), "2026-04-18T17:30:00+10:00"),   # 18:02 NEM
    ],
    ids=["HH:32", "HH:02"],
)
def test_tick_records_the_just_closed_interval_from_tradingis(now_utc, interval_iso):
    """At HH:32 the interval is HH:00-HH:30; at HH:02 it is (HH-1):30-HH:00."""
    service, store = make_service(fetch_price_return=0.09769)
    store.async_record_actual = AsyncMock(return_value=1)

    run_async(service._on_tradingis_tick(now_utc))

    store.async_record_actual.assert_called_once()
    args, kwargs = store.async_record_actual.call_args
    assert args[0] == interval_iso
    assert abs(args[1] - 0.09769) < 1e-9
    source = kwargs.get("source", args[2] if len(args) > 2 else None)
    assert source == "tradingis"


def test_tick_records_nothing_when_tradingis_has_no_price():
    service, store = make_service(fetch_price_return=None)
    store.async_record_actual = AsyncMock(return_value=0)

    run_async(service._on_tradingis_tick(datetime(2026, 4, 18, 7, 32, tzinfo=timezone.utc)))

    store.async_record_actual.assert_not_called()


def test_tick_fetches_every_configured_region():
    service, store = make_service(regions=["QLD1", "NSW1"], fetch_price_return=0.09769)
    store.async_record_actual = AsyncMock(return_value=1)

    run_async(service._on_tradingis_tick(datetime(2026, 4, 18, 7, 32, tzinfo=timezone.utc)))

    assert store.async_record_actual.call_count == 2


# ── The source field on the recorded observation ─────────────────────────────


@pytest.mark.parametrize(
    "kwargs, expected_source",
    [({"source": "tradingis"}, "tradingis"), ({}, "unknown")],
    ids=["tradingis", "default"],
)
def test_record_actual_stamps_the_observation_with_its_source(kwargs, expected_source):
    """End to end through a real CalibrationStore; no source means "unknown"."""
    store = make_store()
    store._forecast_history[INTERVAL_ISO] = [dict(_HISTORY_ENTRY)]

    assert run_async(store.async_record_actual(INTERVAL_ISO, 0.09769, **kwargs)) >= 1

    assert len(store._observations) >= 1
    assert store._observations[0]["actual_source"] == expected_source
