#!/usr/bin/env python3
"""Benchmark entity updates per dispatch poll, for the forecast timeline plan (#333, #334).

Builds a golden scenario through tests/golden/harness.py, then simulates one
dispatch poll that brings a new dispatch interval but no new P5MIN or
pre-dispatch run, as most polls do. Each coordinator notifies its listeners
the way Home Assistant does: always, or with ``always_update=False`` only
when its data changed. The golden coordinator itself always notifies, so the
rule is applied here.

Reports, per entity class:

- state writes per poll;
- milliseconds per ``extra_state_attributes`` build, warm, median of N.

Run from the repository root:

    python scripts/bench_updates.py                 # nsw_local_prices
    python scripts/bench_updates.py --scenario qld_fitted_evening_peak --repeat 50
"""
from __future__ import annotations

import argparse
import collections
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from golden import harness  # noqa: E402

COORDINATORS = ("_shared_constraints", "_shared_p5min", "_shared_predispatch")


def _poll(built, writes: collections.Counter) -> None:
    """One dispatch poll: the dispatch coordinator, then each shared coordinator's refresh with
    nothing new for P5MIN and pre-dispatch. A listener fires per Home Assistant's rule."""
    data = built.hass.data["nem_pd7day"]
    # The harness counts every entity's state writes in _golden_writes, however
    # the write was reached (a listener may hold the bound method from setup).
    before = {id(e): e.__dict__.get("_golden_writes", 0) for e in built.entities}
    dispatch = built.entry.runtime_data.dispatch
    dispatch.async_update_listeners()
    for key in COORDINATORS:
        coord = data.get(key)
        if coord is None:
            continue
        new_data = key == "_shared_constraints"  # a new interval; no new P5MIN or pre-dispatch run
        if new_data or getattr(coord, "always_update", True):
            coord.async_update_listeners()
    for entity in built.entities:
        writes[type(entity).__name__] += entity.__dict__.get("_golden_writes", 0) - before[id(entity)]


def _attr_ms(entities, repeat: int) -> dict[str, float]:
    by_class: dict[str, list[float]] = collections.defaultdict(list)
    for entity in entities:
        if not hasattr(type(entity), "extra_state_attributes"):
            continue
        entity.extra_state_attributes  # warm
        samples = []
        for _ in range(repeat):
            start = time.perf_counter()
            entity.extra_state_attributes
            samples.append((time.perf_counter() - start) * 1000)
        by_class[type(entity).__name__].append(statistics.median(samples))
    return {name: max(values) for name, values in by_class.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", default="nsw_local_prices")
    parser.add_argument("--repeat", type=int, default=20)
    args = parser.parse_args()
    with harness.build_entities(args.scenario) as built:
        counts = collections.Counter(type(e).__name__ for e in built.entities)
        writes: collections.Counter = collections.Counter()
        _poll(built, writes)
        ms = _attr_ms(built.entities, args.repeat)
    print(f"scenario {args.scenario}: {len(built.entities)} entities")
    print(f"{'entity class':48} {'n':>3} {'writes/poll':>12} {'per entity':>11} {'attrs ms (worst)':>17}")
    for name in sorted(counts, key=lambda n: (-writes[n], n)):
        per = writes[name] / counts[name]
        print(f"{name:48} {counts[name]:>3} {writes[name]:>12} {per:>11.1f} {ms.get(name, 0.0):>17.2f}")
    print(f"{'total':48} {sum(counts.values()):>3} {sum(writes.values()):>12}")


if __name__ == "__main__":
    main()
