"""scripts/version_lockstep.py keeps manifest, README and release tag in step."""
from __future__ import annotations

import importlib.util
import json
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "version_lockstep", os.path.join(_ROOT, "scripts", "version_lockstep.py")
)
lockstep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lockstep)

README = """# Title

| Version | Changes |
|---|---|
| 3.5.0 | Newest. |
| 3.4.0 | Older. |
"""


def _manifest(version: str) -> str:
    return json.dumps({"domain": "nem_pd7day", "version": version})


def test_in_step_reports_nothing():
    assert lockstep.check(_manifest("3.5.0"), README) == []
    assert lockstep.check(_manifest("3.5.0"), README, tag="v3.5.0") == []
    assert lockstep.check(_manifest("3.5.0"), README, tag="3.5.0") == []


def test_readme_behind_manifest_is_reported():
    problems = lockstep.check(_manifest("3.6.0"), README)
    assert len(problems) == 1 and "3.5.0" in problems[0] and "3.6.0" in problems[0]


def test_tag_behind_manifest_is_reported():
    problems = lockstep.check(_manifest("3.5.0"), README, tag="v3.4.0")
    assert len(problems) == 1 and "v3.4.0" in problems[0]


def test_malformed_tag_raises():
    with pytest.raises(ValueError):
        lockstep.check(_manifest("3.5.0"), README, tag="release-candidate")


def test_readme_without_table_raises():
    with pytest.raises(ValueError):
        lockstep.check(_manifest("3.5.0"), "# nothing here\n")


def test_repository_is_in_step():
    """The real files, so a version bump that misses one of them fails here
    before it fails in CI."""
    assert lockstep.check(
        lockstep.MANIFEST.read_text(), lockstep.README.read_text()
    ) == []
