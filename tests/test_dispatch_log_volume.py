"""
Tests pinning the DEBUG log volume of one dispatch poll cycle (issue #33).

Background.  With five regions configured the dispatch path used to emit nine
DEBUG records every five minutes:

  1  coordinator.schedule_next_poll()  "Dispatch next boundary poll at ..."
  1  dispatch_client                   "ELEC_NEM_SUMMARY fetched: <all regions>"
  1  dispatch_client                   "Dispatch: 5 regions fetched, settlement=..."
  1  coordinator._async_update_data()  "Finished fetching NEM Dispatch data ... 5 regions"
  5  coordinator._async_update_data()  "  Dispatch: <REGION> settlement=... $<price>/kWh"

Six of those nine restated information another line already carried, so the
cycle now emits two: the scheduler intent line and the single all-region
summary.  The separate five times multiplier reported on #33 came from five
DispatchCoordinator instances being created at setup, which is issue #34 and is
fixed elsewhere.

These tests assert the count, not just the absence of particular strings, so
that any new per-cycle DEBUG line has to be added deliberately.  They also
assert the surviving summary still names every region with its settlement and
price, so a future "dedupe" cannot quietly become a loss of diagnostics.

Run with: python -m pytest tests/test_dispatch_log_volume.py -v
"""
from __future__ import annotations

import inspect
import io
import json
import logging
import os
import sys
import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PACKAGE_LOGGER = "custom_components.nem_pd7day"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── HA stubs, mirroring tests/test_dispatch_and_modes.py ─────────────────────

sys.modules.setdefault("aiohttp", MagicMock())
for _ha_mod in [
    "homeassistant", "homeassistant.core", "homeassistant.config_entries",
    "homeassistant.const", "homeassistant.helpers", "homeassistant.helpers.storage",
    "homeassistant.helpers.event", "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.update_coordinator", "homeassistant.util",
    "homeassistant.util.dt", "homeassistant.components",
    "homeassistant.components.sensor",
]:
    sys.modules.setdefault(_ha_mod, MagicMock())


class _FakeCoordinator:
    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.last_update_success = True
        self.data = None

    def __class_getitem__(cls, item):
        return cls

    async def async_config_entry_first_refresh(self):
        pass

    async def async_refresh(self):
        pass


_uc_mock = MagicMock()
_uc_mock.DataUpdateCoordinator = _FakeCoordinator
_uc_mock.UpdateFailed = Exception
_uc_mock.CoordinatorEntity = object
sys.modules["homeassistant.helpers.update_coordinator"] = _uc_mock

_const_mod = _load(
    "custom_components.nem_pd7day.const",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "const.py"),
)
_nem_time_mod = _load(
    "custom_components.nem_pd7day.nem_time",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "nem_time.py"),
)
_client_mod = _load(
    "custom_components.nem_pd7day.pd7day_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "pd7day_client.py"),
)
_dispatch_mod = _load(
    "custom_components.nem_pd7day.dispatch_client",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "dispatch_client.py"),
)
_coord_mod = _load(
    "custom_components.nem_pd7day.coordinator",
    os.path.join(_ROOT, "custom_components", "nem_pd7day", "coordinator.py"),
)

from custom_components.nem_pd7day.coordinator import DispatchCoordinator

REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]
PRICES_MWH = {"QLD1": 89.5, "NSW1": 75.2, "VIC1": 120.0, "SA1": -5.0, "TAS1": 88.1}


# ── Fixtures and helpers ─────────────────────────────────────────────────────

def _current_boundary_nem() -> datetime:
    """Current 5-minute boundary in NEM time (UTC+10, no daylight saving).

    This is the settlement the coordinator computes as *expected*, so serving
    it back keeps the freshness gate satisfied without any sleeping retry.
    """
    nem_now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=10)
    return nem_now.replace(minute=(nem_now.minute // 5) * 5, second=0, microsecond=0)


def _summary_payload(settlement: datetime) -> bytes:
    ts = settlement.strftime("%Y-%m-%dT%H:%M:%S")
    rows = [
        {
            "SETTLEMENTDATE": ts,
            "REGIONID": region,
            "PRICE": PRICES_MWH[region],
            "PRICE_STATUS": "FIRM",
        }
        for region in REGIONS
    ]
    return json.dumps({"ELEC_NEM_SUMMARY": rows}).encode()


def _make_coordinator() -> DispatchCoordinator:
    """A DispatchCoordinator wired to run the real fetch in-process."""
    coord = DispatchCoordinator.__new__(DispatchCoordinator)
    hass = MagicMock()

    async def _fake_add_executor_job(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    hass.async_add_executor_job = _fake_add_executor_job
    coord.hass = hass
    coord.region = "QLD1"
    coord.prices = {}
    coord.last_updated = None
    coord.data = None
    return coord


def _run_one_cycle(coord: DispatchCoordinator) -> dict:
    """One full poll cycle: fetch prices, then schedule the next boundary."""
    payload = _summary_payload(_current_boundary_nem())

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(payload)

    with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
        prices = run_async(coord._async_update_data())

    with patch.object(_coord_mod, "async_track_point_in_utc_time", return_value=MagicMock()):
        coord.schedule_next_poll()

    return prices


def _debug_records(caplog):
    return [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and r.name.startswith(PACKAGE_LOGGER)
    ]


# ── Line count per cycle ─────────────────────────────────────────────────────

def test_dispatch_cycle_emits_exactly_two_debug_lines(caplog):
    """One dispatch cycle must emit 2 DEBUG records, down from 9 (issue #33).

    Fails before the fix with 9 records: the coordinator timing line, the
    five per-region lines, and the duplicate region count from
    dispatch_client, on top of the two that survive.
    """
    coord = _make_coordinator()

    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        prices = _run_one_cycle(coord)

    assert set(prices) == set(REGIONS), "test must exercise all five regions"

    records = _debug_records(caplog)
    rendered = "\n".join(r.getMessage() for r in records)
    assert len(records) == 2, (
        f"expected 2 DEBUG records per dispatch cycle, got {len(records)}:\n{rendered}"
    )


def test_dispatch_cycle_line_count_is_independent_of_region_count(caplog):
    """The per-cycle count must not scale with the number of regions.

    The old per-region loop made the cost of DEBUG proportional to how many
    regions AEMO returned.  Running a cycle that yields a single region must
    now produce the same two records as the five-region cycle above.
    """
    coord = _make_coordinator()
    single = json.dumps(
        {"ELEC_NEM_SUMMARY": [{
            "SETTLEMENTDATE": _current_boundary_nem().strftime("%Y-%m-%dT%H:%M:%S"),
            "REGIONID": "QLD1",
            "PRICE": 89.5,
            "PRICE_STATUS": "FIRM",
        }]}
    ).encode()

    def fake_urlopen(req, timeout=None):
        return io.BytesIO(single)

    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        with patch.object(_dispatch_mod.urllib.request, "urlopen", side_effect=fake_urlopen):
            prices = run_async(coord._async_update_data())
        with patch.object(_coord_mod, "async_track_point_in_utc_time", return_value=MagicMock()):
            coord.schedule_next_poll()

    assert set(prices) == {"QLD1"}
    assert len(_debug_records(caplog)) == 2


# ── The surviving lines still carry the information ──────────────────────────

def test_surviving_summary_names_every_region_with_settlement_and_price(caplog):
    """This is a dedupe, not a loss of diagnostics.

    Everything the removed per-region loop printed, region id, settlement in
    NEM time, and price, must still be readable off the one summary record.
    """
    coord = _make_coordinator()
    settlement = _current_boundary_nem()

    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        _run_one_cycle(coord)

    summaries = [
        r.getMessage() for r in _debug_records(caplog)
        if "ELEC_NEM_SUMMARY fetched" in r.getMessage()
    ]
    assert len(summaries) == 1, f"expected one all-region summary, got {summaries}"
    summary = summaries[0]

    for region in REGIONS:
        assert region in summary, f"{region} missing from summary: {summary}"
        assert f"${PRICES_MWH[region] / 1000.0:.4f}/kWh" in summary, (
            f"{region} price missing from summary: {summary}"
        )
    assert settlement.strftime("%Y-%m-%dT%H:%M") in summary, (
        f"settlement missing from summary: {summary}"
    )
    # The removed dispatch_client line reported the region count; the count is
    # still recoverable because every region is named.
    assert summary.count("settlement=") == len(REGIONS)


def test_surviving_scheduler_line_still_announces_next_boundary(caplog):
    """The other survivor is the scheduler intent line, which is not a duplicate."""
    coord = _make_coordinator()

    with caplog.at_level(logging.DEBUG, logger=PACKAGE_LOGGER):
        _run_one_cycle(coord)

    boundary_lines = [
        r.getMessage() for r in _debug_records(caplog)
        if "next boundary poll" in r.getMessage()
    ]
    assert len(boundary_lines) == 1, f"expected one scheduler line, got {boundary_lines}"
    assert f"+{_coord_mod._DISPATCH_POLL_DELAY_S}s delay" in boundary_lines[0]


# ── Guard tests: the removed statements must stay removed ────────────────────

def test_coordinator_success_path_has_no_debug_logging():
    """Guard against the per-region loop and timing line coming back.

    Counting records alone would not catch someone re-adding a line while
    also relaxing the count, so pin the source too.  Failure paths are
    excluded: they log at WARNING and are outside this issue.
    """
    src = inspect.getsource(DispatchCoordinator._async_update_data)
    success_path = src.split("except Exception as exc:")[0]
    success_path = "\n".join(
        line for line in success_path.splitlines() if not line.lstrip().startswith("#")
    )
    assert "Finished fetching NEM Dispatch data" not in success_path, (
        "coordinator must not re-add the timing/region-count DEBUG line to "
        "the dispatch success path; HA core's DataUpdateCoordinator already "
        "logs elapsed time"
    )
    assert "for region_id, dp in sorted(prices.items())" not in success_path, (
        "coordinator must not re-add the per-region DEBUG loop; the "
        "ELEC_NEM_SUMMARY line already covers every region"
    )


def test_dispatch_client_summary_path_logs_once():
    """The ELEC_NEM_SUMMARY success path must emit exactly one DEBUG line.

    The DispatchIS fallback keeps its own line, so scope the check to the
    primary path, which is the one that runs every five minutes.
    """
    src = inspect.getsource(_dispatch_mod.fetch_dispatch_prices)
    primary = src.split("# Fallback: DispatchIS_Reports zip")[0]
    # Drop comment lines: the removed statement is quoted in a comment there
    # explaining why it went, and that should not trip the guard.
    primary = "\n".join(
        line for line in primary.splitlines() if not line.lstrip().startswith("#")
    )
    assert primary.count("Dispatch: %d regions fetched") == 0, (
        "dispatch_client must not re-add the duplicate region count on the "
        "ELEC_NEM_SUMMARY success path"
    )
    assert primary.count("ELEC_NEM_SUMMARY fetched") == 1
