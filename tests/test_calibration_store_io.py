"""Stored bytes of CalibrationStore are unchanged (spec 004, invariant 2).

``scripts/record_calibration_store_io.py`` drives the real store through a
fixed sequence: a load from legacy keys, two ingests and a duplicate, actuals
covering first and repeat readings, a region mismatch, out-of-window horizons
and pruning, a refit with stage 2 and one with stage 2 failing, a restart and
a corrupt coefficient payload. It records every Store ``async_save`` payload
(as ``json.dumps``, insertion order kept) and ``async_remove`` in call order,
the package's log records and the fit generation after each step.

The fixture was written by that script on the base commit of spec 004, before
any code moved. Here the same recording is taken from the code on disk and
compared exactly. The log comparison is on level and message only: a log call
may move to another module of the package, which changes its logger name.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

import support

SCRIPT = pathlib.Path(support.ROOT) / "scripts" / "record_calibration_store_io.py"


def _script():
    spec = importlib.util.spec_from_file_location("_record_calibration_store_io", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recorder = _script()
EXPECTED = json.loads(recorder.FIXTURE.read_text(encoding="utf-8"))
STEP_NAMES = [step["step"] for step in EXPECTED["steps"]]


@pytest.fixture(scope="module")
def recording() -> dict:
    # Through a JSON round trip, as the fixture was written.
    return json.loads(json.dumps(recorder.record()))


def _steps(recording: dict) -> dict[str, dict]:
    return {step["step"]: step for step in recording["steps"]}


def test_the_sequence_is_the_recorded_one(recording):
    assert [step["step"] for step in recording["steps"]] == STEP_NAMES
    assert recording["max_total_obs"] == EXPECTED["max_total_obs"]


@pytest.mark.parametrize("name", STEP_NAMES)
def test_storage_calls_are_byte_identical(recording, name):
    expected = _steps(EXPECTED)[name]["calls"]
    actual = _steps(recording).get(name, {}).get("calls")
    assert [(c["op"], c["key"]) for c in actual] == [(c["op"], c["key"]) for c in expected]
    for got, want in zip(actual, expected):
        assert got["json"] == want["json"], f"{want['op']} of {want['key']} changed"


@pytest.mark.parametrize("name", STEP_NAMES)
def test_log_lines_keep_level_text_and_count(recording, name):
    expected = [(r["level"], r["message"]) for r in _steps(EXPECTED)[name]["logs"]]
    actual = [(r["level"], r["message"]) for r in _steps(recording)[name]["logs"]]
    assert actual == expected
    assert all(r["logger"].startswith(recorder.PKG) for r in _steps(recording)[name]["logs"])


@pytest.mark.parametrize("name", STEP_NAMES)
def test_fit_generation_and_result(recording, name):
    expected, actual = _steps(EXPECTED)[name], _steps(recording)[name]
    assert actual["fit_generation"] == expected["fit_generation"]
    assert actual["result"] == expected["result"]


def test_the_fixture_covers_what_invariant_2_lists():
    """Guard against a fixture that silently stopped exercising a path."""
    steps = _steps(EXPECTED)
    calls = [c for step in EXPECTED["steps"] for c in step["calls"]]
    assert {c["op"] for c in calls} == {"save", "remove"}
    assert any(c["key"] == "nem_pd7day.observation_log" and c["op"] == "remove" for c in calls)
    load = [c["key"] for c in steps["load_legacy"]["calls"]]
    assert "nem_pd7day.qld1.calibration_coefficients" in load
    assert "nem_pd7day.qld1.forecast_history" in load
    assert [steps[n]["fit_generation"] for n in (
        "load_legacy", "refit_with_stage2", "refit_stage2_fails",
        "restart_load_scoped", "load_corrupt_coefficients",
    )] == [1, 3, 4, 1, 0]
    assert any(c["op"] == "remove" for c in steps["actual_first_prunes_oldest_day"]["calls"])
    for name in ("actual_region_mismatch", "actual_negative_horizon", "actual_beyond_horizon",
                 "actual_no_history"):
        assert steps[name]["result"] == 0 and steps[name]["calls"] == []
    levels = {r["level"] for step in EXPECTED["steps"] for r in step["logs"]}
    assert levels == {"DEBUG", "INFO", "WARNING"}
