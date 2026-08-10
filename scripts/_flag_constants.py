"""Flag watched module-level constants whose bodies changed in a commit range.

Matching a constant name against the +/- lines of a diff does not work: a
multi-line constant such as DEFAULT_ENABLED_TARIFFS carries its name on the
declaration line, while the edit lands 20-plus lines further down inside the
literal. Widening diff context only moves the cliff edge.

So instead: resolve the line span of each top-level assignment in the current
file, collect the changed line numbers from a zero-context diff, and report a
constant when any changed line falls inside its span.

Used by scripts/release-notes.sh. Exits 0 always; prints one line per hit.
"""

from __future__ import annotations

import re
import subprocess
import sys

WATCHED = [
    "DEFAULT_ENABLED_TARIFFS",
    "DISTRIBUTOR_TARIFFS",
    "TARIFF_NAMES",
    "DEFAULT_SCAN_INTERVAL",
    "OBSERVATION_WINDOW_DAYS",
    "MIN_OBS",
    "OLS_MIN_OBS",
    "HORIZON_EDGES",
    "HORIZON_LABELS",
    "SPIKE_THRESHOLD",
]

HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")
ASSIGN = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*(?::[^=]+)?=")


def changed_lines(rng: str, path: str) -> set[int]:
    """Line numbers touched in the new revision of *path*."""
    out = subprocess.run(
        ["git", "diff", "-U0", rng, "--", path],
        capture_output=True, text=True, check=False,
    ).stdout
    touched: set[int] = set()
    for line in out.splitlines():
        m = HUNK.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2) or 1)
            touched.update(range(start, start + count))
    return touched


def spans(path: str) -> dict[str, tuple[int, int]]:
    """Map each top-level constant to its (first, last) line span."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return {}

    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines, start=1):
        m = ASSIGN.match(line)
        if m:
            starts.append((i, m.group(1)))

    result: dict[str, tuple[int, int]] = {}
    for idx, (start, name) in enumerate(starts):
        # Extend to the line before the next top-level statement, so the whole
        # literal body is covered regardless of how long it is.
        end = len(lines)
        for j in range(start, len(lines)):
            nxt = lines[j]
            if nxt.strip() and not nxt[0].isspace() and not nxt.startswith(")"):
                if j + 1 != start:
                    end = j
                    break
        result[name] = (start, end)
    return result


def main() -> int:
    rng = sys.argv[1]
    paths = subprocess.run(
        ["git", "diff", "--name-only", rng, "--", "custom_components/"],
        capture_output=True, text=True, check=False,
    ).stdout.split()

    for path in paths:
        if not path.endswith(".py"):
            continue
        touched = changed_lines(rng, path)
        if not touched:
            continue
        for name, (start, end) in spans(path).items():
            if name in WATCHED and any(start <= n <= end for n in touched):
                print(f"  !  {name} changed in {path} "
                      f"-- may alter existing installs. Document it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
