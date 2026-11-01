"""The forecast hub: P5MIN and pre-dispatch series worked out once per run, for every sensor (#333, #336).

Before this, every DUID, node and zone sensor rebuilt its P5MIN and
pre-dispatch series from the raw run on every state write: after each dispatch
poll and again for each new run, and the same DUID's series once in its DUID
sensor, again in a node's and again in each zone it belongs to. #331 kept one
zone's pre-dispatch series per sensor; the hub keeps every series once for all
of them.

How a series is kept. Each series function (``forecast_series``,
``node_forecast``, ``zone_p5_forecast``, ``predispatch_adjustments`` and the
pre-dispatch attributes built from them) depends on the clock only through two
things: which of the run's interval or period ends is the first still ahead,
and whether the run is past its age limit. So its result is kept under

    (the run's identity, the call's arguments, the first end ahead, stale)

and is the same object for every sensor and every write until one of those
moves. The function is the one the sensors called before, so a kept value is
exactly what that call returned: no published value changes. For P5MIN the
first end ahead is taken over ``interval_ends``, every region's ends together,
so the key moves at least as often as any one region's series does.

A new run for a kind (``identity`` from ``price_sources``) clears that kind's
entries. A zone in a key is held by the entry, so a zone rebuilt by an options
reload is a new key and its old entries go with the next run.

Services layer: no Home Assistant imports.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Hashable, Optional, Sequence, TypeVar

T = TypeVar("T")

# Above this many entries for one run, the kind's entries are dropped and built
# again: a guard, far above what one run's sensors need (a few hundred).
MAX_ENTRIES = 20_000


@dataclass
class HubStats:
    computed: int = 0
    reused: int = 0


def _identity(run: Any) -> Hashable:
    """The run's identity (``price_sources``), and the object itself by id.

    One run is one object, so the id costs no reuse; it guards against a run
    rebuilt with the same file and run time but other rows (``replace`` in a
    test). The memo holds the run, so its id is not reused while it is kept.
    """
    return (getattr(run, "identity", None), id(run), getattr(run, "run_at", None))


def _p5_ends(run: Any) -> Sequence[datetime]:
    ends = getattr(run, "interval_ends", None)
    if ends is not None:
        return ends
    return sorted({end for series in getattr(run, "rop", {}).values() for end in series})


class ForecastHub:
    """One memo per source kind, keyed on the run's identity."""

    def __init__(self) -> None:
        self._memo: dict[str, tuple[Hashable, dict[Hashable, Any], Any]] = {}
        self._ends: dict[Hashable, tuple[datetime, ...]] = {}
        self.stats = HubStats()

    def _entries(self, kind: str, run: Any) -> dict[Hashable, Any]:
        identity = _identity(run)
        held = self._memo.get(kind)
        if held is None or held[0] != identity:
            held = (identity, {}, run)
            self._memo[kind] = held
            live = {i for i, _entries, _run in self._memo.values()}
            self._ends = {k: v for k, v in self._ends.items() if k in live}
        return held[1]

    def _sorted_ends(self, identity: Hashable, ends: Callable[[], Sequence[datetime]]) -> tuple[datetime, ...]:
        held = self._ends.get(identity)
        if held is None:
            held = self._ends[identity] = tuple(sorted(ends()))
        return held

    def _when(self, run: Any, ends: Callable[[], Sequence[datetime]], now: datetime, max_age: timedelta) -> tuple:
        """The clock as the series functions see it: the first end ahead, and whether the run is stale."""
        held = self._sorted_ends(_identity(run), ends)
        i = bisect_right(held, now)
        return (held[i] if i < len(held) else None, now - run.run_at > max_age)

    def _get(self, kind: str, run: Any, key: Hashable, compute: Callable[[], T]) -> T:
        entries = self._entries(kind, run)
        if key in entries:
            self.stats.reused += 1
            return entries[key][1]
        if len(entries) >= MAX_ENTRIES:
            entries.clear()
        value = compute()
        self.stats.computed += 1
        # The key's arguments are kept with the value, so an object in the key
        # (a zone) stays alive, and its id is not reused, while the entry exists.
        entries[key] = (key, value)
        return value

    # ── P5MIN ───────────────────────────────────────────────────────────────

    def p5(self, kind: str, run: Optional[Any], args: Hashable, now: datetime, max_age: timedelta,
           compute: Callable[[], T]) -> T:
        """A P5MIN series for ``args``; worked out without a run, as without one it costs nothing."""
        if run is None:
            return compute()
        key = (args, self._when(run, lambda: _p5_ends(run), now, max_age))
        return self._get("p5:" + kind, run, key, compute)

    # ── Pre-dispatch ────────────────────────────────────────────────────────

    def pd(self, kind: str, snapshot: Optional[Any], args: Hashable, now: datetime, max_age: timedelta,
           compute: Callable[[], T]) -> T:
        """A pre-dispatch series or attribute set for ``args``."""
        if snapshot is None:
            return compute()
        key = (args, self._when(snapshot, lambda: snapshot.period_ends, now, max_age))
        return self._get("pd:" + kind, snapshot, key, compute)


def shared_forecast_hub(domain_data: dict) -> ForecastHub:
    """The one hub, in ``hass.data[DOMAIN]``. No await between check and set."""
    hub = domain_data.get("_forecast_hub")
    if hub is None:
        hub = domain_data["_forecast_hub"] = ForecastHub()
    return hub
