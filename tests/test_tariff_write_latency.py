"""
A cold tariff state write must not recompute run features per interval.

Issue #135: Home Assistant logged tariff sensor state updates of 0.4 to 0.9 s
on the event loop. Profiling a cold write on a 336 interval run showed 92 per
cent of the time in PD7DayCoordinator.current_run_features, called once per
interval by calibrate_interval and parsing every interval's timestamp each
time: 115,795 parse_iso calls to calibrate one run. The coordinator now
caches the features per run, and this module pins that a cold write performs
one computation, not one per interval.

Run with:  python -m pytest tests/test_tariff_write_latency.py -v
"""
from __future__ import annotations

import types
from datetime import timedelta
from unittest.mock import patch

from test_tariff_calibration_parity import RUN_AT
from test_tariff_spot_memo import _tariff_mod, build, clear_memos

from custom_components.nem_pd7day import coordinator as coord_mod
from custom_components.nem_pd7day.nem_time import parse_iso

PD7DayCoordinator = coord_mod.PD7DayCoordinator


def _with_real_run_features(coord, periods, region):
    """Give the test double the real coordinator's run-feature property."""
    coord._regions = [region]
    type(coord).current_run_features = PD7DayCoordinator.current_run_features
    type(coord)._compute_run_features = staticmethod(
        PD7DayCoordinator._compute_run_features
    )
    run_dt = parse_iso(RUN_AT)
    coord.data.interconnectors = {
        "NSW1-QLD1": types.SimpleNamespace(forecast=[
            types.SimpleNamespace(time=p.time, mwflow=-150.0) for p in periods
        ])
    }
    coord.data.market_summary = types.SimpleNamespace(forecast=[
        types.SimpleNamespace(
            nemtime=(run_dt + timedelta(days=d)).isoformat(), value_tj=70.0
        )
        for d in range(8)
    ])


def test_cold_tariff_write_computes_run_features_once():
    periods, forecast, tariff, export, coord, store = build("QLD1")
    _with_real_run_features(coord, periods, "QLD1")
    clear_memos(coord)
    calls = []
    real = PD7DayCoordinator._compute_run_features

    def counting(price_data):
        calls.append(1)
        return real(price_data)

    with patch.object(_tariff_mod, "spot_to_tariff", return_value=15.5), \
         patch.object(type(coord), "_compute_run_features", staticmethod(counting)):
        attrs = tariff.extra_state_attributes
        assert attrs["forecast"], "the write must still publish a forecast"
        assert len(calls) == 1, (
            f"a cold write of {len(periods)} intervals computed run features "
            f"{len(calls)} times; the per-run cache should make it once"
        )
        # A warm write, and a second cold one for the same run, add nothing.
        tariff.extra_state_attributes
        clear_memos(coord)
        tariff.extra_state_attributes
        assert len(calls) == 1
    print(f"  PASS: run features computed once across a cold write of {len(periods)} intervals")


def test_run_features_recompute_when_the_run_changes():
    periods, forecast, tariff, export, coord, store = build("QLD1")
    _with_real_run_features(coord, periods, "QLD1")
    first = coord.current_run_features
    assert first is coord.current_run_features, "same run must return the cached object"
    price_data = coord.data.prices["QLD1"]
    price_data.forecast_generated_at = "2026-09-05T18:00:00+10:00"
    second = coord.current_run_features
    assert second is not first, "a new run stamp must recompute"
    # An interval count change on the same stamp (a refetched file) recomputes too.
    price_data.forecast = price_data.forecast[:-1]
    assert coord.current_run_features is not second
    print("  PASS: run features recompute on a new run and on a changed interval count")
