"""Shared scaffolding for the test suite.

Test files load integration modules from file so the HA-dependent package
``__init__`` never runs (see conftest.py), and stub ``homeassistant`` with
MagicMocks because HA is not installed. This module holds the one copy of
that machinery. Import it as ``from support import ...``: pytest puts
``tests/`` on ``sys.path`` because conftest.py lives here.

API
---
Paths / loading
  ROOT, PKG_DIR, PKG        repo root, custom_components/nem_pd7day, its dotted name
  NEM_TZ                    timezone(timedelta(hours=10))
  load(name)                exec <PKG_DIR>/<name>.py as a fresh module object
                            registered in sys.modules as PKG.<name>; returns it.
                            Re-executes on every call, so classes imported from
                            an earlier load keep their own module object.
  load_chain(*names)        tuple(load(n) for n in names)

Small helpers
  run_async(coro)           asyncio.new_event_loop().run_until_complete(coro)
  nem_iso(dt)               dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")
  make_zip(csv_bytes, member="PUBLIC_PD7DAY_X.CSV") -> bytes

HA stubs
  install_ha_stubs()        install the aiohttp + homeassistant MagicMock tree.
                            Call it BEFORE load()ing any hass-aware module.
                            Safe to call from every file; see its docstring for
                            which entries are reassigned each call and which are
                            only setdefault'ed.
  FakeCoordinator           DataUpdateCoordinator stand-in (hass, logger, name,
                            update_interval; last_update_success; data; [...] ok)
  FakeCoordinatorEntity     CoordinatorEntity stand-in (coordinator=None, **kw)
  UpdateFailed              Exception subclass installed as UpdateFailed
  FakeStore                 Store(hass, version, key): load -> None, save no-op
  MemoryStore               Store(key) backed by the class dict MemoryStore._data
                            (load/save/remove); for ObservationLog store_factory
  FakeCamera, CameraEntityFeature, NumberMode, NumberEntity, RestoreNumber,
  BinarySensorDeviceClass, SensorDeviceClass, SensorStateClass,
  ClientResponseError, ClientError   the classes the stubs expose

Fixture builders
  make_price_period(nemtime_dt, value=0.10)        MagicMock(nemtime, time, value)
  make_price_data(run_at_dt, periods)              MagicMock(forecast_generated_at, forecast)
  make_real_price_period(client_mod, nemtime_dt, value=0.10)
                                                   client_mod.PricePeriod
  make_pd7day_data(client_mod, run_at, periods, *, region="QLD1",
                   source_file=..., **fields)      client_mod.PD7DayData; run_at may
                                                   be a datetime, str or None
  make_store(store_mod, *, region="QLD1", fh_load_data=None, obs_count=0,
             hass=None)                            store_mod.CalibrationStore via
                                                   __new__ with AsyncMock
                                                   _obs/_coeff/_fh stores and a
                                                   MemoryStore-backed log
  expected_import_price(tariff_mod, lib_c_kwh, rrp_mwh, fee=0.0293,
                        distributor="energex")     published $/kWh for a mocked
                                                   spot_to_tariff return

The builders that build integration objects take the module object the file
loaded (``client_mod`` = pd7day_client, ``store_mod`` = calibration_store,
``tariff_mod`` = tariff_sensor) rather than looking it up in ``sys.modules``:
pytest imports every test file before running any test, so by run time
``sys.modules`` holds the *last* file's copy, not the one this file pinned or
patches. Bind once in the header and keep the old call sites::

    from functools import partial
    import support
    make_store = partial(support.make_store, _store_mod)
"""
from __future__ import annotations

import asyncio
import enum
import importlib.machinery
import importlib.util
import io
import os
import sys
import types
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(ROOT, "custom_components", "nem_pd7day")
PKG = "custom_components.nem_pd7day"
NEM_TZ = timezone(timedelta(hours=10))


# ── Module loading ────────────────────────────────────────────────────────────

def _register_stub_package(name: str, path: str) -> None:
    """Register a package object exposing only __path__, without running its __init__.

    conftest.py does this for pytest runs; repeated here so ``load`` also works
    when a test file is executed as a script.
    """
    if name in sys.modules:
        return
    pkg = types.ModuleType(name)
    pkg.__path__ = [path]
    pkg.__package__ = name
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = pkg.__path__
    pkg.__spec__ = spec
    sys.modules[name] = pkg
    parent, _, child = name.rpartition(".")
    if parent and parent in sys.modules:
        setattr(sys.modules[parent], child, pkg)


def _ensure_packages() -> None:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    _register_stub_package("custom_components", os.path.join(ROOT, "custom_components"))
    _register_stub_package(PKG, PKG_DIR)


def load(name: str) -> types.ModuleType:
    """Execute ``<PKG_DIR>/<name>.py`` as a fresh module registered as ``PKG.<name>``.

    Same semantics as the ``_load(name, path)`` the test files used to carry:
    a new module object every call, registered in ``sys.modules`` before it
    runs so its relative imports resolve, then returned. Load dependencies
    first (``const`` and ``nem_time`` before anything that imports them).
    """
    _ensure_packages()
    full = f"{PKG}.{name}"
    spec = importlib.util.spec_from_file_location(full, os.path.join(PKG_DIR, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def load_chain(*names: str) -> tuple[types.ModuleType, ...]:
    """``load`` each name in order and return the module objects as a tuple."""
    return tuple(load(name) for name in names)


# ── Small helpers ─────────────────────────────────────────────────────────────

def run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def nem_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+10:00")


def make_zip(csv_bytes: bytes, member: str = "PUBLIC_PD7DAY_X.CSV") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, csv_bytes)
    return buf.getvalue()


# ── Fake HA classes ───────────────────────────────────────────────────────────

class SensorDeviceClass(str, enum.Enum):
    MONETARY = "monetary"
    ENERGY = "energy"
    TIMESTAMP = "timestamp"


class SensorStateClass(str, enum.Enum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class BinarySensorDeviceClass(str, enum.Enum):
    PROBLEM = "problem"


class NumberMode(str, enum.Enum):
    AUTO = "auto"
    BOX = "box"
    SLIDER = "slider"


class CameraEntityFeature(enum.IntFlag):
    NONE = 0


class UpdateFailed(Exception):
    """Installed as ``homeassistant.helpers.update_coordinator.UpdateFailed``."""


class FakeCoordinator:
    """Stand-in for DataUpdateCoordinator; supports the ``[...]`` subscript."""

    def __init__(self, hass, logger, name, update_interval):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.last_update_success = True
        self.data = None

    def __class_getitem__(cls, item):
        return cls

    async def async_config_entry_first_refresh(self):
        pass

    async def async_refresh(self):
        pass


class FakeCoordinatorEntity:
    """Stand-in for CoordinatorEntity; supports subscript and the HA init signature."""

    def __init__(self, coordinator=None, **kwargs):
        self.coordinator = coordinator

    def __class_getitem__(cls, item):
        return cls

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    # Real CoordinatorEntity defines this and every subclass chains up to it.
    async def async_added_to_hass(self):
        pass


class FakeStore:
    """Stand-in for ``homeassistant.helpers.storage.Store``: loads None, saves nothing.

    Deliberately has no ``async_delay_save``: ObservationLog switches to its
    delayed-write path whenever the store *class* has that method, and every
    test that wants that path builds its own store class.
    """

    def __init__(self, hass, version, key):
        self._hass = hass
        self._version = version
        self._key = key

    async def async_load(self):
        return None

    async def async_save(self, data):
        pass


class MemoryStore:
    """Dict-backed stand-in for an HA Store: load, save, remove, no delay.

    Matches the ``store_factory`` signature of ObservationLog (key only).
    ``_data`` is shared across every instance and every test file.
    """

    _data: dict = {}

    def __init__(self, key: str) -> None:
        self._key = key

    async def async_load(self):
        return MemoryStore._data.get(self._key)

    async def async_save(self, data) -> None:
        MemoryStore._data[self._key] = data

    async def async_remove(self) -> None:
        MemoryStore._data.pop(self._key, None)


class FakeCamera:
    """Minimal stand-in for homeassistant.components.camera.Camera."""

    def __init__(self) -> None:
        self._removals: list = []

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, func) -> None:
        self._removals.append(func)

    def async_write_ha_state(self) -> None:
        return None


class NumberEntity:
    pass


class RestoreNumber(NumberEntity):
    """Minimal stub of RestoreNumber."""

    _attr_native_value = None

    async def async_added_to_hass(self) -> None:
        pass

    async def async_get_last_number_data(self):
        return None

    def async_write_ha_state(self):
        pass


class ClientError(Exception):
    """Stand-in for aiohttp.ClientError."""


class ClientResponseError(ClientError):
    """Stand-in for aiohttp.ClientResponseError with the attributes the code reads."""

    def __init__(self, request_info=None, history=(), *, status=0, message=""):
        self.request_info = request_info
        self.history = history
        self.status = status
        self.message = message
        super().__init__(f"{status}, message='{message}'")


def _slugify(value: str) -> str:
    return value.lower().replace("-", "_").replace(" ", "_")


# ── The stub tree ─────────────────────────────────────────────────────────────

# Entries the test files only ever ``setdefault``: plain MagicMocks (or a
# configured one when this module creates it), left alone if already present.
_SETDEFAULT_MODULES = (
    "homeassistant",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.helpers",
    "homeassistant.helpers.event",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.selector",
    "homeassistant.util",
    "homeassistant.util.dt",
    "homeassistant.components",
    "homeassistant.loader",
    "aiohttp",
    "voluptuous",
)


def _new_setdefault_module(name: str) -> MagicMock:
    mod = MagicMock()
    if name == "homeassistant.util":
        mod.slugify = _slugify
    elif name == "aiohttp":
        mod.ClientError = ClientError
        mod.ClientResponseError = ClientResponseError
    elif name == "homeassistant.loader":
        integration = types.SimpleNamespace(manifest={"version": "0.0.0-test", "domain": "nem_pd7day"})
        mod.async_get_integration = AsyncMock(return_value=integration)
    return mod


def install_ha_stubs() -> None:
    """Install the MagicMock stand-in for ``aiohttp`` and the ``homeassistant`` tree.

    Two kinds of entry, mirroring what the test files did by hand:

    * ``setdefault`` (kept if already present): ``aiohttp``, ``voluptuous``,
      ``homeassistant``, ``.core``, ``.config_entries``, ``.helpers``,
      ``.helpers.event``, ``.helpers.aiohttp_client``,
      ``.helpers.entity_platform``, ``.helpers.selector``, ``.util``,
      ``.util.dt``, ``.components``, ``.loader``. When this module creates
      them, ``util.slugify`` is real, ``aiohttp`` carries real
      ``ClientError``/``ClientResponseError`` classes, and
      ``loader.async_get_integration`` is an AsyncMock returning a manifest.

    * assigned fresh on every call, so a module ``load``ed afterwards binds
      to *these* classes: ``.helpers.update_coordinator``
      (DataUpdateCoordinator=FakeCoordinator, CoordinatorEntity=
      FakeCoordinatorEntity, UpdateFailed), ``.helpers.storage``
      (Store=FakeStore), ``.helpers.device_registry`` (DeviceInfo=dict),
      ``.const`` (STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory.DIAGNOSTIC,
      Platform.SENSOR/BINARY_SENSOR), ``.components.sensor``
      (SensorDeviceClass, SensorStateClass, SensorEntity=object),
      ``.components.binary_sensor``, ``.components.camera``,
      ``.components.number``.

    A file that needs a different shape for one entry assigns it after this
    call, exactly as before.
    """
    for name in _SETDEFAULT_MODULES:
        if name not in sys.modules:
            sys.modules[name] = _new_setdefault_module(name)

    uc = MagicMock()
    uc.DataUpdateCoordinator = FakeCoordinator
    uc.CoordinatorEntity = FakeCoordinatorEntity
    uc.UpdateFailed = UpdateFailed
    sys.modules["homeassistant.helpers.update_coordinator"] = uc

    storage = MagicMock()
    storage.Store = FakeStore
    sys.modules["homeassistant.helpers.storage"] = storage

    device_registry = MagicMock()
    device_registry.DeviceInfo = dict
    sys.modules["homeassistant.helpers.device_registry"] = device_registry

    const = MagicMock()
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"
    const.EntityCategory = MagicMock()
    const.EntityCategory.DIAGNOSTIC = "diagnostic"
    const.Platform = MagicMock()
    const.Platform.SENSOR = "sensor"
    const.Platform.BINARY_SENSOR = "binary_sensor"
    sys.modules["homeassistant.const"] = const

    sensor = MagicMock()
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    sensor.SensorEntity = object
    sys.modules["homeassistant.components.sensor"] = sensor

    binary_sensor = MagicMock()
    binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass
    binary_sensor.BinarySensorEntity = object
    sys.modules["homeassistant.components.binary_sensor"] = binary_sensor

    camera = MagicMock()
    camera.Camera = FakeCamera
    camera.CameraEntityFeature = CameraEntityFeature
    sys.modules["homeassistant.components.camera"] = camera

    number = MagicMock()
    number.NumberEntity = NumberEntity
    number.NumberMode = NumberMode
    number.RestoreNumber = RestoreNumber
    sys.modules["homeassistant.components.number"] = number


# ── Fixture builders ──────────────────────────────────────────────────────────

def make_price_period(nemtime_dt: datetime, value: float = 0.10) -> MagicMock:
    """A PricePeriod-like MagicMock: ``nemtime`` is the interval END, ``time`` the start."""
    start_dt = nemtime_dt - timedelta(minutes=30)
    return MagicMock(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),
        value=value,
    )


def make_real_price_period(client_mod: types.ModuleType, nemtime_dt: datetime, value: float = 0.10):
    """A real ``client_mod.PricePeriod`` (pd7day_client) with the same time convention."""
    start_dt = nemtime_dt - timedelta(minutes=30)
    return client_mod.PricePeriod(
        nemtime=nem_iso(nemtime_dt),
        time=nem_iso(start_dt),
        value=value,
    )


def make_price_data(run_at_dt: datetime, periods) -> MagicMock:
    """A PD7DayData-like MagicMock carrying only what the store reads."""
    return MagicMock(
        forecast_generated_at=nem_iso(run_at_dt),
        forecast=periods,
    )


def make_pd7day_data(
    client_mod: types.ModuleType,
    run_at: datetime | str | None,
    periods: list,
    *,
    region: str = "QLD1",
    source_file: str = "PUBLIC_PD7DAY_20260415.ZIP",
    **fields: Any,
):
    """A real ``client_mod.PD7DayData`` (pd7day_client) for ``periods``.

    ``run_at`` is a datetime (formatted with nem_iso), an ISO string, or None.
    The summary values default to what ``periods`` implies; pass any
    PD7DayData field as a keyword to override it.
    """
    if isinstance(run_at, datetime):
        run_at = nem_iso(run_at)
    kwargs: dict[str, Any] = dict(
        region=region,
        source_file=source_file,
        forecast_generated_at=run_at,
        interval_minutes=30,
        current_value=periods[0].value if periods else 0.0,
        next_value=periods[1].value if len(periods) > 1 else None,
        min_24h_value=None,
        max_24h_value=None,
        cheapest_2h_window=None,
        forecast=periods,
    )
    kwargs.update(fields)
    return client_mod.PD7DayData(**kwargs)


def make_store(
    store_mod: types.ModuleType,
    *,
    region: str = "QLD1",
    fh_load_data=None,
    obs_count: int = 0,
    hass: Any = None,
):
    """A ``store_mod.CalibrationStore`` built with ``__new__`` and mocked HA storage.

    ``store_mod`` is the calibration_store module this file loaded; the engine
    and observation log come from the names it imported.
    ``_obs_store``/``_coeff_store``/``_fh_store`` are AsyncMocks whose
    ``async_load`` returns None (``_fh_store`` returns ``fh_load_data``); the
    observation log is backed by ``MemoryStore``. ``obs_count`` pre-fills
    that many placeholder observations. ``hass`` defaults to a MagicMock whose
    ``async_add_executor_job`` runs the callable inline.
    """
    if hass is None:
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    store = store_mod.CalibrationStore.__new__(store_mod.CalibrationStore)
    store._hass = hass
    store._region = region
    store._log = store_mod.ObservationLog(hass, region, store_factory=MemoryStore)
    for attr, load_value in (
        ("_obs_store", None),
        ("_coeff_store", None),
        ("_fh_store", fh_load_data),
    ):
        mock = AsyncMock()
        mock.async_load = AsyncMock(return_value=load_value)
        mock.async_save = AsyncMock()
        setattr(store, attr, mock)
    store._engine = store_mod.CalibrationEngine()
    if obs_count:
        store._observations = [{"dummy": i} for i in range(obs_count)]
    store._calibration = None
    store._forecast_history = {}
    store._actual_accum = {}
    return store


def expected_import_price(tariff_mod: types.ModuleType, lib_c_kwh, rrp_mwh, fee=0.0293, distributor="energex"):
    """Published import price in $/kWh for a mocked spot_to_tariff return.

    These tests mock the library to a fixed c/kWh figure, so they have to
    reproduce the way tariff_sensor splits that figure up. aemo_to_tariff
    composes a tariff as spot plus network rate and GSTs the network rate
    itself on seven of the thirteen networks, so the integration separates the
    two components, removes the library's GST from the network component where
    the library applied it, and grosses the total up once (#158).

    Whether that placement is right is the subject of tests/test_tariff_gst.py,
    which probes the real library rather than a mock. This helper exists only so
    the tests around it can go on testing the plumbing: fee handling, the $/MWh
    conversion, dispatch versus forecast selection, and forecast structure.

    ``tariff_mod`` is the tariff_sensor module object the constants are read from.
    """
    tm = tariff_mod
    spot_c = rrp_mwh * tm._DEFAULT_DLF * tm._DEFAULT_MLF * tm._DEFAULT_MARKET / 10
    network_c = lib_c_kwh - spot_c
    if distributor in tm._LIB_APPLIES_GST:
        network_c /= tm.GST
    return round(((spot_c + network_c) / 100 + fee) * tm.GST, 6)
