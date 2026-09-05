"""
Daily segmented observation log for the calibration store (issue #130).

The observation log used to be one JSON file per region, rewritten in full
every time an actual price settled. At MAX_TOTAL_OBS it is about 50 MB, and
a settled interval arrives every half hour, so that was gigabytes a day of
writes across five regions to add a few rows.

Observations are now kept in one file per NEM calendar day of the interval
they describe, under ``nem_pd7day.<region>.observations.<YYYY-MM-DD>``, with
a small manifest listing the days that exist. A settled interval touches one
day's file, a few hundred kilobytes, and pruning removes whole old days
instead of rewriting everything that is kept. The in-memory view is still a
single flat list, oldest day first, so every consumer of
``CalibrationStore.observations`` is unchanged.

The class is Home-Assistant-agnostic: it takes a ``store_factory`` mapping a
storage key to an object with ``async_load`` / ``async_save`` and optionally
``async_delay_save`` / ``async_remove`` (the HA ``Store`` API). The default
factory builds real HA stores. Persistence of a dirty segment goes through
``async_delay_save`` when the store class offers it, so a burst of writes
inside OBS_SAVE_DELAY_S becomes one; a double without it is saved at once.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Iterable

from .const import (
    OBS_SAVE_DELAY_S,
    STORAGE_VERSION,
    observation_manifest_key,
    observation_segment_key,
)

_LOGGER = logging.getLogger(__name__)

# Observations that carry no usable interval_time land here rather than being
# dropped; the store has never validated the field and the fit ignores rows
# it cannot bucket.
UNDATED_SEGMENT = "undated"

StoreFactory = Callable[[str], Any]
LegacyLoader = Callable[[], Awaitable[list[dict] | None]]


def segment_date(obs: dict) -> str:
    """The NEM calendar day an observation belongs to, from its interval time."""
    value = obs.get("interval_time")
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    return UNDATED_SEGMENT


def _default_store_factory(hass: Any) -> StoreFactory:
    from homeassistant.helpers.storage import Store

    def build(key: str) -> Any:
        return Store(hass, STORAGE_VERSION, key)

    return build


class ObservationLog:
    """Observations split by day, presented as one flat list."""

    def __init__(
        self,
        hass: Any,
        region: str,
        *,
        store_factory: StoreFactory | None = None,
    ) -> None:
        self._region = region
        self._factory = store_factory or _default_store_factory(hass)
        self._manifest = self._factory(observation_manifest_key(region))
        self._segments: dict[str, list[dict]] = {}
        self._stores: dict[str, Any] = {}
        self._dirty: set[str] = set()
        self._manifest_dirty = False
        self._observations: list[dict] = []
        self._removed_dates: list[str] = []

    # ── Read side ────────────────────────────────────────────────────────────

    @property
    def observations(self) -> list[dict]:
        """The live flat list, oldest day first. Callers must not mutate it."""
        return self._observations

    @property
    def dates(self) -> list[str]:
        return sorted(self._segments)

    @property
    def dirty_dates(self) -> set[str]:
        return set(self._dirty)

    def _store_for(self, date: str) -> Any:
        store = self._stores.get(date)
        if store is None:
            store = self._factory(observation_segment_key(self._region, date))
            self._stores[date] = store
        return store

    def _rebuild_flat(self) -> None:
        flat: list[dict] = []
        for date in sorted(self._segments):
            flat.extend(self._segments[date])
        self._observations = flat

    # ── Load and migrate ─────────────────────────────────────────────────────

    async def async_load(self, legacy_loaders: Iterable[LegacyLoader] = ()) -> list[dict]:
        """Load every segment named in the manifest, or migrate an older format.

        ``legacy_loaders`` are tried in order only when no manifest exists;
        the first that returns observations is split into segments, saved,
        and the flat list returned. The legacy file is left to the caller to
        remove once the split has been persisted.
        """
        manifest = await self._manifest.async_load()
        dates = list((manifest or {}).get("dates", [])) if isinstance(manifest, dict) else []
        if dates:
            loaded = await asyncio.gather(
                *(self._store_for(date).async_load() for date in dates)
            )
            missing: list[str] = []
            for date, data in zip(dates, loaded):
                rows = (data or {}).get("observations") if isinstance(data, dict) else None
                if rows:
                    self._segments[date] = list(rows)
                else:
                    missing.append(date)
            if missing:
                _LOGGER.warning(
                    "Observation log %s: %d segment(s) named in the manifest are "
                    "missing or empty and were dropped: %s",
                    self._region, len(missing), ", ".join(missing),
                )
                self._manifest_dirty = True
            self._rebuild_flat()
            return self._observations

        for load_legacy in legacy_loaders:
            rows = await load_legacy()
            if rows:
                _LOGGER.info(
                    "Observation log %s: splitting %d observations from the single "
                    "file store into daily segments (issue #130)",
                    self._region, len(rows),
                )
                self.replace_all(rows)
                await self.async_save(immediate=True)
                return self._observations
        return self._observations

    # ── Write side ───────────────────────────────────────────────────────────

    def append(self, obs: dict) -> None:
        date = segment_date(obs)
        if date not in self._segments:
            self._segments[date] = []
            self._manifest_dirty = True
        self._segments[date].append(obs)
        self._dirty.add(date)
        # Appending to the newest day keeps the flat list ordered; a row for
        # an older day (a late-settling interval) needs a rebuild.
        if self._observations and segment_date(self._observations[-1]) > date:
            self._rebuild_flat()
        else:
            self._observations.append(obs)

    def touch(self, obs: dict) -> None:
        """Mark the segment holding ``obs`` dirty after an in-place update."""
        self._dirty.add(segment_date(obs))

    def replace_all(self, rows: Iterable[dict]) -> None:
        """Replace every observation, rebuilding the segments; all dirty."""
        self._segments = {}
        for obs in rows:
            self._segments.setdefault(segment_date(obs), []).append(obs)
        self._dirty = set(self._segments)
        self._manifest_dirty = True
        self._rebuild_flat()

    def prune(self, max_total: int) -> list[dict]:
        """Drop whole oldest days until at most ``max_total`` rows remain.

        Returns the dropped observations so the caller can retire whatever
        it indexes them by. The newest day is never dropped, so a cap below
        one day's worth of rows degrades to keeping that day.
        """
        dropped: list[dict] = []
        total = len(self._observations)
        for date in sorted(self._segments):
            if total <= max_total or len(self._segments) <= 1:
                break
            rows = self._segments.pop(date)
            self._dirty.discard(date)
            self._removed_dates.append(date)
            dropped.extend(rows)
            total -= len(rows)
        if dropped:
            self._manifest_dirty = True
            self._rebuild_flat()
        return dropped

    async def async_save(self, *, immediate: bool = False) -> None:
        """Persist dirty segments and the manifest, remove pruned segments."""
        for date in sorted(self._dirty):
            rows = self._segments.get(date)
            if rows is None:
                continue
            store = self._store_for(date)
            if not immediate and hasattr(type(store), "async_delay_save"):
                store.async_delay_save(
                    lambda rows=rows: {"observations": rows}, OBS_SAVE_DELAY_S
                )
            else:
                await store.async_save({"observations": rows})
        self._dirty.clear()

        removed, self._removed_dates = self._removed_dates, []
        for date in removed:
            store = self._stores.pop(date, None) or self._factory(
                observation_segment_key(self._region, date)
            )
            remove = getattr(type(store), "async_remove", None)
            if remove is not None:
                await store.async_remove()

        if self._manifest_dirty:
            await self._manifest.async_save({"dates": self.dates})
            self._manifest_dirty = False
