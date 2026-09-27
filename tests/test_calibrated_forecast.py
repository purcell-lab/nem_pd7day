"""
CalibratedForecast, the per region calibrated forecast memo provider (spec 003).

The provider owns the memo protocol that sensor.py's CalibratedWriteMixin and
the tariff sensors used to carry themselves: the key, reading and publishing
the region's slot on the coordinator, the lazy build, the currency check, the
guarded publish after an off-loop build (#58, #60, #61, PR #76) and the tariff
spot view (#62). The entity level behaviour stays pinned by
test_calibration_memo.py; these tests pin the provider on its own, with a
plain coordinator stand-in and a counting build and no Home Assistant at all.

Run with:  python -m pytest tests/test_calibrated_forecast.py -v
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from support import load_chain, nem_iso

_const_mod, _nem_time, _inputs_mod, _provider_mod = load_chain(
    "const", "nem_time", "calibration_inputs", "calibrated_forecast",
)

CalibratedForecast = _provider_mod.CalibratedForecast
WarmOutcome = _provider_mod.WarmOutcome
FORECAST_ATTR = _inputs_mod.CALIBRATED_FORECAST_MEMO_ATTR
SPOT_ATTR = _inputs_mod.CALIBRATED_SPOT_MEMO_ATTR

REGION = "QLD1"
RUN_AT = "2026-09-01T18:00:00+10:00"


# ── Stand-ins ────────────────────────────────────────────────────────────────


class Coordinator:
    """What the provider and the calibration inputs read off a coordinator."""

    def __init__(self, memo: bool = True) -> None:
        if memo:
            self._calibrated_forecast_cache: dict = {}
        self._stpasa_index_run = "stpasa-run-1"
        self.data = None
        self.current_run_features = None
        self.index_calls = 0

    def stpasa_index(self):
        self.index_calls += 1
        return None, {}, []


class ReadOnlyCoordinator(Coordinator):
    """A coordinator that refuses new memo attributes, like a read-only mock."""

    def __setattr__(self, name, value) -> None:
        if name in (FORECAST_ATTR, SPOT_ATTR):
            raise AttributeError(name)
        object.__setattr__(self, name, value)


class Store:
    """A calibration store with a controllable fit generation.

    ``apply_to_price`` adds one to the raw price, so a calibrated value is
    always distinguishable from a raw passthrough.
    """

    def __init__(self, fit_generation: int = 1) -> None:
        self.fit_generation = fit_generation
        self._region = REGION
        self.applied = 0

    def apply_to_price(self, raw, horizon_hours, hour_of_day, **_features):
        self.applied += 1
        return {"calibrated": raw + 1.0}


def period(i: int, value: float = 0.1):
    """Interval ``i`` of the run: ``time`` is the START, ``nemtime`` the END."""
    start_dt = _nem_time.parse_iso(RUN_AT) + timedelta(minutes=30 * (i + 1))
    return SimpleNamespace(
        time=nem_iso(start_dt),
        nemtime=nem_iso(start_dt + timedelta(minutes=30)),
        value=value,
    )


def price_data(n: int = 6, run_at: str = RUN_AT):
    return SimpleNamespace(
        forecast_generated_at=run_at,
        forecast=[period(i, 0.1 + i / 100) for i in range(n)],
    )


class CountingBuild:
    """The entity's _calibrated_forecast_values, counted, with an optional hook."""

    def __init__(self, hook=None) -> None:
        self.calls = 0
        self._hook = hook

    def __call__(self, d):
        self.calls += 1
        if self._hook is not None:
            self._hook()
        return [{"time": _inputs_mod.interval_key_for_period(p), "value": p.value} for p in d.forecast]


class Executor:
    """``hass.async_add_executor_job``: runs the job inline and counts the hops."""

    def __init__(self, before=None, after=None) -> None:
        self.calls: list[tuple] = []
        self._before = before
        self._after = after

    async def __call__(self, func, *args):
        self.calls.append((func, args))
        if self._before is not None:
            self._before()
        await asyncio.sleep(0)
        result = func(*args)
        if self._after is not None:
            self._after()
        return result


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def provider(coordinator=None, store=None, region=REGION):
    coordinator = Coordinator() if coordinator is None else coordinator
    store = Store() if store is None else store
    return CalibratedForecast(coordinator, store, region)


# ── Key ──────────────────────────────────────────────────────────────────────


def test_key_is_the_shared_key_builder():
    """One key implementation (#66): the provider calls calibration_inputs."""
    coordinator, store, d = Coordinator(), Store(fit_generation=7), price_data()
    cf = CalibratedForecast(coordinator, store, REGION)

    key = cf.key(d)

    assert key == _inputs_mod.calibrated_forecast_key(coordinator, store, REGION, d)
    assert key == (REGION, RUN_AT, 6, "stpasa-run-1", 7)
    assert coordinator.index_calls >= 1, "the STPASA index was not refreshed before keying"


def test_key_swallows_a_failing_stpasa_index():
    coordinator = Coordinator()

    def _broken():
        raise RuntimeError("store unavailable")

    coordinator.stpasa_index = _broken
    assert provider(coordinator).key(price_data())[0] == REGION


# ── Hit, miss, slot ──────────────────────────────────────────────────────────


def test_cold_memo_misses_and_a_published_value_hits():
    cf = provider()
    d = price_data()
    key = cf.key(d)
    value = [{"time": "t", "value": 1.0}]

    assert cf.cached(key) is None
    cf.publish(key, value)
    assert cf.cached(key) is value


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("not a tuple", id="malformed"),
        pytest.param(("k",), id="short tuple"),
        pytest.param((("other",), [1]), id="mismatched key"),
        pytest.param(None, id="none entry"),
    ],
)
def test_malformed_or_mismatched_entries_miss(entry):
    coordinator = Coordinator()
    cf = provider(coordinator)
    key = cf.key(price_data())
    coordinator._calibrated_forecast_cache[REGION] = entry
    assert cf.cached(key) is None


def test_a_none_value_under_the_live_key_is_a_miss():
    coordinator = Coordinator()
    cf = provider(coordinator)
    key = cf.key(price_data())
    coordinator._calibrated_forecast_cache[REGION] = (key, None)
    assert cf.cached(key) is None


def test_the_slot_lives_on_the_coordinator():
    coordinator = Coordinator()
    cf = provider(coordinator)
    d = price_data()
    value = cf.forecast(d, CountingBuild())
    assert coordinator._calibrated_forecast_cache[REGION] == (cf.key(d), value)


def test_the_lazy_build_creates_the_slot_when_missing():
    coordinator = Coordinator(memo=False)
    cf = provider(coordinator)
    build = CountingBuild()
    d = price_data()

    first = cf.forecast(d, build)
    second = cf.forecast(d, build)

    assert isinstance(coordinator._calibrated_forecast_cache, dict)
    assert second is first
    assert build.calls == 1


def test_publish_does_not_create_the_slot():
    """Only the lazy build creates the memo; the warm's publish never did."""
    coordinator = Coordinator(memo=False)
    cf = provider(coordinator)
    cf.publish(cf.key(price_data()), [])
    assert not hasattr(coordinator, FORECAST_ATTR)


def test_a_read_only_coordinator_still_gets_a_forecast():
    """A coordinator that refuses the attribute gets the build, unmemoised."""
    coordinator = ReadOnlyCoordinator(memo=False)
    cf = provider(coordinator)
    build = CountingBuild()
    d = price_data()

    assert cf.forecast(d, build) == build(d)
    cf.forecast(d, build)
    assert build.calls == 3
    assert not hasattr(coordinator, FORECAST_ATTR)


def test_a_non_dict_memo_attribute_is_unusable():
    """A mock coordinator invents attributes; only a real dict is a memo."""
    coordinator = Coordinator(memo=False)
    coordinator._calibrated_forecast_cache = object()
    cf = provider(coordinator)
    assert cf.cached(cf.key(price_data())) is None


def test_regions_are_kept_apart():
    coordinator = Coordinator()
    qld = provider(coordinator, region="QLD1")
    nsw = provider(coordinator, region="NSW1")
    build = CountingBuild()
    d = price_data()

    qld_value = qld.forecast(d, build)
    nsw_value = nsw.forecast(d, build)

    assert build.calls == 2
    assert nsw_value is not qld_value
    assert set(coordinator._calibrated_forecast_cache) == {"QLD1", "NSW1"}
    assert qld.cached(qld.key(d)) is qld_value
    assert nsw.cached(nsw.key(d)) is nsw_value


def test_views_built_on_each_access_share_one_slot():
    """Stateless: two providers over the same coordinator are the same memo."""
    coordinator, store = Coordinator(), Store()
    build = CountingBuild()
    d = price_data()

    first = CalibratedForecast(coordinator, store, REGION).forecast(d, build)
    second = CalibratedForecast(coordinator, store, REGION).forecast(d, build)

    assert second is first
    assert build.calls == 1


@pytest.mark.parametrize("move", ["new_run", "refit", "new_stpasa_run"])
def test_a_moved_key_input_is_a_miss(move):
    coordinator, store = Coordinator(), Store()
    cf = provider(coordinator, store)
    build = CountingBuild()
    d = price_data()
    cf.forecast(d, build)

    if move == "new_run":
        d = price_data(run_at="2026-09-01T18:30:00+10:00")
    elif move == "refit":
        store.fit_generation += 1
    else:
        coordinator._stpasa_index_run = "stpasa-run-2"

    assert cf.is_current(d) is False
    cf.forecast(d, build)
    assert build.calls == 2


# ── Lazy build ───────────────────────────────────────────────────────────────


def test_the_lazy_build_publishes_under_the_key_taken_first():
    """The key is taken before the build and the result is stored under it,
    even when the build itself moves the key (it runs on the loop, so in
    production nothing can; this pins the order)."""
    coordinator, store = Coordinator(), Store(fit_generation=1)
    cf = provider(coordinator, store)
    d = price_data()
    key_before = cf.key(d)
    build = CountingBuild(hook=lambda: setattr(store, "fit_generation", 2))

    value = cf.forecast(d, build)

    assert coordinator._calibrated_forecast_cache[REGION] == (key_before, value)
    assert cf.is_current(d) is False


def test_the_lazy_build_passes_the_price_data_to_the_build():
    seen = []
    d = price_data()
    provider().forecast(d, lambda arg: seen.append(arg) or [])
    assert seen == [d]


# ── Currency ─────────────────────────────────────────────────────────────────


def test_currency_is_true_with_no_price_data():
    assert provider().is_current(None) is True


def test_currency_tracks_the_memo():
    cf = provider()
    d = price_data()
    assert cf.is_current(d) is False
    cf.forecast(d, CountingBuild())
    assert cf.is_current(d) is True


def test_the_currency_check_is_not_a_coroutine():
    """No await, so it cannot go stale before the write (#61)."""
    assert not asyncio.iscoroutinefunction(CalibratedForecast.is_current)
    assert not asyncio.iscoroutinefunction(CalibratedForecast.forecast)


# ── Warm ─────────────────────────────────────────────────────────────────────


def test_warm_with_no_data_does_nothing():
    executor = Executor()
    outcome = run(provider().warm(None, CountingBuild(), executor, lambda: None))
    assert outcome is WarmOutcome.NO_DATA
    assert executor.calls == []


def test_warm_publishes_under_the_key_taken_on_the_loop():
    coordinator = Coordinator()
    cf = provider(coordinator)
    build = CountingBuild()
    executor = Executor()
    d = price_data()

    outcome = run(cf.warm(d, build, executor, lambda: d))

    assert outcome is WarmOutcome.PUBLISHED
    assert executor.calls == [(build, (d,))], "the build must go through the executor, once"
    assert build.calls == 1
    key, value = coordinator._calibrated_forecast_cache[REGION]
    assert key == cf.key(d)
    assert value == build(d)


def test_warm_hit_makes_no_executor_hop():
    cf = provider()
    d = price_data()
    cf.forecast(d, CountingBuild())
    build = CountingBuild()
    executor = Executor()

    outcome = run(cf.warm(d, build, executor, lambda: d))

    assert outcome is WarmOutcome.HIT
    assert executor.calls == []
    assert build.calls == 0


def test_warm_key_is_taken_before_the_executor_hop():
    """A move that lands before the build starts is a move during the warm."""
    coordinator, store = Coordinator(), Store(fit_generation=1)
    cf = provider(coordinator, store)
    d = price_data()
    executor = Executor(before=lambda: setattr(store, "fit_generation", 2))

    outcome = run(cf.warm(d, CountingBuild(), executor, lambda: d))

    assert outcome is WarmOutcome.SUPERSEDED
    assert REGION not in coordinator._calibrated_forecast_cache


def test_warm_superseded_when_the_key_moves_during_the_hop():
    coordinator, store = Coordinator(), Store(fit_generation=1)
    cf = provider(coordinator, store)
    d = price_data()
    executor = Executor(after=lambda: setattr(store, "fit_generation", 2))

    outcome = run(cf.warm(d, CountingBuild(), executor, lambda: d))

    assert outcome is WarmOutcome.SUPERSEDED
    assert REGION not in coordinator._calibrated_forecast_cache, "a superseded warm published"


def test_warm_superseded_leaves_a_fresher_sibling_entry_alone():
    """The #60 sequence: a sibling publishes the live entry while this warm is away."""
    coordinator, store = Coordinator(), Store(fit_generation=1)
    cf = provider(coordinator, store)
    d = price_data()
    fresh = [{"time": "published by the sibling"}]

    def _sibling():
        store.fit_generation = 2
        cf.publish(cf.key(d), fresh)

    outcome = run(cf.warm(d, CountingBuild(), Executor(after=_sibling), lambda: d))

    assert outcome is WarmOutcome.SUPERSEDED
    assert cf.cached(cf.key(d)) is fresh


def test_warm_superseded_when_the_price_data_is_replaced():
    """Same key, different PriceData object: still not this warm's to publish."""
    coordinator = Coordinator()
    cf = provider(coordinator)
    d = price_data()
    replacement = price_data()
    assert cf.key(replacement) == cf.key(d), "the test needs an equal key to be meaningful"

    outcome = run(cf.warm(d, CountingBuild(), Executor(), lambda: replacement))

    assert outcome is WarmOutcome.SUPERSEDED
    assert REGION not in coordinator._calibrated_forecast_cache


def test_warm_propagates_what_the_executor_raises():
    coordinator = Coordinator()
    cf = provider(coordinator)
    d = price_data()

    async def _broken(func, *args):
        raise RuntimeError("executor unavailable")

    with pytest.raises(RuntimeError, match="executor unavailable"):
        run(cf.warm(d, CountingBuild(), _broken, lambda: d))
    assert REGION not in coordinator._calibrated_forecast_cache
    # The lazy path still supplies the value afterwards.
    assert cf.forecast(d, CountingBuild()) is cf.cached(cf.key(d))


def test_warm_publish_does_not_create_the_slot():
    """As before: the warm publishes into an existing memo only."""
    coordinator = Coordinator(memo=False)
    cf = provider(coordinator)
    d = price_data()

    outcome = run(cf.warm(d, CountingBuild(), Executor(), lambda: d))

    assert outcome is WarmOutcome.PUBLISHED
    assert not hasattr(coordinator, FORECAST_ATTR)


# ── Tariff spot view (#62) ───────────────────────────────────────────────────


def test_spot_without_a_store_is_the_raw_value():
    cf = CalibratedForecast(Coordinator(), None, REGION)
    p = period(0, 0.42)
    assert cf.spot(p, RUN_AT) == 0.42


def test_spot_with_a_store_is_the_shared_calibration():
    coordinator, store = Coordinator(), Store()
    cf = CalibratedForecast(coordinator, store, REGION)
    p = period(3, 0.25)

    assert cf.spot(p, RUN_AT) == _inputs_mod.calibrated_spot_for_period(store, coordinator, p, RUN_AT)
    assert cf.spot(p, RUN_AT) == pytest.approx(1.25)


def test_spot_map_is_none_without_a_store_or_data():
    assert CalibratedForecast(Coordinator(), None, REGION).spot_map(price_data()) is None
    assert provider().spot_map(None) is None


def test_spot_map_is_built_once_and_memoised_per_region():
    coordinator, store = Coordinator(), Store()
    cf = CalibratedForecast(coordinator, store, REGION)
    d = price_data()

    first = cf.spot_map(d)
    applied = store.applied
    second = cf.spot_map(d)

    assert applied == len(d.forecast)
    assert store.applied == applied, "the second read recalibrated"
    assert second is first
    assert getattr(coordinator, SPOT_ATTR)[REGION] == (cf.key(d), first)
    assert first == {
        _inputs_mod.interval_key_for_period(p): cf.spot(p, RUN_AT) for p in d.forecast
    }


def test_spot_map_reads_the_forecast_memo_when_it_is_current():
    coordinator, store = Coordinator(), Store()
    cf = CalibratedForecast(coordinator, store, REGION)
    d = price_data()
    entries = cf.forecast(d, CountingBuild())

    spot_map = cf.spot_map(d)

    assert store.applied == 0, "the spot view calibrated despite a current forecast memo"
    assert spot_map == {e["time"]: e["value"] for e in entries}


def test_spot_map_may_raise():
    """The provider does not swallow; the tariff sensor logs and falls back."""
    coordinator, store = Coordinator(), Store()

    def _broken(*_a, **_k):
        raise RuntimeError("store broken")

    store.apply_to_price = _broken
    with pytest.raises(RuntimeError):
        CalibratedForecast(coordinator, store, REGION).spot_map(price_data())


def test_spot_memoised_hit_and_miss():
    cf = provider()
    p = period(1)
    key = _inputs_mod.interval_key_for_period(p)

    assert cf.spot_memoised(p, {key: 0.33}) == (True, 0.33)
    assert cf.spot_memoised(p, {key: None}) == (True, None), "a memoised None is a hit"
    assert cf.spot_memoised(p, {}) == (False, None)
    assert cf.spot_memoised(p, None) == (False, None)


# ── Layering ─────────────────────────────────────────────────────────────────


def test_the_provider_imports_no_home_assistant_and_no_entity_module():
    import ast
    import pathlib

    source = pathlib.Path(_provider_mod.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert not any(name.split(".")[0] == "homeassistant" for name in imported), imported
    assert not imported & {".sensor", ".tariff_sensor"}, imported
