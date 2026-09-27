"""
forecast_history: joining a PD7DAY run with STPASA, gas and QNI per interval,
deduplicating by run and pruning by age (spec 004). The entry field order is
part of the stored forecast history file.

Run with:  python -m pytest tests/test_forecast_history.py -v
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

from custom_components.nem_pd7day.forecast_history import ingest_run, prune_history
from ha_free import assert_imports_without_home_assistant
from support import NEM_TZ

RUN = "2026-09-19T13:00:00+10:00"
A = "2026-09-20T18:00:00+10:00"
B = "2026-09-20T18:30:00+10:00"


def _prices(*starts_values):
    return NS(forecast=[NS(time=t, value=v) for t, v in starts_values])


def _ingest(history, run_at=RUN, **kw):
    args = dict(
        region="QLD1", run_at=run_at, price_data=_prices((A, 0.1), (B, 0.2)),
        interconnectors={}, case=None, market_summary=None, stpasa=None,
    )
    args.update(kw)
    ingest_run(history, **args)


def test_imports_without_home_assistant():
    assert_imports_without_home_assistant("forecast_history")


def test_entry_fields_in_stored_order_with_every_join():
    stpasa = NS(intervals=[
        NS(interval_datetime="2026-09-20T18:30:00+10:00", run_datetime="R", demand10=1.0,
           demand50=2.0, demand90=3.0, surpluscapacity=4.0, ss_solar_uigf=5.0, ss_wind_uigf=6.0),
        NS(interval_datetime="not a time"),
    ])
    qni = NS(forecast=[NS(time=A, mwflow=-500.0, violationdegree=0.5)])
    gas = NS(forecast=[NS(nemtime="2026-09-20T00:00:00+10:00", value_tj=140.0)])
    history: dict = {}
    _ingest(history, interconnectors={"NSW1-QLD1": qni}, case=NS(intervention=True),
            market_summary=gas, stpasa=stpasa)
    entry = history[A][0]
    assert list(entry) == [
        "run_at", "forecast_price", "gas_tj", "qni_mwflow", "qni_violation",
        "is_intervention", "region", "stpasa_run_at", "stpasa_demand10",
        "stpasa_demand50", "stpasa_demand90", "stpasa_surplus", "stpasa_solar",
        "stpasa_wind",
    ]
    assert list(entry.values()) == [RUN, 0.1, 140.0, -500.0, 0.5, True, "QLD1",
                                    "R", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert list(history[B][0]) == [
        "run_at", "forecast_price", "gas_tj", "qni_mwflow", "qni_violation",
        "is_intervention", "region",
    ]
    assert history[B][0]["qni_mwflow"] is None


def test_gas_is_matched_by_the_date_of_nemtime_not_time():
    # A midnight row: its interval start falls on the previous day.
    gas = NS(forecast=[NS(nemtime="2026-09-21T00:00:00+10:00", time="2026-09-20T23:30:00+10:00",
                          value_tj=150.0)])
    history: dict = {}
    _ingest(history, market_summary=gas)
    assert history[A][0]["gas_tj"] is None


def test_a_run_already_present_is_skipped_but_its_key_is_kept():
    history: dict = {"2026-09-20T19:00:00+10:00": []}
    _ingest(history)
    _ingest(history, price_data=_prices((A, 0.9), ("2026-09-20T19:00:00+10:00", 0.3)))
    assert [e["forecast_price"] for e in history[A]] == [0.1]
    assert [e["forecast_price"] for e in history["2026-09-20T19:00:00+10:00"]] == [0.3]
    _ingest(history, run_at="2026-09-19T18:00:00+10:00")
    assert [e["run_at"] for e in history[A]] == [RUN, "2026-09-19T18:00:00+10:00"]


def test_a_datetime_period_time_is_keyed_as_a_nem_iso_string():
    history: dict = {}
    start = datetime(2026, 9, 20, 8, 0, tzinfo=NEM_TZ)
    _ingest(history, price_data=NS(forecast=[NS(time=start, value=0.1)]))
    assert list(history) == ["2026-09-20T08:00:00+10:00"]


def test_prune_keeps_keys_at_or_after_the_cutoff_in_a_new_dict():
    history = {"2026-09-01T00:00:00+10:00": [1], "2026-09-06T00:00:00+10:00": [2],
               "2026-09-07T00:00:00+10:00": [3]}
    pruned = prune_history(history, "2026-09-06T00:00:00+10:00")
    assert pruned == {"2026-09-06T00:00:00+10:00": [2], "2026-09-07T00:00:00+10:00": [3]}
    assert pruned is not history and len(history) == 3
