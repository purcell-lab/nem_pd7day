"""Stage 3b (#337): pre-dispatch's own stage 1, fitted by band, validated on a holdout, served.

The observations are built so the outcome of each band is known in advance:
below 6 h the pre-dispatch forecast is the actual itself, so no fit can beat
raw there and the band is not admitted; from 6 h it reads twice the actual plus
5 c, which an isotonic map corrects, so the band is admitted.

Run with:  python -m pytest tests/test_predispatch_fit.py -v
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, run_async

install_ha_stubs()

(_const, _engine, _obslog, _fit, _cal, _pdr) = load_chain(
    "const", "calibration_engine", "observation_log", "predispatch_fit", "predispatch_calibration",
    "predispatch_record",
)

START = datetime(2026, 10, 11, tzinfo=NEM_TZ)
NOW = datetime(2026, 11, 1, tzinfo=NEM_TZ)      # 21 days on: 14 to fit, the newest 7 held out
# Three forecasts per band, so every bucket, the short morning ramp's too, has MIN_BAND_OBS rows.
HORIZONS = (0.25, 0.5, 0.75, 1.5, 2.0, 2.5, 3.5, 4.5, 5.5, 7.0, 9.0, 11.0, 14.0, 18.0, 22.0, 26.0, 30.0, 35.0)


def _raw(actual: float, horizon: float) -> float:
    return actual if horizon < 6 else round(2 * actual + 0.05, 6)


def _rows(days: int = 21, start: datetime = START, seed: int = 7) -> list[dict]:
    """Observation rows in stage 3a's format, every half hour, one forecast per horizon."""
    rng = random.Random(seed)
    rows = []
    for i in range(days * 48):
        interval = start + timedelta(minutes=30 * i)
        actual = round(0.05 + 0.10 * rng.random(), 6)
        for h in HORIZONS:
            run_at = interval - timedelta(hours=h)
            rows.append({
                "interval_time": interval.isoformat(), "horizon_hours": h, "pd7day_forecast": _raw(actual, h),
                "actual_rrp": actual, "forecast_run_at": run_at.isoformat(), "hour_of_day": interval.hour,
                "day_of_week": interval.weekday(), "month": interval.month, "is_intervention": False,
            })
    return rows


def _observations(rows):
    return [_engine.Observation(
        interval_time=r["interval_time"], horizon_hours=r["horizon_hours"], pd7day_forecast=r["pd7day_forecast"],
        actual_rrp=r["actual_rrp"], forecast_run_at=r["forecast_run_at"], hour_of_day=r["hour_of_day"],
        day_of_week=r["day_of_week"], month=r["month"], gas_forecast_tj=None, qni_mwflow=None,
        qni_violation_degree=None, is_intervention=r["is_intervention"],
    ) for r in rows]


@pytest.fixture(scope="module")
def fitted():
    return _fit.fit_pd_calibration(_observations(_rows()), "QLD1", NOW)


@pytest.mark.parametrize("horizon, label", [
    (-0.1, None), (0.0, "p00_01"), (0.99, "p00_01"), (1.0, "p01_03"), (3.0, "p03_06"), (6.0, "p06_12"),
    (12.0, "p12_24"), (24.0, "p24_40"), (39.9, "p24_40"), (40.0, "p24_40"), (55.0, "p24_40"),
])
def test_horizon_bands_follow_the_3a_thinning_edges(horizon, label):
    assert _fit.pd_horizon_label(horizon) == label


def test_a_band_is_admitted_only_where_it_beats_raw_on_the_holdout(fitted):
    v = fitted.validation
    for band in ("p00_01", "p01_03", "p03_06"):       # raw is the actual: nothing to gain
        assert v[band]["admitted"] is False and v[band]["reason"] == "no better than raw", band
    for band in ("p06_12", "p12_24", "p24_40"):       # 2x + 5 c: the map corrects it
        assert v[band]["admitted"] is True, band
        assert v[band]["pd_mae"] < v[band]["raw_mae"] / 5, band
        assert v[band]["n"] == 3 * 7 * 48
    assert fitted.admitted == {"p06_12", "p12_24", "p24_40"}


def test_an_admitted_band_serves_its_bucket_and_says_so(fitted):
    start = datetime(2026, 11, 1, 18, 0, tzinfo=NEM_TZ)
    served = fitted.apply(_raw(0.10, 30.0), 30.0, start)
    assert served["calibrated_source"] == "pd_isotonic"
    assert served["bucket"].startswith("p24_40__")
    assert served["calibrated"] == pytest.approx(0.10, abs=0.01)
    assert served["p10"] is not None and served["p90"] is not None and served["n_obs"] >= _fit.MIN_BAND_OBS
    assert fitted.apply(0.10, 2.0, start) is None       # p01_03 not admitted: the caller's fallback


def test_a_thin_bucket_serves_the_next_shorter_band_at_its_time_of_day(fitted):
    start = datetime(2026, 11, 1, 18, 0, tzinfo=NEM_TZ)
    key, _bucket = fitted.bucket_for(16.0, start)
    assert key.startswith("p12_24__")
    tod = key.split("__")[1]
    thin = fitted.buckets[key]
    try:
        fitted.buckets[key] = _fit._Bucket(thin.model, _fit.MIN_BAND_OBS - 1, thin.days)
        assert fitted.bucket_for(16.0, start)[0] == f"p06_12__{tod}"
        fitted.buckets[key] = _fit._Bucket(thin.model, thin.n, _fit.MIN_BAND_DAYS - 1)
        assert fitted.bucket_for(16.0, start)[0] == f"p06_12__{tod}"
    finally:
        fitted.buckets[key] = thin
    assert fitted.bucket_for(-0.5, start) is None


def test_p24_40_must_also_beat_the_interim():
    """At 24 h and beyond the interim already serves; 3b replaces it only where it is better (#337)."""
    def perfect(raw, horizon, start):
        return (raw - 0.05) / 2
    fit = _fit.fit_pd_calibration(_observations(_rows()), "QLD1", NOW, perfect)
    row = fit.validation["p24_40"]
    assert row["admitted"] is False and row["reason"] == "no better than the interim"
    assert row["interim_mae"] == pytest.approx(0.0, abs=1e-9)
    assert {"p06_12", "p12_24"} <= fit.admitted and "p24_40" not in fit.admitted


def test_a_band_not_admitted_serves_nothing_so_the_interim_serves_it():
    """16 November, NSW1 and QLD1: p24_40 lost to the interim on the holdout, and the
    shorter-band chain served p12_24's map at 24 h and beyond instead of the interim."""
    def perfect(raw, horizon, start):
        return (raw - 0.05) / 2
    fit = _fit.fit_pd_calibration(_observations(_rows()), "QLD1", NOW, perfect)
    start = datetime(2026, 11, 1, 18, 0, tzinfo=NEM_TZ)
    assert "p24_40" not in fit.admitted and "p12_24" in fit.admitted
    assert fit.bucket_for(30.0, start) is None and fit.apply(_raw(0.10, 30.0), 30.0, start) is None
    assert fit.bucket_for(16.0, start)[0].startswith("p12_24__")      # its own band still serves
    record = SimpleNamespace(calibration=fit, fit_generation=1)
    models = _cal.ServingModels(pd7day={"QLD1": _Interim()}, pd={"QLD1": record})
    run_at = start - timedelta(hours=30)
    served = _cal.serve("QLD1", start + timedelta(minutes=30), 1000 * _raw(0.10, 30.0), run_at, models)
    assert served.calibrated_source == "pd7day_iso_h24_48"


def test_a_thin_bucket_with_no_shorter_admitted_band_serves_nothing(fitted):
    """p06_12 is the shortest admitted band here: a thin p06_12 bucket skips the
    unadmitted p03_06 to p00_01 and leaves the period to the caller's fallback."""
    start = datetime(2026, 11, 1, 18, 0, tzinfo=NEM_TZ)
    key, bucket = fitted.bucket_for(8.0, start)
    assert key.startswith("p06_12__")
    try:
        fitted.buckets[key] = _fit._Bucket(bucket.model, _fit.MIN_BAND_OBS - 1, bucket.days)
        assert fitted.bucket_for(8.0, start) is None
        assert fitted.apply(_raw(0.10, 8.0), 8.0, start) is None
    finally:
        fitted.buckets[key] = bucket


def test_a_band_with_too_few_holdout_rows_is_not_admitted():
    rows = [r for r in _rows() if not (r["horizon_hours"] >= 24 and r["interval_time"] >= "2026-10-25")]
    fit = _fit.fit_pd_calibration(_observations(rows), "QLD1", NOW)
    assert fit.validation["p24_40"]["admitted"] is False
    assert "p24_40" not in fit.admitted


def test_spikes_and_interventions_are_left_out_as_pd7day_leaves_them_out():
    rows = _rows(days=21)
    spike = dict(rows[-1], pd7day_forecast=5.0, actual_rrp=6.0)
    pulse = dict(rows[-2], is_intervention=True)
    plain = _fit.fit_pd_calibration(_observations(rows), "QLD1", NOW)
    with_both = _fit.fit_pd_calibration(_observations(rows + [spike, pulse]), "QLD1", NOW)
    assert with_both.total_observations == plain.total_observations


def test_the_summary_has_pd7days_bucket_shape_and_the_holdout(fitted):
    summary = fitted.summary()
    assert summary["admitted_bands"] == ["p06_12", "p12_24", "p24_40"]
    assert summary["holdout_days"] == 7 and set(summary["validation"]) == set(_fit.PD_HORIZON_LABELS)
    bucket = next(b for k, b in summary["buckets"].items() if k.startswith("p24_40__"))
    assert {"n", "days", "usable", "ols_a", "iso_n_steps", "iso_mae", "iso_top", "iso_bottom"} <= set(bucket)


# ── Serving order: the fit, then the interim, then passthrough ──────────────

RUN_AT = datetime(2026, 11, 15, 21, 1, tzinfo=NEM_TZ)


class _Interim:
    fit_generation = 1

    def apply_to_price(self, raw, horizon_hours, hour_of_day, *, interval_dt=None):
        return {"calibrated": round(raw * 0.5, 6), "calibrated_source": "isotonic"}


def _end(hours):
    return RUN_AT + timedelta(hours=hours, minutes=30)


@pytest.mark.parametrize("hours, admitted, expected", [
    (30.0, True, "pd_isotonic"),          # the fit, where its band is admitted
    (30.0, False, "pd7day_iso_h24_48"),   # else the interim from 24 h
    (2.0, True, "pd_isotonic"),
    (2.0, False, "passthrough"),          # else passthrough below 24 h
])
def test_pre_dispatch_is_served_by_its_fit_then_the_interim_then_passthrough(hours, admitted, expected):
    fit = SimpleNamespace(apply=lambda raw, h, start: (
        {"calibrated": round(raw * 0.8, 6), "p10": None, "p50": None, "p90": None,
         "calibrated_source": "pd_isotonic", "n_obs": 300} if admitted else None))
    models = _cal.ServingModels(pd7day={"QLD1": _Interim()},
                                pd={"QLD1": SimpleNamespace(calibration=fit, fit_generation=4)})
    served = _cal.serve("QLD1", _end(hours), 200.0, RUN_AT, models)
    assert served.calibrated_source == expected
    assert served.rrp_mwh == {"pd_isotonic": 160.0, "pd7day_iso_h24_48": 100.0, "passthrough": 200.0}[expected]
    if expected == "pd_isotonic":
        assert served.n_obs == 300


def test_interim_kwh_is_the_interims_own_rule():
    start = RUN_AT + timedelta(hours=30)
    assert _cal.interim_kwh(_Interim(), 0.3, 30.0, start) == 0.15
    assert _cal.interim_kwh(_Interim(), 0.3, 12.0, start) is None     # below 24 h it does not serve
    assert _cal.interim_kwh(None, 0.3, 30.0, start) is None


# ── The record: refit once the history is long enough ───────────────────────

def _record(rows):
    log = _obslog.ObservationLog(None, "QLD1", store_factory=lambda key: MagicMock(), key_prefix=_const.PD_KEY_PREFIX)
    for row in rows:
        log.append(row)
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    return _pdr.PredispatchRecord(hass, "QLD1", history_store=MagicMock(), log=log)


def test_the_record_waits_for_14_days_of_observations():
    record = _record(_rows(days=10, start=NOW - timedelta(days=10)))
    assert run_async(record.async_refit(_Interim(), now=NOW)) is None
    assert record.calibration is None and record.fit_generation == 0
    summary = record.pd_summary()
    assert summary["status"] == "not_fitted" and summary["min_history_days"] == 14.0


def test_the_record_fits_serves_and_moves_its_generation():
    record = _record(_rows())
    result = run_async(record.async_refit(None, now=NOW))
    assert result is record.calibration and record.fit_generation == 1
    assert result.admitted == {"p06_12", "p12_24", "p24_40"}
    run_async(record.async_refit(None, now=NOW))
    assert record.fit_generation == 2
    summary = record.pd_summary()
    assert summary["status"] == "fitted" and summary["admitted_bands"] == ["p06_12", "p12_24", "p24_40"]
    # Served through predispatch_calibration, as every consumer reads it.
    models = _cal.ServingModels(pd={"QLD1": record})
    assert _cal.generation("QLD1", models) == (None, 2)
    served = _cal.serve("QLD1", _end(30.0), 1000 * _raw(0.10, 30.0), RUN_AT, models)
    assert served.calibrated_source == "pd_isotonic"
    assert served.rrp_mwh == pytest.approx(100.0, abs=10)
