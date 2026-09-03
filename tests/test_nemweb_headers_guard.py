"""
Every NEMWEB request carries NEMWEB_HEADERS from const.py.

NEMWEB sits behind Akamai bot management that intermittently answers 403 to
requests with automated-looking User-Agents. const.py defines one browser-like
User-Agent for the whole integration; issue #102 found two clients still
sending a hardcoded "nem_pd7day/2.3" and the DispatchIS fallback sending no
User-Agent at all. This test keeps that from coming back.

Style follows tests/test_chart_text_no_dashes.py: an invariant over the
package source rather than over any one call site.
"""
from __future__ import annotations

import os
import re

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "custom_components", "nem_pd7day")

_MODULES = sorted(
    name for name in os.listdir(_PKG)
    if name.endswith(".py") and name != "const.py"
)

# A literal User-Agent key in a headers dict or a UA-looking value.
_HARDCODED_UA = re.compile(r"""["']User-Agent["']\s*:|nem_pd7day/\d""")


@pytest.mark.parametrize("module_name", _MODULES)
def test_no_hardcoded_user_agent_outside_const(module_name):
    with open(os.path.join(_PKG, module_name), encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert not _HARDCODED_UA.search(line), (
                f"{module_name}:{lineno} carries a hardcoded User-Agent; "
                f"use NEMWEB_HEADERS from const.py"
            )


def test_urllib_requests_in_dispatch_client_carry_headers():
    """The DispatchIS fallback uses urllib, so it cannot inherit headers from
    a session: every urlopen there must take a Request built with
    NEMWEB_HEADERS rather than a bare URL string."""
    with open(os.path.join(_PKG, "dispatch_client.py"), encoding="utf-8") as handle:
        src = handle.read()
    bare = re.findall(r"urlopen\(\s*(?:DISPATCHIS_BASE|NEM_SUMMARY_URL|url)\b", src)
    assert not bare, f"bare urlopen(url) calls in dispatch_client.py: {bare}"
    assert src.count("headers=NEMWEB_HEADERS") == 2
    assert "{**NEMWEB_HEADERS" in src
