"""
Tests for pd7day_client._parse_all_tables: CSV parsing, column mapping,
PricePeriod construction and timezone handling.

A wrong column index silently produces wrong data (region "0", price 0) with
no error, and a missing +10:00 suffix breaks every comparison downstream, so
each table's columns are pinned here against the real AEMO layout.

Run with:  python -m pytest tests/test_pd7day_client.py -v
"""
from __future__ import annotations

import pytest

from support import install_ha_stubs, load_chain

install_ha_stubs()

_nem_time, _client_mod = load_chain("nem_time", "pd7day_client")

_parse_all_tables = _client_mod._parse_all_tables
QLD1_INTERCONNECTORS = _client_mod.QLD1_INTERCONNECTORS


# ── Minimal synthetic AEMO CSV builders ──────────────────────────────────────

def _csv(*rows: str) -> bytes:
    """Join rows into a UTF-8 CSV bytes blob."""
    return "\n".join(rows).encode("utf-8")


def _header() -> str:
    return "C,NEOD,PD7DAY,1,PUBLIC_PD7DAY_20260415_072507.zip"


def _case_row(run_dt="2026/04/15 07:25:07", intervention="0", last_changed="2026/04/15 07:25:07") -> str:
    return f"D,PD7DAY,CASESOLUTION,1,{run_dt},{intervention},{last_changed}"


def _price_row(run_dt="2026/04/15 07:25:07", period_id="2026/04/15 08:00:00",
               region="QLD1", price_mwh="85000.00") -> str:
    # AEMO PRICESOLUTION columns (0-indexed):
    # 0=D 1=PD7DAY 2=PRICESOLUTION 3=version 4=RUN_DATETIME 5=RUNNO
    # 6=PERIODID 7=REGIONID 8=PRICE_MWH ... (>=20 cols total)
    tail = ",0,0,0,0,0,0,0,0,0,0,0"  # pad to 20+ cols
    return f"D,PD7DAY,PRICESOLUTION,1,{run_dt},1,{period_id},{region},{price_mwh}{tail}"


def _market_summary_row(run_dt="2026/04/15 07:25:07", period_id="2026/04/15 07:30:00", value_tj="5432.1") -> str:
    # MARKET_SUMMARY: 0=D 1=PD7DAY 2=MARKET_SUMMARY 3=ver 4=RUN_DT 5=PERIODID 6=GPG_TJ
    return f"D,PD7DAY,MARKET_SUMMARY,1,{run_dt},{period_id},{value_tj}"


def _ic_row(run_dt="2026/04/15 07:25:07", period_id="2026/04/15 08:00:00", ic_id="NSW1-QLD1", mwflow="300.0") -> str:
    # INTERCONNECTORSOLUTION: 0=D 1=PD7DAY 2=IC_SOLUTION 3=ver 4=RUN_DT 5=RUNNO
    # 6=PERIODID 7=IC_ID 8=METERED 9=MWFLOW 10=MWLOSSES 11=MARGVAL
    # 12=VIOLATION 13=EXPORTLIMIT 14=IMPORTLIMIT 15=MARGINALLOSS
    return (f"D,PD7DAY,INTERCONNECTORSOLUTION,1,{run_dt},1,{period_id},{ic_id},"
            f"290.0,{mwflow},5.0,0.5,0.0,700.0,700.0,0.000123")


def _parse(csv_bytes: bytes, regions=("QLD1",), interconnector_ids=frozenset()):
    return _parse_all_tables(csv_bytes, regions=list(regions), interconnector_ids=set(interconnector_ids))


# ── PRICESOLUTION ────────────────────────────────────────────────────────────

def test_price_row_column_mapping():
    """
    row[4]=RUN_DATETIME, row[6]=PERIODID (nemtime), row[7]=REGIONID, row[8]=PRICE.
    Price is $/MWh / 1000 = $/kWh; run_at carries +10:00.
    """
    _, case, price_rows, _, _ = _parse(_csv(_header(), _price_row()))
    run_dt_str, prices = price_rows["QLD1"]

    assert len(prices) == 1, f"Expected 1 price period, got {len(prices)}"
    assert abs(prices[0].value - 85.0) < 1e-6, (
        f"Price wrong: expected 85.0 $/kWh, got {prices[0].value}. "
        "If 0.085 or 0, the MWh->kWh division is applied twice or the column index is wrong."
    )
    assert run_dt_str.endswith("+10:00"), f"run_dt_str missing +10:00: {run_dt_str!r}"
    assert run_dt_str.startswith("2026-04-15T07:25:07"), (
        f"run_dt_str has wrong value: {run_dt_str!r}. Check row[4] is RUN_DATETIME not RUNNO."
    )


def test_pricesolution_nemtime_and_interval_start():
    """
    period.nemtime must equal PERIODID (interval END, AEMO convention) and
    period.time must equal nemtime - 30 min (interval START), both ISO-8601
    strings with +10:00.

    period.time is the forecast_history key in CalibrationStore, compared
    against the ISO strings from current_nem_interval(): a datetime key, or a
    time equal to nemtime, gives zero calibration matches (the v1.6.0 bug).
    """
    _, _, price_rows, _, _ = _parse(_csv(_header(), _price_row(period_id="2026/04/15 08:00:00")))
    p = price_rows["QLD1"][1][0]

    assert p.nemtime == "2026-04-15T08:00:00+10:00", f"nemtime wrong: {p.nemtime!r}"
    assert p.time == "2026-04-15T07:30:00+10:00", (
        f"time wrong: {p.time!r}. Expected interval START (nemtime - 30 min) 07:30 with +10:00. "
        "If equal to nemtime, interval_start() is not being applied."
    )


def test_pricesolution_filters_to_requested_regions():
    """Only the requested regions come back, each with its own rows."""
    csv_bytes = _csv(
        _header(),
        _price_row(region="QLD1", price_mwh="85000.00"),
        _price_row(region="NSW1", price_mwh="72000.00"),
        _price_row(region="VIC1", price_mwh="68000.00"),
    )
    _, _, price_rows, _, _ = _parse(csv_bytes, regions=["QLD1", "NSW1"])

    assert set(price_rows) == {"QLD1", "NSW1"}, "VIC1 must not be in price_rows when not requested"
    assert len(price_rows["QLD1"][1]) == 1
    assert len(price_rows["NSW1"][1]) == 1
    assert abs(price_rows["QLD1"][1][0].value - 85.0) < 1e-6
    assert abs(price_rows["NSW1"][1][0].value - 72.0) < 1e-6


def test_pricesolution_sorted_by_nemtime():
    """
    Periods are returned sorted ascending by nemtime, so prices[0] is the
    earliest interval (current_value) and prices[1] the next (next_value).
    AEMO CSV rows are not guaranteed to be in order.
    """
    csv_bytes = _csv(
        _header(),
        _price_row(period_id="2026/04/15 10:00:00", price_mwh="90000.00"),
        _price_row(period_id="2026/04/15 08:00:00", price_mwh="85000.00"),
        _price_row(period_id="2026/04/15 09:00:00", price_mwh="88000.00"),
    )
    _, _, price_rows, _, _ = _parse(csv_bytes)
    prices = price_rows["QLD1"][1]
    times = [p.nemtime for p in prices]

    assert times == sorted(times), f"Prices not sorted by nemtime: {times}"
    assert abs(prices[0].value - 85.0) < 1e-6, "prices[0] must be the earliest interval"
    assert abs(prices[1].value - 88.0) < 1e-6, "prices[1] must be the second interval"


@pytest.mark.parametrize(
    "price_mwh, expected_kwh",
    [("-1000000.00", -1000.0), ("15100000.00", 15100.0)],
    ids=["market_floor", "market_cap"],
)
def test_price_extremes_survive_the_mwh_to_kwh_conversion(price_mwh, expected_kwh):
    """Negative prices (floor events) and the market cap (VOLL = $15,100/MWh) parse correctly."""
    _, _, price_rows, _, _ = _parse(_csv(_header(), _price_row(price_mwh=price_mwh)))
    assert abs(price_rows["QLD1"][1][0].value - expected_kwh) < 1e-3


# ── CASESOLUTION ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flag, expected", [("0", False), ("1", True)])
def test_casesolution_intervention_flag_and_run_datetime(flag, expected):
    """intervention follows the AEMO column; run_datetime carries +10:00.
    Calibration would train on intervention periods if the flag were wrong."""
    _, case, _, _, _ = _parse(_csv(_header(), _case_row(run_dt="2026/04/15 07:25:07", intervention=flag)))
    assert case is not None
    assert case.intervention is expected
    assert case.run_datetime == "2026-04-15T07:25:07+10:00", f"run_datetime: {case.run_datetime!r}"


# ── MARKET_SUMMARY ───────────────────────────────────────────────────────────

def test_market_summary_value_and_tz():
    """MARKET_SUMMARY value_tj and nemtime/time all have correct values and tz."""
    _, _, _, ms, _ = _parse(_csv(_header(), _market_summary_row(period_id="2026/04/15 07:30:00", value_tj="5432.1")))
    assert ms is not None
    assert len(ms.forecast) == 1
    p = ms.forecast[0]
    assert abs(p.value_tj - 5432.1) < 0.01, f"value_tj wrong: {p.value_tj}"
    assert p.nemtime == "2026-04-15T07:30:00+10:00"
    assert p.time == "2026-04-15T07:00:00+10:00", f"MARKET_SUMMARY time (interval START) wrong: {p.time!r}"


# ── INTERCONNECTORSOLUTION ───────────────────────────────────────────────────

def test_interconnector_rows_parsed_and_filtered():
    """MW flow parsed with +10:00 timestamps; unrequested interconnector IDs are dropped."""
    csv_bytes = _csv(
        _header(),
        _ic_row(period_id="2026/04/15 08:00:00", ic_id="NSW1-QLD1", mwflow="350.5"),
        _ic_row(ic_id="VIC1-NSW1", mwflow="500.0"),
    )
    _, _, _, _, ic_rows = _parse(csv_bytes, interconnector_ids={"NSW1-QLD1"})

    assert set(ic_rows) == {"NSW1-QLD1"}, "Unrequested interconnector must be filtered out"
    p = ic_rows["NSW1-QLD1"][0]
    assert abs(p.mwflow - 350.5) < 0.01, f"mwflow wrong: {p.mwflow}"
    assert p.nemtime == "2026-04-15T08:00:00+10:00"
    assert p.time == "2026-04-15T07:30:00+10:00"


def test_qld_interconnectors_constant():
    """QLD1_INTERCONNECTORS must include the two IDs used in production."""
    assert "NSW1-QLD1" in QLD1_INTERCONNECTORS
    assert "N-Q-MNSP1" in QLD1_INTERCONNECTORS


# ── Whole-file behaviour ─────────────────────────────────────────────────────

def test_absent_tables_yield_none_or_empty():
    """A CSV with only PRICESOLUTION rows: case None, market_summary None, no interconnectors, no crash."""
    _, case, price_rows, ms, ic_rows = _parse(_csv(_header(), _price_row()), interconnector_ids={"NSW1-QLD1"})
    assert case is None
    assert ms is None
    assert ic_rows == {}, f"Expected empty ic_rows, got {ic_rows}"
    assert len(price_rows["QLD1"][1]) == 1


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
    _, case, price_rows, ms, ic_rows = _parse(csv_bytes, interconnector_ids={"NSW1-QLD1"})

    assert case is not None and case.intervention is False
    assert len(price_rows["QLD1"][1]) == 2
    assert ms is not None and len(ms.forecast) == 1
    assert "NSW1-QLD1" in ic_rows and len(ic_rows["NSW1-QLD1"]) == 2


@pytest.mark.parametrize(
    "noise",
    [
        ["D,PD7DAY,UNKNOWNTABLE,1,2026/04/15 07:25:07,some,data"],
        ["D,PD7DAY"],
        ["I,PD7DAY,PRICESOLUTION,1,RUN_DATETIME,RUNNO,PERIODID,REGIONID,PRICE", "C,END OF REPORT"],
    ],
    ids=["unknown_table", "short_row", "header_and_comment_rows"],
)
def test_noise_rows_are_ignored(noise):
    """Unknown tables, rows under 5 columns, and I/C rows are skipped without error."""
    _, _, price_rows, _, _ = _parse(_csv(_header(), *noise, _price_row()))
    assert len(price_rows["QLD1"][1]) == 1
