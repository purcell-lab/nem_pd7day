"""Contract tests for tariff_pricing.py, the tariff rules moved out of the sensors.

Spec 001 (docs/specs/001-tariff-pricing.md) moves the conversion of a spot
price into a network tariff price out of tariff_sensor.py: which source prices
a code (the aemo_to_tariff library or the tariff_extensions table, #170 and
#172), the GST correction for the library's inconsistent treatment (#158),
period row normalisation, and the time-of-use window lookup (#62). These tests
pin the module's contract directly, without the HA stubs:

  * invariant 1: RetailPrice.import_dollars is the pre-spec expression, bit
    for bit, compared with ``==`` against that expression written out here;
  * invariant 2: pricer_for makes the catalogue's routing decision for every
    catalogue import and export code;
  * the library call signatures (import with the loss factors, feed-in
    without) and the extension feed-in with the defaults;
  * period row normalisation, the feed-in window rows, and TouWindows;
  * library_available and stdout suppression;
  * the sensors' library-missing guard, which no test pinned before.

Run with:  python -m pytest tests/test_tariff_pricing.py -v
"""
from __future__ import annotations

import ast
import contextlib
import datetime
import io
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from support import EXTENSION_CODE as XCER, PKG_DIR, install_extension_fixture, load_chain, make_price_period, install_ha_stubs

# Standalone copy, per the spec: tariff_pricing needs only the catalogue and
# the extension table, and no HA stubs at all.
_const, _ext, _cat, tp = load_chain("const", "tariff_extensions", "tariff_catalogue", "tariff_pricing")

NEM = datetime.timezone(datetime.timedelta(hours=10))
DISTRIBUTORS = sorted({d for ds in _const.REGION_DISTRIBUTORS.values() for d in ds})
LIBRARY_NAMES = ("spot_to_tariff", "spot_to_feed_in_tariff", "get_periods", "get_daily_fee")

needs_library = pytest.mark.skipif(not tp.library_available(), reason="aemo_to_tariff not installed")


def _catalogue_codes(export: bool) -> list[tuple[str, str]]:
    """Every (distributor, code) the catalogue builds an import or export sensor for, or lists."""
    out: list[tuple[str, str]] = []
    for d in DISTRIBUTORS:
        if export:
            codes = sorted(set(_cat.export_programs(d).values()) | set(_cat.unpaired_export_codes(d)))
        else:
            codes = _cat.import_tariff_codes(d)
        out.extend((d, code) for code in codes)
    return out


# ── Invariant 1: the retail price is the pre-spec expression, bit for bit ────

def _old_retail_price(distributor: str, result_c_kwh: float, rrp_mwh: float, fee: float) -> float:
    """NemPd7dayTariffSensor._retail_price as it was before spec 001, constants inlined."""
    spot_c_kwh = rrp_mwh * 1.05905 * 1.0154 * 1.0154 / 10
    network_c_kwh = result_c_kwh - spot_c_kwh
    if distributor in frozenset({"energex", "ergon", "ausgrid", "endeavour", "essential", "evoenergy", "sapn"}):
        network_c_kwh /= 1.1
    return round(((spot_c_kwh + network_c_kwh) / 100 + fee) * 1.1, 6)


PRICE_GRID = (-1000.0, -55.27, -0.01, 0.0, 0.01, 35.5, 87.31, 300.0, 1234.567, 17500.0)
FEE_GRID = (0.0, 0.0293, 0.05, 0.15)
INTERVAL_ENDS = (
    datetime.datetime(2026, 9, 2, 3, 0, tzinfo=NEM),
    datetime.datetime(2026, 9, 2, 18, 0, tzinfo=NEM),
    datetime.datetime(2027, 1, 14, 13, 30, tzinfo=NEM),
)


def test_constants_are_the_published_values():
    assert (tp.DEFAULT_DLF, tp.DEFAULT_MLF, tp.DEFAULT_MARKET, tp.GST) == (1.05905, 1.0154, 1.0154, 1.1)
    assert tp.COMBINED_LOSS_MULTIPLIER == round(1.05905 * 1.0154 * 1.0154, 6)
    assert tp.LIB_APPLIES_GST == {"energex", "ergon", "ausgrid", "endeavour", "essential", "evoenergy", "sapn"}


@pytest.mark.parametrize("distributor", DISTRIBUTORS + ["unknown"])
def test_import_dollars_matches_the_old_expression_on_a_synthetic_grid(distributor):
    """Every branch of the GST split, independent of the library's output."""
    for lib_c in (-90.0, -3.3, 0.0, 0.07, 15.5, 27.86, 999.99):
        for rrp in PRICE_GRID:
            for fee in FEE_GRID:
                got = tp.RetailPrice(distributor).import_dollars(lib_c, rrp, fee)
                assert got == _old_retail_price(distributor, lib_c, rrp, fee), (lib_c, rrp, fee)


@needs_library
@pytest.mark.parametrize("distributor", DISTRIBUTORS)
def test_import_dollars_matches_the_old_expression_for_every_catalogue_code(distributor):
    """Invariant 1 over the real library's (or extension's) output for every catalogue code."""
    checked = 0
    for code in _cat.import_tariff_codes(distributor):
        pricer = tp.pricer_for(distributor, code)
        for end in INTERVAL_ENDS:
            for rrp in PRICE_GRID:
                try:
                    lib_c = pricer.import_c_kwh(end, rrp)
                except Exception:  # noqa: BLE001 - a code the library cannot price has no price to compare
                    continue
                for fee in FEE_GRID:
                    got = tp.RetailPrice(distributor).import_dollars(lib_c, rrp, fee)
                    assert got == _old_retail_price(distributor, lib_c, rrp, fee), (code, end, rrp, fee)
                    checked += 1
    assert checked, f"no catalogue code of {distributor} could be priced"


def test_export_dollars_is_a_plain_conversion():
    for lib_c in (-12.5, 0.0, 7.0, 14.77, 1753.4):
        assert tp.RetailPrice.export_dollars(lib_c) == round(lib_c / 100, 6)


# ── Invariant 2: routing follows the catalogue ────────────────────────────────

@pytest.mark.parametrize("export", [False, True], ids=["import", "export"])
def test_pricer_for_makes_the_catalogue_decision_for_every_code(monkeypatch, export):
    # The shipped table is empty (#165); the fixture entry keeps both kinds present.
    install_extension_fixture(monkeypatch, tp)
    decisions = set()
    for distributor, code in _catalogue_codes(export):
        pricer = tp.pricer_for(distributor, code, export=export)
        by_extension = _cat.priced_by_extension(distributor, code, export=export)
        decisions.add(by_extension)
        assert isinstance(pricer, tp.ExtensionPricer if by_extension else tp.LibraryPricer), (distributor, code)
        assert (pricer.distributor, pricer.code) == (distributor, code)
        assert pricer.source == ("nem_pd7day extension" if by_extension else "aemo-to-tariff")
    # Non-vacuity: the catalogue carries both kinds.
    assert decisions == {False, True}


def test_pricer_for_follows_priced_by_extension_when_it_changes_its_mind():
    with patch.object(tp.tariff_catalogue, "priced_by_extension", return_value=False) as routed:
        assert isinstance(tp.pricer_for("powercor", "PRCER", export=True), tp.LibraryPricer)
    routed.assert_called_once_with("powercor", "PRCER", export=True)
    with patch.object(tp.tariff_catalogue, "priced_by_extension", return_value=True):
        assert isinstance(tp.pricer_for("energex", "6900"), tp.ExtensionPricer)


# ── Library call signatures ──────────────────────────────────────────────────

END = datetime.datetime(2026, 9, 2, 18, 0, tzinfo=NEM)


def test_library_import_passes_the_loss_factors_explicitly():
    with patch.object(tp, "spot_to_tariff", return_value=15.5) as lib:
        assert tp.LibraryPricer("energex", "6900").import_c_kwh(END, 87.5) == 15.5
    lib.assert_called_once_with(END, "energex", "6900", 87.5, dlf=1.05905, mlf=1.0154, market=1.0154)


def test_library_feed_in_passes_no_loss_factors():
    with patch.object(tp, "spot_to_feed_in_tariff", return_value=6.1) as lib:
        assert tp.LibraryPricer("energex", "6900X").feed_in_c_kwh(END, 87.5) == 6.1
    lib.assert_called_once_with(END, "energex", "6900X", 87.5)


def test_library_periods_take_the_time_and_daily_fee_takes_the_code():
    rows = [("Peak", datetime.time(16), datetime.time(20), 25.0)]
    with patch.object(tp, "get_periods", return_value=iter(rows)) as periods, \
            patch.object(tp, "get_daily_fee", return_value=55.6) as fee:
        pricer = tp.LibraryPricer("energex", "6900")
        assert pricer.period_rows(END) == rows
        assert pricer.daily_fee() == 55.6
        assert pricer.feed_in_rows(END) == []
    # The time picks the price year and a seasonal tariff's season (#165).
    periods.assert_called_once_with("energex", "6900", END)
    fee.assert_called_once_with("energex", "6900")


def test_pricers_raise_what_the_underlying_call_raises():
    with patch.object(tp, "spot_to_tariff", side_effect=ValueError("Unknown tariff code")):
        with pytest.raises(ValueError, match="Unknown tariff code"):
            tp.LibraryPricer("energex", "9999").import_c_kwh(END, 50.0)
    with pytest.raises(KeyError):
        tp.ExtensionPricer("energex", "9999").daily_fee()


def test_extension_feed_in_passes_the_default_loss_factors(monkeypatch):
    """#174: an extension export prices its spot the way the library's feed-in does."""
    install_extension_fixture(monkeypatch, tp)
    real = tp.tariff_extensions.spot_to_feed_in_tariff
    winter_end = datetime.datetime(2026, 7, 15, 18, 0, tzinfo=NEM)
    with patch.object(tp.tariff_extensions, "spot_to_feed_in_tariff", side_effect=real) as ext:
        value = tp.ExtensionPricer("powercor", XCER).feed_in_c_kwh(winter_end, 100.0)
    ext.assert_called_once_with(winter_end, "powercor", XCER, 100.0, dlf=1.05905, mlf=1.0154, market=1.0154)
    # A July evening interval carries the peak export credit.
    assert value == 100.0 * 1.05905 * 1.0154 * 1.0154 / 10 + 7.0


def test_extension_import_passes_the_default_loss_factors(monkeypatch):
    install_extension_fixture(monkeypatch, tp)
    real = tp.tariff_extensions.spot_to_tariff
    with patch.object(tp.tariff_extensions, "spot_to_tariff", side_effect=real) as ext:
        value = tp.ExtensionPricer("powercor", XCER).import_c_kwh(END, 100.0)
    ext.assert_called_once_with(END, "powercor", XCER, 100.0, dlf=1.05905, mlf=1.0154, market=1.0154)
    assert value == 100.0 * 1.05905 * 1.0154 * 1.0154 / 10 + 20.80


def test_extension_rows_and_fee_come_from_the_table_at_the_time_given(monkeypatch):
    install_extension_fixture(monkeypatch, tp)
    pricer = tp.ExtensionPricer("powercor", XCER)
    winter = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=NEM)
    autumn = datetime.datetime(2027, 4, 15, 12, 0, tzinfo=NEM)
    assert [row[-1] for row in pricer.period_rows(winter)] == [4.20, 1.00, 27.86, 4.20]
    assert [row[-1] for row in pricer.period_rows(autumn)] == [4.20, 1.00, 20.80, 4.20]
    assert [row[0] for row in pricer.feed_in_rows(winter)] == ["Peak export credit"]
    assert [row[0] for row in pricer.feed_in_rows(autumn)] == ["Saver export charge"]
    assert pricer.daily_fee() == 43.84


# ── Period rows ──────────────────────────────────────────────────────────────

def test_period_attributes_normalises_four_and_five_tuples():
    rows = [
        ("Peak", datetime.time(16), datetime.time(20), 25.0),
        ("Shoulder", datetime.time(7), datetime.time(16, 30), [11, 12, 1], 19.14),
    ]
    assert tp.period_attributes(rows) == [
        {"period": "Peak", "start": "16:00", "end": "20:00", "network_rate_$/kwh": 0.25},
        {"period": "Shoulder", "start": "07:00", "end": "16:30", "network_rate_$/kwh": 0.1914},
    ]


def test_period_attributes_skips_short_unpriced_and_untimed_rows():
    rows = [
        ("Short", datetime.time(1), datetime.time(2)),
        ("No rate", datetime.time(1), datetime.time(2), None),
        ("No start", None, datetime.time(2), 3.0),
        ("No end", datetime.time(1), None, 3.0),
        ("Off-peak", None, None, None, 10.34),
        ("Kept", datetime.time(21), datetime.time(7), 5.123456789),
    ]
    assert tp.period_attributes(rows) == [
        {"period": "Kept", "start": "21:00", "end": "07:00", "network_rate_$/kwh": round(5.123456789 / 100, 6)},
    ]


def test_period_attributes_lets_a_malformed_row_raise():
    with pytest.raises(AttributeError):
        tp.period_attributes([("Bad", "16:00", "20:00", 25.0)])


def test_feed_in_period_attributes():
    rows = [
        ("Peak export credit", datetime.time(16), datetime.time(21), 7.0),
        ("Saver export charge", datetime.time(11), datetime.time(16), -1.0),
    ]
    assert tp.feed_in_period_attributes(rows) == [
        {"period": "Peak export credit", "start": "16:00", "end": "21:00", "export_adjustment_$/kwh": 0.07},
        {"period": "Saver export charge", "start": "11:00", "end": "16:00", "export_adjustment_$/kwh": -0.01},
    ]
    assert tp.feed_in_period_attributes([]) == []


# ── Time-of-use windows ──────────────────────────────────────────────────────

WINDOWS = [
    {"period": "Peak", "start": "16:00", "end": "20:00", "network_rate_$/kwh": 0.14},
    {"period": "Off-peak", "start": "20:00", "end": "16:00", "network_rate_$/kwh": 0.03},
]


@pytest.mark.parametrize("hour, minute, expected", [
    (16, 0, ("Off-peak", 0.03)),   # 15:55, the wraparound window's early side
    (16, 5, ("Peak", 0.14)),       # 16:00, a window's start is inclusive
    (20, 0, ("Peak", 0.14)),       # 19:55
    (20, 5, ("Off-peak", 0.03)),   # 20:00, a window's end is exclusive
    (0, 0, ("Off-peak", 0.03)),    # 23:55 the previous day, the late side
    (3, 0, ("Off-peak", 0.03)),    # 02:55
])
def test_lookup_steps_back_five_minutes_and_wraps(hour, minute, expected):
    windows = tp.TouWindows.parse(WINDOWS)
    assert windows.lookup(datetime.datetime(2026, 9, 2, hour, minute, tzinfo=NEM)) == expected


def test_lookup_is_first_wins_and_none_when_nothing_matches():
    windows = tp.TouWindows.parse([
        {"period": "Day", "start": "07:00", "end": "22:00", "network_rate_$/kwh": 0.1},
        {"period": "Evening", "start": "16:00", "end": "21:00", "network_rate_$/kwh": 0.3},
        {"start": "22:00", "end": "23:00"},
    ])
    assert windows.lookup(datetime.datetime(2026, 9, 2, 18, 0, tzinfo=NEM)) == ("Day", 0.1)
    assert windows.lookup(datetime.datetime(2026, 9, 2, 22, 30, tzinfo=NEM)) == (None, None)
    assert windows.lookup(datetime.datetime(2026, 9, 2, 3, 0, tzinfo=NEM)) == (None, None)
    assert tp.TouWindows.parse([]).lookup(END) == (None, None)


def test_parse_raises_on_a_malformed_entry():
    with pytest.raises(ValueError):
        tp.TouWindows.parse([{"period": "Broken", "start": "not a time", "end": "20:00"}])
    with pytest.raises(KeyError):
        tp.TouWindows.parse([{"period": "No end", "start": "16:00"}])


def test_parse_looks_up_strptime_at_call_time():
    real_strptime = datetime.datetime.strptime
    calls = []

    class CountingDatetime(datetime.datetime):
        @classmethod
        def strptime(cls, value, fmt):
            calls.append(fmt)
            return real_strptime(value, fmt)

    with patch.object(tp.datetime, "datetime", CountingDatetime):
        tp.TouWindows.parse(WINDOWS)
    assert calls == ["%H:%M"] * 4


# ── Library availability and stdout ──────────────────────────────────────────

def test_library_available_is_false_when_the_names_are_none():
    with contextlib.ExitStack() as stack:
        for name in LIBRARY_NAMES:
            stack.enter_context(patch.object(tp, name, None))
        assert tp.library_available() is False


@pytest.mark.parametrize("missing", LIBRARY_NAMES)
def test_library_available_needs_all_four_names(missing):
    with contextlib.ExitStack() as stack:
        for name in LIBRARY_NAMES:
            stack.enter_context(patch.object(tp, name, None if name == missing else MagicMock()))
        assert tp.library_available() is False


def test_library_available_when_all_four_are_bound():
    with contextlib.ExitStack() as stack:
        for name in LIBRARY_NAMES:
            stack.enter_context(patch.object(tp, name, MagicMock()))
        assert tp.library_available() is True


def test_quiet_stdout_swallows_prints_and_restores_stdout_on_error():
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        with tp.quiet_stdout():
            print("DEBUG: sapower tariff lookup")
        with pytest.raises(RuntimeError), tp.quiet_stdout():
            print("DEBUG: before the failure")
            raise RuntimeError("boom")
        assert sys.stdout is captured
    assert captured.getvalue() == ""


def test_every_library_call_is_quiet_and_generators_are_consumed_inside():
    """Invariant 4: sapower.py prints, and get_periods output is consumed while suppressed."""
    def noisy(value):
        def call(*args, **kwargs):
            print("DEBUG: sapower")
            return value
        return call

    def noisy_rows(*args, **kwargs):
        print("DEBUG: sapower get_periods")
        yield ("Peak", datetime.time(16), datetime.time(20), 25.0)
        print("DEBUG: sapower get_periods, still going")

    captured = io.StringIO()
    pricer = tp.LibraryPricer("sapn", "RTOU")
    with contextlib.redirect_stdout(captured), \
            patch.object(tp, "spot_to_tariff", side_effect=noisy(15.5)), \
            patch.object(tp, "spot_to_feed_in_tariff", side_effect=noisy(6.0)), \
            patch.object(tp, "get_periods", side_effect=noisy_rows), \
            patch.object(tp, "get_daily_fee", side_effect=noisy(55.6)):
        assert pricer.import_c_kwh(END, 50.0) == 15.5
        assert pricer.feed_in_c_kwh(END, 50.0) == 6.0
        assert pricer.period_rows(END) == [("Peak", datetime.time(16), datetime.time(20), 25.0)]
        assert pricer.daily_fee() == 55.6
    assert captured.getvalue() == ""


# ── Invariant 3: a domain module ─────────────────────────────────────────────

def test_tariff_pricing_imports_neither_homeassistant_nor_nem_time():
    with open(os.path.join(PKG_DIR, "tariff_pricing.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
            if node.level and not node.module:
                imported.update("." + alias.name for alias in node.names)
    assert not any(name.split(".")[0] == "homeassistant" for name in imported), imported
    assert not any("nem_time" in name for name in imported), imported


# ── The sensors without the library ──────────────────────────────────────────
#
# When aemo_to_tariff is not importable every guarded tariff sensor method
# returns None or [], extension codes included; only the extension's export
# windows, which never needed the library, are still published. The sensors
# bind tariff_pricing through the tariff_sensor module loaded here, so that
# copy is the one patched.

install_ha_stubs()
_sensor_chain = load_chain("const", "nem_time", "pd7day_client", "calibration_store", "coordinator", "tariff_sensor")
_tariff_mod = _sensor_chain[-1]

WINTER_NOW = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=NEM)
WINTER_END = datetime.datetime(2026, 7, 15, 18, 0, tzinfo=NEM)


def _sensor(cls, distributor: str, code: str):
    sensor = cls.__new__(cls)
    sensor.coordinator = MagicMock(data=None, last_update_success=True)
    sensor._region = "VIC1"
    sensor._distributor = distributor
    sensor._tariff_code = code
    sensor._import_code = code
    sensor._export_code = code
    sensor._entry = MagicMock(entry_id="entry_1", options={}, runtime_data=None)
    sensor._store = None
    sensor.hass = MagicMock()
    sensor.hass.states.get.return_value = None
    return sensor


def _guarded_results(distributor: str, code: str) -> dict[str, object]:
    importer = _sensor(_tariff_mod.NemPd7dayTariffSensor, distributor, code)
    exporter = _sensor(_tariff_mod.NemPd7dayExportTariffSensor, distributor, code)
    period = make_price_period(WINTER_END, value=0.10)
    return {
        "_compute_tariff": importer._compute_tariff(period, calibrated=0.10),
        "_apply_tariff_to_spot": importer._apply_tariff_to_spot(0.10, WINTER_END),
        "_get_tariff_periods": importer._get_tariff_periods(),
        "_get_daily_supply_charge": importer._get_daily_supply_charge(),
        "_compute_export_tariff": exporter._compute_export_tariff(period, calibrated=0.10),
        "_apply_export_tariff_to_spot": exporter._apply_export_tariff_to_spot(0.10, WINTER_END),
        "export _get_tariff_periods": exporter._get_tariff_periods(),
    }


@contextlib.contextmanager
def _library(bound: bool):
    """The four library names as working mocks, or all None as when the import failed."""
    pricing = _tariff_mod.tariff_pricing
    values = {
        "spot_to_tariff": MagicMock(return_value=15.5),
        "spot_to_feed_in_tariff": MagicMock(return_value=6.0),
        "get_periods": MagicMock(return_value=[("Peak", datetime.time(16), datetime.time(20), 25.0)]),
        "get_daily_fee": MagicMock(return_value=55.6),
    }
    with contextlib.ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(patch.object(pricing, name, value if bound else None))
        stack.enter_context(patch.object(_tariff_mod, "now_nem", return_value=WINTER_NOW))
        yield


@pytest.mark.parametrize("distributor, code", [("energex", "6900"), ("powercor", XCER)])
def test_every_guarded_sensor_method_gives_nothing_without_the_library(monkeypatch, distributor, code):
    install_extension_fixture(monkeypatch, _tariff_mod)
    with _library(bound=True):
        # Control: with the library bound, every method produces a value, so
        # the None and [] below come from the guard and nothing else.
        present = _guarded_results(distributor, code)
    for name, value in present.items():
        if name == "export _get_tariff_periods" and code != XCER:
            assert value == [], name
        else:
            assert value not in (None, []), name

    with _library(bound=False):
        assert _tariff_mod.tariff_pricing.library_available() is False
        missing = _guarded_results(distributor, code)
    assert missing["_compute_tariff"] is None
    assert missing["_apply_tariff_to_spot"] is None
    assert missing["_get_tariff_periods"] == []
    assert missing["_get_daily_supply_charge"] is None
    assert missing["_compute_export_tariff"] is None
    assert missing["_apply_export_tariff_to_spot"] is None
    # The extension's export windows do not depend on the library.
    assert missing["export _get_tariff_periods"] == present["export _get_tariff_periods"]
