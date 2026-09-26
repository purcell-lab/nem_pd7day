"""
Tests for tariff_sensor.py: NemPd7dayTariffSensor and TariffForecastDays27Sensor.

State, forecast structure, identity, tariff period windows, calibration
plumbing and the single-entry caches of the import tariff sensor. The
arithmetic of GST placement (#158) is probed against the real library in
test_tariff_gst.py; parity of the calibrated spot with the price forecast
sensor (#66, #68) is in test_tariff_calibration_parity.py; the export sensor
is in test_export_tariff.py.

Run with:  python -m pytest tests/test_tariff_sensor.py -v
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import types
from datetime import datetime, timedelta
from functools import partial
from unittest.mock import MagicMock, patch

import pytest

import support
from support import NEM_TZ, install_ha_stubs, load_chain, make_price_period, nem_iso

install_ha_stubs()

_const_mod, _nem_time, _client_mod, _store_mod, _coord_mod, _tariff_mod, _sensor_mod = load_chain(
    "const", "nem_time", "pd7day_client", "calibration_store", "coordinator",
    "tariff_sensor", "sensor",
)

NemPd7dayTariffSensor = _tariff_mod.NemPd7dayTariffSensor
TariffForecastDays27Sensor = _tariff_mod.TariffForecastDays27Sensor
DOMAIN = _const_mod.DOMAIN
parse_iso = _nem_time.parse_iso

# Reads the tariff constants from this file's module object (see support.py).
expected_import_price = partial(support.expected_import_price, _tariff_mod)


# ── Builders ──────────────────────────────────────────────────────────────────

def make_tariff_sensor(
    region="QLD1",
    distributor="energex",
    tariff_code="8400",
    price_periods=None,
    cls=None,
):
    """Construct a tariff sensor bypassing HA CoordinatorEntity init.

    ``cls`` defaults to NemPd7dayTariffSensor; pass TariffForecastDays27Sensor
    for the day 2-7 variant. runtime_data carries dispatch=None so the
    dispatch-price fast path is skipped and the PD7DAY fallback is exercised;
    the additional-fee number entity resolves to None so the default fee is used.
    """
    cls = cls or NemPd7dayTariffSensor
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
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator, store=None, dispatch=None)

    sensor = cls.__new__(cls)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._tariff_code = tariff_code
    sensor._entry = entry
    sensor._store = None
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{tariff_code}_tariff"
    sensor._attr_name = f"{distributor} {tariff_code}"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    sensor.hass.states.get.return_value = None
    return sensor


make_days27_sensor = partial(make_tariff_sensor, cls=TariffForecastDays27Sensor)


def make_real_sensor(cls, region, distributor, tariff_code):
    """Construct through the real ``__init__`` (FakeCoordinatorEntity base)."""
    coordinator = MagicMock()
    coordinator.data = None
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {}
    return cls(coordinator, entry, region, distributor, tariff_code)


def current_interval_period(value=0.10):
    """A period whose interval covers the wall clock now, for native_value."""
    now = datetime.now(tz=NEM_TZ)
    end = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(minutes=30)
    return make_price_period(end, value=value)


def make_calibrated_tariff_sensor(raw_value=0.10, calibrated_value=0.08, **kwargs):
    """A tariff sensor over one current period with a mock calibration store."""
    period = current_interval_period(raw_value)
    sensor = make_tariff_sensor(price_periods=[period], **kwargs)
    mock_store = MagicMock()
    mock_store.apply_to_price.return_value = {
        "calibrated": calibrated_value,
        "p10": None, "p50": None, "p90": None,
        "ols_mae": None, "calibrated_source": "isotonic",
        "n_obs": 100,
    }
    sensor._store = mock_store
    region = kwargs.get("region", "QLD1")
    sensor.coordinator.data.prices[region].forecast_generated_at = nem_iso(
        datetime.now(tz=NEM_TZ) - timedelta(hours=1)
    )
    return sensor, period, mock_store


def make_forecast(n: int = 330, run: datetime | None = None) -> list:
    """A run of n half hour intervals, distinct values so mix-ups are visible."""
    run = run or datetime(2026, 9, 2, 4, 0, tzinfo=NEM_TZ)
    return [
        make_price_period(run + timedelta(minutes=30 * (i + 1)), value=0.10 + i * 1e-4)
        for i in range(n)
    ]


def count_calls(sensor, name):
    """A wrapper for a method of ``type(sensor)`` that records its calls."""
    original = getattr(type(sensor), name)
    calls = []

    def wrapper(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    return wrapper, calls


# Period tuples as aemo_to_tariff.get_periods returns them: (name, start, end, rate_c).
PEAK_OFFPEAK_TUPLES = [
    ("Peak", _dt.time(14, 0), _dt.time(20, 0), 25.0),
    ("OffPeak", _dt.time(20, 0), _dt.time(14, 0), 5.0),
]

# Windows shaped like _get_tariff_periods output, including a wraparound
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

NO_CUTOFF = datetime(2000, 1, 1, tzinfo=NEM_TZ)

SENSOR_KINDS = [
    pytest.param(make_tariff_sensor, id="import"),
    pytest.param(make_days27_sensor, id="days27"),
]


# ── State and forecast structure ─────────────────────────────────────────────

def test_tariff_sensor_current_value():
    """native_value is the library result plus fee, grossed up once, in $/kWh.

    The current interval is inside the Amber Express cutoff window; native_value
    is not filtered by it. With no store the raw price feeds the library.
    """
    sensor = make_tariff_sensor(price_periods=[current_interval_period(0.10)])
    assert sensor._store is None

    # spot_to_tariff returns 15.5 c/kWh, of which the spot component is
    # 100 $/MWh through the loss factors; the rest is Energex network rate,
    # which the library has already grossed up.
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as mock_stt:
        val = sensor.native_value
    assert val is not None
    expected = expected_import_price(15.5, 100.0)
    assert abs(val - expected) < 1e-6, f"Expected {expected}, got {val}"
    # RRP conversion: 0.10 $/kWh * 1000 = 100 $/MWh
    assert abs(mock_stt.call_args[0][3] - 100.0) < 1e-6


def test_tariff_sensor_forecast_attribute():
    """Forecast attribute has one entry per interval with the per-interval fields."""
    base = datetime(2026, 9, 2, 10, 0, tzinfo=NEM_TZ)
    periods = [make_price_period(base + timedelta(minutes=30 * i), value=0.05 + i * 0.01) for i in range(5)]
    sensor = make_tariff_sensor(price_periods=periods)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        attrs = sensor.extra_state_attributes
    assert attrs["distributor"] == "Energex"
    assert attrs["network"] == "energex"
    assert attrs["tariff_code"] == "8400"
    assert attrs["region"] == "QLD1"
    assert len(attrs["forecast"]) == 5
    for i, entry in enumerate(attrs["forecast"]):
        assert "time" in entry
        assert "period" in entry
        assert "network_rate" in entry
        expected_val = expected_import_price(10.0, (0.05 + i * 0.01) * 1000)
        assert abs(entry["value"] - expected_val) < 1e-6
        # spot_raw is the uncalibrated input value for that interval
        assert abs(entry["spot_raw"] - round(0.05 + i * 0.01, 6)) < 1e-6


@pytest.mark.parametrize("price_periods", [None, []], ids=["no_data", "empty_forecast"])
def test_native_value_none_without_a_current_period(price_periods):
    sensor = make_tariff_sensor(price_periods=price_periods)
    assert sensor.native_value is None


def test_tariff_sensor_handles_spot_to_tariff_exception():
    """spot_to_tariff raises -> native_value returns None (no crash)."""
    sensor = make_tariff_sensor(price_periods=[current_interval_period()])
    with patch.object(_tariff_mod, "spot_to_tariff", side_effect=ValueError("unknown tariff")):
        assert sensor.native_value is None


def test_forecast_attribute_with_none_coordinator_data():
    """extra_state_attributes returns an empty forecast and the static metadata."""
    sensor = make_tariff_sensor(price_periods=None)
    attrs = sensor.extra_state_attributes
    assert attrs["forecast"] == []
    assert attrs["distributor"] == "Energex"
    assert attrs["network"] == "energex"
    assert attrs["tariff_code"] == "8400"
    assert attrs["region"] == "QLD1"
    assert abs(attrs["additional_usage_fee_$/kwh"] - 0.0293) < 1e-6
    assert attrs["gst_multiplier"] == 1.1


@pytest.mark.parametrize("factory, expected", [
    pytest.param(make_tariff_sensor, 367, id="import_full_day_1_7"),
    # time_i = base + 30*i min; time > base + 24 h needs i >= 49, so 367 - 49.
    pytest.param(make_days27_sensor, 318, id="days27_trimmed_at_cutoff"),
])
def test_forecast_length_by_sensor_kind(factory, expected):
    """The base sensor publishes every day 1-7 interval; day 2-7 trims at the cutoff."""
    base = datetime(2026, 5, 19, 6, 0, tzinfo=NEM_TZ)
    periods = [
        make_price_period(base + timedelta(minutes=30 * (i + 1)), value=0.05 + i * 0.0001)
        for i in range(367)
    ]
    sensor = factory(price_periods=periods)
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0), \
            patch.object(_tariff_mod, "_amber_express_cutoff", return_value=base + timedelta(hours=24)):
        forecast = sensor.extra_state_attributes["forecast"]
    assert len(forecast) == expected


def test_stdout_suppressed_during_tariff_calculation():
    """Debug print() calls inside aemo_to_tariff never reach stdout."""
    sensor = make_tariff_sensor(price_periods=[current_interval_period()])

    def noisy_spot_to_tariff(*args, **kwargs):
        print("DEBUG: sapower tariff lookup")
        return 15.5

    def noisy_get_periods(*args, **kwargs):
        print("DEBUG: sapower get_periods called")
        return PEAK_OFFPEAK_TUPLES[:1]

    def noisy_get_daily_fee(*args, **kwargs):
        print("DEBUG: sapower get_daily_fee called")
        return 0.556

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), \
            patch.object(_tariff_mod, "spot_to_tariff", side_effect=noisy_spot_to_tariff), \
            patch.object(_tariff_mod, "get_periods", side_effect=noisy_get_periods), \
            patch.object(_tariff_mod, "get_daily_fee", side_effect=noisy_get_daily_fee):
        # Every code path that calls into aemo_to_tariff
        assert sensor.native_value is not None
        attrs = sensor.extra_state_attributes
        assert len(attrs["tariff_periods"]) == 1
        assert attrs["daily_supply_charge_$"] is not None
    assert captured.getvalue() == "", f"Expected no stdout output but got: {captured.getvalue()!r}"


# ── Identity ──────────────────────────────────────────────────────────────────

def test_tariff_sensor_device_info():
    """The tariff sensor joins the regional device, like every other sensor."""
    info = make_tariff_sensor(region="QLD1").device_info
    assert ("nem_pd7day", "entry_1_QLD1") in info["identifiers"]


@pytest.mark.parametrize("cls, region, distributor, code, name, unique_id", [
    (NemPd7dayTariffSensor, "QLD1", "energex", "6900",
     "Energex Residential Time of Use Energy Tariff (6900)", "entry_1_QLD1_energex_6900_tariff"),
    (NemPd7dayTariffSensor, "SA1", "sapn", "RTOU",
     "SA Power Networks Residential Time of Use Tariff (RTOU)", "entry_1_SA1_sapn_RTOU_tariff"),
    # Unknown tariff code falls back to the code itself
    (NemPd7dayTariffSensor, "QLD1", "energex", "ZZZZ",
     "Energex ZZZZ Tariff (ZZZZ)", "entry_1_QLD1_energex_ZZZZ_tariff"),
    (TariffForecastDays27Sensor, "QLD1", "energex", "6900",
     "Day 2-7 Energex Residential Time of Use Energy Tariff (6900)", "nem_pd7day_QLD1_energex_6900_days27"),
])
def test_init_sets_name_and_unique_id(cls, region, distributor, code, name, unique_id):
    """'{distributor_display} {tariff_name} Tariff ({tariff_code})' and the unique_id, from __init__."""
    sensor = make_real_sensor(cls, region, distributor, code)
    assert sensor._attr_name == name
    assert sensor._attr_unique_id == unique_id


@pytest.mark.parametrize("distributor, tariff_code, enabled", [
    ("energex", "6900", True),
    ("sapn", "RESELE", True),
    ("energex", "8400", False),      # Residential Flat
    ("ergon", "3900", False),        # Residential Transitional Demand
    ("ausgrid", "EA010", False),     # Residential Flat
    ("endeavour", "N70", False),     # Residential Flat
    ("essential", "BLNN2AU", False),  # Residential Anytime
    ("sapn", "RSR", False),          # Residential Single Rate
    ("tasnetworks", "TAS87", False),  # Residential ToU Demand
])
def test_entity_registry_enabled_default(distributor, tariff_code, enabled):
    sensor = make_tariff_sensor(distributor=distributor, tariff_code=tariff_code)
    assert sensor.entity_registry_enabled_default is enabled, f"{distributor}/{tariff_code}"


@pytest.mark.parametrize("cls", [
    pytest.param(TariffForecastDays27Sensor, id="tariff"),
    pytest.param(_sensor_mod.SpotPriceForecastDays27Sensor, id="spot"),
])
def test_day27_sensors_are_diagnostic(cls):
    """Day 2-7 sensors carry entity_category == DIAGNOSTIC."""
    assert cls._attr_entity_category == "diagnostic"


# ── Tariff periods and the window lookup ──────────────────────────────────────
#
# #62: _lookup_period_info used to strptime both ends of every tariff window on
# every interval: 21,140 strptime calls in a five build profile, 0.316 s of
# 0.802 s. _tariff_windows now parses once per period list, keyed on the list's
# identity. _reference_lookup_period_info is the pre-#62 inline logic, kept as
# an oracle so the output can be shown not to have moved.

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


def test_tariff_periods_in_attributes():
    """tariff_periods is a list with the expected keys and c/kWh -> $/kWh conversion."""
    period = make_price_period(datetime(2026, 9, 2, 10, 30, tzinfo=NEM_TZ), value=0.05)
    sensor = make_tariff_sensor(price_periods=[period])

    with patch.object(_tariff_mod, "get_periods", return_value=PEAK_OFFPEAK_TUPLES), \
            patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        tp = sensor.extra_state_attributes["tariff_periods"]
    assert isinstance(tp, list)
    assert len(tp) == 2
    for entry in tp:
        assert {"period", "start", "end", "network_rate_$/kwh"} <= set(entry)
    assert abs(tp[0]["network_rate_$/kwh"] - 0.25) < 1e-6
    assert abs(tp[1]["network_rate_$/kwh"] - 0.05) < 1e-6


def test_unsupported_tariff_code_caches_empty_and_is_silent():
    """ValueError from get_periods -> cache [] once, lookup returns (None, None).

    aemo_to_tariff raises ValueError('Unknown tariff code') for codes it does
    not support (e.g. essential/BLNREX2, endeavour/N61). The cache must hold []
    so subsequent lookups return immediately without re-calling get_periods.
    """
    with patch.object(_tariff_mod, "get_periods", side_effect=ValueError("Unknown tariff code")) as mock_gp:
        sensor = make_tariff_sensor(price_periods=None)
        sensor._cached_tariff_periods = sensor._get_tariff_periods()
        assert sensor._cached_tariff_periods == []
        calls_after_construction = mock_gp.call_count

        period = make_price_period(datetime(2026, 9, 2, 16, 0, tzinfo=NEM_TZ), value=0.07)
        assert sensor._lookup_period_info(period) == (None, None)
        assert mock_gp.call_count == calls_after_construction


def test_sapn_none_window_period_skipped_silently():
    """SAPN SBTOU/SBTOUNE 5-tuples include an Off-peak row with start/end None.

    That row must be skipped silently (no AttributeError from start.strftime,
    no 'get_periods failed' log); the remaining timed rows are returned.
    """
    fake_periods = [
        ("Peak", _dt.time(17, 0), _dt.time(21, 0), [11, 12, 1, 2, 3], 27.5),
        ("Shoulder", _dt.time(7, 0), _dt.time(17, 0), [11, 12, 1, 2, 3], 19.14),
        ("Off-peak", None, None, None, 10.34),
    ]
    sensor = make_tariff_sensor(price_periods=None)
    with patch.object(_tariff_mod, "get_periods", return_value=fake_periods), \
            patch.object(_tariff_mod._LOGGER, "debug") as mock_log:
        tp = sensor._get_tariff_periods()
    assert len(tp) == 2
    assert {e["period"] for e in tp} == {"Peak", "Shoulder"}
    assert not any("get_periods failed" in str(c.args) for c in mock_log.call_args_list)


@pytest.mark.parametrize("nemtime_hour, value, period_name, rate", [
    (16, 0.07, "Peak", 0.25),      # lookup at 15:55 -> Peak
    (22, 0.03, "OffPeak", 0.05),   # lookup at 21:55 -> OffPeak (wraparound branch)
])
def test_forecast_period_and_network_rate(nemtime_hour, value, period_name, rate):
    """Forecast entries resolve period name + network_rate from get_periods output."""
    period = make_price_period(datetime(2026, 9, 2, nemtime_hour, 0, tzinfo=NEM_TZ), value=value)
    sensor = make_tariff_sensor(price_periods=[period])
    with patch.object(_tariff_mod, "get_periods", return_value=PEAK_OFFPEAK_TUPLES), \
            patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        entry = sensor.extra_state_attributes["forecast"][0]
    assert entry["spot_raw"] == round(value, 6)
    assert entry["period"] == period_name
    assert abs(entry["network_rate"] - rate) < 1e-6


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
        {"period": "Renamed", "start": "16:00", "end": "20:00", "network_rate_$/kwh": 0.99},
        {"period": "Off-peak", "start": "20:00", "end": "16:00", "network_rate_$/kwh": 0.03},
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
        {"period": "Broken", "start": "not a time", "end": "20:00", "network_rate_$/kwh": 0.14},
        {"period": "Peak", "start": "16:00", "end": "20:00", "network_rate_$/kwh": 0.14},
    ]
    assert sensor._lookup_period_info(period) == (None, None)
    assert _reference_lookup_period_info(sensor, period) == (None, None)


def test_a_malformed_window_is_not_cached_so_a_fix_takes_effect():
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ))
    sensor = make_tariff_sensor(price_periods=[period])
    broken = [{"period": "Broken", "start": "nope", "end": "20:00", "network_rate_$/kwh": 0.14}]
    sensor._cached_tariff_periods = broken
    assert sensor._lookup_period_info(period) == (None, None)
    assert getattr(sensor, "_cached_tariff_windows", None) is None

    broken[0]["start"] = "16:00"
    assert sensor._lookup_period_info(period) == ("Broken", 0.14)


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


# ── Static attributes ─────────────────────────────────────────────────────────

def test_loss_factors_in_attributes():
    """dlf/mlf/combined present and combined = dlf * mlf * market."""
    sensor = make_tariff_sensor(price_periods=None)
    with patch.object(_tariff_mod, "get_periods", return_value=[]), \
            patch.object(_tariff_mod, "get_daily_fee", return_value=None):
        attrs = sensor.extra_state_attributes
    dlf = attrs["distribution_loss_factor_dlf"]
    mlf = attrs["metering_loss_factor_mlf"]
    market = attrs["market_loss_factor"]
    combined = attrs["combined_loss_multiplier"]
    assert isinstance(dlf, float)
    assert isinstance(mlf, float)
    assert isinstance(combined, float)
    assert abs(combined - round(dlf * mlf * market, 6)) < 1e-9


def test_forecast_description_in_attributes():
    """Description mentions forecast, DLF, MLF, GST and the additional usage fee entity."""
    sensor = make_tariff_sensor(price_periods=None)
    with patch.object(_tariff_mod, "get_periods", return_value=[]), \
            patch.object(_tariff_mod, "get_daily_fee", return_value=None):
        desc = sensor.extra_state_attributes["forecast_description"]
    assert isinstance(desc, str)
    assert "forecast" in desc.lower()
    assert "DLF" in desc
    assert "MLF" in desc
    assert "Energex" in desc
    assert "10% GST" in desc
    assert "additional usage fee" in desc
    assert "nem_pd7day_qld1_additional_usage_fee" in desc


def test_daily_supply_charge_in_attributes():
    """daily_supply_charge_$ is the library's float, or None when it raises."""
    sensor = make_tariff_sensor(price_periods=None)
    with patch.object(_tariff_mod, "get_periods", return_value=[]), \
            patch.object(_tariff_mod, "get_daily_fee", return_value=0.556):
        charge = sensor.extra_state_attributes["daily_supply_charge_$"]
    assert isinstance(charge, float)
    assert abs(charge - 0.556) < 1e-6

    with patch.object(_tariff_mod, "get_periods", return_value=[]), \
            patch.object(_tariff_mod, "get_daily_fee", side_effect=ValueError("nope")):
        assert sensor.extra_state_attributes["daily_supply_charge_$"] is None


# ── Calibration plumbing ──────────────────────────────────────────────────────
#
# A mock store stands in for the calibration here; test_tariff_calibration_parity.py
# runs a real fitted store and compares against the price forecast sensor.

def test_tariff_uses_calibrated_price_not_raw():
    """_compute_tariff passes calibrated $/MWh (not raw) to spot_to_tariff."""
    sensor, _period, _store = make_calibrated_tariff_sensor(raw_value=0.01745, calibrated_value=0.01425)
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as mock_stt:
        assert sensor.native_value is not None
    rrp = mock_stt.call_args[0][3]
    assert abs(rrp - 14.25) < 1e-6, f"Expected calibrated RRP 14.25 $/MWh, got {rrp}"
    assert abs(rrp - 17.45) > 0.1, "raw 0.01745 $/kWh must not reach the library"


def test_tariff_forecast_spot_shows_calibrated():
    """Forecast 'spot' is the calibrated value, 'spot_raw' the uncalibrated one."""
    sensor, _period, _store = make_calibrated_tariff_sensor(raw_value=0.01745, calibrated_value=0.01425)
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=10.0):
        forecast = sensor.extra_state_attributes["forecast"]
    assert forecast
    for entry in forecast:
        assert abs(entry["spot"] - 0.01425) < 1e-6, f"Forecast spot should be calibrated, got {entry['spot']}"
        assert abs(entry["spot_raw"] - 0.01745) < 1e-6


def test_uncalibratable_interval_degrades_to_none_not_zero():
    """A store that cannot produce a number means None on both keys, never 0 or raw."""
    sensor, _period, _store = make_calibrated_tariff_sensor(raw_value=0.12093, calibrated_value=None)
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5):
        forecast = sensor.extra_state_attributes["forecast"]
    assert forecast
    for entry in forecast:
        assert entry["spot"] is None, f"expected None spot, got {entry['spot']!r}"
        assert entry["value"] is None, f"expected None value, got {entry['value']!r}"


# #62: the extra_state_attributes loops calibrate each interval once and hand
# the result to _compute_tariff, instead of calibrating twice. The profile
# showed 3300 _calibrated_value calls against 1650 _compute_tariff calls.

@pytest.mark.parametrize("factory", SENSOR_KINDS)
def test_attribute_loop_calibrates_each_interval_once(factory):
    periods = make_forecast(120)
    sensor = factory(price_periods=periods)
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    wrapper, calls = count_calls(sensor, "_calibrated_value")
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5), \
            patch.object(_tariff_mod, "_amber_express_cutoff", return_value=NO_CUTOFF), \
            patch.object(type(sensor), "_calibrated_value", wrapper):
        attrs = sensor.extra_state_attributes

    assert len(attrs["forecast"]) == 120
    assert len(calls) == 120


@pytest.mark.parametrize("factory", SENSOR_KINDS)
def test_spot_key_is_the_value_that_was_fed_to_the_tariff(factory):
    """The spot attribute and the tariff input must remain the same number."""
    periods = make_forecast(8)
    sensor = factory(price_periods=periods)
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    seen = []

    def fake_calibrate(self, period):
        # Deliberately not derived from period.value, so a caller that
        # recomputed instead of reusing would produce a different number.
        v = 0.5 + len(seen) * 0.01
        seen.append(v)
        return v

    fed = []
    original = type(sensor)._compute_tariff

    def spy(self, period, calibrated=None):
        fed.append(calibrated)
        return original(self, period, calibrated=calibrated)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5), \
            patch.object(type(sensor), "_calibrated_value", fake_calibrate), \
            patch.object(type(sensor), "_compute_tariff", spy), \
            patch.object(_tariff_mod, "_amber_express_cutoff", return_value=NO_CUTOFF):
        attrs = sensor.extra_state_attributes

    assert len(seen) == 8
    assert fed == seen, "the calibrated value was not passed through"
    assert [e["spot"] for e in attrs["forecast"]] == [round(v, 6) for v in seen]


def test_compute_still_calibrates_when_not_given_a_value():
    """native_value calls _compute_tariff directly and must be unaffected."""
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ), value=0.10)
    sensor = make_tariff_sensor(price_periods=[period])
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    calls = []

    def fake_calibrate(self, p):
        calls.append(p)
        return 0.42

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as lib, \
            patch.object(type(sensor), "_calibrated_value", fake_calibrate):
        sensor._compute_tariff(period)

    assert len(calls) == 1, "the default path must still calibrate"
    assert lib.call_args[0][3] == pytest.approx(420.0), "0.42 $/kWh -> 420 $/MWh"


@pytest.mark.parametrize("calibrated, rrp_mwh", [(0.77, 770.0), (0.0, 0.0)],
                         ids=["supplied", "supplied_zero"])
def test_compute_uses_the_supplied_value_and_does_not_calibrate(calibrated, rrp_mwh):
    """0.0 is a legitimate calibrated price; `if calibrated is None` matters."""
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ), value=0.10)
    sensor = make_tariff_sensor(price_periods=[period])
    sensor._cached_tariff_periods = WINDOW_SETS["two_band"]

    def boom(self, p):
        raise AssertionError("_calibrated_value must not be called")

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as lib, \
            patch.object(type(sensor), "_calibrated_value", boom):
        sensor._compute_tariff(period, calibrated=calibrated)

    assert lib.call_args[0][3] == pytest.approx(rrp_mwh)


# ── Single-entry caches ───────────────────────────────────────────────────────

def test_compute_tariff_cache_hit():
    """Calling _compute_tariff twice with the same period calls spot_to_tariff once."""
    period = current_interval_period()
    sensor = make_tariff_sensor(price_periods=[period])
    sensor._period_tariff_cache = None

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as mock_stt:
        result1 = sensor._compute_tariff(period)
        result2 = sensor._compute_tariff(period)
    assert result1 is not None
    assert result1 == result2
    assert mock_stt.call_count == 1


def test_apply_tariff_to_spot_cache_hit():
    """Calling _apply_tariff_to_spot twice with the same inputs calls spot_to_tariff once."""
    now = datetime(2026, 5, 24, 14, 12, 0, tzinfo=NEM_TZ)
    sensor = make_tariff_sensor(price_periods=[])
    sensor._tariff_cache = None

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as mock_stt:
        result1 = sensor._apply_tariff_to_spot(0.10, now)
        result2 = sensor._apply_tariff_to_spot(0.10, now)
    assert result1 is not None
    assert result1 == result2
    assert mock_stt.call_count == 1


@pytest.mark.parametrize("minute, expected_end", [
    (12, (14, 15)),   # inside the hour: the next 5-minute boundary
    (55, (15, 0)),    # the last 5-minute slot ends on the hour: rolls over
    (58, (15, 0)),
])
def test_apply_tariff_to_spot_passes_the_interval_end(minute, expected_end):
    """The dispatch path hands the library the interval END, rolling past the hour.

    The rollover branch used to be reached only when the wall clock happened to
    read minute 55 to 59 while the suite ran, so its coverage came and went.
    """
    now = datetime(2026, 5, 24, 14, minute, 30, tzinfo=NEM_TZ)
    sensor = make_tariff_sensor(price_periods=[])
    sensor._tariff_cache = None
    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5) as lib:
        assert sensor._apply_tariff_to_spot(0.10, now) is not None
    end = lib.call_args.args[0]
    assert (end.hour, end.minute, end.second, end.microsecond) == (*expected_end, 0, 0)

