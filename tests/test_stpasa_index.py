"""
PD7DayCoordinator.stpasa_index: the run-keyed STPASA interval index and the
order its three attributes are published in.

Index cache (perf/calibration-cache-stpasa-index)
  ``stpasa_index`` caches ``_stpasa_index_map`` (interval START ISO to
  StpasaInterval), ``_stpasa_index_sorted`` ((epoch, interval) ascending, for
  the bisect nearest-match fallback in calibration_inputs) and
  ``_stpasa_index_run`` (``run_datetime|fetched_at``, the freshness token).
  The index is rebuilt only when that token changes, replacing the old
  O(forecast_intervals x stpasa_intervals) linear scan. The nearest-match
  lookup itself is covered in test_stpasa_match_tolerance.py and the memo key
  that folds the token in is covered by the calibration memo tests
  (test_calibration_memo*.py).

Publication order
  ``_stpasa_index_run`` is the token ``sensor.py`` folds into the calibrated
  forecast memo key, and the method genuinely runs on two threads: the event
  loop reaches it from ``_calibrated_forecast_key`` on every state write, and
  the executor reaches it from ``_calibrated_forecast_values`` by way of
  ``calibration_inputs.calibrate_interval``. If the key is stored before the
  map, a reader that lands in the gap finds ``_stpasa_index_run`` already
  equal to the key it computed, decides the index is current, and is handed
  the previous run's map. It memoises a forecast built from the old STPASA
  run under the new run's key, and no later reader recomputes it. Storing the
  map and the sorted list first and the key last turns the same interleaving
  into the harmless direction: an old key beside new data fails the reader's
  own freshness check and is rebuilt on the spot.

  These tests do not assert on statement order, which would pass vacuously
  against a reintroduction that reaches the same broken state by another
  route (publishing the key in the middle, say). What is pinned is a state
  invariant, checked from the position of a reader: whenever a reader
  observes ``_stpasa_index_run`` equal to the key the store implies right
  now, the index beside it must be the index that key names, and for a
  cleared key that means no index at all. It is checked at every attribute
  store during a rebuild, both by direct observation and by a second thread
  running the real reader shape from ``_calibrated_forecast_key``: refresh
  the index, then take the key.
"""
from __future__ import annotations

import itertools
import threading

import pytest

from support import install_ha_stubs, load_chain

install_ha_stubs()

(
    _nem_time,
    _engine_mod,
    _cal_store_mod,
    _client_mod,
    _const,
    _executor,
    _retry,
    _stpasa_mod,
    _notice_client_mod,
    _notice_store_mod,
    _coord_mod,
) = load_chain(
    "nem_time",
    "calibration_engine",
    "calibration_store",
    "pd7day_client",
    "const",
    "executor",
    "nemweb_retry",
    "stpasa_client",
    "market_notice_client",
    "notice_store",
    "coordinator",
)

PD7DayCoordinator = _coord_mod.PD7DayCoordinator
StpasaInterval = _stpasa_mod.StpasaInterval
StpasaResult = _stpasa_mod.StpasaResult
interval_start = _nem_time.interval_start
parse_iso = _nem_time.parse_iso

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

_EMPTY = StpasaResult(
    region=REGION,
    run_datetime="2026-09-01T20:00:00+10:00",
    intervals=[],
    fetched_at="2026-09-01T10:00:30+00:00",
)


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


# ── The index itself ──────────────────────────────────────────────────────────

def test_index_is_keyed_on_interval_start_and_sorted_by_epoch():
    result = _result(*RUNS[1])
    coordinator = _ObservedCoordinator(_Store(result))

    got, index_map, sorted_intervals = coordinator.stpasa_index()

    assert got is result
    assert frozenset(index_map) == _expected_starts(result)
    for si in result.intervals:
        assert index_map[interval_start(si.interval_datetime)] is si
    epochs = [e for e, _ in sorted_intervals]
    assert epochs == sorted(epochs)
    assert epochs == [parse_iso(interval_start(si.interval_datetime)).timestamp() for si in result.intervals]
    assert [si for _, si in sorted_intervals] == result.intervals


def test_index_is_rebuilt_only_when_the_run_or_fetched_at_changes():
    """The token is run_datetime|fetched_at: a repeat call reuses the index, a
    same-run refetch or a new run rebuilds it, an empty run clears it."""
    store = _Store(_result(*RUNS[1]))
    coordinator = _ObservedCoordinator(store)

    _, first_map, first_sorted = coordinator.stpasa_index()
    _, again_map, again_sorted = coordinator.stpasa_index()
    assert again_map is first_map and again_sorted is first_sorted, "an unchanged run must reuse the index"

    store.result = _result(*RUNS[2])  # same run_datetime, new fetched_at
    _, refetched_map, _ = coordinator.stpasa_index()
    assert refetched_map is not first_map
    assert frozenset(refetched_map) == _expected_starts(store.result)

    store.result = _result(*RUNS[3])
    _, new_run_map, _ = coordinator.stpasa_index()
    assert new_run_map is not refetched_map
    assert frozenset(new_run_map) == _expected_starts(store.result)

    store.result = _EMPTY
    assert coordinator.stpasa_index() == (_EMPTY, {}, [])
    assert coordinator._stpasa_index_run is None

    store.result = None
    assert coordinator.stpasa_index() == (None, {}, [])


def test_missing_store_yields_an_empty_index():
    assert _ObservedCoordinator(None).stpasa_index() == (None, {}, [])


# ── The publication invariant ─────────────────────────────────────────────────

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


def _transitions():
    """Every ordered pair of distinct states, including the empty result and None.

    This includes the two hand-picked cases: a rebuild from one run to the
    next, and the clearing branch, which returns empty literals rather than
    the cached attributes so a caller cannot be handed the leftover index even
    when the key is already cleared. Both branches must read the same way.
    """
    states = [_result(*spec) for spec in RUNS] + [_EMPTY, None]
    return [
        (before, after)
        for before, after in itertools.product(states, repeat=2)
        if _cache_key(before) != _cache_key(after)
    ]


@pytest.mark.parametrize("index", range(len(_transitions())))
def test_the_pairing_invariant_holds_across_every_transition(index):
    """Each transition is exercised twice: once observing every store directly,
    and once with a reader thread placed at each store in turn. A rebuild that
    skipped the reorder on only one branch, or that published the key in the
    middle rather than first, is caught here."""
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
    assert observations, "no store was observed, so nothing was measured"
    _assert_pairing(observations, store)

    for pause_at in range(1, len(observations) + 1):
        fresh = _ObservedCoordinator(_Store(before))
        fresh.stpasa_index()
        fresh._stpasa_store.result = after
        seen = _interleave_with_a_reader(fresh, fresh._stpasa_store, pause_at)
        _assert_pairing(seen, fresh._stpasa_store)
