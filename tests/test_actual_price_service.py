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

from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

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


REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]
PRICES = {"QLD1": 0.09769, "NSW1": 0.0812, "VIC1": 0.0455, "SA1": -0.012, "TAS1": 0.0901}


def make_service(prices=None):
    """An ActualPriceService with a mocked TradingIS client returning ``prices``."""
    service = _service_mod.ActualPriceService(MagicMock(), MagicMock())
    service._client = MagicMock()
    service._client.fetch_interval_prices = AsyncMock(return_value=dict(PRICES if prices is None else prices))
    return service


def registered_store(service, region="QLD1", entry_id=None):
    store = make_store()
    store.async_record_actual = AsyncMock(return_value=1)
    with patch.object(_service_mod, "async_track_utc_time_change", MagicMock(return_value=MagicMock())):
        service.register(entry_id or f"entry_{region}", region, store)
    return store


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
    service = make_service()
    store = registered_store(service)

    run_async(service._on_tradingis_tick(now_utc))

    store.async_record_actual.assert_called_once_with(
        interval_iso, PRICES["QLD1"], calibration_region="QLD1", source="tradingis",
    )
    (start,), _ = service._client.fetch_interval_prices.call_args
    assert _nem_time.to_nem_iso(start) == interval_iso


def test_tick_records_nothing_when_tradingis_has_no_price():
    service = make_service(prices={})
    store = registered_store(service)
    run_async(service._on_tradingis_tick(datetime(2026, 4, 18, 7, 32, tzinfo=timezone.utc)))
    store.async_record_actual.assert_not_called()


def test_five_entries_share_one_fetch_and_record_their_own_region():
    """#335: five entries used to make five listings and five downloads of one file."""
    service = make_service()
    stores = {region: registered_store(service, region) for region in REGIONS}

    run_async(service._on_tradingis_tick(datetime(2026, 4, 18, 7, 32, tzinfo=timezone.utc)))

    service._client.fetch_interval_prices.assert_awaited_once()
    for region, store in stores.items():
        store.async_record_actual.assert_called_once_with(
            "2026-04-18T17:00:00+10:00", PRICES[region], calibration_region=region, source="tradingis",
        )


def test_a_tick_with_no_entry_fetches_nothing():
    service = make_service()
    run_async(service._on_tradingis_tick(datetime(2026, 4, 18, 7, 32, tzinfo=timezone.utc)))
    service._client.fetch_interval_prices.assert_not_called()


def test_the_interval_is_worked_out_in_nem_time_whatever_the_tick_zone():
    """The old timer ran on Home Assistant's local time; the interval itself was
    always NEM. Any aware time gives the same NEM interval."""
    adelaide = timezone(timedelta(hours=10, minutes=30))
    at = datetime(2026, 4, 18, 18, 2, tzinfo=adelaide)  # 17:32 NEM
    assert _service_mod.closed_interval_start(at) == datetime(2026, 4, 18, 17, 0, tzinfo=_nem_time.NEM_TZ)


# ── One timer, on UTC minutes, for the life of the entries ───────────────────


def test_one_utc_timer_starts_with_the_first_entry_and_stops_with_the_last():
    service = make_service()
    cancel = MagicMock()
    track = MagicMock(return_value=cancel)
    with patch.object(_service_mod, "async_track_utc_time_change", track):
        stop_qld = service.register("e1", "QLD1", make_store())
        stop_nsw = service.register("e2", "NSW1", make_store())
    track.assert_called_once()
    assert track.call_args.kwargs == {"minute": _const_mod.TRADINGIS_FETCH_MINUTES, "second": 0}
    assert service.regions == ["NSW1", "QLD1"]
    stop_qld()
    cancel.assert_not_called()
    stop_nsw()
    cancel.assert_called_once()
    assert service.regions == []


def test_the_service_is_shared_across_entries():
    hass = MagicMock()
    hass.data = {_const_mod.DOMAIN: {}}
    first = _service_mod.shared_actual_price_service(hass, MagicMock())
    assert _service_mod.shared_actual_price_service(hass, MagicMock()) is first
    assert hass.data[_const_mod.DOMAIN][_const_mod.SHARED_TRADINGIS_KEY] is first


def test_five_entries_make_one_listing_and_one_download_end_to_end():
    """Through the real client: two NEMWEB requests a half hour for five entries, not ten."""
    end = datetime(2026, 4, 18, 17, 30, tzinfo=_nem_time.NEM_TZ)
    name = f"PUBLIC_TRADINGIS_{end:%Y%m%d%H%M}_1.zip"
    rows = "".join(
        f'D,TRADING,PRICE,3,"2026/04/18 17:30:00",1,{r},35,{PRICES[r] * 1000:.2f},0,0,"x",FIRM\n' for r in REGIONS
    )
    payload = support.make_zip(("I,TRADING,PRICE,3,x\n" + rows).encode(), "t.csv")
    log = []

    class _Response:
        def __init__(self, url):
            self.status, self._url = 200, url

        async def text(self):
            return f'<a href="{name}">{name}</a>'

        async def read(self):
            return payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    session = MagicMock()
    session.get = lambda url, **kw: (log.append(url), _Response(url))[1]
    service = _service_mod.ActualPriceService(MagicMock(), session)
    service._client._executor_job = None
    stores = {region: registered_store(service, region) for region in REGIONS}

    run_async(service._on_tradingis_tick(datetime(2026, 4, 18, 7, 32, tzinfo=timezone.utc)))

    assert log == [_const_mod.TRADINGIS_BASE_URL, _const_mod.TRADINGIS_BASE_URL + name]
    for region, store in stores.items():
        (iso, price), _kw = store.async_record_actual.call_args
        assert price == pytest.approx(PRICES[region])


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
