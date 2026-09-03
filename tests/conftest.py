"""
Pytest configuration.

Provides:
  * a parent-package bootstrap so individual test files can be run standalone
  * autouse fixtures that prevent test ordering contamination
"""
import importlib
import importlib.machinery
import os
import sys
import types

import pytest

# ── Parent package bootstrap ──────────────────────────────────────────────────
# Test modules load integration submodules directly from file via
# importlib (`_load(...)`) to avoid pulling in the HA-dependent
# `custom_components/nem_pd7day/__init__.py`.  That shim registers e.g.
# "custom_components.nem_pd7day.nem_time" in sys.modules *before* executing it,
# but never registers the parent package.  So when nem_time.py runs its first
# relative import (`from .const import ...`), Python resolves the parent
# package for real, which executes __init__.py -> imports stpasa_client ->
# `from .nem_time import parse_nem_csv` -> finds the still-empty partially
# initialised module -> ImportError.
#
#     ImportError: cannot import name 'parse_nem_csv' from
#     'custom_components.nem_pd7day.nem_time'
#
# In a full-suite run this is masked, because an earlier test module happens to
# import the package first. Standalone runs of a single file fail outright,
# which breaks `pytest tests/test_calibration_engine.py` and any -k / --lf
# workflow.
#
# Registering lightweight stub packages that carry only __path__ lets relative
# imports resolve through normal machinery without ever executing the real
# __init__.py.  `setdefault` semantics mean this never clobbers an already
# imported package, and test modules that genuinely need the real __init__
# (test_lifecycle, test_sensor, ...) still load it explicitly afterwards and
# overwrite the stub.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _register_stub_package(name: str, path: str) -> None:
    """Register a package object exposing only __path__, without running its __init__."""
    if name in sys.modules:
        return
    pkg = types.ModuleType(name)
    pkg.__path__ = [path]
    pkg.__package__ = name
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = pkg.__path__
    pkg.__spec__ = spec
    sys.modules[name] = pkg
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, pkg)


_register_stub_package("custom_components", os.path.join(_ROOT, "custom_components"))
_register_stub_package(
    "custom_components.nem_pd7day",
    os.path.join(_ROOT, "custom_components", "nem_pd7day"),
)


@pytest.fixture(autouse=True)
def restore_sensor_module_globals():
    """
    Restore any module-level names in sensor.py that tests may have patched.

    Some tests patch `custom_components.nem_pd7day.sensor._amber_express_cutoff`
    using `unittest.mock.patch`. If a test fails mid-context-manager the mock can
    leak into subsequent tests.  This fixture captures the original reference
    before each test and restores it afterwards.

    The import is best-effort. sensor.py needs a fully-formed `homeassistant`
    package, but several test modules install partial MagicMock stubs for HA at
    import time.  When such a module is run standalone the import raises
    ModuleNotFoundError ("No module named 'homeassistant.components'"), which
    previously errored every test in the file even though none of them touch
    sensor.py.  There is nothing to protect in that case, so degrade quietly
    rather than failing collection.
    """
    try:
        import custom_components.nem_pd7day.sensor as sensor_mod
    except Exception:
        yield
        return

    sentinel = object()
    original = getattr(sensor_mod, "_amber_express_cutoff", sentinel)
    yield
    if original is not sentinel:
        sensor_mod._amber_express_cutoff = original


@pytest.fixture(autouse=True)
def real_clock_behind_stubbed_dt_util():
    """
    Give every stubbed ``dt_util`` a real ``utcnow`` so time arithmetic works.

    The hass-aware modules read the clock through ``homeassistant.util.dt``
    (issue #109) so a test can freeze it. Most test modules stub the whole
    ``homeassistant`` package with MagicMock, and a MagicMock ``utcnow()``
    returns a MagicMock that breaks datetime arithmetic and comparisons. This
    fixture points ``utcnow`` (and ``parse_time``) at real implementations
    unless a test has already configured them. To freeze time in a test, use
    ``patch.object(module.dt_util, "utcnow", return_value=instant)``, which
    replaces the attribute and so wins over this default.
    """
    from datetime import datetime, time as _time, timezone
    from unittest.mock import MagicMock

    def _utcnow():
        return datetime.now(timezone.utc)

    def _parse_time(value):
        try:
            return _time.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    for name, mod in list(sys.modules.items()):
        if not name.startswith("custom_components.nem_pd7day"):
            continue
        du = getattr(mod, "dt_util", None)
        if not isinstance(du, MagicMock):
            continue
        for attr, impl in (("utcnow", _utcnow), ("parse_time", _parse_time)):
            target = getattr(du, attr)
            if not isinstance(target, MagicMock):
                continue
            if target.side_effect is None and isinstance(target.return_value, MagicMock):
                target.side_effect = impl
    yield
