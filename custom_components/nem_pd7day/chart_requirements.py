"""matplotlib, the chart cameras' optional requirement.

matplotlib is not in manifest.json: where it cannot be installed (a Python
with no wheel for it, such as 3.14 on some installs, where pip falls back to
a source build that fails), a manifest requirement stops the whole
integration loading, sensors and all. Instead the cameras ask Home
Assistant's own requirement installer for it, once per run and in the
background, before they render: where it installs, or is installed already,
the charts render as before; where it does not, one warning is logged and the
cameras serve the placeholder image every chart module already falls back to.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CHART_REQUIREMENTS = ["matplotlib>=3.7.0"]
_TASK_KEY = "chart_requirements"


async def _install(hass: Any) -> bool:
    """Never raises: a failed install must not stop a render, which falls back to the placeholder."""
    try:
        from homeassistant.requirements import async_process_requirements

        await async_process_requirements(hass, DOMAIN, CHART_REQUIREMENTS, is_built_in=False)
    except Exception as err:  # noqa: BLE001 - RequirementsNotFound, or pip itself failing
        _LOGGER.warning(
            "The chart cameras need matplotlib, which could not be installed (%s). "
            "They show a placeholder image; every sensor works without it.", err,
        )
        return False
    importlib.invalidate_caches()  # a package installed by this run is importable now
    return True


async def async_chart_requirements(hass: Any) -> bool:
    """True once matplotlib is installed; every camera shares one attempt per run."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    task = domain_data.get(_TASK_KEY)
    if task is None:
        task = hass.async_create_background_task(_install(hass), "nem_pd7day chart requirements")
        domain_data[_TASK_KEY] = task
    # Shielded: a render cancelled with its entity does not cancel the install.
    return await asyncio.shield(task)
