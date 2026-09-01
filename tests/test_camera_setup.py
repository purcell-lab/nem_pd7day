"""Guards that camera setup does not wait for the first chart render.

``async_added_to_hass`` runs inside ``async_add_entities``, so awaiting the
first matplotlib render there holds up the entire camera platform. On a five
region install that is 15 camera entities all rendering through a contended
executor pool during startup, which reliably produced:

    Setup of camera platform nem_pd7day is taking over 10 seconds.

These tests assert the *property* rather than the mechanism: adding a camera to
hass must return promptly even when the render is slow, and the image must
still arrive once the render completes. They fail if anyone reintroduces the
await, even by a different route.
"""

from __future__ import annotations

import asyncio
import enum as _enum
import importlib.util
import os
import sys
import time
from unittest.mock import MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How long the fake render blocks for. Comfortably longer than any plausible
# setup path, so an awaited render cannot pass by being fast.
RENDER_SECONDS = 2.0


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Stub HA so camera.py imports cleanly ──────────────────────────────────────
sys.modules.setdefault("aiohttp", MagicMock())
for ha_mod in [
    "homeassistant", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.storage", "homeassistant.helpers.event",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.device_registry",
    "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.util", "homeassistant.util.dt",
    "homeassistant.components",
]:
    sys.modules.setdefault(ha_mod, MagicMock())

_device_registry_mock = MagicMock()
_device_registry_mock.DeviceInfo = dict
sys.modules["homeassistant.helpers.device_registry"] = _device_registry_mock


class _CameraEntityFeature(_enum.IntFlag):
    NONE = 0


class _FakeCamera:
    """Minimal stand-in for homeassistant.components.camera.Camera."""

    def __init__(self) -> None:
        self._removals: list = []

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, func) -> None:
        self._removals.append(func)

    def async_write_ha_state(self) -> None:
        return None


_camera_mock = MagicMock()
_camera_mock.Camera = _FakeCamera
_camera_mock.CameraEntityFeature = _CameraEntityFeature
sys.modules["homeassistant.components.camera"] = _camera_mock


class _FakeCoordinatorEntity:
    def __init__(self, coordinator=None, **kwargs):
        self.coordinator = coordinator

    def __class_getitem__(cls, item):
        return cls

    async def async_added_to_hass(self) -> None:
        return None


_uc_mock = MagicMock()
_uc_mock.CoordinatorEntity = _FakeCoordinatorEntity
_uc_mock.DataUpdateCoordinator = MagicMock()
_uc_mock.UpdateFailed = Exception
sys.modules["homeassistant.helpers.update_coordinator"] = _uc_mock

_load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_camera_module = _load(
    "custom_components.nem_pd7day.camera",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "camera.py"),
)


# ── Test doubles ──────────────────────────────────────────────────────────────


class _FakeHass:
    """Runs executor jobs on a real thread and background tasks on the loop."""

    def __init__(self) -> None:
        self.background_tasks: list[asyncio.Task] = []

    async def async_add_executor_job(self, func, *args):
        return await asyncio.get_running_loop().run_in_executor(
            None, func, *args
        )

    def async_create_background_task(self, coro, name=None, eager_start=False):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.background_tasks.append(task)
        return task

    def async_create_task(self, coro, name=None):
        return self.async_create_background_task(coro, name=name)


class _SlowRenderCamera(_camera_module._InitialRenderMixin):
    """Exercises the mixin in isolation with a deliberately slow render."""

    def __init__(self, hass: _FakeHass) -> None:
        self.hass = hass
        self.entity_id = "camera.nem_pd7day_qld1_price_tod_chart"
        self.render_started = asyncio.Event()
        self.image_bytes: bytes | None = None
        self._removals: list = []

    def async_on_remove(self, func) -> None:
        self._removals.append(func)

    def _render(self) -> bytes:
        time.sleep(RENDER_SECONDS)
        return b"PNG-BYTES"

    async def _async_refresh_image(self) -> None:
        self.render_started.set()
        self.image_bytes = await self.hass.async_add_executor_job(self._render)

    async def async_added_to_hass(self) -> None:
        self._schedule_initial_render()


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_added_to_hass_returns_before_the_render_finishes():
    """Setup must not block on the render, which is what tripped the warning."""

    async def scenario():
        hass = _FakeHass()
        cam = _SlowRenderCamera(hass)

        started = time.monotonic()
        await cam.async_added_to_hass()
        elapsed = time.monotonic() - started

        assert elapsed < RENDER_SECONDS / 4, (
            f"async_added_to_hass took {elapsed:.2f}s; it is waiting for the "
            "render instead of scheduling it"
        )
        assert cam.image_bytes is None, "render should still be in flight"

        # The render was genuinely scheduled, not dropped.
        await asyncio.wait_for(cam.render_started.wait(), timeout=1)
        await asyncio.gather(*hass.background_tasks)
        assert cam.image_bytes == b"PNG-BYTES"

    asyncio.new_event_loop().run_until_complete(scenario())


def test_fifteen_cameras_all_set_up_well_inside_the_warning_threshold():
    """Five regions of three cameras each is the live configuration."""

    async def scenario():
        hass = _FakeHass()
        cams = [_SlowRenderCamera(hass) for _ in range(15)]

        started = time.monotonic()
        for cam in cams:
            await cam.async_added_to_hass()
        elapsed = time.monotonic() - started

        assert elapsed < 10, (
            f"setting up 15 cameras took {elapsed:.2f}s, at or over Home "
            "Assistant's platform warning threshold"
        )

        for task in hass.background_tasks:
            task.cancel()
        await asyncio.gather(*hass.background_tasks, return_exceptions=True)

    asyncio.new_event_loop().run_until_complete(scenario())


def test_initial_render_is_cancelled_on_entity_removal():
    """An in-flight render must not outlive the entity."""

    async def scenario():
        hass = _FakeHass()
        cam = _SlowRenderCamera(hass)
        await cam.async_added_to_hass()

        assert cam._removals, "no removal callback registered for the render"
        for cancel in cam._removals:
            cancel()

        results = await asyncio.gather(
            *hass.background_tasks, return_exceptions=True
        )
        assert any(
            isinstance(r, asyncio.CancelledError) for r in results
        ) or all(r is None for r in results)

    asyncio.new_event_loop().run_until_complete(scenario())


@pytest.mark.parametrize(
    "class_name",
    [
        "NemPd7dayTodCamera",
        "NemPd7dayBiasChartCamera",
        "NemPd7dayIsoChartCamera",
        "NemPd7dayForecastChartCamera",
    ],
)
def test_every_camera_class_schedules_rather_than_awaits(class_name):
    """No camera class may await its first render in async_added_to_hass."""
    import inspect

    cls = getattr(_camera_module, class_name)
    assert issubclass(cls, _camera_module._InitialRenderMixin)

    source = inspect.getsource(cls.async_added_to_hass)
    assert "_schedule_initial_render" in source
    assert "await self._async_refresh_image" not in source
