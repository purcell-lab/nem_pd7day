"""Check that an integration module imports without Home Assistant.

Used by the contract tests of the modules spec 004 split out of
calibration_store. The module is imported in a fresh interpreter in which any
``homeassistant`` import raises, with the package registered as a bare stub
(as conftest.py does) so the HA-dependent package ``__init__`` never runs.
The same run reports whether the adapters the spec forbids were pulled in.
"""
from __future__ import annotations

import subprocess
import sys

import support

_PROBE = r"""
import importlib
import sys


class _BlockHomeAssistant:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "homeassistant":
            raise ImportError(f"blocked import of {name}")
        return None


sys.meta_path.insert(0, _BlockHomeAssistant())
sys.path.insert(0, sys.argv[1])
import support

support._ensure_packages()
importlib.import_module(f"{support.PKG}.{sys.argv[2]}")
forbidden = ("calibration_store", "coordinator", "sensor")
print(",".join(sorted(
    name for name in sys.modules
    if name.split(".")[0] == "homeassistant"
    or (name.startswith(support.PKG + ".") and name.rsplit(".", 1)[-1] in forbidden)
)))
"""


def assert_imports_without_home_assistant(module: str) -> None:
    """``module`` (a name inside the package) imports with Home Assistant absent,
    and without calibration_store, coordinator or sensor."""
    tests_dir = f"{support.ROOT}/tests"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, tests_dir, module],
        cwd=support.ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"{module} pulled in: {proc.stdout.strip()}"
