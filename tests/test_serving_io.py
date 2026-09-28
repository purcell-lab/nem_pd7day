"""Every serving output is unchanged, bit for bit (spec 005, invariant 1).

``scripts/record_serving_io.py`` builds four ``CalibrationResult``s from
hand-built models (full, no residual quantiles, no isotonic model, stage 2 for
some buckets only), and no calibration at all, and runs a fixed grid of
inputs through ``CalibrationResult.apply`` and
``CalibrationStore.apply_to_price``. Per input it records ``json.dumps`` of
each output (insertion order kept), whether the returned object is the dict
``BucketModel.apply_all`` returned, and the branch that produced it.

The fixture was written by that script on the base commit of spec 005, before
any code moved. Here the same recording is taken from the code on disk and
compared exactly, and every branch must be reached at least 100 times so none
is pinned vacuously.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

import support

SCRIPT = pathlib.Path(support.ROOT) / "scripts" / "record_serving_io.py"
MIN_PER_BRANCH = 100


def _script():
    spec = importlib.util.spec_from_file_location("_record_serving_io", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recorder = _script()
EXPECTED = recorder.load_fixture()
BRANCHES = (
    list(recorder.STAGE1_LABELS.values())
    + list(recorder.SERVING_LABELS)
    + list(recorder.SPIKE_LABELS)
)


@pytest.fixture(scope="module")
def recording() -> dict:
    # Through a JSON round trip, as the fixture was written.
    return json.loads(recorder._dumps(recorder.record()))


def test_the_grid_is_the_recorded_one(recording):
    for key in ("fields", "scenarios", "stpasa_run_at"):
        assert recording[key] == EXPECTED[key]
    fields = EXPECTED["fields"]
    inputs = fields[: fields.index("network_tight") + 1]
    assert [row[: len(inputs)] for row in recording["inputs"]] == [
        row[: len(inputs)] for row in EXPECTED["inputs"]
    ]
    assert len(EXPECTED["inputs"]) >= 20_000


def test_every_output_is_identical(recording):
    fields = EXPECTED["fields"]
    differ = [
        (i, dict(zip(fields, got)), want)
        for i, (got, want) in enumerate(zip(recording["inputs"], EXPECTED["inputs"]))
        if got != want
    ]
    assert not differ, (
        f"{len(differ)} of {len(EXPECTED['inputs'])} inputs changed; first: "
        f"{differ[0][1]} expected {dict(zip(fields, differ[0][2]))}"
    )


@pytest.mark.parametrize("branch", BRANCHES)
def test_every_branch_is_reached(branch):
    assert recorder.branch_counts(EXPECTED).get(branch, 0) >= MIN_PER_BRANCH


def test_refusals_return_the_stage1_dict_and_stage2_a_new_one():
    """Invariant 4, as recorded: identity follows the serving outcome."""
    fields = EXPECTED["fields"]
    serving, same = fields.index("serving"), fields.index("apply_is_stage1")
    for row in EXPECTED["inputs"]:
        if row[serving] == "store_passthrough":
            assert row[same] is None
        else:
            assert row[same] is row[serving].startswith("refuse_")
