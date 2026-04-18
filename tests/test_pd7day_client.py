"""
Tests for pd7day_client._parse_all_tables — CSV parsing, column mapping,
PricePeriod construction, timezone handling.

This module is zero-coverage in previous test suites.  Every bug that
involves wrong column indices or missing +10:00 suffix would be caught here.

Run with:  python -m pytest tests/test_pd7day_client.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# ── Module loader ─────────────────────────────────────────────────────────────

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Stub aiohttp before loading pd7day_client (it imports aiohttp at top level)
sys.modules.setdefault("aiohttp", MagicMock())

_nem_time = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)

from custom_components.nem_pd7day.pd7day_client import (
    _parse_all_tables,
    PricePeriod,
    PD7DayData,
    CaseSolutionData,
    MarketSummaryData,
    InterconnectorData,
    QLD1_INTERCONNECTORS,
)

NEM_TZ = timezone(timedelta(hours=10))

# ── Minimal synthetic AEMO CSV builders ──────────────────────────────────────

def _csv(*rows: str) -> bytes:
    """Join rows into a UTF-8 CSV bytes blob."""
    return "\n".join(rows).encode("utf-8")


def _header() -> str:
    return "C,NEOD,PD7DAY,1,PUBLIC_PD7DAY_20260415_072507.zip"


def _case_row(run_dt="2026/04/15 07:25:07", intervention="0",
              last_changed="2026/04/15 07:25:07") -> str:
    return f"D,PD7DAY,CASESOLUTION,1,{run_dt},{intervention},{last_changed}"


def _price_row(run_dt="2026/04/15 07:25:07", period_id="2026/04/15 08:00:00",
               region="QLD1", price_mwh="85000.00") -> str:
    # AEMO PRICESOLUTION columns (0-indexed):
    # 0=D 1=PD7DAY 2=PRICESOLUTION 3=version 4=RUN_DATETIME 5=RUNNO
    # 6=PERIODID 7=REGIONID 8=PRICE_MWH ... (≥20 cols total)
    tail = ",0,0,0,0,0,0,0,0,0,0,0"  # pad to 20+ cols
    return f"D,PD7DAY,PRICESOLUTION,1,{run_dt},1,{period_id},{region},{price_mwh}{tail}"


def _market_summary_row(run_dt="2026/04/15 07:25:07",
                        period_id="2026/04/15 07:30:00",
                        value_tj="5432.1") -> str:
    # MARKET_SUMMARY: 0=D 1=PD7DAY 2=MARKET_SUMMARY 3=ver 4=RUN_DT 5=PERIODID 6=GPG_TJ
    return f"D,PD7DAY,MARKET_SUMMARY,1,{run_dt},{period_id},{value_tj}"


def _ic_row(run_dt="2026/04/15 07:25:07", period_id="2026/04/15 08:00:00",
            ic_id="NSW1-QLD1", mwflow="300.0") -> str:
    # INTERCONNECTORSOLUTION: 0=D 1=PD7DAY 2=IC_SOLUTION 3=ver 4=RUN_DT 5=RUNNO
    # 6=PERIODID 7=IC_ID 8=METERED 9=MWFLOW 10=MWLOSSES 11=MARGVAL
    # 12=VIOLATION 13=EXPORTLIMIT 14=IMPORTLIMIT 15=MARGINALLOSS
    return (f"D,PD7DAY,INTERCONNECTORSOLUTION,1,{run_dt},1,{period_id},{ic_id},"
            f"290.0,{mwflow},5.0,0.5,0.0,700.0,700.0,0.000123")


# ── Tests: PRICESOLUTION parsing ──────────────────────────────────────────────

def test_price_row_column_mapping():
    """
    Verify column indices for PRICESOLUTION rows match the real AEMO format.
    row[4]=RUN_DATETIME, row[6]=PERIODID(nemtime), row[7]=REGIONID, row[8]=PRICE
    A wrong column index here silently produces wrong data (e.g. region="0" or
    price=0 with no error).
    """
    csv_bytes = _csv(
        _header(),
        _price_row(
            run_dt="2026/04/15 07:25:07",
            period_id="2026/04/15 08:00:00",
            region="QLD1",
            price_mwh="85000.00",
        ),
    )
    _, case, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    run_dt_str, prices = price_rows["QLD1"]

    assert len(prices) == 1, f"Expected 1 price period, got {len(prices)}"
    p = prices[0]

    # Price: 85000 $/MWh ÷ 1000 = 85.0 $/kWh
    assert abs(p.value - 85.0) < 1e-6, (
        f"Price wrong: expected 85.0 $/kWh, got {p.value}. "
        f"If 0.085 or 0, the MWh→kWh division is applied twice or col index is wrong."
    )

    # run_at must have +10:00 suffix
    assert run_dt_str and run_dt_str.endswith("+10:00"), (
        f"run_dt_str missing +10:00: {run_dt_str!r}"
    )

    # region correctly parsed (not "BASERUN" or "0")
    assert run_dt_str.startswith("2026-04-15T07:25:07"), (
        f"run_dt_str has wrong value: {run_dt_str!r}. "
        f"Check row[4] is RUN_DATETIME not RUNNO."
    )


def test_pricesolution_nemtime_and_interval_start():
    """
    period.nemtime must equal PERIODID (interval END, AEMO convention).
    period.time must equal nemtime − 30 min (interval START).
    Both must carry +10:00 suffix.
    """
    csv_bytes = _csv(
        _header(),
        _price_row(period_id="2026/04/15 08:00:00"),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    _, prices = price_rows["QLD1"]
    p = prices[0]

    assert p.nemtime == "2026-04-15T08:00:00+10:00", (
        f"nemtime wrong: {p.nemtime!r}. Expected interval END 08:00 with +10:00."
    )
    assert p.time == "2026-04-15T07:30:00+10:00", (
        f"time wrong: {p.time!r}. Expected interval START (nemtime−30min) 07:30 with +10:00. "
        f"If equal to nemtime, interval_start() is not being applied."
    )


def test_pricesolution_region_filter():
    """Only rows matching the requested region must be returned."""
    csv_bytes = _csv(
        _header(),
        _price_row(region="QLD1", price_mwh="85000.00"),
        _price_row(region="NSW1", price_mwh="72000.00"),
        _price_row(region="VIC1", price_mwh="68000.00"),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert "QLD1" in price_rows
    assert "NSW1" not in price_rows, "NSW1 should not be in price_rows when not requested"
    assert len(price_rows["QLD1"][1]) == 1


def test_pricesolution_multi_region():
    """Multiple regions can be requested simultaneously."""
    csv_bytes = _csv(
        _header(),
        _price_row(region="QLD1", price_mwh="85000.00"),
        _price_row(region="NSW1", price_mwh="72000.00"),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1", "NSW1"], interconnector_ids=set()
    )
    assert len(price_rows["QLD1"][1]) == 1
    assert len(price_rows["NSW1"][1]) == 1
    assert abs(price_rows["QLD1"][1][0].value - 85.0) < 1e-6
    assert abs(price_rows["NSW1"][1][0].value - 72.0) < 1e-6


def test_pricesolution_sorted_by_nemtime():
    """
    Periods must be returned sorted ascending by nemtime so that
    prices[0] is always the earliest interval and current_value is correct.
    AEMO CSV rows are not guaranteed to be in order.
    """
    csv_bytes = _csv(
        _header(),
        _price_row(period_id="2026/04/15 10:00:00", price_mwh="90000.00"),
        _price_row(period_id="2026/04/15 08:00:00", price_mwh="85000.00"),
        _price_row(period_id="2026/04/15 09:00:00", price_mwh="88000.00"),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    _, prices = price_rows["QLD1"]
    times = [p.nemtime for p in prices]
    assert times == sorted(times), (
        f"Prices not sorted by nemtime: {times}. "
        f"prices[0].value will be wrong (current price)."
    )
    assert abs(prices[0].value - 85.0) < 1e-6, "prices[0] must be earliest interval"


def test_price_negative_value():
    """Negative prices (market floor events) must be parsed correctly."""
    csv_bytes = _csv(
        _header(),
        _price_row(price_mwh="-1000000.00"),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    _, prices = price_rows["QLD1"]
    assert abs(prices[0].value - (-1000.0)) < 1e-4, (
        f"Negative price not parsed correctly: {prices[0].value}"
    )


def test_price_spike_value():
    """Market cap price (VOLL = $15,100/MWh) must survive the ÷1000 conversion."""
    csv_bytes = _csv(
        _header(),
        _price_row(price_mwh="15100000.00"),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    _, prices = price_rows["QLD1"]
    assert abs(prices[0].value - 15100.0) < 1e-3


# ── Tests: CASESOLUTION parsing ───────────────────────────────────────────────

def test_casesolution_no_intervention():
    """intervention=False when AEMO column is '0'."""
    csv_bytes = _csv(_header(), _case_row(intervention="0"))
    _, case, _, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert case is not None
    assert case.intervention is False


def test_casesolution_intervention_flag():
    """intervention=True when AEMO column is '1'."""
    csv_bytes = _csv(_header(), _case_row(intervention="1"))
    _, case, _, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert case is not None
    assert case.intervention is True, (
        "Intervention flag not parsed. Calibration will incorrectly train on "
        "intervention periods if this is wrong."
    )


def test_casesolution_run_datetime_has_tz():
    """CaseSolutionData.run_datetime must carry +10:00."""
    csv_bytes = _csv(_header(), _case_row(run_dt="2026/04/15 07:25:07"))
    _, case, _, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert case.run_datetime == "2026-04-15T07:25:07+10:00", (
        f"run_datetime: {case.run_datetime!r}"
    )


def test_missing_casesolution_returns_none():
    """If CASESOLUTION row is absent, case must be None (not crash)."""
    csv_bytes = _csv(
        _header(),
        _price_row(),
    )
    _, case, _, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert case is None


# ── Tests: MARKET_SUMMARY parsing ─────────────────────────────────────────────

def test_market_summary_value_and_tz():
    """MARKET_SUMMARY value_tj and nemtime/time both have correct values + tz."""
    csv_bytes = _csv(
        _header(),
        _market_summary_row(period_id="2026/04/15 07:30:00", value_tj="5432.1"),
    )
    _, _, _, ms, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert ms is not None
    assert len(ms.forecast) == 1
    p = ms.forecast[0]
    assert abs(p.value_tj - 5432.1) < 0.01, f"value_tj wrong: {p.value_tj}"
    assert p.nemtime == "2026-04-15T07:30:00+10:00"
    assert p.time == "2026-04-15T07:00:00+10:00", (
        f"MARKET_SUMMARY time (interval START) wrong: {p.time!r}"
    )


def test_missing_market_summary_returns_none():
    """If no MARKET_SUMMARY rows, market_summary must be None."""
    csv_bytes = _csv(_header(), _price_row())
    _, _, _, ms, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert ms is None


# ── Tests: INTERCONNECTORSOLUTION parsing ─────────────────────────────────────

def test_interconnector_mwflow_and_tz():
    """MW flow parsed correctly and nemtime/time carry +10:00."""
    csv_bytes = _csv(
        _header(),
        _ic_row(period_id="2026/04/15 08:00:00", ic_id="NSW1-QLD1", mwflow="350.5"),
    )
    _, _, _, _, ic_rows = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids={"NSW1-QLD1"}
    )
    assert "NSW1-QLD1" in ic_rows
    p = ic_rows["NSW1-QLD1"][0]
    assert abs(p.mwflow - 350.5) < 0.01, f"mwflow wrong: {p.mwflow}"
    assert p.nemtime == "2026-04-15T08:00:00+10:00"
    assert p.time == "2026-04-15T07:30:00+10:00"


def test_interconnector_filter():
    """Only requested interconnector IDs must be returned."""
    csv_bytes = _csv(
        _header(),
        _ic_row(ic_id="NSW1-QLD1", mwflow="300.0"),
        _ic_row(ic_id="VIC1-NSW1", mwflow="500.0"),
    )
    _, _, _, _, ic_rows = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids={"NSW1-QLD1"}
    )
    assert "NSW1-QLD1" in ic_rows
    assert "VIC1-NSW1" not in ic_rows, "Unrequested interconnector must be filtered out"


def test_qld_interconnectors_constant():
    """QLD1_INTERCONNECTORS must include the two IDs used in production."""
    assert "NSW1-QLD1" in QLD1_INTERCONNECTORS, "NSW1-QLD1 missing from QLD1_INTERCONNECTORS"
    assert "N-Q-MNSP1" in QLD1_INTERCONNECTORS, "N-Q-MNSP1 missing from QLD1_INTERCONNECTORS"


def test_missing_interconnector_returns_empty():
    """If no INTERCONNECTORSOLUTION rows, ic_rows must be empty dict."""
    csv_bytes = _csv(_header(), _price_row())
    _, _, _, _, ic_rows = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids={"NSW1-QLD1"}
    )
    assert ic_rows == {}, f"Expected empty ic_rows, got {ic_rows}"


# ── Tests: full multi-table CSV ───────────────────────────────────────────────

def test_full_csv_all_tables_parsed():
    """All four table types in one CSV must all be parsed correctly."""
    csv_bytes = _csv(
        _header(),
        _case_row(intervention="0"),
        _price_row(period_id="2026/04/15 08:00:00", price_mwh="85000.00"),
        _price_row(period_id="2026/04/15 08:30:00", price_mwh="82000.00"),
        _market_summary_row(period_id="2026/04/15 07:30:00", value_tj="4321.0"),
        _ic_row(period_id="2026/04/15 08:00:00", ic_id="NSW1-QLD1", mwflow="300.0"),
        _ic_row(period_id="2026/04/15 08:30:00", ic_id="NSW1-QLD1", mwflow="310.0"),
    )
    _, case, price_rows, ms, ic_rows = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids={"NSW1-QLD1"}
    )

    assert case is not None and case.intervention is False
    assert len(price_rows["QLD1"][1]) == 2
    assert ms is not None and len(ms.forecast) == 1
    assert "NSW1-QLD1" in ic_rows and len(ic_rows["NSW1-QLD1"]) == 2


def test_unknown_table_type_ignored():
    """Rows with unknown table names must be silently ignored (no crash)."""
    csv_bytes = _csv(
        _header(),
        "D,PD7DAY,UNKNOWNTABLE,1,2026/04/15 07:25:07,some,data",
        _price_row(),
    )
    # Must not raise
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert len(price_rows["QLD1"][1]) == 1


def test_short_row_ignored():
    """Rows with fewer than 5 columns must be silently ignored."""
    csv_bytes = _csv(
        _header(),
        "D,PD7DAY",    # too short
        _price_row(),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert len(price_rows["QLD1"][1]) == 1


def test_non_d_rows_ignored():
    """I (header) and C (comment) rows must be ignored."""
    csv_bytes = _csv(
        _header(),
        "I,PD7DAY,PRICESOLUTION,1,RUN_DATETIME,RUNNO,PERIODID,REGIONID,PRICE",
        _price_row(),
        "C,END OF REPORT",
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    assert len(price_rows["QLD1"][1]) == 1


# ── Tests: PD7DayData derived properties ─────────────────────────────────────

def test_pd7day_data_current_and_next_value():
    """
    fetch_all assembles PD7DayData.current_value = prices[0].value
    and next_value = prices[1].value.  Verify ordering matters.
    """
    csv_bytes = _csv(
        _header(),
        _case_row(),
        _price_row(period_id="2026/04/15 10:00:00", price_mwh="90000.00"),
        _price_row(period_id="2026/04/15 08:00:00", price_mwh="85000.00"),  # earliest
        _price_row(period_id="2026/04/15 09:00:00", price_mwh="88000.00"),
    )
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    _, prices = price_rows["QLD1"]
    # After sort, prices[0] = 08:00, prices[1] = 09:00
    assert abs(prices[0].value - 85.0) < 1e-6, "current_value must be earliest period"
    assert abs(prices[1].value - 88.0) < 1e-6, "next_value must be second period"


def test_period_time_is_string_not_datetime():
    """
    period.time must be a str (ISO-8601 +10:00), not a datetime object.
    CalibrationStore.ingest_forecast() treats it as a dict key and compares
    with ISO strings from current_nem_interval().  A datetime key causes
    zero matches (the v1.6.0 bug).
    """
    csv_bytes = _csv(_header(), _price_row(period_id="2026/04/15 08:00:00"))
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    _, prices = price_rows["QLD1"]
    p = prices[0]

    assert isinstance(p.time, str), (
        f"period.time must be str, got {type(p.time)}. "
        f"A datetime key will never match current_nem_interval() ISO strings."
    )
    assert isinstance(p.nemtime, str), (
        f"period.nemtime must be str, got {type(p.nemtime)}"
    )
    assert p.time.endswith("+10:00"), f"period.time missing +10:00: {p.time!r}"
    assert p.nemtime.endswith("+10:00"), f"period.nemtime missing +10:00: {p.nemtime!r}"


def test_period_time_is_interval_start_not_end():
    """
    period.time must be the interval START (nemtime − 30 min).
    This is used as the forecast_history key in CalibrationStore.
    current_nem_interval() also returns interval START.
    A period.time equal to nemtime would cause zero calibration matches.
    """
    csv_bytes = _csv(_header(), _price_row(period_id="2026/04/15 08:00:00"))
    _, _, price_rows, _, _ = _parse_all_tables(
        csv_bytes, regions=["QLD1"], interconnector_ids=set()
    )
    _, prices = price_rows["QLD1"]
    p = prices[0]

    assert p.nemtime != p.time, (
        f"period.time equals period.nemtime — interval_start() not applied. "
        f"Both = {p.time!r}"
    )
    assert p.time == "2026-04-15T07:30:00+10:00", (
        f"period.time (interval START) wrong: {p.time!r}. "
        f"Expected 07:30 (= 08:00 − 30min)."
    )
