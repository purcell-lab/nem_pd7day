#!/usr/bin/env python3
"""Fail a refactor pull request that changes a golden-master snapshot.

Spec 000 part C. A pull request whose title starts ``refactor:`` claims to
change no published value, and the golden master is how that claim is
checked; regenerating a snapshot would make the check pass by definition. So
for those pull requests any change under tests/golden/snapshots/ fails. Other
pull requests may change snapshots, and must say which values move and why.

Usage (CI passes the title through the environment, never inline):
    PR_TITLE="..." python scripts/check_golden_untouched.py --base origin/main
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOTS = "tests/golden/snapshots/"


def is_refactor(title: str) -> bool:
    return title.strip().lower().startswith("refactor:")


def violations(title: str, changed: list[str]) -> list[str]:
    """Snapshot files a refactor changed; empty when the title is not a refactor."""
    if not is_refactor(title):
        return []
    return sorted(path for path in changed if path.startswith(SNAPSHOTS))


def changed_files(base: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def main(argv: list[str]) -> int:
    title = os.environ.get("PR_TITLE", "")
    base = argv[argv.index("--base") + 1] if "--base" in argv else "origin/main"
    if not is_refactor(title):
        print(f"check_golden_untouched: not a refactor pull request ({title!r}); snapshots may change")
        return 0
    bad = violations(title, changed_files(base))
    if bad:
        print("check_golden_untouched: FAILED, a refactor pull request changed golden snapshots:")
        print("\n".join(f"  {path}" for path in bad))
        print("A refactor must not move a published value. If a value is meant to move, "
              "this is not a refactor: retitle the pull request and say which values move and why.")
        return 1
    print("check_golden_untouched: ok, no snapshot changed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
