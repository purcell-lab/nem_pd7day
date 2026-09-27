"""
Tests for tariff_sensor.py: TariffEntityBase, spec 002.

The import, day 2-7 and export tariff sensors share one Home Assistant
lifecycle through a real base class. These tests pin what the golden master
does not record: that no method is shared by assignment (the pattern behind
#66), the key order of each attribute dictionary (the snapshot sorts keys),
the method resolution order, and the boundary tick and dispatch subscription
of both the import and the export sensor (write counts and listener
registrations are not in the snapshot).

Run with:  python -m pytest tests/test_tariff_entity_base.py -v
"""
from __future__ import annotations

import inspect
import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from support import NEM_TZ, install_ha_stubs, load_chain, make_price_period, run_async

install_ha_stubs()

_const_mod, _nem_time, _client_mod, _store_mod, _coord_mod, _tariff_mod, _sensor_mod = load_chain(
    "const", "nem_time", "pd7day_client", "calibration_store", "coordinator",
    "tariff_sensor", "sensor",
)

TariffEntityBase = _tariff_mod.TariffEntityBase
NemPd7dayTariffSensor = _tariff_mod.NemPd7dayTariffSensor
TariffForecastDays27Sensor = _tariff_mod.TariffForecastDays27Sensor
NemPd7dayExportTariffSensor = _tariff_mod.NemPd7dayExportTariffSensor
DOMAIN = _const_mod.DOMAIN

TARIFF_CLASSES = [
    pytest.param(NemPd7dayTariffSensor, id="import"),
    pytest.param(TariffForecastDays27Sensor, id="days27"),
    pytest.param(NemPd7dayExportTariffSensor, id="export"),
]

NO_CUTOFF = datetime(2000, 1, 1, tzinfo=NEM_TZ)

# Stands in for the four staleness keys, in the order staleness_attributes
# returns them, so the key lists below also pin where they are spliced in.
STALENESS = {"data_age_hours": None, "last_success_at": None, "is_stale": False, "stale_reason": None}

# Taken from the three attribute dictionaries at f2a4452, before the base.
IMPORT_KEYS = [
    "tariff_code",
    "data_age_hours", "last_success_at", "is_stale", "stale_reason",
    "tariff_name",
    "distributor",
    "region",
    "network",
    "tariff_periods",
    "daily_supply_charge_$",
    "demand_charge",
    "distribution_loss_factor_dlf",
    "metering_loss_factor_mlf",
    "market_loss_factor",
    "combined_loss_multiplier",
    "additional_usage_fee_$/kwh",
    "gst_multiplier",
    "library_version",
    "tariff_source",
    "forecast_description",
    "forecast",
]
DAYS27_KEYS = list(IMPORT_KEYS)
EXPORT_KEYS = [
    "tariff_code",
    "data_age_hours", "last_success_at", "is_stale", "stale_reason",
    "import_tariff_code",
    "tariff_name",
    "distributor",
    "region",
    "network",
    "distribution_loss_factor_dlf",
    "metering_loss_factor_mlf",
    "market_loss_factor",
    "combined_loss_multiplier",
    "additional_usage_fee_$/kwh",
    "gst_multiplier",
    "library_version",
    "tariff_source",
    "export_periods",
    "forecast",
]
FORECAST_ENTRY_KEYS = ["time", "nemtime", "spot_raw", "spot", "value", "period", "network_rate"]


# ── Builders ──────────────────────────────────────────────────────────────────

def make_sensor(cls, price_periods=None, dispatch=None):
    """Construct a tariff sensor through ``__new__``, as the existing builders do.

    Sets exactly the attributes tests/test_tariff_sensor.py and
    tests/test_export_tariff.py set, so this also exercises invariant 4.
    """
    region = "QLD1"
    coordinator = MagicMock()
    if price_periods is not None:
        price_data = MagicMock()
        price_data.forecast = price_periods
        coordinator.data = MagicMock()
        coordinator.data.prices = {region: price_data}
    else:
        coordinator.data = None
    coordinator.last_update_success = True

    entry = MagicMock()
    entry.entry_id = "entry_1"
    entry.options = {}
    entry.runtime_data = types.SimpleNamespace(coordinator=coordinator, store=None, dispatch=dispatch)

    sensor = cls.__new__(cls)
    sensor.coordinator = coordinator
    sensor._region = region
    sensor._distributor = "energex"
    if cls is NemPd7dayExportTariffSensor:
        sensor._import_code = "8400"
        sensor._export_code = "8400"
    else:
        sensor._tariff_code = "8400"
    sensor._entry = entry
    sensor._store = None
    sensor.hass = MagicMock()
    sensor.hass.data = {DOMAIN: {}}
    sensor.hass.states.get.return_value = None
    return sensor


def forecast(n=4):
    run = datetime(2026, 9, 2, 4, 0, tzinfo=NEM_TZ)
    return [make_price_period(run + timedelta(minutes=30 * (i + 1)), value=0.10 + i * 1e-3) for i in range(n)]


def attributes(sensor):
    with patch.object(_tariff_mod.tariff_pricing, "spot_to_tariff", return_value=15.5), \
            patch.object(_tariff_mod.tariff_pricing, "spot_to_feed_in_tariff", return_value=6.0), \
            patch.object(_tariff_mod, "staleness_attributes", return_value=dict(STALENESS)), \
            patch.object(_tariff_mod, "_amber_express_cutoff", return_value=NO_CUTOFF):
        return sensor.extra_state_attributes


# ── Invariant 2: no method shared by assignment ──────────────────────────────

def _module_classes():
    return [
        cls for _name, cls in inspect.getmembers(_tariff_mod, inspect.isclass)
        if cls.__module__ == _tariff_mod.__name__
    ]


def _functions_of(value):
    """The plain functions behind a class attribute: itself, or a property's accessors."""
    if isinstance(value, (staticmethod, classmethod)):
        value = value.__func__
    if isinstance(value, property):
        return [f for f in (value.fget, value.fset, value.fdel) if f is not None]
    return [value] if isinstance(value, types.FunctionType) else []


def test_the_module_defines_the_tariff_classes():
    assert {c.__name__ for c in _module_classes()} >= {
        "TariffEntityBase", "NemPd7dayTariffSensor", "TariffForecastDays27Sensor",
        "NemPd7dayExportTariffSensor",
    }


def test_no_method_is_shared_by_assignment():
    """Every function a class holds was defined on that class (#66)."""
    borrowed = [
        f"{cls.__name__}.{name} is {func.__qualname__}"
        for cls in _module_classes()
        for name, value in vars(cls).items()
        for func in _functions_of(value)
        if func.__qualname__.split(".")[0] != cls.__name__
    ]
    assert not borrowed, borrowed


# ── Invariant 3: attribute key order ─────────────────────────────────────────

@pytest.mark.parametrize(
    "cls, keys",
    [
        pytest.param(NemPd7dayTariffSensor, IMPORT_KEYS, id="import"),
        pytest.param(TariffForecastDays27Sensor, DAYS27_KEYS, id="days27"),
        pytest.param(NemPd7dayExportTariffSensor, EXPORT_KEYS, id="export"),
    ],
)
@pytest.mark.parametrize("with_data", [True, False], ids=["data", "no_data"])
def test_attribute_key_order_is_unchanged(cls, keys, with_data):
    sensor = make_sensor(cls, price_periods=forecast() if with_data else None)
    attrs = attributes(sensor)
    assert list(attrs) == keys
    if with_data:
        assert len(attrs["forecast"]) == 4
        for entry in attrs["forecast"]:
            assert list(entry) == FORECAST_ENTRY_KEYS
    else:
        assert attrs["forecast"] == []


def test_days27_trims_only_the_forecast():
    """The day 2-7 sensor publishes the import dictionary over fewer intervals."""
    periods = forecast(6)
    cutoff = _nem_time.parse_iso(periods[2].time)
    importer = make_sensor(NemPd7dayTariffSensor, price_periods=periods)
    days27 = make_sensor(TariffForecastDays27Sensor, price_periods=periods)
    with patch.object(_tariff_mod.tariff_pricing, "spot_to_tariff", return_value=15.5), \
            patch.object(_tariff_mod, "_amber_express_cutoff", return_value=cutoff):
        full = importer.extra_state_attributes
        trimmed = days27.extra_state_attributes
    assert [e["time"] for e in trimmed["forecast"]] == [p.time for p in periods[3:]]
    assert trimmed["forecast"] == full["forecast"][3:]
    assert {k: v for k, v in trimmed.items() if k != "forecast"} == \
        {k: v for k, v in full.items() if k != "forecast"}


def test_days27_reads_the_cutoff_only_with_price_data():
    sensor = make_sensor(TariffForecastDays27Sensor, price_periods=None)
    with patch.object(_tariff_mod, "_amber_express_cutoff", side_effect=AssertionError("read")):
        assert sensor.extra_state_attributes["forecast"] == []


# ── Invariant 5: method resolution order ─────────────────────────────────────

@pytest.mark.parametrize("cls", TARIFF_CLASSES)
def test_base_sits_immediately_before_coordinator_entity(cls):
    mro = cls.__mro__
    assert TariffEntityBase in mro
    assert mro[mro.index(TariffEntityBase) + 1] is _tariff_mod.CoordinatorEntity


# ── Invariant 6: boundary tick and dispatch subscription ─────────────────────

@pytest.mark.parametrize(
    "now, expected",
    [
        pytest.param(datetime(2026, 9, 2, 10, 29, 59, 999999, tzinfo=NEM_TZ),
                     datetime(2026, 9, 2, 10, 30, 5, tzinfo=NEM_TZ), id="before_30"),
        pytest.param(datetime(2026, 9, 2, 10, 30, 0, tzinfo=NEM_TZ),
                     datetime(2026, 9, 2, 11, 0, 5, tzinfo=NEM_TZ), id="at_30"),
        pytest.param(datetime(2026, 9, 2, 10, 59, 59, 999999, tzinfo=NEM_TZ),
                     datetime(2026, 9, 2, 11, 0, 5, tzinfo=NEM_TZ), id="before_00"),
        pytest.param(datetime(2026, 9, 2, 11, 0, 0, tzinfo=NEM_TZ),
                     datetime(2026, 9, 2, 11, 30, 5, tzinfo=NEM_TZ), id="at_00"),
        pytest.param(datetime(2026, 9, 2, 23, 45, 0, tzinfo=NEM_TZ),
                     datetime(2026, 9, 3, 0, 0, 5, tzinfo=NEM_TZ), id="midnight"),
    ],
)
@pytest.mark.parametrize("cls", TARIFF_CLASSES)
def test_next_boundary_is_the_next_half_hour_plus_five_seconds(cls, now, expected):
    sensor = make_sensor(cls)
    with patch.object(_tariff_mod, "dt_util", types.SimpleNamespace(now=lambda: now)):
        assert sensor._next_nem_boundary() == expected
    assert cls._BOUNDARY_DELAY == timedelta(seconds=5)


def _lifecycle(sensor, events):
    """Record state writes, timer schedules and removals on ``sensor``."""
    sensor.async_write_ha_state = MagicMock(side_effect=lambda: events.append("write"))
    sensor.async_on_remove = MagicMock()
    unsub = MagicMock(name="timer_unsub")

    def track(hass, action, point):
        events.append(("schedule", point))
        return unsub

    return unsub, track


@pytest.mark.parametrize("cls", TARIFF_CLASSES)
def test_tick_writes_once_and_reschedules_once(cls):
    sensor = make_sensor(cls)
    events = []
    unsub, track = _lifecycle(sensor, events)
    now = datetime(2026, 9, 2, 10, 30, 5, tzinfo=NEM_TZ)
    with patch.object(_tariff_mod, "async_track_point_in_time", side_effect=track) as tracker, \
            patch.object(_tariff_mod, "dt_util", types.SimpleNamespace(now=lambda: now)):
        run_async(sensor._handle_interval_tick(now))
    assert events == ["write", ("schedule", datetime(2026, 9, 2, 11, 0, 5, tzinfo=NEM_TZ))]
    tracker.assert_called_once()
    assert tracker.call_args.args[0] is sensor.hass
    assert tracker.call_args.args[1] == sensor._handle_interval_tick
    sensor.async_on_remove.assert_called_once_with(unsub)


@pytest.mark.parametrize(
    "runtime",
    [
        pytest.param("dispatch", id="with_dispatch"),
        pytest.param("no_dispatch", id="dispatch_none"),
        pytest.param("no_runtime_data", id="runtime_data_none"),
    ],
)
@pytest.mark.parametrize("cls", TARIFF_CLASSES)
def test_added_to_hass_calls_super_schedules_and_subscribes(cls, runtime):
    events = []
    dispatch = None
    listener_unsub = MagicMock(name="listener_unsub")
    if runtime == "dispatch":
        dispatch = MagicMock()

        def add_listener(callback):
            events.append("subscribe")
            dispatch.callback = callback
            return listener_unsub

        dispatch.async_add_listener = MagicMock(side_effect=add_listener)
    sensor = make_sensor(cls, dispatch=dispatch)
    if runtime == "no_runtime_data":
        sensor._entry.runtime_data = None
    timer_unsub, track = _lifecycle(sensor, events)

    async def parent_added(self):
        events.append("super")

    now = datetime(2026, 9, 2, 10, 10, tzinfo=NEM_TZ)
    with patch.object(_tariff_mod.CoordinatorEntity, "async_added_to_hass", parent_added), \
            patch.object(_tariff_mod, "async_track_point_in_time", side_effect=track), \
            patch.object(_tariff_mod, "dt_util", types.SimpleNamespace(now=lambda: now)):
        run_async(sensor.async_added_to_hass())

    boundary = ("schedule", datetime(2026, 9, 2, 10, 30, 5, tzinfo=NEM_TZ))
    removals = [c.args for c in sensor.async_on_remove.call_args_list]
    if runtime == "dispatch":
        assert events == ["super", boundary, "subscribe"]
        dispatch.async_add_listener.assert_called_once()
        assert removals == [(timer_unsub,), (listener_unsub,)]
        # The listener writes state, once per dispatch update.
        dispatch.callback()
        assert events[-1] == "write"
        sensor.async_write_ha_state.assert_called_once()
    else:
        assert events == ["super", boundary]
        assert removals == [(timer_unsub,)]
        sensor.async_write_ha_state.assert_not_called()
