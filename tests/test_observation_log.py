"""
ObservationLog: one file per NEM day, presented as one flat list (issue #130).

Run with:  python -m pytest tests/test_observation_log.py -v
"""
from __future__ import annotations

import asyncio

from custom_components.nem_pd7day.observation_log import (
    UNDATED_SEGMENT,
    ObservationLog,
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
