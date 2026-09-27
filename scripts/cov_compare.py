#!/usr/bin/env python3
"""Fail when a source line the base branch's tests executed is no longer executed.

Spec 000 part C, the coverage gate for the technical debt plan. Takes two
coverage.py JSON reports, one run on the base branch and one on the head, and
maps every executed base line to its head line through a diff of the two
versions of the file. A line that still exists in the head but is no longer
executed is lost coverage and fails the check. Lines the change deleted or
edited are reported, not failed: removing code is not losing coverage.

A line that is executed only sometimes (a branch that depends on the wall
clock, the order tests run in, or a race) shows up here as lost coverage on an
unrelated change. That is a defect in the tests, to be fixed with a
deterministic test for the branch, not by rerunning.

Usage:
    python scripts/cov_compare.py BASE.json HEAD.json --base-ref origin/main
"""
from __future__ import annotations

import difflib
import json
import pathlib
import subprocess
import sys
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent


def line_map(base_lines: list[str], head_lines: list[str]) -> dict[int, int]:
    """1-based base line number -> head line number, for lines the diff keeps unchanged."""
    mapping: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(None, base_lines, head_lines, autojunk=False)
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[i1 + k + 1] = j1 + k + 1
    return mapping


def compare(
    base: dict, head: dict,
    read_base: Callable[[str], list[str] | None],
    read_head: Callable[[str], list[str] | None],
) -> tuple[dict[str, list[int]], dict[str, int], int, int]:
    """(lost lines per file, edited-or-deleted executed lines per file, base total, head total)."""
    lost: dict[str, list[int]] = {}
    edited: dict[str, int] = {}
    base_files = base.get("files", {})
    head_files = head.get("files", {})
    for path, record in sorted(base_files.items()):
        executed = set(record.get("executed_lines", []))
        head_executed = set(head_files.get(path, {}).get("executed_lines", []))
        base_src = read_base(path)
        head_src = read_head(path)
        if head_src is None:  # the file was deleted: nothing of it can be lost
            edited[path] = len(executed)
            continue
        mapping = line_map(base_src or [], head_src)
        gone = sorted(line for line in executed if line in mapping and mapping[line] not in head_executed)
        changed = sum(1 for line in executed if line not in mapping)
        if gone:
            lost[path] = gone
        if changed:
            edited[path] = changed
    base_total = sum(len(r.get("executed_lines", [])) for r in base_files.values())
    head_total = sum(len(r.get("executed_lines", [])) for r in head_files.values())
    return lost, edited, base_total, head_total


def _git_lines(ref: str) -> Callable[[str], list[str] | None]:
    def read(path: str) -> list[str] | None:
        proc = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True, text=True)
        return proc.stdout.splitlines() if proc.returncode == 0 else None
    return read


def _worktree_lines(path: str) -> list[str] | None:
    file = ROOT / path
    return file.read_text(encoding="utf-8").splitlines() if file.exists() else None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    base = json.loads(pathlib.Path(argv[0]).read_text(encoding="utf-8"))
    head = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
    base_ref = argv[argv.index("--base-ref") + 1] if "--base-ref" in argv else "origin/main"
    lost, edited, base_total, head_total = compare(base, head, _git_lines(base_ref), _worktree_lines)

    print(f"cov_compare: executed lines {base_total} on {base_ref}, {head_total} on the head")
    for path, count in sorted(edited.items()):
        print(f"  {path}: {count} executed line(s) edited or deleted by this change (not a loss)")
    if lost:
        print("cov_compare: FAILED, lines the base branch's tests executed are no longer executed:")
        for path, lines in sorted(lost.items()):
            shown = ", ".join(map(str, lines[:30])) + (" ..." if len(lines) > 30 else "")
            print(f"  {path}: {len(lines)} line(s), at these lines of the base version: {shown}")
        return 1
    print("cov_compare: ok, no executed line lost")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
