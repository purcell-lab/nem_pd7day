"""
No em dash or en dash in user-facing strings.json or translation files.

PR #95 took the two characters out of every chart label and added a guard
over the chart modules. That guard scans modules that import matplotlib, so
it never looked at strings.json, whose config-flow "unknown" error carried an
em dash (issue #97). This closes the gap for the JSON side: every string
value in strings.json and under translations/ is checked after JSON decoding,
so both literal characters and \\u escapes are caught.
"""
from __future__ import annotations

import json
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "custom_components", "nem_pd7day")

_DASHES = ("\u2014", "\u2013")


def _json_files() -> list[str]:
    files = [os.path.join(_PKG, "strings.json")]
    tdir = os.path.join(_PKG, "translations")
    if os.path.isdir(tdir):
        files.extend(
            os.path.join(tdir, name) for name in sorted(os.listdir(tdir))
            if name.endswith(".json")
        )
    return files


def _string_values(obj, path=""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _string_values(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _string_values(value, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


@pytest.mark.parametrize("path", _json_files(), ids=os.path.basename)
def test_no_dash_in_user_facing_json_strings(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    offenders = [
        (key, value) for key, value in _string_values(data)
        if any(d in value for d in _DASHES)
    ]
    assert not offenders, f"{os.path.basename(path)}: {offenders}"


def test_strings_json_is_covered():
    assert any(p.endswith("strings.json") for p in _json_files())
