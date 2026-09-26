"""
Tests for tariff_sensor.py: NemPd7dayExportTariffSensor.

The export sensor converts through spot_to_feed_in_tariff and publishes the
raw feed-in rate: no additional usage fee and no GST, which is correct only
because the library adds none to a feed-in rate either (probed in
test_tariff_gst.py). Export programs are enumerated from the library's own
pairings (#159, test_tariff_catalogue.py). The calibrated spot must agree
with the price forecast sensor (#66, test_tariff_calibration_parity.py) and,
as in the import sensor, each interval is calibrated once per attribute build
(#62).

Run with:  python -m pytest tests/test_export_tariff.py -v
"""
from __future__ import annotations

import contextlib
import io
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, make_price_period, nem_iso, run_async

install_ha_stubs()

_const_mod, _nem_time, _client_mod, _store_mod, _coord_mod, _tariff_mod, _sensor_mod = load_chain(
    "const", "nem_time", "pd7day_client", "calibration_store", "coordinator",
    "tariff_sensor", "sensor",
)

NemPd7dayExportTariffSensor = _tariff_mod.NemPd7dayExportTariffSensor
DOMAIN = _const_mod.DOMAIN

PEAK_NEMTIME = datetime(2026, 5, 24, 18, 0, tzinfo=NEM_TZ)


# ── Builders ──────────────────────────────────────────────────────────────────

def make_export_sensor(
    region="NSW1",
    distributor="ausgrid",
    import_code="EA025",
    export_code="EA029",
    price_periods=None,
) -> NemPd7dayExportTariffSensor:
    """Construct a NemPd7dayExportTariffSensor bypassing HA CoordinatorEntity init."""
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

    sensor = NemPd7dayExportTariffSensor.__new__(NemPd7dayExportTariffSensor)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = distributor
    sensor._import_code = import_code
    sensor._export_code = export_code
    sensor._entry = entry
    sensor._store = None
    sensor._attr_unique_id = f"entry_1_{region}_{distributor}_{import_code}_export_tariff"
    sensor._attr_name = f"{distributor} {export_code} export"
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    sensor.hass.states.get.return_value = None
    return sensor


def make_calibrated_export_sensor(raw_value=0.01745, calibrated_value=0.01425):
    """An export sensor over one peak period with a mock calibration store."""
    period = make_price_period(PEAK_NEMTIME, value=raw_value)
    sensor = make_export_sensor(price_periods=[period])
    mock_store = MagicMock()
    mock_store.apply_to_price.return_value = {
        "calibrated": calibrated_value,
        "p10": None, "p50": None, "p90": None,
        "ols_mae": None, "calibrated_source": "isotonic",
        "n_obs": 100,
    }
    sensor._store = mock_store
    sensor.coordinator.data.prices["NSW1"].forecast_generated_at = nem_iso(PEAK_NEMTIME - timedelta(hours=6))
    return sensor, period, mock_store


def make_forecast(n: int) -> list:
    run = datetime(2026, 9, 2, 4, 0, tzinfo=NEM_TZ)
    return [
        make_price_period(run + timedelta(minutes=30 * (i + 1)), value=0.10 + i * 1e-4)
        for i in range(n)
    ]


# ── Value ─────────────────────────────────────────────────────────────────────

def test_export_native_value_is_the_raw_feed_in_rate():
    """native_value converts through spot_to_feed_in_tariff and adds no fee and no GST."""
    period = make_price_period(PEAK_NEMTIME, value=0.10)  # 0.10 $/kWh = 100 $/MWh
    sensor = make_export_sensor(price_periods=[period])
    feed_in_rate_c = 14.77

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=feed_in_rate_c) as mock_fit:
        val = sensor.native_value

    mock_fit.assert_called_once()
    assert abs(mock_fit.call_args[0][3] - 100.0) < 1e-6  # rrp_mwh
    expected_raw = round(feed_in_rate_c / 100, 6)
    assert val == expected_raw, f"Export tariff should be raw {expected_raw}, got {val}"
    old_formula = round((feed_in_rate_c / 100 + 0.0293) * 1.1, 6)
    assert val != old_formula, f"Export tariff should NOT include fee+GST ({old_formula})"


def test_export_tariff_stdout_suppressed():
    """Debug print() calls inside the library's feed-in conversion never reach stdout."""
    sensor = make_export_sensor(price_periods=[make_price_period(PEAK_NEMTIME, value=0.10)])

    def noisy_feed_in(*args, **kwargs):
        print("DEBUG: sapower feed_in_tariff lookup")
        return 14.77

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), \
            patch.object(_tariff_mod, "spot_to_feed_in_tariff", side_effect=noisy_feed_in):
        assert sensor.native_value is not None
    assert captured.getvalue() == "", f"Expected no stdout but got: {captured.getvalue()!r}"


# ── Identity and registration ─────────────────────────────────────────────────

@pytest.mark.parametrize("distributor, import_code, export_code, name", [
    ("ausgrid", "EA025", "EA029", "Ausgrid Residential Electrify Export Tariff (EA029)"),
    # A feed-in name that already says Export is not doubled.
    ("essential", "BLNRSS2", "BLNREX2", "Essential Energy LV Residential Solar Export Tariff (BLNREX2)"),
])
def test_init_sets_name_and_unique_id(distributor, import_code, export_code, name):
    """'<network> <feed-in name> Export Tariff (<code>)' and the _export_tariff unique_id, from __init__."""
    coordinator = MagicMock()
    coordinator.data = None
    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {}
    sensor = NemPd7dayExportTariffSensor(coordinator, entry, "NSW1", distributor, import_code, export_code)
    assert sensor._attr_name == name
    assert sensor._attr_unique_id == f"entry_1_NSW1_{distributor}_{import_code}_export_tariff"


def test_export_programs_registered_in_setup():
    """async_setup_entry registers one export sensor per library pairing for the region."""
    coordinator = MagicMock()
    coordinator.data = None

    entry = MagicMock()
    entry.entry_id = "entry_export"
    entry.data = {_const_mod.CONF_REGION: "NSW1"}
    entry.options = {
        _const_mod.CONF_FORECAST_MODE: _const_mod.FORECAST_MODE_DAYS_2_7,
        _const_mod.CONF_ACTIVE_TARIFF: "ausgrid/EA025",
    }
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator, store=MagicMock(), dispatch=None)

    hass = MagicMock()
    hass.data = {DOMAIN: {}}

    created = []

    def _add_entities(entities, update_before_add=False):
        created.extend(entities)

    run_async(_sensor_mod.async_setup_entry(hass, entry, _add_entities))

    export_sensors = [e for e in created if isinstance(e, NemPd7dayExportTariffSensor)]
    # NSW1 pairings come from the library (issue #159): ausgrid EA025->EA029 and
    # EA225->EA029, endeavour N71->N61 and N95->N95, essential BLNRSS2->BLNREX2 and
    # BLNBSS1->BLNBEX1, evoenergy 026->026.
    programs = {(s._distributor, s._import_code): s._export_code for s in export_sensors}
    assert programs == {
        ("ausgrid", "EA025"): "EA029",
        ("ausgrid", "EA225"): "EA029",
        ("endeavour", "N71"): "N61",
        ("endeavour", "N95"): "N95",
        ("essential", "BLNRSS2"): "BLNREX2",
        ("essential", "BLNBSS1"): "BLNBEX1",
        ("evoenergy", "026"): "026",
    }
    assert all(s._attr_unique_id.endswith("_export_tariff") for s in export_sensors)


# ── Calibration plumbing ──────────────────────────────────────────────────────

def test_export_tariff_uses_calibrated_price():
    """Export tariff passes calibrated $/MWh (not raw) to spot_to_feed_in_tariff."""
    sensor, _period, _store = make_calibrated_export_sensor(raw_value=0.01745, calibrated_value=0.01425)
    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        assert sensor.native_value is not None
    rrp = mock_fit.call_args[0][3]
    assert abs(rrp - 14.25) < 1e-6, f"Expected calibrated RRP 14.25 $/MWh, got {rrp}"


def test_export_tariff_forecast_spot_shows_calibrated():
    """Export forecast 'spot' is the calibrated value; 'spot_raw' the input; period fields present."""
    sensor, _period, _store = make_calibrated_export_sensor(raw_value=0.01745, calibrated_value=0.01425)
    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=10.0):
        forecast = sensor.extra_state_attributes["forecast"]
    assert forecast
    for entry in forecast:
        assert abs(entry["spot"] - 0.01425) < 1e-6, f"Export forecast spot should be calibrated, got {entry['spot']}"
        assert abs(entry["spot_raw"] - 0.01745) < 1e-6
        assert "period" in entry
        assert "network_rate" in entry


# #62: the export attribute loop calibrates each interval once and hands the
# result to _compute_export_tariff; native_value's direct call is unchanged.

def test_export_loop_calibrates_each_interval_once():
    periods = make_forecast(120)
    sensor = make_export_sensor(price_periods=periods)

    original = type(sensor)._calibrated_value
    calls = []

    def wrapper(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=8.0), \
            patch.object(type(sensor), "_calibrated_value", wrapper):
        attrs = sensor.extra_state_attributes

    assert len(attrs["forecast"]) == 120
    assert len(calls) == 120


def test_export_spot_key_is_the_value_that_was_fed_to_the_tariff():
    """The spot attribute and the feed-in input must remain the same number."""
    periods = make_forecast(8)
    sensor = make_export_sensor(price_periods=periods)

    seen = []

    def fake_calibrate(self, period):
        # Deliberately not derived from period.value, so a caller that
        # recomputed instead of reusing would produce a different number.
        v = 0.5 + len(seen) * 0.01
        seen.append(v)
        return v

    fed = []
    original = type(sensor)._compute_export_tariff

    def spy(self, period, calibrated=None):
        fed.append(calibrated)
        return original(self, period, calibrated=calibrated)

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=15.5), \
            patch.object(type(sensor), "_calibrated_value", fake_calibrate), \
            patch.object(type(sensor), "_compute_export_tariff", spy):
        attrs = sensor.extra_state_attributes

    assert len(seen) == 8
    assert fed == seen, "the calibrated value was not passed through"
    assert [e["spot"] for e in attrs["forecast"]] == [round(v, 6) for v in seen]


def test_export_compute_still_calibrates_when_not_given_a_value():
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ), value=0.10)
    sensor = make_export_sensor(price_periods=[period])
    calls = []

    def fake_calibrate(self, p):
        calls.append(p)
        return 0.42

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=15.5) as lib, \
            patch.object(type(sensor), "_calibrated_value", fake_calibrate):
        sensor._compute_export_tariff(period)

    assert len(calls) == 1, "the default path must still calibrate"
    assert lib.call_args[0][3] == pytest.approx(420.0), "0.42 $/kWh -> 420 $/MWh"


def test_export_compute_uses_the_supplied_value_and_does_not_calibrate():
    period = make_price_period(datetime(2026, 9, 2, 18, 0, tzinfo=NEM_TZ), value=0.10)
    sensor = make_export_sensor(price_periods=[period])

    def boom(self, p):
        raise AssertionError("_calibrated_value must not be called")

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=15.5) as lib, \
            patch.object(type(sensor), "_calibrated_value", boom):
        sensor._compute_export_tariff(period, calibrated=0.77)

    assert lib.call_args[0][3] == pytest.approx(770.0)


# ── Single-entry caches ───────────────────────────────────────────────────────

def test_compute_export_tariff_cache_hit():
    """Calling _compute_export_tariff twice with the same period calls the library once."""
    period = make_price_period(PEAK_NEMTIME, value=0.10)
    sensor = make_export_sensor(price_periods=[period])
    sensor._period_export_tariff_cache = None

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        result1 = sensor._compute_export_tariff(period)
        result2 = sensor._compute_export_tariff(period)
    assert result1 is not None
    assert result1 == result2
    assert mock_fit.call_count == 1


def test_apply_export_tariff_to_spot_cache_hit():
    """Calling _apply_export_tariff_to_spot twice with the same inputs calls the library once."""
    now = datetime(2026, 5, 24, 14, 12, 0, tzinfo=NEM_TZ)
    sensor = make_export_sensor(price_periods=[])
    sensor._export_tariff_cache = None

    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=14.77) as mock_fit:
        result1 = sensor._apply_export_tariff_to_spot(0.10, now)
        result2 = sensor._apply_export_tariff_to_spot(0.10, now)
    assert result1 is not None
    assert result1 == result2
    assert mock_fit.call_count == 1


@pytest.mark.parametrize("minute, expected_end", [
    (12, (14, 15)),   # inside the hour: the next 5-minute boundary
    (55, (15, 0)),    # the last 5-minute slot ends on the hour: rolls over
    (58, (15, 0)),
])
def test_apply_export_tariff_to_spot_passes_the_interval_end(minute, expected_end):
    """The dispatch path hands the library the interval END, rolling past the hour.

    The rollover branch used to be reached only when the wall clock happened to
    read minute 55 to 59 while the suite ran, so its coverage came and went.
    """
    now = datetime(2026, 5, 24, 14, minute, 30, tzinfo=NEM_TZ)
    sensor = make_export_sensor(price_periods=[])
    sensor._export_tariff_cache = None
    with patch.object(_tariff_mod, "spot_to_feed_in_tariff", return_value=15.5) as lib:
        assert sensor._apply_export_tariff_to_spot(0.10, now) is not None
    end = lib.call_args.args[0]
    assert (end.hour, end.minute, end.second, end.microsecond) == (*expected_end, 0, 0)

