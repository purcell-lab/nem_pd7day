#!/usr/bin/env python3
"""Fail when the integration's version is not the same everywhere it appears.

Three places carry the version and nothing kept them in step: manifest.json
(what Home Assistant and HACS read), the newest row of the README version
table (what users read), and the release tag (what the zip is published
under). A release cut from a manifest that still says the previous version
installs as that previous version.

Usage:
    python scripts/version_lockstep.py              # manifest vs README
    python scripts/version_lockstep.py --tag v3.5.0 # also vs a release tag
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "nem_pd7day" / "manifest.json"
README = ROOT / "README.md"

_ROW = re.compile(r"^\|\s*(\d+\.\d+\.\d+)\s*\|")


def manifest_version(text: str) -> str:
    return str(json.loads(text)["version"])


def readme_newest_version(text: str) -> str:
    """The version in the first data row after the '| Version | Changes |' header."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\|\s*Version\s*\|\s*Changes\s*\|", line):
            for candidate in lines[i + 1:]:
                m = _ROW.match(candidate)
                if m:
                    return m.group(1)
            raise ValueError("README version table has a header but no rows")
    raise ValueError("README has no '| Version | Changes |' table")


def tag_version(tag: str) -> str:
    """'v3.5.0' or '3.5.0' -> '3.5.0'; anything else is an error."""
    m = re.fullmatch(r"v?(\d+\.\d+\.\d+)", tag.strip())
    if not m:
        raise ValueError(f"release tag {tag!r} is not vX.Y.Z")
    return m.group(1)


def check(manifest_text: str, readme_text: str, tag: str | None = None) -> list[str]:
    """Return the mismatches, empty when everything is in step."""
    problems: list[str] = []
    manifest = manifest_version(manifest_text)
    readme = readme_newest_version(readme_text)
    if readme != manifest:
        problems.append(
            f"README newest version row is {readme} but manifest.json says {manifest}"
        )
    if tag is not None:
        tagged = tag_version(tag)
        if tagged != manifest:
            problems.append(f"release tag is {tag} but manifest.json says {manifest}")
    return problems


def main(argv: list[str]) -> int:
    tag = None
    if "--tag" in argv:
        tag = argv[argv.index("--tag") + 1]
    problems = check(MANIFEST.read_text(), README.read_text(), tag)
    for problem in problems:
        print(f"version_lockstep: {problem}")
    if not problems:
        print(f"version_lockstep: {manifest_version(MANIFEST.read_text())} everywhere")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
