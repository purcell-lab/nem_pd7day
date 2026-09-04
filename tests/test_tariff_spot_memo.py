"""
Per run memo for the tariff calibrated spot, issue #62 item 4.

The tariff sensors rebuilt the whole calibrated forecast on every state write,
and so did every other tariff entity of the same region, while the price
forecast sensor of that region had already computed and memoised the same
numbers. This adds a per region memo slot the tariff path reads, filled either
from the forecast memo sensor.py already keeps or from one shared build.

Sharing a slot with the forecast path is only safe because PR #77 unified the
two calibration call sites on ``calibration_inputs.calibrate_interval`` and
threads ``run_at_iso`` through it, so both paths use the same model and the same
per run stage-2 band floor. That is the assumption these tests are here to
police. The central assertion is not that the memo is faster: it is that every
published number is byte for byte what the unmemoised code published, over a
full seven day run that crosses the passthrough, isotonic_below_domain,
isotonic and isotonic+stpasa branches.

Which of these fail without the production change: the two call counting tests
and the memo key signature test. The equality tests are pins, and they pass on
main by construction, because a change that altered a published tariff price
would be the defect rather than the fix. They are the deliverable that makes
the memo reviewable, so they are asserted over a sweep with the branch coverage
itself asserted, not over a couple of hand picked intervals.

Run with:  python -m pytest tests/test_tariff_spot_memo.py -v
or simply: python tests/test_tariff_spot_memo.py
"""
from __future__ import annotations

import inspect
import os
import random
import sys
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_tariff_calibration_parity import (  # noqa: E402
    RUN_AT,
    _tariff_mod,
    make_period,
    make_sensors,
    make_stpasa_interval,
)

from custom_components.nem_pd7day import calibration_inputs  # noqa: E402
from custom_components.nem_pd7day.nem_time import parse_iso  # noqa: E402


def full_run(n_intervals: int = 336):
    """A seven day run spanning every branch of the calibration pipeline.

    The price choices include values below the fitted domain, which
    take isotonic_below_domain, and a spike above SPIKE_THRESHOLD. The STPASA gap
    leaves in band intervals with no features, which must degrade to isotonic
    only on both the memoised and the unmemoised path alike.
    """
    run_dt = parse_iso(RUN_AT)
    rng = random.Random(11)
    periods = []
    stpasa = []
    for i in range(n_intervals):
        start_dt = run_dt + timedelta(minutes=30 * (i + 1))
        # -0.15 is below the fitted domain of every bucket and 3.4 is above
        # SPIKE_THRESHOLD, so the sweep reaches both ends of the pipeline.
        value = rng.choice([-0.15, -0.05, -0.00757, 0.0, 0.03, 0.12093, 0.52396, 3.4])
        periods.append(make_period(start_dt, value))
        # The STPASA gap is placed over peak hours of an in band day so that
        # some fitted buckets get no features and take the isotonic only
        # branch. Moving it moves the branch coverage the sweep asserts.
        if not 118 <= i < 132:
            stpasa.append(
                make_stpasa_interval(start_dt, solar=rng.uniform(0.0, 4000.0))
            )
    return periods, stpasa


def prime(sensor) -> None:
    """Populate the caches __init__ populates, for a __new__ built sensor."""
    sensor._tariff_cache = None
    sensor._period_tariff_cache = None
    sensor._cached_tariff_periods = sensor._get_tariff_periods()
    if hasattr(sensor, "_get_daily_supply_charge"):
        sensor._cached_daily_supply_charge = sensor._get_daily_supply_charge()


def build(region: str = "QLD1"):
    periods, stpasa = full_run()
    forecast, tariff, export, coord, store = make_sensors(periods, stpasa, region)
    for s in (tariff, export):
        prime(s)
    return periods, forecast, tariff, export, coord, store


def days27_of(tariff):
    """A day 2 to 7 sensor sharing the import sensor's coordinator and store."""
    cls = _tariff_mod.TariffForecastDays27Sensor
    sensor = cls.__new__(cls)
    sensor.__dict__.update(tariff.__dict__)
    return sensor


def attrs_of(sensor, memoised: bool):
    """The forecast attribute list, with the memo either used or bypassed."""
    library = (
        "spot_to_feed_in_tariff"
        if isinstance(sensor, _tariff_mod.NemPd7dayExportTariffSensor)
        else "spot_to_tariff"
    )
    with patch.object(_tariff_mod, library, return_value=15.5):
        if memoised:
            return sensor.extra_state_attributes["forecast"]
        with patch.object(type(sensor), "_calibrated_spot_map", lambda self, d: None):
            return sensor.extra_state_attributes["forecast"]


def clear_memos(coord) -> None:
    coord._calibrated_forecast_cache = {}
    coord._calibrated_spot_cache = {}


def count_apply(store):
    """Count CalibrationStore.apply_to_price calls without changing results."""
    real = store.apply_to_price
    calls = []

    def wrapper(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    return real, wrapper, calls


def sources_over_run(forecast_sensor) -> dict:
    d = forecast_sensor._price_data
    counts: dict = {}
    for entry in forecast_sensor._calibrated_forecast(d):
        src = entry.get("calibrated_source")
        counts[src] = counts.get(src, 0) + 1
    return counts


# ── The sweep the memo has to survive ────────────────────────────────────────


def test_sweep_reaches_every_calibration_branch():
    """Without this the equality assertions below would prove very little."""
    _periods, forecast, _tariff, _export, coord, _store = build()
    counts = sources_over_run(forecast)
    clear_memos(coord)
    for branch in ("passthrough", "isotonic_below_domain", "isotonic", "isotonic+stpasa"):
        assert counts.get(branch, 0) > 0, (
            f"the sweep never reached {branch}, so pinning tariff output over "
            f"it would be vacuous: {counts}"
        )
    print(f"  PASS: sweep covers {counts}")


def test_memo_does_not_change_any_published_tariff_price():
    """Every attribute of every interval is identical with and without the memo.

    Compared as whole dicts rather than only on ``spot``, so a memo that moved
    ``value``, ``network_rate`` or ``spot_raw`` would fail here too.
    """
    _periods, _forecast, tariff, export, coord, _store = build()
    days27 = days27_of(tariff)
    for sensor in (tariff, days27, export):
        clear_memos(coord)
        unmemoised = attrs_of(sensor, memoised=False)
        clear_memos(coord)
        memoised = attrs_of(sensor, memoised=True)
        assert len(unmemoised) == len(memoised) > 0
        assert unmemoised == memoised, (
            f"{type(sensor).__name__} published different attributes with the "
            "memo in place"
        )
        print(
            f"  PASS: {type(sensor).__name__} identical over "
            f"{len(memoised)} intervals"
        )
    clear_memos(coord)


def test_memo_hit_and_miss_agree_interval_by_interval():
    """A memo hit equals the direct calibration for the same interval."""
    periods, _forecast, tariff, _export, coord, _store = build()
    d = tariff._price_data
    spot_map = tariff._calibrated_spot_map(d)
    assert spot_map, "no memo was built"
    for period in periods:
        assert tariff._calibrated_value_memoised(period, spot_map) == (
            tariff._calibrated_value(period)
        ), f"memo disagrees with the direct call at {period.time}"
    print(f"  PASS: memo agrees with the direct call on {len(periods)} intervals")
    clear_memos(coord)


def test_memo_filled_by_the_forecast_sensor_gives_the_same_prices():
    """Reading the forecast memo must publish what a tariff build would.

    This is the shared slot case: the price forecast sensor has already been
    warmed for this run, so the tariff path takes its numbers from the list
    sensor.py memoised rather than calibrating anything.
    """
    _periods, forecast, tariff, _export, coord, _store = build()
    clear_memos(coord)
    own = attrs_of(tariff, memoised=True)

    clear_memos(coord)
    forecast._calibrated_forecast(forecast._price_data)
    real, wrapper, calls = count_apply(_store)
    _store.apply_to_price = wrapper
    try:
        shared = attrs_of(tariff, memoised=True)
    finally:
        _store.apply_to_price = real

    assert shared == own, "the forecast memo and a tariff build disagree"
    assert not calls, (
        f"the tariff write calibrated {len(calls)} intervals despite the "
        "forecast memo holding the run"
    )
    print("  PASS: forecast memo serves identical prices and no recalibration")
    clear_memos(coord)


def test_tariff_spot_still_equals_forecast_value():
    """Issue #66 parity holds whichever sensor fills the slot first."""
    _periods, forecast, tariff, _export, coord, _store = build()
    for label, warm_forecast_first in (("tariff first", False), ("forecast first", True)):
        clear_memos(coord)
        if warm_forecast_first:
            forecast._calibrated_forecast(forecast._price_data)
        entries = attrs_of(tariff, memoised=True)
        ff = {
            e["time"]: e
            for e in forecast._calibrated_forecast(forecast._price_data)
        }
        for entry in entries:
            g = ff[entry["time"]]
            assert entry["spot"] == round(g["value"], 6), (
                f"{label}: tariff spot {entry['spot']} against forecast "
                f"{round(g['value'], 6)} at {entry['time']}"
            )
        print(f"  PASS: parity holds, {label}, {len(entries)} intervals")
    clear_memos(coord)


# ── The cost, which is the point of the change ───────────────────────────────


def test_second_write_of_a_run_calibrates_nothing():
    """The reported defect: every write rebuilt the whole forecast."""
    _periods, _forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    real, wrapper, calls = count_apply(store)
    store.apply_to_price = wrapper
    try:
        attrs_of(tariff, memoised=True)
        first = len(calls)
        calls.clear()
        attrs_of(tariff, memoised=True)
        second = len(calls)
    finally:
        store.apply_to_price = real
    assert first > 300, f"the first write should calibrate the run, saw {first}"
    assert second == 0, f"the second write recalibrated {second} intervals"
    print(f"  PASS: first write calibrated {first} intervals, second calibrated 0")
    clear_memos(coord)


def test_other_entities_of_the_region_reuse_the_slot():
    """22 tariff entities on the install, 5 regions: one build per region.

    The export class and the day 2 to 7 class carry their own attribute loops,
    so each is checked rather than assumed.
    """
    _periods, _forecast, tariff, export, coord, store = build()
    days27 = days27_of(tariff)
    clear_memos(coord)
    real, wrapper, calls = count_apply(store)
    store.apply_to_price = wrapper
    try:
        attrs_of(tariff, memoised=True)
        assert len(calls) > 300
        for sensor in (export, days27):
            calls.clear()
            entries = attrs_of(sensor, memoised=True)
            assert entries, f"{type(sensor).__name__} published nothing"
            assert not calls, (
                f"{type(sensor).__name__} calibrated {len(calls)} intervals "
                "that the region had already calibrated"
            )
            print(f"  PASS: {type(sensor).__name__} reused the region slot")
    finally:
        store.apply_to_price = real
    clear_memos(coord)


# ── Invalidation, which is where a memo becomes a wrong price ────────────────


def test_a_refit_invalidates_the_slot():
    """A calibration refit moves every price, so the slot must not survive it."""
    _periods, _forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    before = attrs_of(tariff, memoised=True)

    # A refit bumps fit_generation. Drop the isotonic model at the same time so
    # the recomputed prices are visibly different, which is what makes the
    # assertion below meaningful rather than a tautology.
    store._calibration.iso_models = {}
    store._calibration.ols_models = {}
    store._fit_generation = 2

    real, wrapper, calls = count_apply(store)
    store.apply_to_price = wrapper
    try:
        after = attrs_of(tariff, memoised=True)
    finally:
        store.apply_to_price = real

    assert len(calls) > 300, "the refit did not force a rebuild"
    assert after != before, (
        "the refit changed the model but the published prices did not move, "
        "so this test is not proving invalidation"
    )
    print(f"  PASS: refit forced a rebuild of {len(calls)} intervals")
    clear_memos(coord)


def test_a_new_stpasa_index_invalidates_the_slot():
    """Any STPASA refetch moves fetched_at and can move a stage-2 price."""
    _periods, _forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    attrs_of(tariff, memoised=True)
    coord._stpasa_index_run = "a-different-stpasa-run|refetched"
    real, wrapper, calls = count_apply(store)
    store.apply_to_price = wrapper
    try:
        attrs_of(tariff, memoised=True)
    finally:
        store.apply_to_price = real
    assert len(calls) > 300, "a new STPASA index did not force a rebuild"
    print("  PASS: a new STPASA index forces a rebuild")
    clear_memos(coord)


def test_a_new_run_invalidates_the_slot():
    """A new PD7DAY run must not be priced from the previous run's slot."""
    _periods, _forecast, tariff, _export, coord, store = build()
    clear_memos(coord)
    attrs_of(tariff, memoised=True)
    d = tariff._price_data
    d.forecast_generated_at = "2026-09-01T05:00:00+10:00"
    real, wrapper, calls = count_apply(store)
    store.apply_to_price = wrapper
    try:
        attrs_of(tariff, memoised=True)
    finally:
        store.apply_to_price = real
    assert len(calls) > 300, "a new run did not force a rebuild"
    print("  PASS: a new run forces a rebuild")
    clear_memos(coord)


def test_an_interval_absent_from_the_memo_is_calibrated_not_nulled():
    """A memo miss must never be published as none.

    Returning none for an interval the memo simply does not carry would be the
    missing data rule applied to the wrong thing: there is an honest calibrated
    price available, the memo just does not hold it.
    """
    periods, _forecast, tariff, _export, coord, _store = build()
    period = periods[0]
    direct = tariff._calibrated_value(period)
    assert direct is not None, "fixture interval does not calibrate, pick another"
    assert tariff._calibrated_value_memoised(period, {}) == direct
    assert tariff._calibrated_value_memoised(period, None) == direct
    print("  PASS: a memo miss falls through to the calibration call")
    clear_memos(coord)


def test_no_store_means_no_memo_and_raw_passthrough():
    """With no calibration store there is nothing to memoise."""
    _periods, _forecast, tariff, _export, coord, _store = build()
    tariff._store = None
    assert tariff._calibrated_spot_map(tariff._price_data) is None
    period = tariff._price_data.forecast[0]
    assert tariff._calibrated_value_memoised(period, None) == period.value
    clear_memos(coord)
    print("  PASS: no store means no memo and the raw value passes through")


def test_memo_key_cannot_be_derived_inside_the_helper():
    """PR #76's rule: take the memo key on the loop and pass it down.

    Pinned as a signature requirement rather than a comment, because the defect
    PR #76 fixed was a key taken late and used to publish a result computed
    under a key that had already moved. A default here would let a caller stop
    passing one.
    """
    sig = inspect.signature(calibration_inputs.calibrated_spot_map)
    assert "key" in sig.parameters, "the memo helper lost its key argument"
    assert sig.parameters["key"].default is inspect.Parameter.empty, (
        "calibrated_spot_map must require the key from its caller"
    )
    print("  PASS: the memo key is a required argument")


def test_one_key_implementation_is_shared_with_sensor_py():
    """sensor.py and the tariff path must agree on what makes the slot stale."""
    from custom_components.nem_pd7day.sensor import PD7DayForecastSensor

    _periods, forecast, tariff, _export, coord, store = build()
    d = tariff._price_data
    assert forecast._calibrated_forecast_key(d) == calibration_inputs.calibrated_forecast_key(
        coord, store, tariff._region, d
    ), "the forecast sensor's key and the shared key builder disagree"
    assert PD7DayForecastSensor._calibrated_forecast_key.__doc__
    clear_memos(coord)
    print("  PASS: one key implementation, shared")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as err:
                failures += 1
                print(f"  FAIL: {name}: {err}")
    print("all memo tests passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)
