"""
Tests for diagnostics.py: async_get_config_entry_diagnostics().

The download carries the entry data, region, calibration summary, the STPASA
and PD7DAY run times and the integration version. The version comes from the
loader's cached manifest: the coroutine used to call manifest_path.read_text()
on the event loop and Home Assistant flagged it as a blocking call from a
custom integration.

Run with:  python -m pytest tests/test_diagnostics.py -v
"""
from __future__ import annotations

import builtins
import contextlib
import pathlib
import types
from unittest.mock import MagicMock, patch

import pytest

from support import install_ha_stubs, load_chain, run_async

install_ha_stubs()

_const_mod, _diag_mod = load_chain("const", "diagnostics")

CONF_REGION = _const_mod.CONF_REGION
COORDINATOR_KEY = _const_mod.COORDINATOR_KEY
DOMAIN = _const_mod.DOMAIN
STORE_KEY = _const_mod.STORE_KEY
async_get_config_entry_diagnostics = _diag_mod.async_get_config_entry_diagnostics

MANIFEST_VERSION = "9.9.9-test"


@contextlib.contextmanager
def loader_returning(version: str = MANIFEST_VERSION):
    """Patch the module's async_get_integration; yields the list of domains it was asked for."""
    calls: list[str] = []

    async def _fake_async_get_integration(hass, domain):
        calls.append(domain)
        return types.SimpleNamespace(manifest={"version": version, "domain": DOMAIN})

    with patch.object(_diag_mod, "async_get_integration", _fake_async_get_integration):
        yield calls


def make_hass_and_entry(region: str = "NSW1"):
    """Build a mock hass + config entry wired with coordinator, store and STPASA store."""
    entry = MagicMock()
    entry.entry_id = "entry_abc"
    entry.data = {CONF_REGION: region}
    entry.options = {CONF_REGION: region}

    store = MagicMock()
    store.summary_attributes.return_value = {
        "status": "active",
        "fitted_at": "2026-06-01T08:00:00+10:00",
        "observation_count": 42,
        "active_buckets": 3,
    }

    stpasa_latest = MagicMock()
    stpasa_latest.run_datetime = "2026-06-12T13:30:00+10:00"
    stpasa_store = MagicMock()
    stpasa_store.latest.return_value = stpasa_latest

    price_data = MagicMock()
    price_data.forecast_generated_at = "2026-06-12T13:00:00+10:00"
    result = MagicMock()
    result.prices = {region: price_data}
    coordinator = MagicMock()
    coordinator.data = result

    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_KEY: coordinator,
                STORE_KEY: store,
                "stpasa_store": stpasa_store,
            }
        }
    }
    return hass, entry


@pytest.mark.parametrize("region", ["NSW1", "VIC1"])
def test_diagnostics_payload(region):
    """All expected top-level keys, with the wired values and the entry's region."""
    hass, entry = make_hass_and_entry(region)
    with loader_returning():
        result = run_async(async_get_config_entry_diagnostics(hass, entry))

    assert isinstance(result, dict)
    assert {
        "entry_data", "region", "calibration_summary",
        "stpasa_run_datetime", "pd7day_run_datetime", "integration_version",
    } <= set(result)
    assert result["region"] == region
    assert result["entry_data"][CONF_REGION] == region
    assert result["calibration_summary"]["observation_count"] == 42
    assert result["stpasa_run_datetime"] == "2026-06-12T13:30:00+10:00"
    assert result["pd7day_run_datetime"] == "2026-06-12T13:00:00+10:00"
    assert result["integration_version"] == MANIFEST_VERSION


def test_integration_version_uses_loader_not_disk():
    """The loader is consulted once and no manifest is read from disk on the event loop."""
    hass, entry = make_hass_and_entry()
    real_open = open
    opened: list[str] = []

    def _tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def _forbidden_read_text(self, *args, **kwargs):
        raise AssertionError(f"diagnostics read {self} from disk on the event loop")

    with loader_returning() as calls, \
            patch.object(builtins, "open", _tracking_open), \
            patch.object(pathlib.Path, "read_text", _forbidden_read_text):
        result = run_async(async_get_config_entry_diagnostics(hass, entry))

    assert result["integration_version"] == MANIFEST_VERSION
    assert calls == [DOMAIN]
    assert not [p for p in opened if p.endswith("manifest.json")]


def test_integration_version_none_when_loader_fails():
    """A loader failure degrades to None rather than breaking the download."""
    hass, entry = make_hass_and_entry(region="QLD1")

    async def _boom(hass_arg, domain):
        raise RuntimeError("integration not found")

    with patch.object(_diag_mod, "async_get_integration", _boom):
        result = run_async(async_get_config_entry_diagnostics(hass, entry))

    assert result["integration_version"] is None
    assert result["region"] == "QLD1"  # the rest of the payload still comes through
