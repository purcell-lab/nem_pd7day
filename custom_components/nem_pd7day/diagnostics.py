"""Diagnostics support for NEM PD7DAY."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    COORDINATOR_KEY,
    DOMAIN,
    NEMWEB_SEMAPHORE_KEY,
    STORE_KEY,
    get_region,
)


def _integration_version() -> str | None:
    """Read the integration version from manifest.json."""
    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    return manifest.get("version")


def _calibration_summary(store: Any) -> dict[str, Any] | None:
    """Return the calibration summary attributes if available."""
    if store is None:
        return None
    summary_fn = getattr(store, "summary_attributes", None)
    if not callable(summary_fn):
        return None
    try:
        return summary_fn()
    except Exception:  # noqa: BLE001
        return None


def _stpasa_run_datetime(stpasa_store: Any) -> str | None:
    """Return the latest STPASA run_datetime if available."""
    if stpasa_store is None:
        return None
    latest_fn = getattr(stpasa_store, "latest", None)
    if not callable(latest_fn):
        return None
    latest = latest_fn()
    if latest is None:
        return None
    return getattr(latest, "run_datetime", None)


def _pd7day_run_datetime(coordinator: Any, region: str) -> str | None:
    """Return the latest PD7DAY forecast_generated_at for the region if available."""
    if coordinator is None:
        return None
    result = getattr(coordinator, "data", None)
    if result is None:
        return None
    prices = getattr(result, "prices", None)
    if not prices:
        return None
    price_data = prices.get(region)
    if price_data is None:
        return None
    return getattr(price_data, "forecast_generated_at", None)


def _nemweb_gate(hass: HomeAssistant) -> dict[str, Any] | None:
    """Counters from the shared NEMWEB request gate, or None if absent.

    These are what let a future 403 investigation tell "NEMWEB throttled us"
    apart from "we throttled ourselves", which the bare semaphore this gate
    replaced could never express. See issue #22.
    """
    gate = hass.data.get(DOMAIN, {}).get(NEMWEB_SEMAPHORE_KEY)
    diagnostics = getattr(gate, "diagnostics", None)
    if not callable(diagnostics):
        return None
    return diagnostics()


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    region = get_region(entry)

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get(COORDINATOR_KEY)
    store = entry_data.get(STORE_KEY)
    stpasa_store = entry_data.get("stpasa_store")

    # All values here are derived from AEMO's public forecast data — there are
    # no credentials or personal data in the config entry, so nothing to redact.
    return {
        "entry_data": dict(entry.data),
        "region": region,
        "calibration_summary": _calibration_summary(store),
        "stpasa_run_datetime": _stpasa_run_datetime(stpasa_store),
        "pd7day_run_datetime": _pd7day_run_datetime(coordinator, region),
        "nemweb_gate": _nemweb_gate(hass),
        "integration_version": _integration_version(),
    }
