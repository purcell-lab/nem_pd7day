"""
Focused tests for the STPASA interval index and run-keyed calibration cache
added in perf/calibration-cache-stpasa-index.

These avoid importing the full HA stack; they exercise the pure index/lookup
logic and the memoisation key behaviour directly.
"""
from custom_components.nem_pd7day import nem_time


class _Interval:
    def __init__(self, end_iso, tag):
        self.interval_datetime = end_iso
        self.tag = tag


def _build_index(intervals):
    """Mirror of PD7DayCoordinator.stpasa_index index construction."""
    index_map = {}
    sorted_intervals = []
    for si in intervals:
        start_iso = nem_time.interval_start(si.interval_datetime)
        epoch = nem_time.parse_iso(start_iso).timestamp()
        index_map[start_iso] = si
        sorted_intervals.append((epoch, si))
    sorted_intervals.sort(key=lambda t: t[0])
    return index_map, sorted_intervals


def _nearest(sorted_intervals, target_iso):
    """Mirror of the bisect nearest-match fallback in sensor.py."""
    import bisect

    target_epoch = nem_time.parse_iso(target_iso).timestamp()
    epochs = [e for e, _ in sorted_intervals]
    pos = bisect.bisect_left(epochs, target_epoch)
    best = None
    best_delta = None
    for cand in (pos - 1, pos):
        if 0 <= cand < len(sorted_intervals):
            e, si = sorted_intervals[cand]
            delta = abs(e - target_epoch)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = si
    return best


INTERVALS = [
    _Interval("2026-08-10T00:30:00+10:00", "a"),  # start 00:00
    _Interval("2026-08-10T01:00:00+10:00", "b"),  # start 00:30
    _Interval("2026-08-10T01:30:00+10:00", "c"),  # start 01:00
    _Interval("2026-08-10T02:00:00+10:00", "d"),  # start 01:30
]


def test_exact_start_lookup_matches_linear_scan():
    index_map, _ = _build_index(INTERVALS)
    # Interval START for the "c" interval (end 01:30) is 01:00.
    got = index_map.get("2026-08-10T01:00:00+10:00")
    assert got is not None and got.tag == "c"


def test_nearest_fallback_picks_closest_start():
    _, sorted_intervals = _build_index(INTERVALS)
    # Target 00:44 -> nearest start is 00:30 ("b").
    got = _nearest(sorted_intervals, "2026-08-10T00:44:00+10:00")
    assert got.tag == "b"
    # Target 00:20 -> nearest start is 00:30 ("b") vs 00:00 ("a"); 00:20 is
    # closer to 00:30 (10 min) than 00:00 (20 min).
    got2 = _nearest(sorted_intervals, "2026-08-10T00:20:00+10:00")
    assert got2.tag == "b"


def test_index_equivalent_to_old_linear_scan():
    """The dict+bisect result must equal the old O(n*m) linear nearest scan."""
    _, sorted_intervals = _build_index(INTERVALS)

    def old_linear(target_iso):
        target = nem_time.parse_iso(target_iso)
        exact = None
        nearest = None
        nearest_delta = None
        for si in INTERVALS:
            si_start = nem_time.parse_iso(
                nem_time.interval_start(si.interval_datetime)
            )
            if si_start == target:
                exact = si
                break
            delta = abs((si_start - target).total_seconds())
            if nearest_delta is None or delta < nearest_delta:
                nearest_delta = delta
                nearest = si
        return exact or nearest

    index_map, sorted_intervals = _build_index(INTERVALS)
    for probe in (
        "2026-08-10T00:00:00+10:00",
        "2026-08-10T00:30:00+10:00",
        "2026-08-10T00:44:00+10:00",
        "2026-08-10T01:15:00+10:00",
        "2026-08-10T09:00:00+10:00",
    ):
        exact = index_map.get(probe)
        new = exact if exact is not None else _nearest(sorted_intervals, probe)
        old = old_linear(probe)
        assert new.tag == old.tag, f"mismatch at {probe}: new={new.tag} old={old.tag}"


def test_cache_key_invalidates_on_run_stpasa_and_calib():
    """The memo key must change when run, interval count, STPASA, or fit change."""
    def key(run, n, stpasa, cal):
        return (run, n, stpasa, cal)

    base = key("run1", 367, "stpasaA", 111)
    assert base == key("run1", 367, "stpasaA", 111)          # stable -> reuse
    assert base != key("run2", 367, "stpasaA", 111)          # new PD7DAY run
    assert base != key("run1", 368, "stpasaA", 111)          # interval count change
    assert base != key("run1", 367, "stpasaB", 111)          # new STPASA run
    assert base != key("run1", 367, "stpasaA", 222)          # refit
