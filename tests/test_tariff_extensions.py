"""Tariffs the library lacks are priced from tariff_extensions until it carries them.

Issue #170: Powercor's Residential CER two-way tariff (PRCER) was not in
aemo-to-tariff 0.7.27 and the catalogue is the library's, so a Powercor
customer had no PRCER sensor. The extension table carried it in the
library's conventions; the catalogue lets the library win once an installed
release has the code. 0.7.28 does (#165), so the table is empty and the
machinery is exercised through a synthetic entry carrying PRCER's schedule
(support.extension_fixture). The PRCER vectors now check the library.

Rates: Powercor 2026-27 Tariff Summary (7 May 2026), GST exclusive.

Run with:  python -m pytest tests/test_tariff_extensions.py -v
"""
from __future__ import annotations

import datetime
import logging
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from unittest.mock import MagicMock

from support import EXTENSION_CODE as XCER, install_extension_fixture, load_chain

_const, _ext, _cat = load_chain("const", "tariff_extensions", "tariff_catalogue")

MEL = ZoneInfo("Australia/Melbourne")


def _end(month, hour, minute=0, day=15):
    """An interval END in Melbourne local time, in the 2026-27 year."""
    year = 2026 if month >= 7 else 2027
    return datetime.datetime(year, month, day, hour, minute, tzinfo=MEL)


@pytest.fixture
def fixture_entry(monkeypatch):
    install_extension_fixture(monkeypatch, _ext, _cat)


IMPORT_VECTORS = [
    (7, 16, 5, 27.86),    # first peak interval, winter
    (1, 20, 55, 27.86),   # last peak interval, summer
    (9, 16, 5, 20.80),    # shoulder-season peak
    (4, 18, 0, 20.80),
    (7, 16, 0, 1.00),     # last saver interval (16:00 end covers 15:55)
    (9, 11, 5, 1.00),     # first saver interval
    (7, 11, 0, 4.20),     # last off-peak interval before saver
    (9, 21, 5, 4.20),     # first off-peak interval after peak
    (1, 3, 0, 4.20),
]
EXPORT_VECTORS = [
    (7, 18, +7.00),    # winter peak export credit
    (2, 16, +7.00),    # 16:05 end is the first credit interval; 16:00 end is charge
    (12, 18, +7.00),   # December carries the credit...
    (12, 13, -1.00),   # ...and the saver export charge
    (9, 13, -1.00),    # Sep-May charge
    (9, 18, 0.0),      # shoulder season, no credit
    (7, 13, 0.0),      # Jun-Aug, no charge
    (7, 22, 0.0),      # outside both windows
]


# ── The extension machinery, through the fixture entry ────────────────────────

@pytest.mark.parametrize("month, hour, minute, rate", IMPORT_VECTORS, ids=lambda v: str(v))
def test_extension_import_rate_by_season_and_window(fixture_entry, month, hour, minute, rate):
    price = _ext.spot_to_tariff(_end(month, hour, minute), "powercor", XCER, 100.0)
    assert price == pytest.approx(10.0 + rate)


def test_extension_import_applies_the_loss_factors_to_spot_like_the_library(fixture_entry):
    price = _ext.spot_to_tariff(_end(7, 3, 0), "powercor", XCER, 100.0, dlf=1.05, mlf=1.01, market=1.02)
    assert price == pytest.approx(100.0 * 1.05 * 1.01 * 1.02 / 10 + 4.20)


@pytest.mark.parametrize("month, hour, adjustment", EXPORT_VECTORS, ids=lambda v: str(v))
def test_extension_export_credit_and_charge_windows(fixture_entry, month, hour, adjustment):
    minute = 5 if hour == 16 else 0
    price = _ext.spot_to_feed_in_tariff(_end(month, hour, minute), "powercor", XCER, 100.0)
    assert price == pytest.approx(10.0 + adjustment)


def test_extension_periods_follow_the_season_and_fee_is_the_schedule(fixture_entry):
    assert [r[3] for r in _ext.get_periods("powercor", XCER, _end(12, 12))] == [4.20, 1.00, 27.86, 4.20]
    assert [r[3] for r in _ext.get_periods("powercor", XCER, _end(10, 12))] == [4.20, 1.00, 20.80, 4.20]
    assert [r[0] for r in _ext.feed_in_periods_for("powercor", XCER, _end(12, 12))] == [
        "Peak export credit", "Saver export charge",
    ]
    assert [r[0] for r in _ext.feed_in_periods_for("powercor", XCER, _end(7, 12))] == ["Peak export credit"]
    assert [r[0] for r in _ext.feed_in_periods_for("powercor", XCER, _end(10, 12))] == ["Saver export charge"]
    assert _ext.get_daily_fee("powercor", XCER) == 43.84


def test_every_extension_names_its_source_and_exit(fixture_entry):
    for (distributor, code), ext in _ext.EXTENSIONS.items():
        assert ext.source and ext.remove_when, (distributor, code)
        assert set().union(*ext.season_months.values()) == set(range(1, 13)), (distributor, code)


# ── The catalogue merges an extension and lets the library win ────────────────

aemo_to_tariff = pytest.importorskip("aemo_to_tariff")


def test_the_shipped_table_is_empty_while_the_library_carries_prcer():
    assert _ext.EXTENSIONS == {}
    assert "PRCER" in _cat._library_import_tariffs("powercor")
    assert not _cat.priced_by_extension("powercor", "PRCER")
    assert not _cat.priced_by_extension("powercor", "PRCER", export=True)


def test_an_extension_is_in_the_catalogue_after_the_library_codes(fixture_entry):
    codes = _cat.import_tariff_codes("powercor")
    assert codes[-1] == XCER and codes.count("PRCER") == 1
    assert _cat.tariff_name("powercor", XCER) == "Test CER"
    assert _cat.tariff_name("powercor", XCER, export=True) == "Test CER Export"
    assert _cat.priced_by_extension("powercor", XCER)
    assert _cat.priced_by_extension("powercor", XCER, export=True)
    assert _cat.seasonal("powercor", XCER)
    assert not _cat.priced_by_extension("powercor", "PRTOU")
    assert not _cat.priced_by_extension("energex", "6900", export=True)


def test_prcer_pairs_with_itself_as_an_export_program():
    assert _cat.export_programs("powercor") == {"PRCER": "PRCER"}
    assert _cat.export_program_supported("powercor", "PRCER")
    assert _const.EXPORT_TARIFF_PROGRAMS[("powercor", "PRCER")] == "PRCER"


def test_the_library_wins_once_it_carries_the_code(fixture_entry, monkeypatch, caplog):
    """A release with the code makes the extension entry inert, and says so once."""
    lib = SimpleNamespace(
        powercor=SimpleNamespace(
            __name__="powercor",
            tariffs={"PRTOU": {"name": "Residential TOU"}, XCER: {"name": "Test CER (library)"}},
            feed_in_tariffs={XCER: {"name": "Test CER Export (library)"}},
        ),
    )
    monkeypatch.setattr(_cat, "_att", lib)
    monkeypatch.setattr(_cat, "_logged_superseded", set())
    with caplog.at_level(logging.INFO, logger=_cat.__name__):
        assert not _cat.priced_by_extension("powercor", XCER)
        assert not _cat.priced_by_extension("powercor", XCER, export=True)
        assert not _cat.priced_by_extension("powercor", XCER)
    assert sum("can be removed" in r.getMessage() for r in caplog.records) == 1
    assert _cat.tariff_name("powercor", XCER) == "Test CER (library)"
    assert _cat.import_tariff_codes("powercor").count(XCER) == 1


# ── The library's PRCER is the schedule the extension carried ─────────────────

LOSS = {"dlf": 1.05905, "mlf": 1.0154, "market": 1.0154}


def test_library_prcer_prices_every_interval_as_the_extension_did(fixture_entry):
    """#165: 0.7.28 took the extension's place; a year of intervals agree to the cent."""
    from aemo_to_tariff import get_daily_fee, spot_to_feed_in_tariff, spot_to_tariff

    start = datetime.datetime(2026, 7, 1, 0, 5, tzinfo=MEL)
    for step in range(0, 365 * 24, 7):   # every 7 hours, so the hour and weekday both walk
        for minute in (0, 5, 55):
            end = start + datetime.timedelta(hours=step, minutes=minute)
            assert spot_to_tariff(end, "powercor", "PRCER", 100.0, **LOSS) == pytest.approx(
                _ext.spot_to_tariff(end, "powercor", XCER, 100.0, **LOSS)), end
            assert spot_to_feed_in_tariff(end, "powercor", "PRCER", 100.0) == pytest.approx(
                _ext.spot_to_feed_in_tariff(end, "powercor", XCER, 100.0, **LOSS)), end
    assert get_daily_fee("powercor", "PRCER") == _ext.get_daily_fee("powercor", XCER)


@pytest.mark.parametrize("month, hour, minute, rate", IMPORT_VECTORS, ids=lambda v: str(v))
def test_library_prcer_import_rate_by_season_and_window(month, hour, minute, rate):
    from aemo_to_tariff import spot_to_tariff

    assert spot_to_tariff(_end(month, hour, minute), "powercor", "PRCER", 100.0, dlf=1, mlf=1, market=1) == pytest.approx(10.0 + rate)


def test_library_month_gated_feed_in_rows_follow_the_network_month():
    rows = _cat.month_gated_feed_in_rows
    assert [r[0] for r in rows("powercor", "PRCER", _end(12, 12))] == ["Peak export credit", "Saver export charge"]
    assert [r[3] for r in rows("powercor", "PRCER", _end(7, 12))] == [7.0]
    assert [r[3] for r in rows("powercor", "PRCER", _end(10, 12))] == [-1.0]
    # 23:30 on 30 November in NEM time is 00:30 on 1 December in Melbourne
    # (daylight saving), where the peak season has begun.
    nem_edge = datetime.datetime(2026, 11, 30, 23, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=10)))
    assert [r[0] for r in rows("powercor", "PRCER", nem_edge)] == ["Peak export credit", "Saver export charge"]
    assert rows("powercor", "PRTOU", _end(12, 12)) == []
    assert rows("endeavour", "N61", _end(12, 12)) == []   # four-field rows are not published
    assert _cat.seasonal("powercor", "PRCER") and not _cat.seasonal("powercor", "PRTOU")


# ── The sensors: PRCER through the library, the fixture through the extension ─

from test_export_tariff import make_export_sensor  # noqa: E402
from test_tariff_sensor import _tariff_mod, make_price_period, make_real_sensor, make_tariff_sensor  # noqa: E402

_SPOT = 100.0 * _tariff_mod.tariff_pricing.DEFAULT_DLF * _tariff_mod.tariff_pricing.DEFAULT_MLF * _tariff_mod.tariff_pricing.DEFAULT_MARKET / 10


def _sensor_module_clock(monkeypatch, sensor, when):
    # Each test file loads its own tariff_sensor module object; patch the one
    # this sensor's class was defined in.
    monkeypatch.setitem(type(sensor)._get_tariff_periods.__globals__, "now_nem", lambda: when)


@pytest.fixture
def sensor_fixture_entry(monkeypatch):
    install_extension_fixture(monkeypatch, _tariff_mod)


@pytest.mark.parametrize("code, source", [("PRCER", "aemo-to-tariff"), (XCER, "nem_pd7day extension")])
def test_import_sensor_prices_the_cer_schedule(sensor_fixture_entry, monkeypatch, code, source):
    # A winter evening peak interval: 100 $/MWh spot, 27.86 c/kWh network,
    # 0.0293 $/kWh usage fee, GST once over the lot.
    end = _end(7, 18, 0).astimezone(datetime.timezone(datetime.timedelta(hours=10)))
    period = make_price_period(end, 0.10)
    sensor = make_tariff_sensor(region="VIC1", distributor="powercor", tariff_code=code, price_periods=[period])
    sensor._get_additional_fee = lambda: 0.0293
    value = sensor._compute_tariff(period, calibrated=0.10)
    assert value == pytest.approx(round(((_SPOT + 27.86) / 100 + 0.0293) * 1.1, 6))
    _sensor_module_clock(monkeypatch, sensor, end)
    attrs = sensor.extra_state_attributes
    assert attrs["tariff_source"] == source
    # The schedule's 43.84 c/day, published in $/day (#171).
    assert attrs["daily_supply_charge_$"] == 0.4384
    # The season at the sensor's clock, not the wall clock's.
    assert [p["network_rate_$/kwh"] for p in attrs["tariff_periods"]] == [0.042, 0.01, 0.2786, 0.042]
    _sensor_module_clock(monkeypatch, sensor, _end(10, 12))
    assert [p["network_rate_$/kwh"] for p in sensor.extra_state_attributes["tariff_periods"]] == [0.042, 0.01, 0.208, 0.042]


def test_a_seasonal_sensor_does_not_serve_its_construction_time_periods(monkeypatch):
    real = make_real_sensor(_tariff_mod.NemPd7dayTariffSensor, "VIC1", "powercor", "PRCER")
    assert real._attr_name == "Powercor Residential CER Tariff (PRCER)"
    assert real._seasonal_periods is True
    real._cached_tariff_periods = [{"period": "stale"}]
    _sensor_module_clock(monkeypatch, real, _end(7, 12))
    assert [p["period"] for p in real._tariff_periods_for_attrs()] == ["Off-peak", "Saver", "Peak", "Off-peak"]
    flat = make_real_sensor(_tariff_mod.NemPd7dayTariffSensor, "VIC1", "powercor", "PRTOU")
    assert flat._seasonal_periods is False
    flat._cached_tariff_periods = [{"period": "cached"}]
    assert flat._tariff_periods_for_attrs() == [{"period": "cached"}]


@pytest.mark.parametrize("code, source", [("PRCER", "aemo-to-tariff"), (XCER, "nem_pd7day extension")])
def test_export_sensor_prices_the_cer_schedule(sensor_fixture_entry, monkeypatch, code, source):
    end = _end(7, 18, 0).astimezone(datetime.timezone(datetime.timedelta(hours=10)))
    period = make_price_period(end, 0.10)
    sensor = make_export_sensor(region="VIC1", distributor="powercor", import_code=code, export_code=code, price_periods=[period])
    value = sensor._compute_export_tariff(period, calibrated=0.10)
    # Loss factors on spot, the library's feed-in composition.
    assert value == pytest.approx(round((_SPOT + 7.0) / 100, 6))
    assert value != pytest.approx(round((10.0 + 7.0) / 100, 6))
    _sensor_module_clock(monkeypatch, sensor, end)
    attrs = sensor.extra_state_attributes
    assert attrs["tariff_source"] == source
    assert attrs["export_periods"] == [
        {"period": "Peak export credit", "start": "16:00", "end": "21:00", "export_adjustment_$/kwh": 0.07},
    ]
    real = _tariff_mod.NemPd7dayExportTariffSensor(MagicMock(data=None), MagicMock(entry_id="entry_1", options={}), "VIC1", "powercor", "PRCER", "PRCER")
    assert real._attr_name == "Powercor Residential CER Export Tariff (PRCER)"


def test_library_tariffs_keep_their_source_label():
    period = make_price_period(_end(7, 18, 0), 0.10)
    sensor = make_tariff_sensor(region="VIC1", distributor="powercor", tariff_code="PRTOU", price_periods=[period])
    assert sensor.extra_state_attributes["tariff_source"] == "aemo-to-tariff"
