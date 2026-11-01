"""Tariff sensors on the price timeline (#339 stage 5b).

The tariff ``forecast`` is the region's price timeline from now, priced: the
dispatch price, P5MIN's 5-minute prices, pre-dispatch, then the calibrated
PD7DAY spot. Each price is computed once per interval while the calibrated
forecast and the fee hold. A write that finds the calibrated forecast memo
stale waits for the region's warm rather than calibrate on the event loop.
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import support
from support import NEM_TZ, install_ha_stubs, load_chain, make_price_period, nem_iso

install_ha_stubs()

(_const_mod, _nem_time, _tf_mod, _tariff_mod) = load_chain("const", "nem_time", "tariff_forecast", "tariff_sensor")

DOMAIN = _const_mod.DOMAIN
REGION = "QLD1"
RUN = datetime(2026, 9, 2, 4, 0, tzinfo=NEM_TZ)


def _forecast(n=12):
    """PD7DAY half hours from the run: 04:00-04:30 first."""
    return [make_price_period(RUN + timedelta(minutes=30 * (i + 1)), value=round(0.10 + i / 100, 6)) for i in range(n)]


def _sensor(cls=None, periods=None, dispatch=None, p5=None):
    cls = cls or _tariff_mod.NemPd7dayTariffSensor
    coordinator = MagicMock()
    price_data = MagicMock()
    price_data.forecast = periods if periods is not None else _forecast()
    price_data.forecast_generated_at = nem_iso(RUN)
    price_data.source_file = "pd7day.zip"
    coordinator.data = MagicMock()
    coordinator.data.prices = {REGION: price_data}
    coordinator.last_update_success = True
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator, store=None, dispatch=dispatch)
    sensor = cls.__new__(cls)
    sensor.coordinator = coordinator
    sensor._region = REGION
    sensor._distributor = "energex"
    sensor._tariff_code = "8400"
    sensor._entry = entry
    sensor._store = None
    sensor._attr_unique_id = "entry_1_QLD1_energex_8400_tariff"
    sensor._attr_name = "energex 8400"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    if p5 is not None:
        sensor.hass.data[DOMAIN][_const_mod.SHARED_P5MIN_KEY] = types.SimpleNamespace(latest=p5)
    sensor.hass.states.get.return_value = None
    return sensor


def _dispatch(end, rrp_kwh):
    prices = {REGION: types.SimpleNamespace(interval_datetime=end.strftime("%Y/%m/%d %H:%M:%S"), rrp=rrp_kwh)}
    return types.SimpleNamespace(prices=prices)


def _p5(run_at, first_end, count, base=100.0):
    rrp = {REGION: {first_end + timedelta(minutes=5 * i): base + i for i in range(count)}}
    return types.SimpleNamespace(run_at=run_at, source="p5.zip", rrp=rrp)


# ── The service ──────────────────────────────────────────────────────────────

def _entries():
    return [
        {"time": "2026-09-02T04:00:00+10:00", "nemtime": "2026-09-02T04:05:00+10:00", "raw_value": 0.09,
         "value": 0.09, "source": "dispatch", "resolution_minutes": 5},
        {"time": "2026-09-02T04:05:00+10:00", "nemtime": "2026-09-02T04:30:00+10:00", "raw_value": 0.2,
         "value": 0.15, "source": "pd7day", "resolution_minutes": 25},
        {"time": "2026-09-02T04:30:00+10:00", "nemtime": "2026-09-02T05:00:00+10:00", "raw_value": 0.3,
         "value": None, "source": "pd7day", "resolution_minutes": 30},
    ]


def test_tariff_entries_keep_their_keys_and_say_their_source():
    calls = []

    def price(interval, spot):
        calls.append((interval.nemtime, spot))
        return round(spot + 0.1, 6)

    out = _tf_mod.tariff_entries(_entries(), price, lambda i: ("Peak", 0.25), _tf_mod.PriceCache())
    assert [list(e) for e in out] == [[
        "time", "nemtime", "spot_raw", "spot", "value", "period", "network_rate", "source", "resolution_minutes",
    ]] * 3
    assert out[0] == {"time": "2026-09-02T04:00:00+10:00", "nemtime": "2026-09-02T04:05:00+10:00", "spot_raw": 0.09,
                      "spot": 0.09, "value": 0.19, "period": "Peak", "network_rate": 0.25,
                      "source": "dispatch", "resolution_minutes": 5}
    assert out[1]["spot_raw"] == 0.2 and out[1]["spot"] == 0.15 and out[1]["value"] == 0.25
    assert out[2]["value"] is None and out[2]["spot"] is None, "an uncalibrated interval is not priced"
    assert len(calls) == 2


def test_each_interval_is_priced_once_while_the_generation_holds():
    cache = _tf_mod.PriceCache()
    calls = []

    def price(interval, spot):
        calls.append(interval.nemtime)
        return spot

    lookups = []

    def lookup(interval):
        lookups.append(interval.nemtime)
        return None, None

    for _ in range(3):
        cache.renew(("calibrated", 0.0))
        _tf_mod.tariff_entries(_entries(), price, lookup, cache)
    assert len(calls) == 2 and len(lookups) == 3
    cache.renew(("calibrated", 0.05))  # the usage fee moved
    _tf_mod.tariff_entries(_entries(), price, lookup, cache)
    assert len(calls) == 4


# ── The sensor ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cls", [_tariff_mod.NemPd7dayTariffSensor, _tariff_mod.NemPd7dayExportTariffSensor],
                         ids=["import", "export"])
def test_the_tariff_forecast_is_the_timeline_from_now(monkeypatch, cls):
    now = RUN + timedelta(minutes=62)  # 05:02: the 04:00 and 04:30 half hours have ended
    support.freeze_timeline_clock(monkeypatch, now)
    sensor = _sensor(cls, dispatch=_dispatch(RUN + timedelta(minutes=65), 0.0951),
                     p5=_p5(RUN + timedelta(minutes=60), RUN + timedelta(minutes=65), 12))
    if cls is _tariff_mod.NemPd7dayExportTariffSensor:
        sensor._import_code = "8400"
        sensor._export_code = "8400"
    forecast = sensor.extra_state_attributes["forecast"]

    assert forecast[0]["time"] == "2026-09-02T05:00:00+10:00", "ended intervals are dropped"
    assert [e["source"] for e in forecast[:2]] == ["dispatch", "p5min"]
    assert forecast[0]["spot"] == 0.0951 and forecast[0]["resolution_minutes"] == 5
    assert forecast[1]["spot"] == 0.101  # the 05:05-05:10 P5MIN interval, 101 $/MWh
    p5 = [e for e in forecast if e["source"] == "p5min"]
    assert p5[-1]["nemtime"] == "2026-09-02T06:00:00+10:00"
    pd7 = [e for e in forecast if e["source"] == "pd7day"]
    assert pd7[0]["time"] == "2026-09-02T06:00:00+10:00" and pd7[0]["spot"] == 0.14
    for e in forecast:
        interval = _tf_mod.Interval(e["time"], e["nemtime"], e["spot_raw"])
        assert e["value"] == sensor._price_period(interval, calibrated=e["spot"]), e


def test_the_day27_tariff_forecast_starts_at_the_cutoff(monkeypatch):
    support.freeze_timeline_clock(monkeypatch, RUN + timedelta(minutes=1))
    sensor = _sensor(_tariff_mod.TariffForecastDays27Sensor)
    cutoff = RUN + timedelta(hours=3)
    monkeypatch.setattr(_tariff_mod, "day27_start_for", lambda *a: _nem_time.Day27Start(cutoff, "clock"))
    forecast = sensor.extra_state_attributes["forecast"]
    assert forecast[0]["time"] == "2026-09-02T07:30:00+10:00"  # the first half hour starting after 07:00
    assert {e["source"] for e in forecast} == {"pd7day"}


# ── Writing once the memo is current ─────────────────────────────────────────

class _View:
    def __init__(self, current):
        self.current = current
        self.waits = 0

    def is_current(self, d):
        return self.current

    async def wait_current(self, price_data_now, timeout):
        self.waits += 1
        await asyncio.sleep(0)
        self.current = True
        return True


def _writing_sensor(monkeypatch, current):
    sensor = _sensor()
    view = _View(current)
    monkeypatch.setattr(type(sensor), "_calibrated_view", lambda self: view)
    sensor.async_write_ha_state = MagicMock()
    tasks = []
    sensor.entity_id = "sensor.tariff"
    sensor.hass.async_create_background_task = (
        lambda coro, name: tasks.append(asyncio.ensure_future(coro, loop=loop)) or tasks[-1]
    )
    loop = asyncio.new_event_loop()
    return sensor, view, tasks, loop


def test_a_current_memo_writes_at_once(monkeypatch):
    sensor, view, tasks, loop = _writing_sensor(monkeypatch, current=True)
    sensor._write_when_calibrated()
    sensor.async_write_ha_state.assert_called_once()
    assert tasks == [] and view.waits == 0
    loop.close()


def test_a_stale_memo_waits_for_the_warm_then_writes_once(monkeypatch):
    sensor, view, tasks, loop = _writing_sensor(monkeypatch, current=False)
    sensor._write_when_calibrated()
    sensor._write_when_calibrated()  # a second trigger while waiting does not start another wait
    sensor.async_write_ha_state.assert_not_called()
    assert len(tasks) == 1
    loop.run_until_complete(tasks[0])
    loop.close()
    sensor.async_write_ha_state.assert_called_once()
    assert view.waits == 1 and sensor._awaiting_memo is False


def test_a_new_run_from_the_coordinator_goes_through_the_wait(monkeypatch):
    sensor, view, tasks, loop = _writing_sensor(monkeypatch, current=False)
    sensor._handle_coordinator_update()
    assert len(tasks) == 1
    loop.run_until_complete(tasks[0])
    loop.close()
    sensor.async_write_ha_state.assert_called_once()


def test_past_the_wait_a_tariff_still_prices_from_the_spot_view(monkeypatch):
    """No warm came (every forecast sensor disabled): the spot view calibrates, as before v3.46.0."""
    support.freeze_timeline_clock(monkeypatch, support.BEFORE_ANY_DATA)
    sensor = _sensor()
    forecast = sensor.extra_state_attributes["forecast"]
    assert len(forecast) == 12 and forecast[0]["spot"] == 0.10 and forecast[0]["source"] == "pd7day"


def test_a_current_memo_means_no_spot_map(monkeypatch):
    """The spot view is only the fallback (#340): with the memo current, a tariff never builds it."""
    support.freeze_timeline_clock(monkeypatch, support.BEFORE_ANY_DATA)
    sensor = _sensor()
    sensor.coordinator._calibrated_forecast_cache = {}  # the memo slot the forecast sensors fill
    view = sensor._calibrated_view()
    calibrated = [{"time": p.time, "nemtime": p.nemtime, "raw_value": p.value, "value": p.value}
                  for p in sensor._price_data.forecast]
    view.publish(view.key(sensor._price_data), calibrated)
    monkeypatch.setattr(type(sensor), "_calibrated_spot_map", lambda self, d: pytest.fail("spot map built"))
    assert len(sensor.extra_state_attributes["forecast"]) == 12
