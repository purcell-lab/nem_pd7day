"""Build every entity one config entry publishes, from a scenario, with only HA stubbed.

``build_entities(name)`` is a context manager. Inside it:

  * the Home Assistant stub tree is installed fresh (``support.install_ha_stubs``
    plus the richer stand-ins below) and every module of the integration is
    loaded fresh from file, in import order, so nothing another test file
    loaded or patched can leak in; on exit ``sys.modules`` is put back exactly
    as it was;
  * the scenario is built against those module objects;
  * a FrozenClock holds every wall-clock read at the scenario's instant;
  * ``entry.runtime_data`` is assembled the way ``__init__.async_setup_entry``
    assembles it, from the real CalibrationStore, ForecastStore, StpasaStore,
    GridNoticeStore, SharedPD7DayFetch, PD7DayCoordinator and the shared
    DispatchCoordinator, with the network replaced by the scenario;
  * the real ``async_setup_entry`` of the sensor, binary_sensor, camera and
    number platforms runs, in __init__.PLATFORMS order, and each entity is
    added the way Home Assistant adds it: an entity id is assigned, ``hass``
    is set and ``async_added_to_hass`` is awaited.

It yields a ``Built`` whose ``entities`` are read by ``snapshot.snapshot``
while the clock is still frozen.

What is stubbed, and why it is enough:

  * DataUpdateCoordinator / CoordinatorEntity: the real HA contract for
    refresh, listeners and ``last_update_success``, nothing more.
  * Store: backed by ``support.MemoryStore`` under a per-scenario namespace,
    with a JSON round trip on every write as HA's Store does.
  * RestoreNumber: restores the scenario's usage fee.
  * hass: ``data``, an inline executor, ``states.get`` serving the usage fee
    number's state, and background tasks that are closed rather than run.
    The tasks entities schedule on add are the first chart renders (the
    snapshot renders the forecast chart itself) and warm-then-write state
    updates, whose values the snapshot's own reads compute through the same
    lazy path, so nothing published depends on them.

Deliberate differences from a live setup, each harmless to what is published:

  * The startup refit runs before platform setup rather than as a background
    task after it, so entities are built over the fit a live install
    converges to once the refit's re-push has been handled. The time-of-day
    statistics are computed at the first refresh, before the refit, as live.
  * ``update_before_add`` is not honoured: in HA it asks the coordinator for
    a refresh, which here would be the same fetch again.
  * Background tasks are not run (see above).
  * The stale scenario replays its day: set up at the morning fetch, STPASA
    saved when the central fetch would have saved it, then the failing fetch
    at the frozen instant.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import sys
import types
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import support
from support import PKG, PKG_DIR

from . import clock as clock_mod
from .scenarios import SCENARIOS, Scenario

_LOGGER = logging.getLogger("golden.harness")

PLATFORM_ORDER = ("sensor", "binary_sensor", "camera", "number")
_STUB_PREFIXES = ("homeassistant", "aiohttp", "voluptuous")
_NAMESPACE_PREFIX = "golden::"


# ── Home Assistant stand-ins ─────────────────────────────────────────────────

class GoldenCoordinator:
    """DataUpdateCoordinator: refresh, listeners, last_update_success."""

    def __init__(self, hass: Any, logger: Any, name: str | None = None, update_interval: Any = None, **_: Any) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data: Any = None
        self.last_update_success = True
        self._listeners: list[Callable[[], None]] = []

    def __class_getitem__(cls, item: Any) -> type:
        return cls

    def async_add_listener(self, update_callback: Callable[[], None], context: Any = None) -> Callable[[], None]:
        self._listeners.append(update_callback)

        def remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove

    def async_update_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    def async_set_updated_data(self, data: Any) -> None:
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()

    async def async_refresh(self) -> None:
        try:
            self.data = await self._async_update_data()  # type: ignore[attr-defined]
            self.last_update_success = True
        except support.UpdateFailed:
            self.last_update_success = False
        self.async_update_listeners()

    async def async_config_entry_first_refresh(self) -> None:
        await self.async_refresh()
        if not self.last_update_success:
            raise RuntimeError(f"{self.name}: first refresh failed (ConfigEntryNotReady)")

    async def async_request_refresh(self) -> None:
        await self.async_refresh()


class GoldenCoordinatorEntity:
    """CoordinatorEntity: holds the coordinator, listens once added."""

    _attr_should_poll = False

    def __init__(self, coordinator: Any = None, context: Any = None) -> None:
        self.coordinator = coordinator

    def __class_getitem__(cls, item: Any) -> type:
        return cls

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_coordinator_update))

    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def async_on_remove(self, func: Callable[[], None]) -> None:
        self.__dict__.setdefault("_golden_on_remove", []).append(func)

    def async_write_ha_state(self) -> None:
        self.__dict__["_golden_writes"] = self.__dict__.get("_golden_writes", 0) + 1

    async def async_will_remove_from_hass(self) -> None:
        return None


class GoldenRestoreNumber(support.NumberEntity):
    """RestoreNumber whose last data is the scenario's usage fee."""

    _attr_native_value = None

    async def async_added_to_hass(self) -> None:
        return None

    async def async_get_last_number_data(self) -> Any:
        value = self.hass.golden_restore.get(self.entity_id)  # type: ignore[attr-defined]
        if value is None:
            return None
        return types.SimpleNamespace(native_value=value, native_unit_of_measurement="$/kWh")

    def async_write_ha_state(self) -> None:
        self.__dict__["_golden_writes"] = self.__dict__.get("_golden_writes", 0) + 1


def _ha_json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Store payload is not JSON serialisable: {type(value).__name__}")


class GoldenStore:
    """HA ``Store`` over ``support.MemoryStore``, namespaced per scenario.

    Every write is round-tripped through JSON, which is what reaches disk in
    Home Assistant, so a payload that would not survive the real store fails
    here too, and a later mutation of the saved object cannot leak into the
    stored copy.
    """

    namespace = _NAMESPACE_PREFIX

    def __init__(self, hass: Any, version: int, key: str, *args: Any, **kwargs: Any) -> None:
        self.key = key
        self.version = version
        self._mem = support.MemoryStore(f"{self.namespace}{key}")

    @staticmethod
    def _encode(data: Any) -> Any:
        return json.loads(json.dumps(data, default=_ha_json_default))

    async def async_load(self) -> Any:
        return copy.deepcopy(await self._mem.async_load())

    async def async_save(self, data: Any) -> None:
        await self._mem.async_save(self._encode(data))

    def async_delay_save(self, data_func: Callable[[], Any], delay: float = 0) -> None:
        support.MemoryStore._data[self._mem._key] = self._encode(data_func())

    async def async_remove(self) -> None:
        await self._mem.async_remove()


def _callback(func: Callable) -> Callable:
    """``homeassistant.core.callback``: marks, never wraps."""
    try:
        func._hass_callback = True  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return func


def _install_golden_stubs() -> None:
    support.install_ha_stubs()
    sys.modules["homeassistant.core"].callback = _callback  # type: ignore[attr-defined]
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = GoldenCoordinator  # type: ignore[attr-defined]
    uc.CoordinatorEntity = GoldenCoordinatorEntity  # type: ignore[attr-defined]
    uc.UpdateFailed = support.UpdateFailed  # type: ignore[attr-defined]
    sys.modules["homeassistant.helpers.update_coordinator"] = uc
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = GoldenStore  # type: ignore[attr-defined]
    sys.modules["homeassistant.helpers.storage"] = storage
    number = types.ModuleType("homeassistant.components.number")
    number.NumberEntity = support.NumberEntity  # type: ignore[attr-defined]
    number.NumberMode = support.NumberMode  # type: ignore[attr-defined]
    number.RestoreNumber = GoldenRestoreNumber  # type: ignore[attr-defined]
    sys.modules["homeassistant.components.number"] = number


# Classes that stand in for Home Assistant. snapshot.py resolves entity
# properties the way HA does, and needs to know which bases are not the
# integration's own.
STUB_BASES: tuple[type, ...] = (
    GoldenCoordinatorEntity, GoldenRestoreNumber, support.NumberEntity, support.FakeCamera,
)


# ── hass, entry, tasks ────────────────────────────────────────────────────────

class _ClosedTask:
    """What a background task looks like when the harness declines to run it."""

    def __init__(self, coro: Any) -> None:
        if asyncio.iscoroutine(coro):
            coro.close()

    def add_done_callback(self, cb: Callable[[Any], None]) -> None:
        cb(self)

    def cancel(self) -> bool:
        return False

    def done(self) -> bool:
        return True


class _States:
    def __init__(self) -> None:
        self._by_id: dict[str, Callable[[], Any]] = {}

    def serve(self, entity_id: str, state: Callable[[], Any]) -> None:
        self._by_id[entity_id] = state

    def get(self, entity_id: str) -> Any:
        state = self._by_id.get(entity_id)
        if state is None:
            return None
        return types.SimpleNamespace(entity_id=entity_id, state=state(), attributes={})


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.states = _States()
        self.golden_restore: dict[str, Any] = {}
        self.config_entries = MagicMock()
        self.services = MagicMock()

    async def async_add_executor_job(self, target: Callable, *args: Any) -> Any:
        return target(*args)

    def async_create_task(self, coro: Any, *args: Any, **kwargs: Any) -> _ClosedTask:
        return _ClosedTask(coro)

    def async_create_background_task(self, coro: Any, *args: Any, **kwargs: Any) -> _ClosedTask:
        return _ClosedTask(coro)


class FakeEntry:
    def __init__(self, entry_id: str, region: str, options: dict[str, Any]) -> None:
        self.entry_id = entry_id
        self.title = f"NEM PD7DAY {region}"
        self.data = {"region": region}
        self.options = options
        self.runtime_data: Any = None
        self.unloads: list[Callable] = []

    def async_on_unload(self, func: Callable) -> None:
        self.unloads.append(func)

    def async_create_background_task(self, hass: Any, coro: Any, name: str | None = None, *args: Any, **kwargs: Any) -> _ClosedTask:
        return _ClosedTask(coro)


# ── Network replacements ─────────────────────────────────────────────────────

class _ScenarioPD7DayClient:
    """PD7DayClient for SharedPD7DayFetch: the scenario's run, or a failure."""

    def __init__(self, scenario: Scenario, error_cls: type) -> None:
        self._result = scenario.pd7day
        self._error_cls = error_cls
        self.fail_status: int | None = None

    async def newest_file(self) -> dict[str, str]:
        if self.fail_status is not None:
            raise self._error_cls(
                f"NEMWEB answered {self.fail_status}", retryable=False, status=self.fail_status,
            )
        return {"name": self._result.source_file, "href": self._result.source_file}

    async def fetch_all(self, regions: Any, interconnector_ids: Any = None, file_meta: Any = None) -> Any:
        return self._result


class _ScenarioNoticeClient:
    """MarketNoticeClient: the scenario's notice bodies through the real parser."""

    def __init__(self, notices: Any, parse: Callable[[str, int], Any]) -> None:
        self._notices = sorted(notices)
        self._parse = parse
        self.last_seen_notice_id = 0

    async def fetch_new_notices(self) -> list[Any]:
        found = []
        newest = self.last_seen_notice_id
        for notice_id, body in self._notices:
            if notice_id <= self.last_seen_notice_id:
                continue
            newest = max(newest, notice_id)
            parsed = self._parse(body, notice_id)
            if parsed is not None:
                found.append(parsed)
        self.last_seen_notice_id = newest
        return found


# ── Module loading ───────────────────────────────────────────────────────────

def _import_order() -> list[str]:
    import ast

    names = sorted(f[:-3] for f in os.listdir(PKG_DIR) if f.endswith(".py"))
    deps: dict[str, set[str]] = {}
    for name in names:
        with open(os.path.join(PKG_DIR, f"{name}.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        found: set[str] = set()
        stack: list[ast.AST] = list(tree.body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module:
                    found.add(node.module.split(".")[0])
                else:
                    found.update(a.name for a in node.names)
            stack.extend(ast.iter_child_nodes(node))
        deps[name] = found & set(names)
    order: list[str] = []

    def visit(name: str, stack: tuple[str, ...] = ()) -> None:
        if name in order:
            return
        if name in stack:
            raise RuntimeError(f"import cycle: {stack + (name,)}")
        for dep in sorted(deps[name]):
            visit(dep, stack + (name,))
        order.append(name)

    for name in names:
        if name != "__init__":
            visit(name)
    visit("__init__")
    return order


IMPORT_ORDER = _import_order()


@contextlib.contextmanager
def isolated_integration() -> Iterator[types.SimpleNamespace]:
    """Fresh HA stubs and a fresh load of every integration module; restored on exit."""
    support._ensure_packages()
    package = sys.modules[PKG]
    owned = [
        key for key in sys.modules
        if key.startswith(PKG + ".") or any(key == p or key.startswith(p + ".") for p in _STUB_PREFIXES)
    ]
    saved_modules = {key: sys.modules.pop(key) for key in owned}
    missing = object()
    saved_attrs = {name: getattr(package, name, missing) for name in IMPORT_ORDER}
    try:
        _install_golden_stubs()
        mods = types.SimpleNamespace()
        for name in IMPORT_ORDER:
            module = support.load(name)
            setattr(mods, "init" if name == "__init__" else name, module)
            if name != "__init__":
                setattr(package, name, module)
        yield mods
    finally:
        for key in [k for k in sys.modules if k.startswith(PKG + ".") or any(
            k == p or k.startswith(p + ".") for p in _STUB_PREFIXES
        )]:
            del sys.modules[key]
        sys.modules.update(saved_modules)
        for name, value in saved_attrs.items():
            if value is missing:
                if hasattr(package, name):
                    delattr(package, name)
            else:
                setattr(package, name, value)


def clock_modules(mods: types.SimpleNamespace) -> dict[str, types.ModuleType]:
    """Scan name -> module object, for FrozenClock."""
    import importlib

    out: dict[str, types.ModuleType] = {}
    for name in IMPORT_ORDER:
        out[name] = getattr(mods, "init" if name == "__init__" else name)
    for scanned in clock_mod.SCAN.modules():
        if clock_mod.is_third_party(scanned):
            out[scanned] = importlib.import_module(scanned)
    return out


# ── Entity ids ───────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _device_names(entities: list[tuple[str, Any]]) -> dict[tuple, str]:
    names: dict[tuple, str] = {}
    for _domain, entity in entities:
        info = _device_info(entity)
        if info and info.get("name"):
            names.setdefault(_identifier_key(info), info["name"])
    return names


def _identifier_key(info: dict) -> tuple:
    return tuple(sorted(tuple(i) for i in info.get("identifiers", ())))


def _device_info(entity: Any) -> dict | None:
    for klass in type(entity).__mro__:
        if klass.__module__.startswith(PKG) and "device_info" in klass.__dict__:
            return entity.device_info
    return getattr(entity, "_attr_device_info", None)


def _entity_name(entity: Any) -> str | None:
    for klass in type(entity).__mro__:
        if klass.__module__.startswith(PKG) and "name" in klass.__dict__:
            return entity.name
    return getattr(entity, "_attr_name", None)


def suggest_entity_ids(entities: list[tuple[str, Any]]) -> list[str]:
    """Entity ids as Home Assistant generates them for a new install.

    has_entity_name entities are named "<device name> <entity name>", the
    device name coming from whichever entity of the device supplies one (the
    device registry holds one name per device); others are named by their own
    name. Collisions get "_2", "_3" in add order.
    """
    device_names = _device_names(entities)
    taken: set[str] = set()
    out = []
    for domain, entity in entities:
        name = _entity_name(entity)
        if getattr(entity, "_attr_has_entity_name", False):
            info = _device_info(entity)
            device = device_names.get(_identifier_key(info)) if info else None
            full = " ".join(p for p in (device, name) if p)
        else:
            full = name or ""
        base = f"{domain}.{_slug(full) or 'unnamed'}"
        candidate, n = base, 2
        while candidate in taken:
            candidate, n = f"{base}_{n}", n + 1
        taken.add(candidate)
        out.append(candidate)
    return out


# ── Build ────────────────────────────────────────────────────────────────────

@dataclass
class Built:
    scenario: Scenario
    mods: types.SimpleNamespace
    hass: FakeHass
    entry: FakeEntry
    clock: clock_mod.FrozenClock
    entities: list[Any] = field(default_factory=list)
    # id(entity) -> platform domain, in add order alongside ``entities``.
    domains: dict[int, str] = field(default_factory=dict)


def _write_storage(payloads: dict[str, Any]) -> None:
    for key, payload in payloads.items():
        support.MemoryStore._data[f"{GoldenStore.namespace}{key}"] = GoldenStore._encode(payload)


def _clear_namespace(namespace: str) -> None:
    for key in [k for k in support.MemoryStore._data if isinstance(k, str) and k.startswith(namespace)]:
        del support.MemoryStore._data[key]


async def _setup(built: Built) -> None:
    """Mirror __init__.async_setup_entry, then run the four platforms."""
    sc, mods, hass, entry, clock = built.scenario, built.mods, built.hass, built.entry, built.clock
    const = mods.const
    domain = const.DOMAIN
    setup_at = sc.stale.first_fetch_at if sc.stale else sc.now
    clock.set(setup_at)

    # ── Storage as the live install would hold it at setup ───────────────
    payloads = dict(sc.calibration.payloads(mods, sc.market, setup_at))
    stpasa_key = mods.stpasa_store._stpasa_storage_key(sc.region)
    stpasa_later = False
    if sc.stpasa is not None:
        if sc.stpasa_fetched_at <= setup_at:
            payloads[stpasa_key] = asdict(sc.stpasa)
        else:
            stpasa_later = True
    if sc.scarcity_samples is not None:
        payloads[f"nem_pd7day_{entry.entry_id}_qld1_scarcity_premium_samples"] = dict(sc.scarcity_samples)
    _write_storage(payloads)
    if sc.calibration.prefit:
        # The fit a previous run left in the coefficient store.
        clock.set(setup_at - timedelta(hours=1))
        earlier = mods.calibration_store.CalibrationStore(hass, sc.region)
        await earlier.async_load()
        await earlier.async_refit()
        clock.set(setup_at)

    # ── __init__.async_setup_entry, network replaced by the scenario ─────
    hass.data.setdefault(domain, {})
    hass.data[domain][const.NEMWEB_SEMAPHORE_KEY] = mods.nemweb_gate.NemwebGate(
        const.NEMWEB_MAX_CONCURRENT_REQUESTS, const.NEMWEB_MIN_REQUEST_GAP_S,
    )
    if const.CONF_FORECAST_MODE not in entry.options:
        entry.options = {**entry.options, const.CONF_FORECAST_MODE: const.FORECAST_MODE_DAYS_2_7}
    region = const.get_region(entry)
    interconnector_ids = const.interconnectors_for_regions([region])
    trace = mods.startup_trace.StartupTrace(region, _LOGGER)

    store = mods.calibration_store.CalibrationStore(hass, region)
    await sc.calibration.load_into(store)
    forecast_store = mods.forecast_store.ForecastStore(hass, region)
    stpasa_refresh = hass.data[domain].setdefault(
        mods.stpasa_refresh.STPASA_REFRESH_KEY, mods.stpasa_refresh.StpasaRefreshCoordination(),
    )
    stpasa_store = mods.stpasa_store.StpasaStore(hass, region, refresh=stpasa_refresh)
    await stpasa_store.load()
    hass.data[domain].setdefault("stpasa_stores", {})[region] = stpasa_store

    setup_lock = hass.data[domain].setdefault(const.SETUP_LOCK_KEY, asyncio.Lock())
    notice_store = mods.notice_store.GridNoticeStore(hass)
    await notice_store.async_load()
    hass.data[domain]["notice_store"] = notice_store
    notice_client = _ScenarioNoticeClient(sc.notices, mods.market_notice_client._parse_notice_body)
    hass.data[domain]["notice_client"] = notice_client

    pd7day_client = _ScenarioPD7DayClient(sc, mods.nemweb_retry.NemwebFetchError)
    hass.data[domain][const.SHARED_FETCH_KEY] = mods.pd7day_shared.SharedPD7DayFetch(pd7day_client)

    coordinator = mods.coordinator.PD7DayCoordinator(
        hass, [region], store,
        interconnector_ids=interconnector_ids,
        notice_store=notice_store,
        notice_client=notice_client,
        forecast_store=forecast_store,
        stpasa_store=stpasa_store,
    )
    cached = await forecast_store.load()
    if cached is not None:
        coordinator.async_set_updated_data(cached)
    else:
        await coordinator.async_config_entry_first_refresh()

    dispatch_prices = dict(sc.dispatch or {})
    mods.coordinator.fetch_dispatch_prices = lambda expected, *a, **k: dict(dispatch_prices)
    dispatch = await mods.shared_dispatch.async_shared_dispatch(hass, setup_lock, trace)

    entry.runtime_data = mods.init.NemPd7dayEntryData(
        coordinator=coordinator,
        store=store,
        forecast_store=forecast_store,
        stpasa_store=stpasa_store,
        dispatch=dispatch,
        notice_store=notice_store,
        region=region,
    )

    # Startup refit (__init__'s _do_refit, guarded the same way).
    if store.observation_count >= 10:
        await store.async_refit()
        if coordinator.data is not None:
            coordinator.async_set_updated_data(coordinator.data)
        else:
            await coordinator.async_refresh()

    # ── The rest of the day, for the stale scenario ───────────────────────
    if stpasa_later:
        clock.set(sc.stpasa_fetched_at)
        await stpasa_store.save(sc.stpasa)
    if sc.stale is not None:
        clock.set(sc.now)
        pd7day_client.fail_status = sc.stale.failure_status
        await coordinator.async_refresh()
    clock.set(sc.now)
    await coordinator.async_fetch_notices()

    # ── Platform setup, then HA's add flow per entity ─────────────────────
    added: list[tuple[str, Any]] = []
    for platform in PLATFORM_ORDER:
        batch: list[Any] = []

        def add_entities(new: Any, update_before_add: bool = False, _batch: list = batch) -> None:
            _batch.extend(new)

        await getattr(mods, platform).async_setup_entry(hass, entry, add_entities)
        added.extend((platform, e) for e in batch)

    ids = suggest_entity_ids(added)
    fee_entity_id = const.additional_fee_entity_id(region)
    for (platform, entity), entity_id in zip(added, ids):
        entity.hass = hass
        entity.entity_id = entity_id
        built.entities.append(entity)
        built.domains[id(entity)] = platform
        if platform == "number":
            if sc.usage_fee is not None:
                hass.golden_restore[entity_id] = sc.usage_fee

            def number_state(e: Any = entity) -> str:
                return str(e.native_value)

            hass.states.serve(entity_id, number_state)
            # The tariff sensors read the fee from const.additional_fee_entity_id,
            # which is not the id HA derives from the number's name; serve it
            # there too so the scenario's fee reaches them.
            hass.states.serve(fee_entity_id, number_state)
    for entity in built.entities:
        await entity.async_added_to_hass()


@contextlib.contextmanager
def build_entities(scenario: str | Callable[[Any], Scenario]) -> Iterator[Built]:
    """Build every entity of one scenario; read them inside the ``with`` block.

    Takes a scenario name from ``scenarios.SCENARIOS`` or a builder. The
    scenario is built against the module objects loaded here, which is why
    this takes the builder rather than a ready ``Scenario``.
    """
    builder = SCENARIOS[scenario] if isinstance(scenario, str) else scenario
    namespace = f"{_NAMESPACE_PREFIX}{getattr(builder, '__name__', 'scenario')}::"
    _clear_namespace(namespace)
    previous_namespace = GoldenStore.namespace
    GoldenStore.namespace = namespace
    loop = asyncio.new_event_loop()
    try:
        with isolated_integration() as mods:
            sc = builder(mods)
            modules = clock_modules(mods)
            with clock_mod.FrozenClock(sc.now, modules) as frozen:
                hass = FakeHass()
                entry = FakeEntry(sc.entry_id, sc.region, dict(sc.options))
                built = Built(scenario=sc, mods=mods, hass=hass, entry=entry, clock=frozen)
                loop.run_until_complete(_setup(built))
                yield built
    finally:
        loop.close()
        GoldenStore.namespace = previous_namespace
        _clear_namespace(namespace)
