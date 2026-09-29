"""Every fitted value, stored field and log line is unchanged, bit for bit (spec 006, invariant 1).

``scripts/record_fit_io.py`` generates fixed observation sets (a 100-day QLD1
history with every row-level special case, the other regions and an unknown
one, thin and degenerate buckets, a bucket over ``MAX_OBS`` and four crafted
stage 2 buckets) and fits each one with ``CalibrationEngine.fit``, with and
without an explicit ``now``, and ``fit_ols_stage2``, on a frozen wall clock.
Per case it records ``to_storage`` of each result, the isotonic breakpoints
as ``float.hex`` and every log record of the fit.

The fixture was written by that script on the base commit of spec 006, before
any code moved. Here the same recording is taken from the code on disk and
compared exactly, and every branch the fit can reach must be reached so none
is pinned vacuously.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

import support

SCRIPT = pathlib.Path(support.ROOT) / "scripts" / "record_fit_io.py"

# CPython 3.12 made the built-in sum() of floats compensated, and _ols,
# _ols_metrics and _compute_run_features sum floats with it. The fixture is
# recorded on 3.13, the version CI runs; see test_golden_master.py.
pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="fixture recorded on CPython 3.13; sum() of floats differs before 3.12",
)


def _script():
    spec = importlib.util.spec_from_file_location("_record_fit_io", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recorder = _script()
EXPECTED = recorder.load_fixture()


@pytest.fixture(scope="module")
def recording() -> dict:
    # Through a JSON round trip, as the fixture was written.
    return json.loads(recorder._dumps(recorder.record()))


def test_the_inputs_are_the_recorded_ones(recording):
    for key in ("obs_fields", "stpasa_fields", "instant", "now"):
        assert recording[key] == EXPECTED[key]
    assert recording["inputs"] == EXPECTED["inputs"]


@pytest.mark.parametrize("case", sorted(EXPECTED["outputs"]))
def test_every_output_is_identical(recording, case):
    got, want = recording["outputs"][case], EXPECTED["outputs"][case]
    for part in ("fit_now", "fit_clock"):
        for field in want[part]:
            assert got[part][field] == want[part][field], f"{case}: {part}.{field} changed"
    assert got["branches"] == want["branches"]


@pytest.mark.parametrize("branch", recorder.REQUIRED_BRANCHES)
def test_every_branch_is_reached(branch):
    assert recorder.branch_counts(EXPECTED)[branch] > 0
