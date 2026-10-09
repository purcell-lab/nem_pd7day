"""
The day 2-7 series starts where the Amber forecast it is summed with ends (#235).

HAEO sums an Amber forecast with the day 2-7 series and reads either as 0
outside its span. Started by the clock alone (nem_time._amber_express_cutoff),
the day 2-7 series left a hole after Amber's last interval: 30 minutes on most
refreshes (8 Oct, 14:15: Amber covered to 14:00 the next day, day 2-7 began at
14:30) and about 9 hours just after 12:30 NEM, before Amber extends past
tomorrow 04:00.

The attribute shapes below are the live ones from 8 and 9 October 2026:
Amber Electric (core, ``forecasts`` with ``end_time``) and Amber Express
(``forecast`` with interval starts, ``detailedForecast`` with ``end_time``).

Run with:  python -m pytest tests/test_amber_forecast.py -v
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, make_price_period, nem_iso

install_ha_stubs()

(
    _nem_time,
    _engine_mod,
    _client_mod,
    _const_mod,
    _store_mod,
    _dispatch_mod,
    _coord_mod,
    _stpasa_client_mod,
    _amber_mod,
    _tariff_mod,
    _sensor_mod,
) = load_chain(
    "nem_time",
    "calibration_engine",
    "pd7day_client",
    "const",
    "calibration_store",
    "dispatch_client",
    "coordinator",
    "stpasa_client",
    "amber_forecast",
    "tariff_sensor",
    "sensor",
)

CONF_AMBER = _const_mod.CONF_AMBER_FORECAST_ENTITY
Day27Start = _nem_time.Day27Start
amber_coverage_end = _nem_time.amber_coverage_end
day27_start = _nem_time.day27_start


def _electric(last_start_utc: str, last_end_utc: str) -> dict:
    """Amber Electric (core): ``forecasts``, starts one second past the boundary."""
    return {
        "forecasts": [
            {"duration": 30, "start_time": "2026-10-09T03:00:01+00:00",
             "end_time": "2026-10-09T03:30:00+00:00", "per_kwh": 0.024},
            {"duration": 30, "start_time": last_start_utc, "end_time": last_end_utc,
             "nem_date": "2026-10-09T14:00:00+10:00", "per_kwh": 0.0263},
        ],
        "channel_type": "general",
    }


def _express(last_start_nem: str, detailed: bool = True) -> dict:
    """Amber Express: ``forecast`` lists interval starts; ``detailedForecast`` carries ends."""
    last = datetime.fromisoformat(last_start_nem)
    starts = [last - timedelta(minutes=30), last]
    attrs: dict = {
        "forecast": [{"time": nem_iso(t), "value": 0.04} for t in starts],
        "interpolation_mode": "previous",
    }
    if detailed:
        attrs["detailedForecast"] = [
            {"duration": 30, "start_time": nem_iso(t + timedelta(seconds=1)),
             "end_time": nem_iso(t + timedelta(minutes=30))}
            for t in starts
        ]
    return attrs


T_1418 = datetime(2026, 10, 8, 14, 18, tzinfo=NEM_TZ)
END_1400 = datetime(2026, 10, 9, 14, 0, tzinfo=NEM_TZ)


# ── Where Amber's coverage ends ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "attrs",
    [
        pytest.param(_electric("2026-10-09T03:30:01+00:00", "2026-10-09T04:00:00+00:00"), id="amber electric"),
        pytest.param(_express("2026-10-09T13:30:00+10:00"), id="amber express, detailed"),
        pytest.param(_express("2026-10-09T13:30:00+10:00", detailed=False), id="amber express, starts only"),
    ],
)
def test_both_integrations_end_where_their_last_interval_ends(attrs):
    """8 Oct 14:18: both integrations' last interval ran 13:30-14:00 on 9 Oct."""
    assert amber_coverage_end(attrs) == END_1400


@pytest.mark.parametrize(
    "attrs",
    [{}, {"forecasts": []}, {"forecast": "not a list"}, {"forecasts": [{"end_time": None}]},
     {"forecast": [{"time": "garbage"}]}],
)
def test_no_readable_forecast_has_no_end(attrs):
    assert amber_coverage_end(attrs) is None


# ── Where the day 2-7 series starts ──────────────────────────────────────────

def test_the_30_minute_hole_at_the_24h_edge_is_closed():
    """8 Oct 14:18: the clock started day 2-7 at 14:30, Amber ended at 14:00."""
    clock = day27_start(None, now=T_1418)
    joined = day27_start(_electric("2026-10-09T03:30:01+00:00", "2026-10-09T04:00:00+00:00"), now=T_1418)

    assert (joined.at, joined.source) == (END_1400, "amber")
    assert joined.includes(END_1400)                                  # no hole
    assert not joined.includes(END_1400 - timedelta(minutes=30))      # no overlap
    assert clock.source == "clock" and not clock.includes(END_1400)   # the old hole


def test_just_after_1230_the_series_starts_at_tomorrow_0400_not_24h_ahead():
    """12:31, before Amber extends: Amber still ends at 04:00 tomorrow. The clock
    rule had already jumped to now + 24 h, a 9-hour hole."""
    now = datetime(2026, 10, 8, 12, 31, tzinfo=NEM_TZ)
    amber = _express("2026-10-09T03:30:00+10:00")
    start = day27_start(amber, now=now)

    assert (start.at, start.source) == (datetime(2026, 10, 9, 4, 0, tzinfo=NEM_TZ), "amber")
    assert day27_start(None, now=now).at == now + timedelta(hours=24)


@pytest.mark.parametrize(
    "attrs",
    [
        pytest.param(None, id="no entity"),
        pytest.param({}, id="no forecast"),
        pytest.param(_express("2026-10-08T13:30:00+10:00"), id="coverage already over"),
    ],
)
def test_without_a_current_amber_forecast_the_clock_rule_applies(attrs):
    start = day27_start(attrs, now=T_1418)
    assert start == Day27Start(_nem_time._amber_express_cutoff(T_1418), "clock")


def test_the_clock_rule_keeps_its_exclusive_cutoff():
    cutoff = datetime(2026, 10, 9, 3, 30, tzinfo=NEM_TZ)
    clock = Day27Start(cutoff, "clock")
    assert not clock.includes(cutoff) and clock.includes(cutoff + timedelta(minutes=30))


# ── Reading the configured entity ────────────────────────────────────────────

def _hass_with(state):
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=state)
    return hass


def _entry(entity_id=None):
    return types.SimpleNamespace(options={} if entity_id is None else {CONF_AMBER: entity_id})


def test_unset_or_empty_option_reads_no_entity():
    hass = _hass_with(MagicMock())
    for entry in (_entry(), _entry(""), types.SimpleNamespace()):
        assert _amber_mod.amber_attributes(hass, entry) is None
    hass.states.get.assert_not_called()


def test_an_unavailable_or_missing_entity_gives_no_attributes():
    assert _amber_mod.amber_attributes(_hass_with(None), _entry("sensor.amber_general_forecast")) is None
    gone = types.SimpleNamespace(state="unavailable", attributes={"forecasts": []})
    assert _amber_mod.amber_attributes(_hass_with(gone), _entry("sensor.amber_general_forecast")) is None


# ── Re-writing when the start moves ──────────────────────────────────────────

def test_writes_follow_the_start_not_every_amber_update():
    """Amber updates every 5 minutes; the start moves about every 30 (#215)."""
    state = types.SimpleNamespace(state="0.11", attributes=_express("2026-10-09T13:30:00+10:00"))
    entity = types.SimpleNamespace(hass=_hass_with(state), async_on_remove=MagicMock())
    write = MagicMock()
    handlers = []

    def track(hass, entity_ids, handler):
        handlers.append((entity_ids, handler))
        return "unsub"

    now = T_1418
    # homeassistant.core is stubbed, so its callback decorator must pass through.
    with patch.object(_amber_mod, "async_track_state_change_event", track), \
            patch.object(_amber_mod, "callback", lambda fn: fn), \
            patch.object(_nem_time, "now_nem", lambda: now):
        _amber_mod.track_day27_start(entity, _entry("sensor.amber_express_amber_general_price"), write)
        (ids, handler), = handlers
        assert ids == ["sensor.amber_express_amber_general_price"]
        entity.async_on_remove.assert_called_once_with("unsub")

        handler(None)                                     # a price update, same coverage
        assert write.call_count == 0
        state.attributes = _express("2026-10-09T14:00:00+10:00")
        handler(None)                                     # coverage extended by one interval
        assert write.call_count == 1
        handler(None)
        assert write.call_count == 1


def test_no_option_subscribes_to_nothing():
    entity = types.SimpleNamespace(hass=MagicMock(), async_on_remove=MagicMock())
    with patch.object(_amber_mod, "async_track_state_change_event", side_effect=AssertionError):
        _amber_mod.track_day27_start(entity, _entry(), MagicMock())
    entity.async_on_remove.assert_not_called()


# ── The day 2-7 sensors ──────────────────────────────────────────────────────

def _spot_sensor(amber_state):
    sensor = _sensor_mod.SpotPriceForecastDays27Sensor.__new__(_sensor_mod.SpotPriceForecastDays27Sensor)
    sensor.coordinator = MagicMock()
    sensor._region = "QLD1"
    sensor._store = None
    sensor._entry = types.SimpleNamespace(
        options={CONF_AMBER: "sensor.amber_general_forecast"},
        runtime_data=types.SimpleNamespace(dispatch=None),
    )
    sensor.hass = _hass_with(amber_state)
    return sensor


def test_the_spot_sensor_starts_exactly_where_amber_ends():
    now = _nem_time.now_nem()
    boundary = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
    amber_end = boundary + timedelta(hours=23, minutes=30)
    amber = types.SimpleNamespace(state="0.11", attributes={"forecasts": [
        {"start_time": nem_iso(amber_end - timedelta(minutes=30)), "end_time": nem_iso(amber_end)},
    ]})
    sensor = _spot_sensor(amber)
    periods = [make_price_period(boundary + timedelta(minutes=30 * (i + 1)), value=0.05 + i * 1e-3)
               for i in range(96)]
    price_data = MagicMock(forecast=periods, forecast_generated_at=nem_iso(boundary),
                           region="QLD1", interval_minutes=30, source_file="test.xml")
    sensor.coordinator.data = MagicMock(prices={"QLD1": price_data})

    attrs = sensor.extra_state_attributes

    assert attrs["forecast"][0]["time"] == nem_iso(amber_end) == attrs["forecast_start"]
    assert attrs["forecast_start_source"] == "amber"
    assert attrs["next_value"] == attrs["forecast"][0]["value"]
    assert len(attrs["forecast"]) == 96 - 47
