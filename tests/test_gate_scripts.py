"""The refactor gates of spec 000 part C each fail on a deliberate violation.

Run with:  python -m pytest tests/test_gate_scripts.py -v
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

import support

SCRIPTS = pathlib.Path(support.ROOT) / "scripts"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"_gate_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


size = _script("size_check")
cov = _script("cov_compare")
golden = _script("check_golden_untouched")


# ── Size ratchet ──────────────────────────────────────────────────────────────

def _body(lines: int) -> str:
    return "\n".join(f"    x{i} = {i}" for i in range(lines - 1))


def test_size_check_measures_functions_methods_nested_and_classes():
    inner_body = "\n".join(f"        y{i} = {i}" for i in range(61))  # inner: def line + 61 = 62 lines
    source = (
        f"def small():\n{_body(10)}\n\n"
        f"def big():\n{_body(61)}\n\n"
        "class Many:\n" + "".join(f"    def m{i}(self):\n        return {i}\n" for i in range(16))
        + f"\ndef outer():\n    def inner():\n{inner_body}\n    return inner\n"
    )
    found = size.measure(source, "mod")
    assert set(found["functions"]) == {"mod:big", "mod:outer", "mod:outer.inner"}
    assert found["functions"]["mod:big"] == 61
    assert found["classes"] == {"mod:Many": {"lines": 33, "methods": 16}}


def test_size_check_fails_a_new_offender_and_a_grown_one_and_passes_a_shrunk_one():
    baseline = {"functions": {"m:old": 100}, "classes": {"m:Big": {"lines": 300, "methods": 20}}}
    assert size.compare({"functions": {"m:old": 90}, "classes": {"m:Big": {"lines": 280, "methods": 20}}}, baseline) == []
    problems = size.compare(
        {"functions": {"m:old": 101, "m:new": 61}, "classes": {"m:Big": {"lines": 300, "methods": 21}}},
        baseline,
    )
    assert any("new function" in p and "m:new" in p for p in problems)
    assert any("grew" in p and "m:old" in p for p in problems)
    assert any("grew" in p and "methods" in p for p in problems)


def test_size_baseline_cannot_be_loosened_against_the_base_branch():
    base = {"functions": {"m:a": 100}, "classes": {}}
    assert size.loosened({"functions": {"m:a": 90}, "classes": {}}, base) == []
    assert size.loosened({"functions": {"m:a": 101}, "classes": {}}, base)
    assert size.loosened({"functions": {"m:a": 100, "m:b": 70}, "classes": {}}, base)


def test_the_committed_baseline_matches_the_code():
    current = size.measure_package()
    baseline = json.loads(size.BASELINE.read_text(encoding="utf-8"))
    assert size.compare(current, baseline) == []


# ── Coverage comparison ───────────────────────────────────────────────────────

def _report(**files):
    return {"files": {path: {"executed_lines": lines} for path, lines in files.items()}}


def test_cov_compare_maps_lines_through_an_edit_that_shifts_them():
    base_src = ["a", "b", "c", "d"]
    head_src = ["new 1", "new 2", "a", "b", "c", "d"]  # everything moved down two lines
    lost, edited, _, _ = cov.compare(
        _report(**{"m.py": [1, 2, 3, 4]}), _report(**{"m.py": [3, 4, 5, 6]}),
        lambda p: base_src, lambda p: head_src,
    )
    assert lost == {} and edited == {}


def test_cov_compare_fails_a_line_that_still_exists_but_is_no_longer_executed():
    src = ["a", "b", "c"]
    lost, _, _, _ = cov.compare(
        _report(**{"m.py": [1, 2, 3]}), _report(**{"m.py": [1, 3]}),
        lambda p: src, lambda p: src,
    )
    assert lost == {"m.py": [2]}


def test_cov_compare_reports_deleted_code_without_failing():
    lost, edited, _, _ = cov.compare(
        _report(**{"m.py": [1, 2, 3], "gone.py": [1]}), _report(**{"m.py": [1, 2]}),
        lambda p: ["a", "b", "c"], lambda p: None if p == "gone.py" else ["a", "b"],
    )
    assert lost == {}
    assert edited == {"m.py": 1, "gone.py": 1}


# ── Golden snapshots untouched by a refactor ──────────────────────────────────

@pytest.mark.parametrize("title, changed, expected", [
    ("refactor: extract TariffPricer", ["custom_components/x.py", "tests/golden/snapshots/a.json.gz"],
     ["tests/golden/snapshots/a.json.gz"]),
    ("Refactor: extract TariffPricer", ["tests/golden/snapshots/a.json.gz"], ["tests/golden/snapshots/a.json.gz"]),
    ("refactor: extract TariffPricer", ["custom_components/x.py", "tests/test_x.py"], []),
    ("Fix #182", ["tests/golden/snapshots/a.json.gz"], []),
], ids=["refactor_touching", "case_insensitive", "refactor_clean", "not_a_refactor"])
def test_golden_untouched_fails_only_a_refactor_that_changes_a_snapshot(title, changed, expected):
    assert golden.violations(title, changed) == expected


def test_scripts_directory_is_where_the_gates_live():
    assert pathlib.Path(size.__file__).parent == SCRIPTS
