"""Tariffs the library lacks are priced from tariff_extensions until it carries them.

Issue #170: Powercor's Residential CER two-way tariff (PRCER) is not in
aemo-to-tariff 0.7.27 and the catalogue is the library's, so a Powercor
customer had no PRCER sensor. The extension table carries it in the
library's conventions; the catalogue lets the library win once an installed
release has the code.

Rates: Powercor 2026-27 Tariff Summary (7 May 2026), GST exclusive.

Run with:  python -m pytest tests/test_tariff_extensions.py -v
"""
from __future__ import annotations

import datetime
import importlib.util
import logging
import os
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_const = _load("custom_components.nem_pd7day.const", os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"))
_ext = _load("custom_components.nem_pd7day.tariff_extensions", os.path.join(_ROOT, "custom_components", "nem_pd7day", "tariff_extensions.py"))
_cat = _load("custom_components.nem_pd7day.tariff_catalogue", os.path.join(_ROOT, "custom_components", "nem_pd7day", "tariff_catalogue.py"))

MEL = ZoneInfo("Australia/Melbourne")


def _end(month, hour, minute=0, day=15):
    """An interval END in Melbourne local time, in the 2026-27 year."""
    year = 2026 if month >= 7 else 2027
    return datetime.datetime(year, month, day, hour, minute, tzinfo=MEL)


# ── Pricing, mirrored on the upstream test vectors ────────────────────────────

@pytest.mark.parametrize("month, hour, minute, rate", [
    (7, 16, 5, 27.86),    # first peak interval, winter
    (1, 20, 55, 27.86),   # last peak interval, summer
    (9, 16, 5, 20.80),    # shoulder-season peak
    (4, 18, 0, 20.80),
    (7, 16, 0, 1.00),     # last saver interval (16:00 end covers 15:55)
    (9, 11, 5, 1.00),     # first saver interval
    (7, 11, 0, 4.20),     # last off-peak interval before saver
    (9, 21, 5, 4.20),     # first off-peak interval after peak
    (1, 3, 0, 4.20),
], ids=lambda v: str(v))
def test_prcer_import_rate_by_season_and_window(month, hour, minute, rate):
    price = _ext.spot_to_tariff(_end(month, hour, minute), "powercor", "PRCER", 100.0)
    assert price == pytest.approx(10.0 + rate)


def test_prcer_import_applies_the_loss_factors_to_spot_like_the_library():
    price = _ext.spot_to_tariff(_end(7, 3, 0), "powercor", "PRCER", 100.0, dlf=1.05, mlf=1.01, market=1.02)
    assert price == pytest.approx(100.0 * 1.05 * 1.01 * 1.02 / 10 + 4.20)


@pytest.mark.parametrize("month, hour, adjustment", [
    (7, 18, +7.00),    # winter peak export credit
    (2, 16, +7.00),    # 16:05 end is the first credit interval; 16:00 end is charge
    (12, 18, +7.00),   # December carries the credit...
    (12, 13, -1.00),   # ...and the saver export charge
    (9, 13, -1.00),    # Sep-May charge
    (9, 18, 0.0),      # shoulder season, no credit
    (7, 13, 0.0),      # Jun-Aug, no charge
    (7, 22, 0.0),      # outside both windows
], ids=lambda v: str(v))
def test_prcer_export_credit_and_charge_windows(month, hour, adjustment):
    minute = 5 if hour == 16 else 0
    price = _ext.spot_to_feed_in_tariff(_end(month, hour, minute), "powercor", "PRCER", 100.0)
    assert price == pytest.approx(10.0 + adjustment)


def test_prcer_periods_follow_the_season_and_fee_is_the_schedule():
    assert [r[3] for r in _ext.get_periods("powercor", "PRCER", _end(12, 12))] == [4.20, 1.00, 27.86, 4.20]
    assert [r[3] for r in _ext.get_periods("powercor", "PRCER", _end(10, 12))] == [4.20, 1.00, 20.80, 4.20]
    assert [r[0] for r in _ext.feed_in_periods_for("powercor", "PRCER", _end(12, 12))] == [
        "Peak export credit", "Saver export charge",
    ]
    assert [r[0] for r in _ext.feed_in_periods_for("powercor", "PRCER", _end(7, 12))] == ["Peak export credit"]
    assert [r[0] for r in _ext.feed_in_periods_for("powercor", "PRCER", _end(10, 12))] == ["Saver export charge"]
    assert _ext.get_daily_fee("powercor", "PRCER") == 43.84


def test_every_extension_names_its_source_and_exit():
    for (distributor, code), ext in _ext.EXTENSIONS.items():
        assert ext.source and ext.remove_when, (distributor, code)
        assert set().union(*ext.season_months.values()) == set(range(1, 13)), (distributor, code)


# ── The catalogue merges the extension and lets the library win ───────────────

aemo_to_tariff = pytest.importorskip("aemo_to_tariff")


def test_prcer_is_in_the_catalogue_after_the_library_codes():
    codes = _cat.import_tariff_codes("powercor")
    assert codes[-1] == "PRCER"
    assert "PRCER" not in _cat._library_import_tariffs("powercor")
    assert _cat.tariff_name("powercor", "PRCER") == "Residential CER"
    assert _cat.tariff_name("powercor", "PRCER", export=True) == "Residential CER Export"
    assert _cat.priced_by_extension("powercor", "PRCER")
    assert _cat.priced_by_extension("powercor", "PRCER", export=True)
    assert not _cat.priced_by_extension("powercor", "PRTOU")
    assert not _cat.priced_by_extension("energex", "6900", export=True)


def test_prcer_pairs_with_itself_as_an_export_program():
    assert _cat.export_programs("powercor") == {"PRCER": "PRCER"}
    assert _cat.export_program_supported("powercor", "PRCER")
    assert _const.EXPORT_TARIFF_PROGRAMS[("powercor", "PRCER")] == "PRCER"


def test_the_library_wins_once_it_carries_the_code(monkeypatch, caplog):
    """A release with PRCER makes the extension entry inert, and says so once."""
    lib = SimpleNamespace(
        powercor=SimpleNamespace(
            __name__="powercor",
            tariffs={"PRTOU": {"name": "Residential TOU"}, "PRCER": {"name": "Residential CER (library)"}},
            feed_in_tariffs={"PRCER": {"name": "Residential CER Export (library)"}},
        ),
    )
    monkeypatch.setattr(_cat, "_att", lib)
    monkeypatch.setattr(_cat, "_logged_superseded", set())
    with caplog.at_level(logging.INFO, logger=_cat.__name__):
        assert not _cat.priced_by_extension("powercor", "PRCER")
        assert not _cat.priced_by_extension("powercor", "PRCER", export=True)
        assert not _cat.priced_by_extension("powercor", "PRCER")
    assert sum("can be removed" in r.getMessage() for r in caplog.records) == 1
    assert _cat.tariff_name("powercor", "PRCER") == "Residential CER (library)"
    assert _cat.import_tariff_codes("powercor").count("PRCER") == 1


# ── The sensors price PRCER through the extension ─────────────────────────────

from test_export_tariff import make_export_sensor  # noqa: E402
from test_tariff_sensor import _tariff_mod, make_price_period, make_real_sensor, make_tariff_sensor  # noqa: E402


def _sensor_module_clock(monkeypatch, sensor, when):
    # Each test file loads its own tariff_sensor module object; patch the one
    # this sensor's class was defined in.
    monkeypatch.setitem(type(sensor)._get_tariff_periods.__globals__, "now_nem", lambda: when)


def test_import_sensor_prices_prcer_from_the_extension(monkeypatch):
    # A winter evening peak interval: 100 $/MWh spot, 27.86 c/kWh network,
    # 0.0293 $/kWh usage fee, GST once over the lot.
    end = _end(7, 18, 0).astimezone(datetime.timezone(datetime.timedelta(hours=10)))
    period = make_price_period(end, 0.10)
    sensor = make_tariff_sensor(region="VIC1", distributor="powercor", tariff_code="PRCER", price_periods=[period])
    sensor._get_additional_fee = lambda: 0.0293
    value = sensor._compute_tariff(period, calibrated=0.10)
    spot = 100.0 * _tariff_mod._DEFAULT_DLF * _tariff_mod._DEFAULT_MLF * _tariff_mod._DEFAULT_MARKET / 10
    assert value == pytest.approx(round(((spot + 27.86) / 100 + 0.0293) * 1.1, 6))
    _sensor_module_clock(monkeypatch, sensor, end)
    attrs = sensor.extra_state_attributes
    assert attrs["tariff_source"] == "nem_pd7day extension"
    # The attribute carries what get_daily_fee returns, c/day, for library
    # tariffs and extension tariffs alike (its name says $; see #171).
    assert attrs["daily_supply_charge_$"] == pytest.approx(43.84)
    assert [p["network_rate_$/kwh"] for p in attrs["tariff_periods"]] == [0.042, 0.01, 0.2786, 0.042]
    real = make_real_sensor(_tariff_mod.NemPd7dayTariffSensor, "VIC1", "powercor", "PRCER")
    assert real._attr_name == "Powercor Residential CER Tariff (PRCER)"


def test_export_sensor_prices_prcer_from_the_extension(monkeypatch):
    end = _end(7, 18, 0).astimezone(datetime.timezone(datetime.timedelta(hours=10)))
    period = make_price_period(end, 0.10)
    sensor = make_export_sensor(region="VIC1", distributor="powercor", import_code="PRCER", export_code="PRCER", price_periods=[period])
    value = sensor._compute_export_tariff(period, calibrated=0.10)
    # Same spot composition as the library's feed-in path: loss factors on spot.
    spot = 100.0 * _tariff_mod._DEFAULT_DLF * _tariff_mod._DEFAULT_MLF * _tariff_mod._DEFAULT_MARKET / 10
    assert value == pytest.approx(round((spot + 7.0) / 100, 6))
    assert value != pytest.approx(round((10.0 + 7.0) / 100, 6))
    _sensor_module_clock(monkeypatch, sensor, end)
    attrs = sensor.extra_state_attributes
    assert attrs["tariff_source"] == "nem_pd7day extension"
    assert attrs["export_periods"] == [
        {"period": "Peak export credit", "start": "16:00", "end": "21:00", "export_adjustment_$/kwh": 0.07},
    ]
    real = _tariff_mod.NemPd7dayExportTariffSensor(MagicMock(data=None), MagicMock(entry_id="entry_1", options={}), "VIC1", "powercor", "PRCER", "PRCER")
    assert real._attr_name == "Powercor Residential CER Export Tariff (PRCER)"


def test_library_tariffs_keep_their_source_label():
    period = make_price_period(_end(7, 18, 0), 0.10)
    sensor = make_tariff_sensor(region="VIC1", distributor="powercor", tariff_code="PRTOU", price_periods=[period])
    assert sensor.extra_state_attributes["tariff_source"] == "aemo-to-tariff"
