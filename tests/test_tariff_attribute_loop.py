"""
Tests for the tariff attribute loop work in #62.

Two changes are covered:

  1. The three ``extra_state_attributes`` loops calibrate each interval once and
     pass the result into the tariff computation, instead of calibrating twice.
  2. ``_lookup_period_info`` matches against ``time`` objects parsed once by
     ``_tariff_windows``, instead of ``strptime``-ing both ends of every tariff
     period window on every interval.

Both are pure performance changes, so the tests are mostly about proving the
output did not move. ``_reference_lookup_period_info`` reproduces the previous
inline matching logic and is used as an oracle across every interval of a full
seven day forecast for several real tariffs.

Run with:  python -m pytest tests/test_tariff_attribute_loop.py -v
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from test_tariff_sensor import (  # noqa: E402
    DOMAIN,
    NEM_TZ,
    _tariff_mod,
    make_price_period,
    make_tariff_sensor,
)

from custom_components.nem_pd7day.nem_time import parse_iso  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_forecast(n: int = 330, run: datetime | None = None) -> list:
    """A run of n half hour intervals, distinct values so mix-ups are visible."""
    run = run or datetime(2026, 9, 2, 4, 0, tzinfo=NEM_TZ)
    return [
        make_price_period(run + timedelta(minutes=30 * (i + 1)), value=0.10 + i * 1e-4)
        for i in range(n)
    ]


def make_days27_sensor(region="QLD1", distributor="energex", tariff_code="8400",
                       price_periods=None):
    """A TariffForecastDays27Sensor built the same way as the base sensor."""
    base = make_tariff_sensor(
        region=region, distributor=distributor, tariff_code=tariff_code,
        price_periods=price_periods,
    )
    cls = _tariff_mod.TariffForecastDays27Sensor
    sensor = cls.__new__(cls)
    sensor.__dict__.update(base.__dict__)
    return sensor


def make_export_sensor(region="QLD1", distributor="energex", export_code="EXP",
                       import_code="8400", price_periods=None):
    """A NemPd7dayExportTariffSensor with the same fakes as make_tariff_sensor."""
    coordinator = MagicMock()
    if price_periods is not None:
        price_data = MagicMock()
        price_data.forecast = price_periods
        coordinator.data = MagicMock()
        coordinator.data.prices = {region: price_data}
    else:
        coordinator.data = None
    coordinator.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {}

    cls = _tariff_mod.NemPd7dayExportTariffSensor
    sensor = cls.__new__(cls)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._export_code = export_code
    sensor._import_code = import_code
    sensor._entry = entry
    sensor._store = None
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{export_code}_export"
    sensor._attr_name = "Export Tariff"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    sensor.hass.states.get.return_value = None
    return sensor


def count_calls(sensor, name):
    """Wrap a bound method so calls can be counted without changing behaviour."""
    original = getattr(type(sensor), name)
    calls = []

    def wrapper(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    return original, wrapper, calls


def _reference_lookup_period_info(sensor, period):
    """The pre-#62 inline implementation, kept verbatim as an oracle."""
    try:
        tariff_periods = sensor._cached_tariff_periods
        if not tariff_periods:
            return None, None
        lookup_dt = parse_iso(period.nemtime) - _dt.timedelta(minutes=5)
        t = lookup_dt.time()
        for entry in tariff_periods:
            start = _dt.datetime.strptime(entry["start"], "%H:%M").time()
            end = _dt.datetime.strptime(entry["end"], "%H:%M").time()
            if start <= t < end or (start > end and (t >= start or t < end)):
                return entry.get("period"), entry.get("network_rate_$/kwh")
        return None, None
    except Exception:
        return None, None


# Windows shaped like real aemo_to_tariff output, including a wraparound
# overnight window and an SAPN style three band day.
WINDOW_SETS = {
    "two_band": [
        {"period": "Peak", "start": "16:00", "end": "20:00", "network_rate_$/kwh": 0.14},
        {"period": "Off-peak", "start": "20:00", "end": "16:00", "network_rate_$/kwh": 0.03},
    ],
    "three_band": [
        {"period": "Solar sponge", "start": "10:00", "end": "15:00", "network_rate_$/kwh": 0.01},
        {"period": "Peak", "start": "17:00", "end": "21:00", "network_rate_$/kwh": 0.18},
        {"period": "Off-peak", "start": "21:00", "end": "10:00", "network_rate_$/kwh": 0.05},
    ],
    "midnight_edges": [
        {"period": "Night", "start": "00:00", "end": "07:00", "network_rate_$/kwh": 0.02},
        {"period": "Day", "start": "07:00", "end": "00:00", "network_rate_$/kwh": 0.09},
    ],
    "no_match_gap": [
        {"period": "Peak", "start": "16:00", "end": "20:00", "network_rate_$/kwh": 0.14},
    ],
    "empty": [],
}


# ── Single calibration per interval ───────────────────────────────────────────

def test_base_loop_calibrates_each_interval_once():
    """3300 _calibrated_value calls for 1650 intervals was the bug. See #62."""
    periods = make_forecast(120)
    sensor = make_tariff_sensor(price_periods=periods)
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    original, wrapper, calls = count_calls(sensor, "_calibrated_value")
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5), \
            patch.object(type(sensor), "_calibrated_value", wrapper):
        attrs = sensor.extra_state_attributes

    assert len(attrs["forecast"]) == 120
    assert len(calls) == 120


def test_days27_loop_calibrates_each_interval_once():
    periods = make_forecast(120)
    sensor = make_days27_sensor(price_periods=periods)
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    original, wrapper, calls = count_calls(sensor, "_calibrated_value")
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5), \
            patch.object(_tariff_mod, "_amber_express_cutoff",
                         return_value=datetime(2000, 1, 1, tzinfo=NEM_TZ)), \
            patch.object(type(sensor), "_calibrated_value", wrapper):
        attrs = sensor.extra_state_attributes

    assert len(attrs["forecast"]) == 120
    assert len(calls) == 120


def test_export_loop_calibrates_each_interval_once():
    periods = make_forecast(120)
    sensor = make_export_sensor(price_periods=periods)

    original, wrapper, calls = count_calls(sensor, "_calibrated_value")
    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=8.0), \
            patch.object(type(sensor), "_calibrated_value", wrapper):
        attrs = sensor.extra_state_attributes

    assert len(attrs["forecast"]) == 120
    assert len(calls) == 120


@pytest.mark.parametrize("factory,compute,library", [
    (make_tariff_sensor, "_compute_tariff", "spot_to_tariff"),
    (make_days27_sensor, "_compute_tariff", "spot_to_tariff"),
    (make_export_sensor, "_compute_export_tariff", "spot_to_feed_in_tariff"),
])
def test_spot_key_is_the_value_that_was_fed_to_the_tariff(factory, compute, library):
    """The spot attribute and the tariff input must remain the same number."""
    periods = make_forecast(8)
    sensor = factory(price_periods=periods)
    if hasattr(sensor, "_tariff_code"):
        sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    seen = []

    def fake_calibrate(self, period):
        # Deliberately not derived from period.value, so a caller that
        # recomputed instead of reusing would produce a different number.
        v = 0.5 + len(seen) * 0.01
        seen.append(v)
        return v

    fed = []
    original = getattr(type(sensor), compute)

    def spy(self, period, calibrated=None):
        fed.append(calibrated)
        return original(self, period, calibrated=calibrated)

    with patch.object(_tariff_mod, library, return_value=15.5), \
            patch.object(type(sensor), "_calibrated_value", fake_calibrate), \
            patch.object(type(sensor), compute, spy), \
            patch.object(_tariff_mod, "_amber_express_cutoff",
                         return_value=datetime(2000, 1, 1, tzinfo=NEM_TZ)):
        attrs = sensor.extra_state_attributes

    assert len(seen) == 8
    assert fed == seen, "the calibrated value was not passed through"
    assert [e["spot"] for e in attrs["forecast"]] == [round(v, 6) for v in seen]


@pytest.mark.parametrize("factory,compute,library", [
    (make_tariff_sensor, "_compute_tariff", "spot_to_tariff"),
    (make_export_sensor, "_compute_export_tariff", "spot_to_feed_in_tariff"),
])
def test_compute_still_calibrates_when_not_given_a_value(factory, compute, library):
    """native_value calls the compute directly and must be unaffected."""
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ), value=0.10)
    sensor = factory(price_periods=[period])
    if hasattr(sensor, "_tariff_code"):
        sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    calls = []

    def fake_calibrate(self, p):
        calls.append(p)
        return 0.42

    with patch.object(_tariff_mod, library, return_value=15.5) as lib, \
            patch.object(type(sensor), "_calibrated_value", fake_calibrate):
        getattr(sensor, compute)(period)

    assert len(calls) == 1, "the default path must still calibrate"
    assert lib.call_args[0][3] == pytest.approx(420.0), "0.42 $/kWh -> 420 $/MWh"


@pytest.mark.parametrize("factory,compute,library", [
    (make_tariff_sensor, "_compute_tariff", "spot_to_tariff"),
    (make_export_sensor, "_compute_export_tariff", "spot_to_feed_in_tariff"),
])
def test_compute_uses_the_supplied_value_and_does_not_calibrate(factory, compute, library):
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ), value=0.10)
    sensor = factory(price_periods=[period])
    if hasattr(sensor, "_tariff_code"):
        sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    def boom(self, p):
        raise AssertionError("_calibrated_value must not be called")

    with patch.object(_tariff_mod, library, return_value=15.5) as lib, \
            patch.object(type(sensor), "_calibrated_value", boom):
        getattr(sensor, compute)(period, calibrated=0.77)

    assert lib.call_args[0][3] == pytest.approx(770.0)


def test_a_supplied_zero_is_not_treated_as_absent():
    """0.0 is a legitimate calibrated price; `if calibrated is None` matters."""
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ), value=0.10)
    sensor = make_tariff_sensor(price_periods=[period])
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    def boom(self, p):
        raise AssertionError("_calibrated_value must not be called for 0.0")

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as lib, \
            patch.object(type(sensor), "_calibrated_value", boom):
        sensor._compute_tariff(period, calibrated=0.0)

    assert lib.call_args[0][3] == pytest.approx(0.0)


# ── Precomputed period windows ────────────────────────────────────────────────

@pytest.mark.parametrize("window_name", sorted(WINDOW_SETS))
def test_lookup_matches_the_previous_implementation_for_every_interval(window_name):
    """Oracle test: identical period and rate across a full seven day run."""
    periods = make_forecast(336)
    sensor = make_tariff_sensor(price_periods=periods)
    sensor._cached_tariff_periods = WINDOW_SETS[window_name]

    for period in periods:
        assert sensor._lookup_period_info(period) == \
            _reference_lookup_period_info(sensor, period), period.nemtime


def test_windows_are_parsed_once_not_once_per_interval():
    periods = make_forecast(200)
    sensor = make_tariff_sensor(price_periods=periods)
    sensor._cached_tariff_periods = WINDOW_SETS["three_band"]

    real_strptime = _dt.datetime.strptime
    calls = []

    class CountingDatetime(_dt.datetime):
        @classmethod
        def strptime(cls, value, fmt):
            calls.append(fmt)
            return real_strptime(value, fmt)

    with patch.object(_tariff_mod.datetime, "datetime", CountingDatetime):
        for period in periods:
            sensor._lookup_period_info(period)

    hm = [f for f in calls if f == "%H:%M"]
    assert len(hm) == 6, f"expected 3 windows x 2 ends parsed once, got {len(hm)}"


def test_replacing_the_period_cache_rebuilds_the_windows():
    """Identity keying, so a refreshed tariff structure is not served stale."""
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ))
    sensor = make_tariff_sensor(price_periods=[period])

    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]
    assert sensor._lookup_period_info(period) == ("Peak", 0.14)

    sensor._cached_tariff_periods = [
        {"period": "Renamed", "start": "16:00", "end": "20:00",
         "network_rate_$/kwh": 0.99},
        {"period": "Off-peak", "start": "20:00", "end": "16:00",
         "network_rate_$/kwh": 0.03},
    ]
    assert sensor._lookup_period_info(period) == ("Renamed", 0.99)


def test_an_equal_but_distinct_list_also_rebuilds():
    """Identity, not equality, so a rebuilt-but-identical list is safe too."""
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ))
    sensor = make_tariff_sensor(price_periods=[period])
    sensor._cached_tariff_periods = list(WINDOW_SETS["two_band"])
    assert sensor._lookup_period_info(period) == ("Peak", 0.14)
    first = sensor._cached_tariff_windows[1]

    sensor._cached_tariff_periods = list(WINDOW_SETS["two_band"])
    assert sensor._lookup_period_info(period) == ("Peak", 0.14)
    assert sensor._cached_tariff_windows[1] is not first


def test_a_malformed_window_still_yields_none_rather_than_being_skipped():
    """Behaviour preserved: a bad entry aborts the lookup, it does not skip on.

    The pre-#62 code parsed inline inside one try/except, so a malformed entry
    returned (None, None) for the interval even though a later window would have
    matched. _tariff_windows deliberately does not catch, to keep that.
    """
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ))
    sensor = make_tariff_sensor(price_periods=[period])
    sensor._cached_tariff_periods = [
        {"period": "Broken", "start": "not a time", "end": "20:00",
         "network_rate_$/kwh": 0.14},
        {"period": "Peak", "start": "16:00", "end": "20:00",
         "network_rate_$/kwh": 0.14},
    ]
    assert sensor._lookup_period_info(period) == (None, None)
    assert _reference_lookup_period_info(sensor, period) == (None, None)


def test_a_malformed_window_is_not_cached_so_a_fix_takes_effect():
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ))
    sensor = make_tariff_sensor(price_periods=[period])
    broken = [{"period": "Broken", "start": "nope", "end": "20:00",
               "network_rate_$/kwh": 0.14}]
    sensor._cached_tariff_periods = broken
    assert sensor._lookup_period_info(period) == (None, None)
    assert getattr(sensor, "_cached_tariff_windows", None) is None

    broken[0]["start"] = "16:00"
    assert sensor._lookup_period_info(period) == ("Broken", 0.14)


def test_wraparound_window_matches_on_both_sides_of_midnight():
    sensor = make_tariff_sensor()
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]
    # nemtime is interval END and the lookup subtracts 5 min, so the interval
    # ending at 20:00 lands at 19:55 and is still Peak, while the next one is
    # not. Both edges of the Peak window and both sides of midnight covered.
    cases = [
        ((16, 0), "Off-peak"),   # 15:55
        ((16, 30), "Peak"),      # 16:25
        ((17, 0), "Peak"),       # 16:55
        ((20, 0), "Peak"),       # 19:55
        ((20, 30), "Off-peak"),  # 20:25
        ((0, 0), "Off-peak"),    # 23:55 previous day
        ((3, 0), "Off-peak"),    # 02:55
    ]
    for (hour, minute), expected in cases:
        period = make_price_period(datetime(2026, 9, 2, hour, minute, tzinfo=NEM_TZ))
        assert sensor._lookup_period_info(period)[0] == expected, (hour, minute)


def test_periods_attribute_shape_is_unchanged():
    """The windows are a private derivative; tariff_periods must not change."""
    periods = make_forecast(4)
    sensor = make_tariff_sensor(price_periods=periods)
    sensor._cached_tariff_periods = WINDOW_SETS["three_band"]
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5):
        attrs = sensor.extra_state_attributes
    assert attrs["tariff_periods"] == WINDOW_SETS["three_band"]
    for entry in attrs["tariff_periods"]:
        assert isinstance(entry["start"], str)
        assert isinstance(entry["end"], str)
