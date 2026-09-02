"""
The STPASA index key must never be visible while it names the previous index.

``PD7DayCoordinator.stpasa_index`` caches three attributes: ``_stpasa_index_map``,
``_stpasa_index_sorted`` and ``_stpasa_index_run``. The last of those is the
freshness token, and it is the one ``sensor.py`` folds into the calibrated
forecast memo key in ``_calibrated_forecast_key``. Nothing about the three
stores is atomic together, and the method genuinely runs on two threads: the
event loop reaches it from ``_calibrated_forecast_key`` on every state write,
and the executor reaches it from ``_calibrated_forecast_values`` by way of
``calibration_inputs.calibrate_interval``, which is the half deliberately kept
off the loop.

That makes one interleaving expensive. If the key is stored before the map, a
reader that lands in the gap computes the new cache key from the store, finds
``_stpasa_index_run`` already equal to it, decides the index is current, and is
handed the previous run's map. It then memoises a forecast built from the old
STPASA run under the new run's key, and no later reader recomputes it, because
every later reader computes the same key and finds a hit. The stale forecast
lasts for the life of the run rather than for the width of the race.

Storing the map and the sorted list first and the key last turns the same
interleaving into the harmless direction: an old key alongside new data, which
fails the reader's own freshness check and is rebuilt on the spot.

These tests do not assert on statement order. An order assertion passes
vacuously against any reintroduction of the bug that reaches the same broken
state by a different route, for instance publishing the key in the middle. What
is pinned here is a state invariant, checked from the position of a reader:

  whenever a reader observes ``_stpasa_index_run`` equal to the cache key the
  store implies right now, the index alongside it must be the index that key
  names, and for a cleared key that means no index at all.

Any other pairing, an older key or ``None`` beside newer data, is the harmless
direction: the reader's own freshness check fails and it rebuilds.

The invariant is checked at every single attribute store during a rebuild, both
by direct observation and by a second thread running the real reader shape from
``_calibrated_forecast_key``: refresh the index, then take the key.

Reuses the Home Assistant stub preamble installed by ``test_sensor``, the same
way ``test_calibration_memo_key_toctou`` does.
"""

from __future__ import annotations

import itertools
import threading

if __name__ == "__main__":  # pragma: no cover - standalone entry point only
    # conftest installs the parent package bootstrap that lets the integration
    # modules resolve their relative imports. Under pytest it is applied for us.
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import conftest  # noqa: F401

import pytest
import test_sensor as ts  # noqa: F401 - installs the HA stubs

from custom_components.nem_pd7day.coordinator import PD7DayCoordinator
from custom_components.nem_pd7day.nem_time import interval_start
from custom_components.nem_pd7day.stpasa_client import StpasaInterval, StpasaResult

REGION = "QLD1"

# Every timestamp here is explicit. NEM time is UTC+10 with no DST, and
# interval_datetime is the interval END, so the index is keyed on the END
# minus 30 minutes.
RUNS = [
    ("2026-09-01T12:00:00+10:00", "2026-09-01T02:00:30+00:00", 4),
    ("2026-09-01T14:00:00+10:00", "2026-09-01T04:00:30+00:00", 6),
    # Same run_datetime refetched: only fetched_at moves, which is exactly the
    # case the cache key exists to catch.
    ("2026-09-01T14:00:00+10:00", "2026-09-01T04:30:12+00:00", 3),
    ("2026-09-01T16:00:00+10:00", "2026-09-01T06:00:07+00:00", 1),
]


def _result(run_datetime: str, fetched_at: str, count: int) -> StpasaResult:
    """Build a run whose interval set is unique to that run, so a torn read shows."""
    hour = int(run_datetime[11:13])
    intervals = [
        StpasaInterval(
            interval_datetime=f"2026-09-0{1 + (hour + i) // 24}"
            f"T{(hour + i) % 24:02d}:30:00+10:00",
            run_datetime=run_datetime,
            demand10=7400.0 + i,
            demand50=7000.0 + i,
            demand90=6600.0 + i,
            surpluscapacity=4941.0,
            ss_solar_uigf=120.0,
            ss_wind_uigf=900.0,
        )
        for i in range(count)
    ]
    return StpasaResult(
        region=REGION,
        run_datetime=run_datetime,
        intervals=intervals,
        fetched_at=fetched_at,
    )


def _cache_key(result) -> str | None:
    """The key ``stpasa_index`` computes, mirrored here so the test can predict it."""
    if result is None or not result.intervals:
        return None
    return f"{result.run_datetime}|{result.fetched_at}"


def _expected_starts(result) -> frozenset:
    if result is None or not result.intervals:
        return frozenset()
    return frozenset(interval_start(si.interval_datetime) for si in result.intervals)


class _Store:
    """The STPASA store, with a swappable latest result."""

    def __init__(self, result=None) -> None:
        self.result = result

    def latest(self):
        return self.result


class _ObservedCoordinator:
    """Just enough of PD7DayCoordinator to run the real ``stpasa_index``.

    Calls a hook after every store to one of the three index attributes, which
    is where a reader on the other thread would be able to look.
    """

    stpasa_index = PD7DayCoordinator.stpasa_index
    _hook = None

    def __init__(self, store) -> None:
        self._stpasa_store = store
        self._stpasa_index_run = None
        self._stpasa_index_map = {}
        self._stpasa_index_sorted = []

    def install_hook(self, hook) -> None:
        object.__setattr__(self, "_hook", hook)

    def __setattr__(self, name, value) -> None:
        object.__setattr__(self, name, value)
        hook = self._hook
        if hook is not None and name.startswith("_stpasa_index"):
            hook(self, name)

    # The reader shape from sensor.py _calibrated_forecast_key: refresh the
    # coordinator index, then take the key that the memo entry will be filed
    # under. The pair returned here is exactly the pair that would be memoised.
    def read_as_the_memo_would(self):
        _result_, index_map, _sorted = self.stpasa_index()
        key = getattr(self, "_stpasa_index_run", None)
        return key, frozenset(index_map)


def _assert_pairing(observations, store) -> None:
    """The invariant, checked from every position a reader could have occupied."""
    live_key = _cache_key(store.result)
    live_starts = _expected_starts(store.result)
    for where, key, starts in observations:
        if key != live_key:
            # An older key, or None, alongside whatever data. This is the
            # harmless direction: the reader's own freshness check fails and it
            # rebuilds. Nothing to assert.
            continue
        assert starts == live_starts, (
            "a reader observing the key the store implies right now was handed "
            "data that key does not name, so it would file stale STPASA data "
            "under a key that every later reader computes and nothing "
            f"invalidates: observed after storing {where}, key {key}, index "
            f"{sorted(starts)}, expected {sorted(live_starts)}"
        )


# ── The interleave, observed directly ─────────────────────────────────────────

def test_no_store_leaves_the_key_naming_an_index_that_is_not_there():
    """Snapshot the three attributes after every store during a rebuild."""
    store = _Store(_result(*RUNS[0]))
    coordinator = _ObservedCoordinator(store)
    coordinator.stpasa_index()

    store.result = _result(*RUNS[1])
    observations: list[tuple[str, str | None, frozenset]] = []
    coordinator.install_hook(
        lambda c, name: observations.append(
            (
                name,
                object.__getattribute__(c, "_stpasa_index_run"),
                frozenset(object.__getattribute__(c, "_stpasa_index_map")),
            )
        )
    )
    coordinator.stpasa_index()

    assert observations, "no store was observed, so nothing was measured"
    _assert_pairing(observations, store)


def test_the_cleared_key_leaves_no_index_behind_it():
    """The clearing branch has to hold the same invariant as the rebuild.

    Worth being plain about the strength of this one. The clearing branch
    returns empty literals rather than the cached attributes, so a caller of
    ``stpasa_index`` cannot be handed the leftover index even when the key is
    already cleared, and ``None`` matches no computed key regardless. What this
    pins is attribute level consistency, so both branches read the same way. It
    is not a live defect.
    """
    store = _Store(_result(*RUNS[1]))
    coordinator = _ObservedCoordinator(store)
    coordinator.stpasa_index()
    assert coordinator._stpasa_index_map, "nothing was cached, so nothing is cleared"

    # A run with no intervals is the branch that clears the index.
    store.result = StpasaResult(
        region=REGION,
        run_datetime="2026-09-01T18:00:00+10:00",
        intervals=[],
        fetched_at="2026-09-01T08:00:30+00:00",
    )
    observations: list[tuple[str, str | None, frozenset]] = []
    coordinator.install_hook(
        lambda c, name: observations.append(
            (
                name,
                object.__getattribute__(c, "_stpasa_index_run"),
                frozenset(object.__getattribute__(c, "_stpasa_index_map")),
            )
        )
    )
    coordinator.stpasa_index()

    assert observations, "no store was observed, so nothing was measured"
    _assert_pairing(observations, store)


# ── The interleave, driven by a real second thread ────────────────────────────

def _interleave_with_a_reader(coordinator, store, pause_at):
    """Run a rebuild, letting a second thread read at the ``pause_at``-th store.

    The writer blocks after that store and does not continue until the reader
    has completed a full read, which is how a reader that lands in the gap is
    reproduced without depending on timing luck.
    """
    reader_may_go = threading.Event()
    reader_done = threading.Event()
    writer_thread = threading.current_thread()
    seen: list[tuple[str, str | None, frozenset]] = []
    stores: list[str] = []

    def reader() -> None:
        reader_may_go.wait(5)
        try:
            key, starts = coordinator.read_as_the_memo_would()
            seen.append(("reader", key, starts))
        finally:
            reader_done.set()

    def hook(_c, name) -> None:
        if threading.current_thread() is not writer_thread:
            return
        stores.append(name)
        if len(stores) == pause_at:
            reader_may_go.set()
            reader_done.wait(5)

    thread = threading.Thread(target=reader, name="stpasa-index-reader")
    thread.start()
    coordinator.install_hook(hook)
    try:
        coordinator.stpasa_index()
    finally:
        coordinator.install_hook(None)
        reader_may_go.set()
        thread.join(5)

    assert not thread.is_alive(), "the reader thread never finished"
    assert seen, "the reader never observed anything"
    assert len(stores) >= pause_at, (
        f"only {len(stores)} stores happened, so the reader could not be placed "
        f"at store {pause_at}"
    )
    return seen


@pytest.mark.parametrize("pause_at", [1, 2, 3])
def test_a_concurrent_reader_never_memoises_the_old_index_under_the_new_key(pause_at):
    """A reader on the other thread must not be handed a mismatched pair."""
    store = _Store(_result(*RUNS[0]))
    coordinator = _ObservedCoordinator(store)
    coordinator.stpasa_index()

    store.result = _result(*RUNS[1])
    seen = _interleave_with_a_reader(coordinator, store, pause_at)
    _assert_pairing(seen, store)


# ── Invariant sweep ───────────────────────────────────────────────────────────

_EMPTY = StpasaResult(
    region=REGION,
    run_datetime="2026-09-01T20:00:00+10:00",
    intervals=[],
    fetched_at="2026-09-01T10:00:30+00:00",
)


def _transitions():
    """Every ordered pair of distinct states, including the empty result."""
    states = [_result(*spec) for spec in RUNS] + [_EMPTY, None]
    return [
        (before, after)
        for before, after in itertools.product(states, repeat=2)
        if _cache_key(before) != _cache_key(after)
    ]


@pytest.mark.parametrize("index", range(len(_transitions())))
def test_the_pairing_invariant_holds_across_every_transition(index):
    """Sweep the transitions rather than trusting the two hand-picked cases.

    Each pair is exercised twice: once observing every store directly, and once
    with a reader thread placed at each store in turn. A rebuild that skipped
    the reorder on only one branch, or that published the key in the middle
    rather than first, is caught here as well as by the point cases.
    """
    before, after = _transitions()[index]
    store = _Store(before)
    coordinator = _ObservedCoordinator(store)
    coordinator.stpasa_index()

    store.result = after
    observations: list[tuple[str, str | None, frozenset]] = []
    coordinator.install_hook(
        lambda c, name: observations.append(
            (
                name,
                object.__getattribute__(c, "_stpasa_index_run"),
                frozenset(object.__getattribute__(c, "_stpasa_index_map")),
            )
        )
    )
    coordinator.stpasa_index()
    coordinator.install_hook(None)
    _assert_pairing(observations, store)

    for pause_at in range(1, len(observations) + 1):
        fresh = _ObservedCoordinator(_Store(before))
        fresh.stpasa_index()
        fresh._stpasa_store.result = after
        seen = _interleave_with_a_reader(fresh, fresh._stpasa_store, pause_at)
        _assert_pairing(seen, fresh._stpasa_store)


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    def _run_all() -> None:
        failures = 0
        cases: list[tuple[str, object]] = [
            (
                "no store leaves the key naming an absent index",
                test_no_store_leaves_the_key_naming_an_index_that_is_not_there,
            ),
            (
                "the cleared key leaves no index behind it",
                test_the_cleared_key_leaves_no_index_behind_it,
            ),
        ]
        for at in (1, 2, 3):
            cases.append(
                (
                    f"a concurrent reader is safe with the pause at store {at}",
                    lambda a=at: (
                        test_a_concurrent_reader_never_memoises_the_old_index_under_the_new_key(a)
                    ),
                )
            )
        for i in range(len(_transitions())):
            cases.append(
                (
                    f"pairing invariant holds across transition {i}",
                    lambda i=i: test_the_pairing_invariant_holds_across_every_transition(i),
                )
            )
        for description, fn in cases:
            try:
                fn()
            except AssertionError as err:
                failures += 1
                print(f"  FAIL: {description}\n        {err}")
            else:
                print(f"  PASS: {description}")
        if failures:
            raise SystemExit(1)
        print(f"\nAll {len(cases)} checks passed.")

    _run_all()
