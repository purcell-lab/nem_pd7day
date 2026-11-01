"""Every integration module sits in an import layer (#340).

import-linter only checks the modules a contract names, so a new module left
out of .importlinter would escape both contracts unnoticed. This fails first.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "custom_components" / "nem_pd7day"


def _layered() -> set[str]:
    config = (ROOT / ".importlinter").read_text(encoding="utf-8")
    return set(re.findall(r"custom_components\.nem_pd7day\.(\w+)", config))


def test_every_module_is_in_a_layer():
    modules = {p.stem for p in PACKAGE.glob("*.py") if p.stem != "__init__"}
    missing = sorted(modules - _layered())
    assert not missing, f"add these to a layer in .importlinter: {missing}"


def test_nothing_layered_has_gone():
    modules = {p.stem for p in PACKAGE.glob("*.py")}
    stale = sorted(_layered() - modules)
    assert not stale, f"remove these from .importlinter: {stale}"
