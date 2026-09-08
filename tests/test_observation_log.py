"""
ObservationLog: one file per NEM day, presented as one flat list (issue #130).

Run with:  python -m pytest tests/test_observation_log.py -v
"""
from __future__ import annotations

import asyncio

from custom_components.nem_pd7day.observation_log import (
    UNDATED_SEGMENT,
    ObservationLog,
    _sort_key,
    segment_date,
)


class _Backend:
    """Shared dict of key -> saved data, plus a log of every call."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []


class _Store:
    """HA Store stand-in without delayed saves."""

    def __init__(self, backend: _Backend, key: str) -> None:
        self._b = backend
        self.key = key

    async def async_load(self):
        self._b.calls.append(("load", self.key))
        return self._b.data.get(self.key)

    async def async_save(self, data) -> None:
        self._b.calls.append(("save", self.key))
        self._b.data[self.key] = data

    async def async_remove(self) -> None:
        self._b.calls.append(("remove", self.key))
        self._b.data.pop(self.key, None)


class _DelayStore(_Store):
    """HA Store stand-in with delayed saves, recorded rather than scheduled."""

    def async_delay_save(self, data_func, delay) -> None:
        self._b.calls.append(("delay_save", self.key))
        self._b.data[self.key] = data_func()
        self._b.data[self.key + "#delay"] = delay


def _obs(day: str, hour: int, run: str = "07:30") -> dict:
    return {
        "interval_time": f"{day}T{hour:02d}:00:00+10:00",
        "forecast_run_at": f"{day}T{run}:00+10:00",
        "actual_rrp": 0.05,
        "pd7day_forecast": 0.06,
    }


def _undated_obs() -> dict:
    return {
        "interval_time": None,
        "forecast_run_at": "2026-09-01T07:30:00+10:00",
        "actual_rrp": 0.05,
        "pd7day_forecast": 0.06,
    }


def _log(backend: _Backend, store_cls=_Store) -> ObservationLog:
    return ObservationLog(
        hass=object(), region="SA1",
        store_factory=lambda key: store_cls(backend, key),
    )


def test_segment_date_reads_the_nem_day_and_tolerates_junk():
    assert segment_date(_obs("2026-09-05", 13)) == "2026-09-05"
    assert segment_date({"interval_time": None}) == UNDATED_SEGMENT
    assert segment_date({}) == UNDATED_SEGMENT
    assert segment_date({"interval_time": "not a date"}) == UNDATED_SEGMENT


# ── Issue #141: undated segments must sort as the oldest, not the newest ─────

def test_sort_key_puts_undated_before_every_real_date():
    """A plain string sort put "undated" after every "YYYY-MM-DD" key, so an
    undated segment read as the newest possible day everywhere in the module.
    It is not newer than anything, so its key must compare lowest instead."""
    assert _sort_key(UNDATED_SEGMENT) < _sort_key("2026-01-01")
    assert _sort_key("2026-09-04") < _sort_key("2026-09-05")
    assert _sort_key(UNDATED_SEGMENT) < _sort_key("0001-01-01")


def test_prune_drops_the_undated_segment_before_any_dated_day():
    """Before the fix, "undated" sorted last, so prune reached it after every
    real day and the newest-day guard then protected it as if it were the
    most recent: a genuine dated day was dropped in its place. 3 undated
    rows plus 3 dated days of 4 rows each is 15 total; capping at 12 must
    drop exactly the 3 undated rows and leave every dated day intact."""
    b = _Backend()
    log = _log(b)
    for _ in range(3):
        log.append(_undated_obs())
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        for hour in range(4):
            log.append(_obs(day, hour))
    assert len(log.observations) == 15

    dropped = log.prune(max_total=12)
    assert len(dropped) == 3
    assert all(segment_date(o) == UNDATED_SEGMENT for o in dropped), (
        "prune must drop the undated segment, not a dated day"
    )
    assert log.dates == ["2026-09-01", "2026-09-02", "2026-09-03"], (
        "every dated day must survive a prune that only needs to drop the "
        "undated segment to reach the cap"
    )
    assert len(log.observations) == 12


def test_undated_rows_do_not_present_as_the_most_recent():
    """The flat list is documented oldest day first; an undated row is not
    newer than a real date and must not sit at the tail as though it were."""
    b = _Backend()
    log = _log(b)
    log.append(_obs("2026-09-01", 12))
    log.append(_undated_obs())
    log.append(_obs("2026-09-02", 12))
    assert segment_date(log.observations[0]) == UNDATED_SEGMENT
    assert segment_date(log.observations[-1]) == "2026-09-02", (
        "the most recent dated row must be the tail, not the undated one"
    )


def test_append_after_an_undated_row_does_not_force_repeated_rebuilds():
    """Once an undated segment exists, every following append used to
    compare its date against the tail's "undated" string and lose (any real
    date sorts lower than "undated" lexically), forcing a full _rebuild_flat
    on every single append thereafter. One rebuild is needed to move the
    undated segment ahead of the dated tail; nothing after that should
    rebuild for same-day or later appends."""
    b = _Backend()
    log = _log(b)
    log.append(_obs("2026-09-01", 0))

    calls = []
    real_rebuild = log._rebuild_flat

    def counting() -> None:
        calls.append(1)
        real_rebuild()

    log._rebuild_flat = counting

    log.append(_undated_obs())
    assert len(calls) == 1, "inserting the undated row ahead of the dated tail needs one rebuild"

    for hour in range(1, 20):
        log.append(_obs("2026-09-01", hour))
    assert len(calls) == 1, (
        f"19 subsequent same-day appends triggered {len(calls) - 1} extra "
        "rebuilds; each one should have taken the O(1) append path"
    )
    assert len(log.observations) == 21


def test_empty_log_loads_to_nothing_and_saves_nothing():
    b = _Backend()
    log = _log(b)
    assert asyncio.run(log.async_load()) == []
    asyncio.run(log.async_save())
    assert b.data == {}
    assert b.calls == [("load", "nem_pd7day.sa1.observation_segments")]


def test_append_writes_only_the_touched_day_and_the_manifest():
    b = _Backend()
    log = _log(b)
    log.append(_obs("2026-09-04", 12))
    log.append(_obs("2026-09-04", 13))
    log.append(_obs("2026-09-05", 12))
    asyncio.run(log.async_save())
    assert sorted(b.data) == [
        "nem_pd7day.sa1.observation_segments",
        "nem_pd7day.sa1.observations.2026-09-04",
        "nem_pd7day.sa1.observations.2026-09-05",
    ]
    assert b.data["nem_pd7day.sa1.observation_segments"] == {"dates": ["2026-09-04", "2026-09-05"]}
    assert len(b.data["nem_pd7day.sa1.observations.2026-09-04"]["observations"]) == 2

    # A second interval on the 5th touches only that day.
    b.calls.clear()
    log.append(_obs("2026-09-05", 13))
    asyncio.run(log.async_save())
    assert b.calls == [("save", "nem_pd7day.sa1.observations.2026-09-05")]
    assert [o["interval_time"][:13] for o in log.observations] == [
        "2026-09-04T12", "2026-09-04T13", "2026-09-05T12", "2026-09-05T13",
    ]


def test_late_row_for_an_older_day_keeps_the_flat_list_in_day_order():
    b = _Backend()
    log = _log(b)
    log.append(_obs("2026-09-05", 12))
    log.append(_obs("2026-09-04", 23))
    assert [segment_date(o) for o in log.observations] == ["2026-09-04", "2026-09-05"]


def test_touch_marks_the_day_of_an_in_place_update_dirty():
    b = _Backend()
    log = _log(b)
    row = _obs("2026-09-04", 12)
    log.append(row)
    log.append(_obs("2026-09-05", 12))
    asyncio.run(log.async_save())
    b.calls.clear()
    row["actual_rrp"] = 0.07
    log.touch(row)
    asyncio.run(log.async_save())
    assert b.calls == [("save", "nem_pd7day.sa1.observations.2026-09-04")]
    assert b.data["nem_pd7day.sa1.observations.2026-09-04"]["observations"][0]["actual_rrp"] == 0.07


def test_delayed_save_is_used_when_the_store_class_offers_it():
    b = _Backend()
    log = _log(b, _DelayStore)
    log.append(_obs("2026-09-05", 12))
    asyncio.run(log.async_save())
    assert ("delay_save", "nem_pd7day.sa1.observations.2026-09-05") in b.calls
    assert b.data["nem_pd7day.sa1.observations.2026-09-05#delay"] == 300
    # The manifest is small and always written at once.
    assert ("save", "nem_pd7day.sa1.observation_segments") in b.calls
    # A migration save is immediate even on a delaying store.
    b2 = _Backend()
    log2 = _log(b2, _DelayStore)
    rows = [_obs("2026-09-01", 12), _obs("2026-09-02", 12)]

    async def legacy():
        return rows

    asyncio.run(log2.async_load(legacy_loaders=(legacy,)))
    assert [c for c in b2.calls if c[0] == "delay_save"] == []
    assert sorted(k for k in b2.data if "observations." in k) == [
        "nem_pd7day.sa1.observations.2026-09-01",
        "nem_pd7day.sa1.observations.2026-09-02",
    ]


def test_load_reads_the_manifest_days_oldest_first():
    b = _Backend()
    b.data["nem_pd7day.sa1.observation_segments"] = {"dates": ["2026-09-05", "2026-09-03"]}
    b.data["nem_pd7day.sa1.observations.2026-09-03"] = {"observations": [_obs("2026-09-03", 12)]}
    b.data["nem_pd7day.sa1.observations.2026-09-05"] = {"observations": [_obs("2026-09-05", 12)]}
    log = _log(b)
    rows = asyncio.run(log.async_load())
    assert [segment_date(o) for o in rows] == ["2026-09-03", "2026-09-05"]
    assert log.dates == ["2026-09-03", "2026-09-05"]
    assert log.dirty_dates == set()


def test_load_drops_manifest_days_whose_file_is_missing_and_rewrites_the_manifest():
    b = _Backend()
    b.data["nem_pd7day.sa1.observation_segments"] = {"dates": ["2026-09-03", "2026-09-04"]}
    b.data["nem_pd7day.sa1.observations.2026-09-04"] = {"observations": [_obs("2026-09-04", 12)]}
    log = _log(b)
    rows = asyncio.run(log.async_load())
    assert len(rows) == 1
    asyncio.run(log.async_save())
    assert b.data["nem_pd7day.sa1.observation_segments"] == {"dates": ["2026-09-04"]}


def test_migration_splits_a_single_file_by_day_and_only_when_there_is_no_manifest():
    b = _Backend()
    legacy_rows = [_obs("2026-08-30", 12), _obs("2026-08-30", 13), _obs("2026-09-01", 12)]
    hits = []

    async def first():
        hits.append("first")
        return None

    async def second():
        hits.append("second")
        return legacy_rows

    log = _log(b)
    rows = asyncio.run(log.async_load(legacy_loaders=(first, second)))
    assert hits == ["first", "second"]
    assert rows == legacy_rows
    assert b.data["nem_pd7day.sa1.observation_segments"] == {"dates": ["2026-08-30", "2026-09-01"]}
    assert len(b.data["nem_pd7day.sa1.observations.2026-08-30"]["observations"]) == 2

    # With a manifest present the legacy loaders are never consulted.
    hits.clear()
    log2 = _log(b)
    asyncio.run(log2.async_load(legacy_loaders=(first, second)))
    assert hits == []
    assert len(log2.observations) == 3


def test_prune_drops_whole_oldest_days_and_removes_their_files():
    b = _Backend()
    log = _log(b)
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        for hour in range(4):
            log.append(_obs(day, hour))
    asyncio.run(log.async_save())
    dropped = log.prune(max_total=8)
    assert [segment_date(o) for o in dropped] == ["2026-09-01"] * 4
    assert log.dates == ["2026-09-02", "2026-09-03"]
    assert len(log.observations) == 8
    b.calls.clear()
    asyncio.run(log.async_save())
    assert ("remove", "nem_pd7day.sa1.observations.2026-09-01") in b.calls
    assert "nem_pd7day.sa1.observations.2026-09-01" not in b.data
    assert b.data["nem_pd7day.sa1.observation_segments"] == {"dates": ["2026-09-02", "2026-09-03"]}
    # No day was rewritten to prune.
    assert [c for c in b.calls if c[0] == "save" and "observations." in c[1]] == []


def test_prune_never_drops_the_newest_day():
    b = _Backend()
    log = _log(b)
    for hour in range(5):
        log.append(_obs("2026-09-05", hour))
    assert log.prune(max_total=2) == []
    assert len(log.observations) == 5


def test_replace_all_rebuilds_every_day_dirty():
    b = _Backend()
    log = _log(b)
    log.replace_all([_obs("2026-09-02", 12), _obs("2026-09-01", 12)])
    assert log.dates == ["2026-09-01", "2026-09-02"]
    assert [segment_date(o) for o in log.observations] == ["2026-09-01", "2026-09-02"]
    assert log.dirty_dates == {"2026-09-01", "2026-09-02"}


# ── Issue #144 (finding D): a pruned day must not be recreated by a write ────
# that outlived it. Investigated against Home Assistant's real Store
# (homeassistant/helpers/storage.py): Store.async_remove() calls
# self._async_cleanup_delay_listener(), which cancels a pending
# async_delay_save timer on *that instance*. That only protects a pruned day
# because ObservationLog hands async_remove() the identical Store object the
# delayed save was scheduled on, via the per-date cache in self._stores,
# rather than a freshly constructed one. This test pins that reuse rather
# than changing behaviour, since no bug survived the check against the real
# Store implementation.

def test_prune_removes_a_delayed_day_through_the_same_store_instance():
    b = _Backend()
    seen: dict[str, list[tuple[str, int]]] = {"delay_save": [], "remove": []}

    class _TrackedDelayStore(_DelayStore):
        def async_delay_save(self, data_func, delay) -> None:
            seen["delay_save"].append((self.key, id(self)))
            super().async_delay_save(data_func, delay)

        async def async_remove(self) -> None:
            seen["remove"].append((self.key, id(self)))
            await super().async_remove()

    log = _log(b, _TrackedDelayStore)
    log.append(_obs("2026-09-01", 12))
    asyncio.run(log.async_save())  # schedules a delayed save for 2026-09-01

    # A second day, still open, so the delayed save above is scheduled and
    # pending, not yet fired, when 2026-09-01 is pruned below.
    log.append(_obs("2026-09-02", 12))
    dropped = log.prune(max_total=1)
    assert len(dropped) == 1 and segment_date(dropped[0]) == "2026-09-01"
    asyncio.run(log.async_save())  # must remove 2026-09-01 through that same instance

    target_key = "nem_pd7day.sa1.observations.2026-09-01"
    delay_ids = [obj_id for key, obj_id in seen["delay_save"] if key == target_key]
    remove_ids = [obj_id for key, obj_id in seen["remove"] if key == target_key]
    assert len(delay_ids) == 1 and len(remove_ids) == 1
    assert delay_ids[0] == remove_ids[0], (
        "a pruned day must be removed through the exact Store instance its "
        "delayed save was scheduled on, or Home Assistant's real cancel-on-"
        "remove guarantee does not apply to it and a late-firing delayed "
        "write can recreate the file after removal"
    )
