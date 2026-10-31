"""
matplotlib as the chart cameras' optional requirement: it is not in
manifest.json, where a requirement that cannot be installed (no wheel for the
Python, a failed source build) stops the whole integration loading. The
cameras ask Home Assistant's requirement installer for it once per run, in the
background, and render either way.

Run with:  python -m pytest tests/test_chart_requirements.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from support import install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const, _req = load_chain("const", "chart_requirements")
MANIFEST = Path(__file__).parents[1] / "custom_components" / "nem_pd7day" / "manifest.json"


class _Hass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.tasks: list = []

    def async_create_background_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


class RequirementsNotFound(Exception):
    pass


@pytest.fixture
def installer(monkeypatch):
    """A stand-in homeassistant.requirements recording each install request."""
    calls: list = []
    module = types.ModuleType("homeassistant.requirements")
    module.RequirementsNotFound = RequirementsNotFound
    outcome = {"fail": False}

    async def async_process_requirements(hass, name, requirements, is_built_in=True):
        calls.append((name, list(requirements), is_built_in))
        await asyncio.sleep(0)
        if outcome["fail"]:
            raise RequirementsNotFound(f"Requirements for {name} not found: {requirements}.")

    module.async_process_requirements = async_process_requirements
    monkeypatch.setitem(sys.modules, "homeassistant.requirements", module)
    return calls, outcome


def test_matplotlib_is_not_a_manifest_requirement():
    requirements = json.loads(MANIFEST.read_text())["requirements"]
    assert not any(r.lower().startswith("matplotlib") for r in requirements)
    assert _req.CHART_REQUIREMENTS == ["matplotlib>=3.7.0"]


def test_every_camera_shares_one_install_attempt(installer):
    calls, _outcome = installer

    async def scenario():
        hass = _Hass()
        results = await asyncio.gather(*(_req.async_chart_requirements(hass) for _ in range(15)))
        again = await _req.async_chart_requirements(hass)
        return results, again

    results, again = run_async(scenario())
    assert results == [True] * 15 and again is True
    assert calls == [(_const.DOMAIN, ["matplotlib>=3.7.0"], False)]


def test_a_failed_install_warns_once_and_renders_go_on(installer, caplog):
    calls, outcome = installer
    outcome["fail"] = True

    async def scenario():
        hass = _Hass()
        return [await _req.async_chart_requirements(hass) for _ in range(3)]

    assert run_async(scenario()) == [False, False, False]
    assert len(calls) == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING" and "matplotlib" in r.getMessage()]
    assert len(warnings) == 1 and "placeholder" in warnings[0].getMessage()


def test_no_installer_at_all_is_a_failed_install_not_an_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "homeassistant.requirements", None)  # import raises ImportError

    async def scenario():
        return await _req.async_chart_requirements(_Hass())

    assert run_async(scenario()) is False


def test_a_cancelled_render_does_not_cancel_the_install(installer):
    calls, _outcome = installer

    async def scenario():
        hass = _Hass()
        waiter = asyncio.get_running_loop().create_task(_req.async_chart_requirements(hass))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        return await _req.async_chart_requirements(hass)

    assert run_async(scenario()) is True and len(calls) == 1
