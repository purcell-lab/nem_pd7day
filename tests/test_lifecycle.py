"""
Tests for __init__.py's lifecycle wiring: the refit closure, the force_refit
service, unload hygiene, and the integration's manifest and services files.

async_setup_entry cannot be run outside Home Assistant, so the refit and
service handler are checked structurally against the source. Since #366 the
setup is split into phase functions that async_setup_entry calls in order, so
a check that used to look inside async_setup_entry looks inside the phase that
now owns the code, and also checks that async_setup_entry calls that phase.
History:
  * v1.7.0: _do_refit was defined inside _refit(), so the _fetch_then_refit
    closure created by the publish-time scheduler raised NameError on every
    scheduled AEMO fetch. It is a module function now.
  * Startup refit is unconditional when >= 10 observations exist; a
    ``store.calibration is None`` guard left iso_model unpopulated after a
    restart because the calibration loaded from storage but the model is not
    persisted.
  * Issue #106: unloading one region must pop its STPASA store (the dict was
    only dropped when the last entry unloaded); every task is tied to the
    entry so unload cancels in-flight work; the scheduler registers one unload
    hook, not one per timer per day. Home Assistant 2026.9 schedules whatever
    an on-unload callback returns as a task, so the lambda that returned the
    popped StpasaStore raised "TypeError: a coroutine was expected" and left
    the entry in failed_unload on the live install (QLD1, 18 September 2026).

The calibration-store summary tests at the end belong in
test_calibration_store.py; they are kept here because that file is owned
elsewhere.

Run with:  python -m pytest tests/test_lifecycle.py -v
"""
from __future__ import annotations

import ast
import json
import os
import re
from datetime import timedelta
from functools import partial
from unittest.mock import MagicMock

import yaml

import support
from support import PKG_DIR, install_ha_stubs, load_chain

install_ha_stubs()

_nem_time, _engine_mod, _client_mod, _const_mod, _store_mod = load_chain(
    "nem_time", "calibration_engine", "pd7day_client", "const", "calibration_store",
)

# Bound to this file's module object, not the last file's (see support.py).
make_store = partial(support.make_store, _store_mod)

INIT_PATH = os.path.join(PKG_DIR, "__init__.py")


def _init_source() -> str:
    with open(INIT_PATH) as f:
        return f.read()


def _setup_entry_ast() -> ast.AsyncFunctionDef:
    tree = ast.parse(_init_source())
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
    )


def _nested_function(scope: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    return next(
        node for node in ast.walk(scope)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name
    )


def _module_function(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    """A function defined at the top level of ``__init__.py`` (a setup phase)."""
    return next(
        node for node in ast.parse(_init_source()).body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name
    )


def _setup_calls() -> list[str]:
    """Names of the functions async_setup_entry's own statements call, in order."""
    return [
        node.func.id
        for stmt in _setup_entry_ast().body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def _awaits_in_order(fn: ast.AST) -> list[str]:
    """Source of every ``await`` expression statement in ``fn``, in order."""
    return [
        ast.unparse(node.value)
        for node in ast.walk(fn)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)
    ]


# ── Setup phases (#366) ───────────────────────────────────────────────────────

def test_setup_runs_its_phases_in_order():
    """The stores load before the shared sources and coordinators, the record
    and runtime_data exist before the platforms read them, and the timers and
    services come after the platforms, as when it was one function."""
    phases = [
        "_ensure_nemweb_gate",
        "_migrate_forecast_mode",
        "_async_setup_stores",
        "_async_setup_shared_sources",
        "_async_setup_coordinators",
        "_async_load_pd_record",
        "NemPd7dayEntryData",
        "_wire_central_stpasa_fetch",
        "_forward_platforms",
        "_schedule_publish_fetches",
        "_register_actual_prices",
        "_schedule_refits",
        "_register_services_and_listeners",
    ]
    assert [name for name in _setup_calls() if name in phases] == phases


# ── Refit closure ─────────────────────────────────────────────────────────────

def test_do_refit_is_defined_at_module_scope_and_runs_after_the_fetch():
    """v1.7.0: _do_refit must not be nested in _refit, so the scheduled
    _fetch_then_refit can reach it. It is a module function (#366), which every
    phase reaches; and the refit must follow the fetch so it sees the new
    observations."""
    module_level = {
        node.name for node in ast.parse(_init_source()).body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert "_do_refit" in module_level

    schedule = _module_function("_schedule_publish_fetches")
    assert "_schedule_publish_fetches" in _setup_calls()
    fetch_then_refit = _nested_function(schedule, "_fetch_then_refit")
    assert _awaits_in_order(fetch_then_refit) == [
        "await coordinator.async_refresh()",
        "await _do_refit(store, pd_record, coordinator)",
    ]


def test_a_restored_cache_is_served_with_its_age_and_still_refreshed():
    """Issue #365: the cache-hit branch restores through restore_cached and queues the
    background refresh; only a miss blocks setup on the first refresh.

    The cache is now restored across the gap since the last publish, hours
    old, so the restore must carry its fetch time into the staleness
    attributes, and the refresh that replaces it with any newer run must
    still be queued.
    """
    assert "_async_start_coordinator" in ast.unparse(_module_function("_async_setup_coordinators"))
    branch = next(
        node for node in ast.walk(_module_function("_async_start_coordinator"))
        if isinstance(node, ast.If) and ast.unparse(node.test) == "cached is not None"
    )
    hit = "\n".join(ast.unparse(n) for n in branch.body)
    miss = "\n".join(ast.unparse(n) for n in branch.orelse)
    assert "restore_cached(coordinator, cached)" in hit
    assert "_delayed_refresh(coordinator, delay)" in hit
    assert "async_set_updated_data" not in hit
    assert "async_config_entry_first_refresh" not in hit
    assert "await coordinator.async_config_entry_first_refresh()" in miss


def test_do_refit_skips_below_ten_observations_then_refits():
    do_refit = _module_function("_do_refit")
    # The first statement after the docstring is the guard.
    guard = next(stmt for stmt in do_refit.body if not isinstance(stmt, ast.Expr))
    assert isinstance(guard, ast.If)
    assert ast.unparse(guard.test) == "store.observation_count < 10"
    assert isinstance(guard.body[-1], ast.Return)
    assert "await store.async_refit()" in _awaits_in_order(do_refit)


def test_startup_refit_is_unconditional_when_observations_exist():
    """The old ``store.calibration is None`` guard is gone; >= 10 observations always refit."""
    src = _init_source()
    assert "store.calibration is None" not in src, (
        "Startup refit must be unconditional; the guard leaves iso_model unpopulated after a restart"
    )
    assert "store.observation_count >= 10" in src
    assert "_do_refit" in src


def test_refit_interval_is_24h():
    """A shorter interval wastes CPU; a longer one lets the summary fall behind observation_count."""
    assert _const_mod.REFIT_INTERVAL == timedelta(hours=24)


# ── force_refit service ──────────────────────────────────────────────────────

def test_force_refit_service_handler_filters_by_entry_id_then_refits_and_refreshes():
    services = _module_function("_register_services_and_listeners")
    assert "_register_services_and_listeners" in _setup_calls()
    assert 'hass.services.async_register(DOMAIN, "force_refit", _handle_force_refit)' in ast.get_source_segment(_init_source(), services)

    handler = _nested_function(services, "_handle_force_refit")
    assert "cfg_entry.entry_id != entry_id" in ast.unparse(handler)
    assert _awaits_in_order(handler) == [
        "await st.async_refit()",
        "await coord.async_refresh()",
    ]


def test_services_yaml_defines_force_refit():
    services_path = os.path.join(PKG_DIR, "services.yaml")
    assert os.path.exists(services_path), "services.yaml missing from integration directory"
    with open(services_path) as f:
        services = yaml.safe_load(f)
    assert "entry_id" in services["force_refit"]["fields"]


# ── Unload hygiene (issue #106) ───────────────────────────────────────────────

def _register_by_region():
    """``__init__._register_by_region``, compiled on its own from the source."""
    fn = _nested_function(ast.parse(_init_source()), "_register_by_region")
    module = ast.Module(body=[fn], type_ignores=[])
    namespace: dict = {"DOMAIN": _const_mod.DOMAIN}
    exec(compile("from __future__ import annotations\n" + ast.unparse(module), INIT_PATH, "exec"), namespace)
    return namespace["_register_by_region"]


def test_stpasa_and_calibration_stores_are_deregistered_per_entry():
    """Unloading one region pops its stores so the central STPASA fetch stops
    writing .storage for a region the user removed (#106), and pre-dispatch is
    not served through a removed region's model (#337); the callback must
    return None (HA 2026.9 schedules any return value as a task)."""
    src = _init_source()
    stores_phase = ast.unparse(_module_function("_async_setup_stores"))
    assert "_async_setup_stores" in _setup_calls()
    assert "_register_by_region(hass, entry, 'stpasa_stores', region, stpasa_store)" in stores_phase
    assert "_register_by_region(hass, entry, CALIBRATION_STORES_KEY, region, store)" in stores_phase
    assert "lambda: stpasa_stores.pop" not in src
    register = _register_by_region()
    hass = MagicMock(data={_const_mod.DOMAIN: {}})
    unloads: list = []
    entry = MagicMock(async_on_unload=unloads.append)
    register(hass, entry, "stpasa_stores", "QLD1", "qld")
    register(hass, entry, "stpasa_stores", "NSW1", "nsw")
    assert hass.data[_const_mod.DOMAIN]["stpasa_stores"] == {"QLD1": "qld", "NSW1": "nsw"}
    assert unloads[0]() is None
    assert hass.data[_const_mod.DOMAIN]["stpasa_stores"] == {"NSW1": "nsw"}


def test_no_untracked_tasks_in_init():
    """Every task is tied to the config entry, so unload cancels a refit or
    fetch still in flight rather than letting it write to a torn-down store."""
    assert "hass.async_create_task(" not in _init_source(), (
        "__init__.py starts a task with hass.async_create_task; use "
        "entry.async_create_background_task so it is cancelled on unload"
    )


def test_scheduled_fetch_registers_one_unload_hook():
    """One cancel_all for the publish-time scheduler, not one cancel per timer per day."""
    src = _init_source()
    assert "entry.async_on_unload(fetch_scheduler.cancel_all)" in src
    assert "entry.async_on_unload(cancel)" not in src


# ── manifest.json ─────────────────────────────────────────────────────────────

def test_manifest_has_hacs_fields_and_a_valid_version():
    """HACS needs the listed fields; the version is MAJOR.MINOR.PATCH with an
    optional PEP 440 pre-release suffix (2.3.34b1, 2.3.34rc1)."""
    with open(os.path.join(PKG_DIR, "manifest.json")) as f:
        manifest = json.load(f)
    missing = {"domain", "name", "version", "documentation", "issue_tracker"} - set(manifest)
    assert not missing, f"manifest.json missing required HACS fields: {missing}"
    version = manifest["version"]
    assert re.match(r"^\d+\.\d+\.\d+([ab]\d+|rc\d+)?$", version), (
        f"Version {version!r} is not a valid HA version string"
    )


# ── CalibrationStore summary (belongs in test_calibration_store.py) ──────────

def test_store_counts_are_live_not_from_the_last_refit():
    """observation_count tracks _observations; active_bucket_count is 0 before
    any fit; summary_attributes() reports the live count, not the stale
    total_observations of the last refit, which diverge between refits."""
    store = make_store()
    assert store.observation_count == 0
    assert store.active_bucket_count == 0
    store._observations.append({"test": 1})
    store._observations.append({"test": 2})
    assert store.observation_count == 2

    store = make_store(obs_count=66)
    cal = MagicMock()
    cal.fitted_at = "2026-04-15T08:17:00+10:00"
    cal.summary.return_value = {
        "fitted_at": "2026-04-15T08:17:00+10:00",
        "total_observations": 21,  # stale: fitted when there were 21
        "buckets": {},
    }
    store._calibration = cal

    assert store.summary_attributes()["observation_count"] == 66
