#!/usr/bin/env python3
"""Fail when a function or class in the integration grows past the size limits.

Spec 000 part C, the size ratchet for the technical debt plan. Limits: no
function (including methods and nested functions) over 60 lines, no class over
250 lines or 15 methods. Today's offenders are listed in size_baseline.json with
their sizes; an entry may shrink or disappear, but a new offender, or a listed
one that grows, fails. With --base-baseline, the baseline itself is checked
against the base branch's copy, so it cannot be loosened to let a change in.

Usage:
    python scripts/size_check.py                      # check against the baseline
    python scripts/size_check.py --update             # tighten the baseline to today
    python scripts/size_check.py --base-baseline F    # also refuse a loosened baseline
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "custom_components" / "nem_pd7day"
BASELINE = pathlib.Path(__file__).resolve().parent / "size_baseline.json"

MAX_FUNCTION_LINES = 60
MAX_CLASS_LINES = 250
MAX_CLASS_METHODS = 15


def _span(node: ast.AST) -> int:
    return int(node.end_lineno) - int(node.lineno) + 1  # type: ignore[attr-defined]


def measure(source: str, module: str) -> dict[str, Any]:
    """Every function and class in ``source`` over a limit, keyed by qualified name."""
    functions: dict[str, int] = {}
    classes: dict[str, dict[str, int]] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                if _span(child) > MAX_FUNCTION_LINES:
                    functions[name] = _span(child)
                visit(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}{child.name}"
                methods = sum(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in child.body
                )
                if _span(child) > MAX_CLASS_LINES or methods > MAX_CLASS_METHODS:
                    classes[name] = {"lines": _span(child), "methods": methods}
                visit(child, f"{name}.")
            else:
                visit(child, prefix)

    visit(ast.parse(source), f"{module}:")
    return {"functions": functions, "classes": classes}


def measure_package(package: pathlib.Path = PACKAGE) -> dict[str, Any]:
    out: dict[str, Any] = {"functions": {}, "classes": {}}
    for path in sorted(package.glob("*.py")):
        found = measure(path.read_text(encoding="utf-8"), path.stem)
        out["functions"].update(found["functions"])
        out["classes"].update(found["classes"])
    return out


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Problems with ``current`` measured against ``baseline``; empty means pass."""
    problems: list[str] = []
    for name, lines in sorted(current["functions"].items()):
        allowed = baseline.get("functions", {}).get(name)
        if allowed is None:
            problems.append(f"new function over {MAX_FUNCTION_LINES} lines: {name} ({lines} lines)")
        elif lines > allowed:
            problems.append(f"function grew past its baseline: {name} ({allowed} -> {lines} lines)")
    for name, size in sorted(current["classes"].items()):
        allowed = baseline.get("classes", {}).get(name)
        if allowed is None:
            problems.append(
                f"new class over {MAX_CLASS_LINES} lines or {MAX_CLASS_METHODS} methods: "
                f"{name} ({size['lines']} lines, {size['methods']} methods)"
            )
            continue
        for key in ("lines", "methods"):
            if size[key] > allowed[key]:
                problems.append(f"class grew past its baseline: {name} {key} {allowed[key]} -> {size[key]}")
    return problems


def loosened(baseline: dict[str, Any], base_baseline: dict[str, Any]) -> list[str]:
    """Entries ``baseline`` adds or grows relative to the base branch's copy."""
    problems: list[str] = []
    for name, lines in baseline.get("functions", {}).items():
        before = base_baseline.get("functions", {}).get(name)
        if before is None or lines > before:
            problems.append(f"baseline loosened for function {name}: {before} -> {lines}")
    for name, size in baseline.get("classes", {}).items():
        before = base_baseline.get("classes", {}).get(name)
        if before is None or any(size[k] > before[k] for k in ("lines", "methods")):
            problems.append(f"baseline loosened for class {name}: {before} -> {size}")
    return problems


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    current = measure_package()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}

    if "--update" in argv:
        # Tighten only: today's sizes, but never above what the baseline allowed.
        problems = compare(current, baseline) if baseline else []
        if problems:
            print("size_check: refusing to update a baseline the code already exceeds:")
            print("\n".join(f"  {p}" for p in problems))
            return 1
        BASELINE.write_text(_dump(current), encoding="utf-8")
        print(f"size_check: baseline written with {len(current['functions'])} functions "
              f"and {len(current['classes'])} classes")
        return 0

    problems = compare(current, baseline)
    if "--base-baseline" in argv:
        path = pathlib.Path(argv[argv.index("--base-baseline") + 1])
        if path.exists() and path.stat().st_size:
            problems += loosened(baseline, json.loads(path.read_text(encoding="utf-8")))

    stale = sorted(
        [n for n in baseline.get("functions", {}) if n not in current["functions"]]
        + [n for n in baseline.get("classes", {}) if n not in current["classes"]]
    )
    if problems:
        print("size_check: failed")
        print("\n".join(f"  {p}" for p in problems))
        return 1
    print(f"size_check: ok ({len(current['functions'])} functions and "
          f"{len(current['classes'])} classes on the baseline)")
    if stale:
        print("size_check: now within the limits, remove from the baseline with --update: "
              + ", ".join(stale))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
