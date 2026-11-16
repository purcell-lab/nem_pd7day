"""Pre-dispatch served through PD7DAY's h24_48 stage 1 model at 24 h and beyond (#337 interim).

On 15 November QLD1's first day 2-7 point, 24.5 h into the pre-dispatch run,
was served raw at 30.2 c where Amber's last point was about 13 c: PD7DAY's own
h24_48 fit compresses raw prices at that horizon hard. Pre-dispatch from 24 h
is now served through that model, labelled ``pd7day_iso_h24_48``; below 24 h,
and without a fit, it stays passthrough.

Run with:  python -m pytest tests/test_predispatch_calibration.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain

install_ha_stubs()

_const, _cal, _tl, _tf = load_chain("const", "predispatch_calibration", "price_timeline", "timeline_forecast")

RUN_AT = datetime(2026, 11, 15, 21, 1, tzinfo=NEM_TZ)


class _Model:
    """A region's CalibrationStore: records what it is asked and halves the price."""

    def __init__(self, source: str = "isotonic", generation: int = 3) -> None:
        self.source, self.fit_generation, self.calls = source, generation, []

    def apply_to_price(self, raw, horizon_hours, hour_of_day, *, interval_dt=None):
        self.calls.append((raw, horizon_hours, hour_of_day, interval_dt))
        return {
            "calibrated": round(raw * 0.5, 6), "p10": round(raw * 0.4, 6), "p50": round(raw * 0.5, 6),
            "p90": round(raw * 0.6, 6), "calibrated_source": self.source,
            "band_source": "stage1_quantile", "n_obs": 250,
        }


def _end(hours_ahead_of_start: float) -> datetime:
    """The end of the period whose start is ``hours_ahead_of_start`` after the run."""
    return RUN_AT + timedelta(hours=hours_ahead_of_start) + timedelta(minutes=30)


def test_from_24_hours_the_period_is_served_through_pd7days_h24_48_model():
    model = _Model()
    end = datetime(2026, 11, 16, 22, 0, tzinfo=NEM_TZ)  # the 21:30 point of the 15 November report
    served = _cal.serve("QLD1", end, 302.0, RUN_AT, {"QLD1": model})
    assert served.calibrated_source == "pd7day_iso_h24_48"
    assert served.horizon_hours == 24.48
    assert (served.rrp_mwh, served.raw_mwh) == (151.0, 302.0)
    assert (served.p10_mwh, served.p50_mwh, served.p90_mwh) == (120.8, 151.0, 181.2)
    assert (served.band_source, served.n_obs) == ("stage1_quantile", 250)
    # $/kWh in, the period's horizon, the hour and instant of its start: PD7DAY's own keying (#208).
    start = end - timedelta(minutes=30)
    assert model.calls == [(0.302, 24.48, 21, start)]


@pytest.mark.parametrize("hours", [0.5, 6.0, 12.0, 23.99])
def test_below_24_hours_the_period_stays_passthrough(hours):
    model = _Model()
    served = _cal.serve("QLD1", _end(hours), 80.0, RUN_AT, {"QLD1": model})
    assert (served.rrp_mwh, served.calibrated_source, served.raw_mwh) == (80.0, "passthrough", 80.0)
    assert served.horizon_hours == round(hours, 2)
    assert served.p10_mwh is None and model.calls == []


def test_at_48_hours_and_beyond_pre_dispatch_is_not_served_through_h24_48():
    served = _cal.serve("QLD1", _end(48.0), 80.0, RUN_AT, {"QLD1": _Model()})
    assert served.calibrated_source == "passthrough"


@pytest.mark.parametrize("models, run_at, rrp", [
    (None, RUN_AT, 80.0),                          # no registry
    ({}, RUN_AT, 80.0),                            # no store for the region
    ({"NSW1": _Model()}, RUN_AT, 80.0),            # another region's store
    ({"QLD1": _Model()}, None, 80.0),              # no run time: no horizon
    ({"QLD1": _Model()}, RUN_AT, None),            # no price
    ({"QLD1": _Model("passthrough")}, RUN_AT, 80.0),  # no PD7DAY fit, or none for the bucket
], ids=["no_registry", "no_store", "other_region", "no_run_at", "no_price", "unfitted"])
def test_without_a_pd7day_fit_serving_stays_passthrough(models, run_at, rrp):
    served = _cal.serve("QLD1", _end(30.0), rrp, run_at, models)
    assert (served.rrp_mwh, served.calibrated_source) == (rrp, "passthrough")


def test_calibrated_rrp_mwh_is_the_served_price():
    model = {"QLD1": _Model()}
    assert _cal.calibrated_rrp_mwh("QLD1", _end(25.0), 200.0, RUN_AT, model) == 100.0
    assert _cal.calibrated_rrp_mwh("QLD1", _end(2.0), 200.0, RUN_AT, model) == 200.0
    assert _cal.calibrated_rrp_mwh("QLD1", _end(25.0), 200.0) == 200.0   # the old call: passthrough


def test_the_registries_and_their_generations_are_read_from_hass_data():
    model, record = _Model(generation=7), SimpleNamespace(calibration=None, fit_generation=2)
    hass = SimpleNamespace(data={_const.DOMAIN: {
        _cal.CALIBRATION_STORES_KEY: {"QLD1": model}, _cal.PD_RECORDS_KEY: {"QLD1": record},
    }})
    held = _cal.models_for(SimpleNamespace(hass=hass))
    assert (held.pd7day, held.pd) == ({"QLD1": model}, {"QLD1": record})
    empty = _cal.ServingModels()
    assert _cal.models_for(SimpleNamespace(hass=None)) == empty
    assert _cal.models_for(object()) == empty       # an entity not added yet
    assert _cal.generation("QLD1", held) == (7, 2)
    assert _cal.generation("QLD1", {"QLD1": model}) == (7, None)   # the PD7DAY stores alone
    assert _cal.generation("SA1", held) == (None, None)


def _pd_run(first_start: datetime, periods: int, base: float = 300.0):
    prices = {"QLD1": {first_start + timedelta(minutes=30 * (i + 1)): base + i for i in range(periods)}}
    return SimpleNamespace(run_at=RUN_AT, source="pd.zip", prices=prices)


def test_the_timeline_serves_each_pre_dispatch_point_through_its_horizon():
    """The seam of the 15 November report: below 24 h raw, from 24 h through h24_48."""
    run = _pd_run(datetime(2026, 11, 16, 20, 30, tzinfo=NEM_TZ), 4)   # starts 23.48 h to 24.98 h ahead
    now = datetime(2026, 11, 15, 21, 5, tzinfo=NEM_TZ)
    timeline = _tl.build_timeline(now, "QLD1", pd=run, pd_models={"QLD1": _Model()})
    points = [p for p in timeline.points if p["source"] == "predispatch"]
    assert [p["horizon_hours"] for p in points] == [23.48, 23.98, 24.48, 24.98]
    assert [p["calibrated_source"] for p in points] == ["passthrough", "passthrough",
                                                         "pd7day_iso_h24_48", "pd7day_iso_h24_48"]
    assert [p["raw_value"] for p in points] == [0.3, 0.301, 0.302, 0.303]
    assert [p["value"] for p in points] == [0.3, 0.301, 0.151, 0.1515]
    assert (points[2]["p10"], points[2]["p90"], points[2]["band_source"]) == (0.1208, 0.1812, "stage1_quantile")
    # The forecast entries every spot, day 2-7 and tariff consumer reads carry the same.
    entries = [e for e in _tf.forecast_entries(timeline, []) if e["source"] == "predispatch"]
    assert [(e["raw_value"], e["value"], e["horizon_hours"], e["calibrated_source"]) for e in entries[2:]] == [
        (0.302, 0.151, 24.48, "pd7day_iso_h24_48"), (0.303, 0.1515, 24.98, "pd7day_iso_h24_48"),
    ]
    assert (entries[2]["band_source"], entries[2]["n_obs"]) == ("stage1_quantile", 250)
    # Without the registry, as before: passthrough throughout.
    plain = _tl.build_timeline(now, "QLD1", pd=run)
    assert {p["calibrated_source"] for p in plain.points} == {"passthrough"}
