#!/usr/bin/env python3
"""Fail when a source line the base branch's tests executed is no longer executed.

Spec 000 part C, the coverage gate for the technical debt plan. Takes two
coverage.py JSON reports, one run on the base branch and one on the head, and
maps every executed base line to its head line through a diff of the two
versions of the file, following blocks of code the change moved (#368). A line
that still exists in the head but is no longer executed is lost coverage and
fails the check. Lines the change deleted or
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
import re
import subprocess
import sys
import tempfile
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent


# A matched run shorter than this is a common line or two (`return None`, `)`,
# `else:`), which the diff can pair with an identical line anywhere in the file
# (#368). It is trusted only where a long run beside it puts it.
MIN_RUN = 3


def line_map(base_lines: list[str], head_lines: list[str]) -> dict[int, int]:
    """1-based base line number -> head line number, for lines the change kept.

    A line is kept when the diff leaves it unchanged in place, or when it sits
    in a block of at least MIN_RUN lines that the change moved elsewhere in the
    file. A short unchanged run counts only at the same offset as the nearest
    long run before or after it: elsewhere it is a coincidental match of a
    common line, and comparing its coverage would compare the wrong line.
    """
    runs = _unchanged_runs(base_lines, head_lines)
    long_offsets = [(i1, j1 - i1) for i1, i2, j1 in runs if i2 - i1 >= MIN_RUN]
    mapping: dict[int, int] = {}
    for i1, i2, j1 in runs:
        if i2 - i1 >= MIN_RUN or _in_step(i1, j1 - i1, long_offsets):
            for k in range(i2 - i1):
                mapping[i1 + k + 1] = j1 + k + 1
    _map_moved_blocks(base_lines, head_lines, mapping)
    return mapping


def _unchanged_runs(base_lines: list[str], head_lines: list[str]) -> list[tuple[int, int, int]]:
    """Runs of unchanged lines as (base start, base end, head start), 0-based, end exclusive.

    Taken from git's diff with the indent heuristic. Where a deleted block
    begins and ends with the same line, the diff may cut it at either copy;
    difflib cut #362's moved method one line early, which paired its
    `return None` with the next function's (#368). Git places the cut at the
    block's natural edge, a blank line or a dedent. difflib stands in where git
    cannot run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        base_file, head_file = pathlib.Path(tmp, "base"), pathlib.Path(tmp, "head")
        base_file.write_text("".join(f"{line}\n" for line in base_lines), encoding="utf-8")
        head_file.write_text("".join(f"{line}\n" for line in head_lines), encoding="utf-8")
        try:
            proc = subprocess.run(
                ["git", "diff", "--no-index", "--no-color", "--no-ext-diff", "--histogram",
                 "--indent-heuristic", "-U0", str(base_file), str(head_file)],
                capture_output=True, text=True,
            )
        except OSError:
            proc = None
    if proc is None or proc.returncode not in (0, 1):
        matcher = difflib.SequenceMatcher(None, base_lines, head_lines, autojunk=False)
        return [(i1, i2, j1) for tag, i1, i2, j1, _j2 in matcher.get_opcodes() if tag == "equal"]
    removed: set[int] = set()
    added: set[int] = set()
    for hunk in _HUNK.finditer(proc.stdout):
        old_start, old_count = int(hunk[1]), int(hunk[2] or 1)
        new_start, new_count = int(hunk[3]), int(hunk[4] or 1)
        removed.update(range(old_start - 1, old_start - 1 + old_count))
        added.update(range(new_start - 1, new_start - 1 + new_count))
    kept_base = [i for i in range(len(base_lines)) if i not in removed]
    kept_head = [j for j in range(len(head_lines)) if j not in added]
    runs: list[tuple[int, int, int]] = []
    for i, j in zip(kept_base, kept_head):
        if runs and runs[-1][1] == i and runs[-1][2] + (i - runs[-1][0]) == j:
            runs[-1] = (runs[-1][0], i + 1, runs[-1][2])
        else:
            runs.append((i, i + 1, j))
    return runs


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def _in_step(i1: int, offset: int, long_offsets: list[tuple[int, int]]) -> bool:
    """True when a short run keeps the offset of the nearest long run on either side.

    With no long run in the file at all there is nothing to judge by, and the
    run is trusted as the diff gives it.
    """
    if not long_offsets:
        return True
    before = [off for start, off in long_offsets if start < i1]
    after = [off for start, off in long_offsets if start > i1]
    return (bool(before) and before[-1] == offset) or (bool(after) and after[0] == offset)


def _map_moved_blocks(base_lines: list[str], head_lines: list[str], mapping: dict[int, int]) -> None:
    """Map unmapped base lines that reappear, as a block of MIN_RUN or more, among unmapped head lines."""
    claimed = set(mapping.values())
    line = 1
    while line <= len(base_lines):
        if line in mapping:
            line += 1
            continue
        start = line
        while line <= len(base_lines) and line not in mapping:
            line += 1
        segment = base_lines[start - 1:line - 1]
        if len(segment) < MIN_RUN:
            continue
        free = [j for j in range(1, len(head_lines) + 1) if j not in claimed]
        free_text = [head_lines[j - 1] for j in free]
        matcher = difflib.SequenceMatcher(None, segment, free_text, autojunk=False)
        for block in matcher.get_matching_blocks():
            for a, b, size in _contiguous(block.a, block.b, block.size, free):
                if size < MIN_RUN:
                    continue
                for k in range(size):
                    mapping[start + a + k] = free[b + k]
                    claimed.add(free[b + k])


def _contiguous(a: int, b: int, size: int, free: list[int]) -> list[tuple[int, int, int]]:
    """Split a matching block where the free head lines it covers are not consecutive in the file."""
    parts: list[tuple[int, int, int]] = []
    begin = 0
    for k in range(1, size + 1):
        if k == size or free[b + k] != free[b + k - 1] + 1:
            parts.append((a + begin, b + begin, k - begin))
            begin = k
    return parts


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
