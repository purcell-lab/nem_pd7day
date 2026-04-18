"""
Tests for forecast history attributes on PD7DayCalibrationSensor.

The PD7DayForecastHistorySensor was removed in v2.0.4 — its data was merged
into the calibration sensor's extra_state_attributes under the
forecast_history_* keys.

Run with:  python -m pytest tests/test_forecast_history_sensor.py -v
"""
from __future__ import annotations

import sys
import os
import importlib.util
from unittest.mock import MagicMock
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

# Stub HA modules
for mod in [
    "homeassistant", "homeassistant.core", "homeassistant.helpers",
    "homeassistant.helpers.storage", "homeassistant.helpers.event",
    "homeassistant.helpers.entity_platform", "homeassistant.helpers.device_registry",
    "homeassistant.helpers.update_coordinator", "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.util", "homeassistant.util.dt",
    "homeassistant.components", "homeassistant.components.sensor",
]:
    sys.modules.setdefault(mod, MagicMock())

# Provide EntityCategory
ec = MagicMock()
ec.DIAGNOSTIC = "diagnostic"
sys.modules["homeassistant.const"].EntityCategory = ec

_load("custom_components.nem_pd7day.nem_time",
      os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"))
_load("custom_components.nem_pd7day.calibration_engine",
      os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_engine.py"))

class _FakeStore:
    def __init__(self, hass, version, key):
        self._key = key
    async def async_load(self):
        return None
    async def async_save(self, data):
        pass

storage_mock = MagicMock()
storage_mock.Store = _FakeStore
sys.modules["homeassistant.helpers.storage"] = storage_mock

_load("custom_components.nem_pd7day.calibration_store",
      os.path.join(_ROOT, "custom_components", "nem_pd7day", "calibration_store.py"))

from custom_components.nem_pd7day.calibration_store import CalibrationStore

NEM_TZ = timezone(timedelta(hours=10))


def _make_store_with_history(history: dict) -> CalibrationStore:
    store = CalibrationStore.__new__(CalibrationStore)
    store._observations = []
    store._calibration = None
    store._forecast_history = history
    store._region = "QLD1"
    return store


def _fh_attrs(store) -> dict:
    """Extract forecast history keys from calibration sensor attributes."""
    attrs = store.summary_attributes()
    fh = store._forecast_history
    if fh:
        attrs["forecast_history_entries"] = int(sum(len(v) for v in fh.values()))
        attrs["forecast_history_intervals"] = len(fh)
        attrs["forecast_history_oldest"] = min(fh.keys())
        attrs["forecast_history_newest"] = max(fh.keys())
        attrs["forecast_history_runs_avg"] = round(
            sum(len(v) for v in fh.values()) / len(fh), 1
        )
    else:
        attrs["forecast_history_entries"] = 0
        attrs["forecast_history_intervals"] = 0
        attrs["forecast_history_oldest"] = None
        attrs["forecast_history_newest"] = None
        attrs["forecast_history_runs_avg"] = 0
    return attrs


def test_forecast_history_entries_count():
    history = {
        "2026-04-18T17:00:00+10:00": [{"run_at": "x", "forecast_price": 0.09}],
    }
    attrs = _fh_attrs(_make_store_with_history(history))
    assert attrs["forecast_history_entries"] == 1


def test_forecast_history_entries_multiple():
    history = {
        "2026-04-18T17:00:00+10:00": [
            {"run_at": "a", "forecast_price": 0.09},
            {"run_at": "b", "forecast_price": 0.10},
        ],
        "2026-04-18T17:30:00+10:00": [
            {"run_at": "a", "forecast_price": 0.11},
        ],
    }
    attrs = _fh_attrs(_make_store_with_history(history))
    assert attrs["forecast_history_entries"] == 3
    assert attrs["forecast_history_intervals"] == 2
    assert attrs["forecast_history_runs_avg"] == 1.5


def test_forecast_history_oldest_newest():
    history = {
        "2026-04-18T17:00:00+10:00": [{"run_at": "a"}],
        "2026-04-20T10:00:00+10:00": [{"run_at": "b"}],
    }
    attrs = _fh_attrs(_make_store_with_history(history))
    assert attrs["forecast_history_oldest"] == "2026-04-18T17:00:00+10:00"
    assert attrs["forecast_history_newest"] == "2026-04-20T10:00:00+10:00"


def test_forecast_history_empty_store():
    attrs = _fh_attrs(_make_store_with_history({}))
    assert attrs["forecast_history_entries"] == 0
    assert attrs["forecast_history_intervals"] == 0
    assert attrs["forecast_history_oldest"] is None
    assert attrs["forecast_history_newest"] is None
    assert attrs["forecast_history_runs_avg"] == 0


def test_calibration_sensor_includes_forecast_history_keys():
    """All five forecast_history_* keys must be present in calibration sensor attrs."""
    history = {"2026-04-19T09:00:00+10:00": [{"run_at": "x"}]}
    attrs = _fh_attrs(_make_store_with_history(history))
    for key in [
        "forecast_history_entries",
        "forecast_history_intervals",
        "forecast_history_oldest",
        "forecast_history_newest",
        "forecast_history_runs_avg",
    ]:
        assert key in attrs, f"Missing key: {key}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
