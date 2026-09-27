# Spec 002: Tariff entity base

Status: approved 27 September 2026; implemented (this PR); drafted against `main` at 6e25da9 (spec 001 merged)
Plan: docs/architecture/tech-debt-plan.md, step 002

## Responsibility

The import and export tariff sensors share the same Home Assistant lifecycle:

- the half-hour boundary tick;
- the subscription to dispatch prices;
- the device, the region's price data, availability and the current interval;
- the usage fee read;
- the calibrated spot price each forecast interval is priced from.

That lifecycle changes when Home Assistant or the calibration path changes, not when a tariff does. Today it is written twice in `custom_components/nem_pd7day/tariff_sensor.py`: once in `NemPd7dayTariffSensor` (`:101-310`) and again, copied, in `NemPd7dayExportTariffSensor` (`:738-871`). The export sensor also takes three of the import sensor's methods by assignment (`:866-871`). That assignment is how #66 happened, and it is the source of the three remaining mypy errors in this file (`:909`, `:995`, `:999`).

The same file builds the forecast list three times, in the import, day 2-7 and export `extra_state_attributes` (`:566-590`, `:670-702`, `:990-1014`). The day 2-7 sensor also repeats the import sensor's whole attribute dictionary (`:712-738` against `:607-638`) to change nothing but which intervals it lists.

This spec moves the shared lifecycle and the calibrated-spot methods into one real base class, `TariffEntityBase`. It shares the forecast loop and the import attribute dictionary through methods rather than copies. Each sensor then holds only its identity (unique id, name, default enablement), its pricing, and its own attributes.

The calibrated-spot methods move into the base unchanged. Spec 003 moves them out again into a `CalibratedForecastProvider` shared with `sensor.py`. Doing that here would mix two specs, so spec 002 only makes them real inherited methods.

## Current behaviour this must preserve

"Golden" means the golden-master snapshots in `tests/golden/snapshots/`. Every scenario covers the import, export and day 2-7 tariff sensors of each configured region, recording state, attributes, availability, registry fields and device info. It does not record write counts or listener registrations, so the new tests below pin the lifecycle directly.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Class attributes: `state_class` None, unit `$/kWh`, display precision 4, icon `mdi:currency-usd`, `has_entity_name` True, `should_poll` False, `_unrecorded_attributes` `{"forecast", "forecast_description"}`, `_BOUNDARY_DELAY` 5 s. The day 2-7 sensor adds entity category DIAGNOSTIC and `entity_registry_enabled_default` True | `:104-113`, `:643-646`, `:741-749` | golden (entity registry fields); `test_tariff_sensor.py` |
| `async_added_to_hass`: calls `super()` first, then schedules the next boundary, then subscribes to the dispatch coordinator when `entry.runtime_data.dispatch` exists, with the listener registered through `async_on_remove` | `:143-156`, `:785-796` | `test_sensor.py::test_tariff_sensor_subscribes_to_dispatch_coordinator` (import only) |
| Next boundary: the next :00 or :30 of `dt_util.now()`, plus 5 s. The tick writes state and reschedules | `:158-184`, `:798-820` | not pinned today; see invariant 6 |
| `device_info`: identifiers `{(DOMAIN, f"{entry_id}_{region}")}` | `:186-190`, `:822-826` | `test_tariff_sensor.py::test_tariff_sensor_device_info`; golden |
| `_price_data`: `coordinator.data.prices.get(region)`, or None without data | `:197-201`, `:832-836` | `test_tariff_sensor.py` (price_periods=None cases) |
| `available`: `last_update_success`, price data present, and the library function bound: `spot_to_tariff` for import, `spot_to_feed_in_tariff` for export | `:203-209`, `:838-844` | golden `available`; `test_tariff_pricing.py` library-missing test |
| `_current_period`: the first period with `time <= now_nem() < nemtime`, skipping unparsable periods, else the first period, else None | `:211-222`, `:846-856` | `test_tariff_sensor.py::test_native_value_none_without_a_current_period`; golden state |
| `_get_additional_fee`: `read_additional_fee(hass, region)` (#181) | `:224-226`, `:858-859` | `test_tariff_sensor.py` fee tests around line 843; golden fee attribute |
| `_calibrated_value`, `_calibrated_value_memoised`, `_calibrated_spot_map`: bodies, docstrings and log message unchanged (#62, #66, #68, #76) | `:228-309`; borrowed at `:866-871` | `test_tariff_calibration_parity.py`; `test_calibration_memo.py` tariff cases; golden `spot` |
| `native_value`: the dispatch price first (`_apply_*_to_spot(rrp, now_nem())`, rounded to 6 places), else the existing debug log "%s/%s: no dispatch price for %s ... falling back to PD7DAY forecast" (text unchanged) with the import or export code, else `_compute_*` on the current period | `:463-488`, `:965-987` | `test_sensor.py::test_native_value_prefers_dispatch_price`, `::test_tariff_native_value_prefers_dispatch_then_pd7day`; golden state |
| Forecast entry per interval: `{"time", "nemtime", "spot_raw": round(value, 6), "spot": round(spot, 6) or None, "value", "period", "network_rate"}`. The spot comes from the memo taken once per build; the price is None when the spot is None; `_compute_*` is given `calibrated=spot` | `:572-590`, `:681-702`, `:995-1014` | `test_tariff_sensor.py`; `test_export_tariff.py`; `test_tariff_calibration_parity.py`; golden `forecast` |
| The day 2-7 sensor lists only periods with `parse_iso(time) > _amber_express_cutoff()`, reading the cutoff only when price data exists | `:674-680` | `test_tariff_sensor.py` day 2-7 tests around lines 272 and 674; golden (days 2-7 scenarios) |
| The import and day 2-7 attribute dictionaries have the same keys in the same order. The export dictionary has its own keys and order | `:607-638`, `:712-738`, `:1023-1042` | golden (keys and values; key order is not in the snapshot, see Invariants) |
| Unique ids: `{entry_id}_{region}_{distributor}_{code}_tariff`, `nem_pd7day_{region}_{distributor}_{code}_days27` and `{entry_id}_{region}_{distributor}_{import_code}_export_tariff`. Names as today | `:128-134`, `:659-663`, `:767-783` | golden (unique ids, names) |
| Construction-time caches: `_tariff_cache`, `_period_tariff_cache`, `_cached_tariff_periods`, `_cached_daily_supply_charge` on import; `_export_tariff_cache`, `_period_export_tariff_cache`, `_cached_tariff_periods = []` on export | `:135-141`, `:771-776` | `test_tariff_sensor.py`, `test_export_tariff.py` |
| Test builders construct sensors through `__new__` and set only `coordinator`, `_region`, `_distributor`, `_tariff_code` or `_import_code`/`_export_code`, `_entry`, `_store`, `hass` and the caches | `tests/test_tariff_sensor.py:46`, `tests/test_export_tariff.py:42`, `tests/test_tariff_calibration_parity.py:281,293` | the whole tariff test suite |

## Interfaces

Everything stays in `tariff_sensor.py`, in the adapter layer. No new module.

```python
class TariffEntityBase(CoordinatorEntity[PD7DayCoordinator], SensorEntity):
    """Lifecycle, device, price data, usage fee and calibrated spot for one region's tariff entity."""

    # the class attributes listed in the first row of the table, once

    def __init__(self, coordinator: PD7DayCoordinator, entry: ConfigEntry,
                 region: str, distributor: str, store: Any = None) -> None: ...

    # lifecycle
    async def async_added_to_hass(self) -> None: ...
    def _next_nem_boundary(self) -> datetime.datetime: ...
    def _schedule_next_boundary(self) -> None: ...
    async def _handle_interval_tick(self, _now: datetime.datetime) -> None: ...

    # region and device
    @property
    def device_info(self) -> DeviceInfo: ...
    @property
    def _price_data(self) -> Any: ...
    @property
    def available(self) -> bool: ...            # uses _pricing_bound()
    def _current_period(self, forecast: list) -> Any: ...
    def _get_additional_fee(self) -> float: ...

    # calibrated spot (moved verbatim; spec 003 moves them on)
    def _calibrated_value(self, period) -> float | None: ...
    def _calibrated_value_memoised(self, period, spot_map: dict | None) -> float | None: ...
    def _calibrated_spot_map(self, d) -> dict | None: ...

    # template methods
    @property
    def native_value(self) -> float | None: ...
    def _forecast_entries(self, d: Any, periods: Iterable[Any]) -> list[dict[str, Any]]: ...

    # hooks each tariff sensor implements
    def _pricing_bound(self) -> bool: ...                        # the library function this sensor prices with is bound
    @property
    def _priced_code(self) -> str: ...                           # the code named in the fallback debug log
    def _price_now(self, rrp_kwh: float, now: datetime.datetime) -> float | None: ...
    def _price_period(self, period: Any, calibrated: float | None = None) -> float | None: ...


class NemPd7dayTariffSensor(TariffEntityBase):
    # keeps: __init__ (identity and caches), entity_registry_enabled_default,
    # _tariff_windows, _lookup_period_info, _retail_price, _compute_tariff,
    # _apply_tariff_to_spot, _tariff_periods_for_attrs, _get_tariff_periods,
    # _get_daily_supply_charge, _build_forecast_description
    # hooks: _pricing_bound, _priced_code, _price_now -> _apply_tariff_to_spot,
    #        _price_period -> _compute_tariff
    def _forecast_periods(self, d: Any) -> list[Any]: ...        # all of d.forecast
    @property
    def extra_state_attributes(self) -> dict[str, Any]: ...      # one dict, built from _forecast_periods


class TariffForecastDays27Sensor(NemPd7dayTariffSensor):
    # keeps: __init__, entity_registry_enabled_default, class attributes
    def _forecast_periods(self, d: Any) -> list[Any]: ...        # the amber express cutoff trim
    # no extra_state_attributes of its own


class NemPd7dayExportTariffSensor(TariffEntityBase):
    # keeps: __init__ (identity and caches), entity_registry_enabled_default,
    # _get_tariff_periods, _lookup_period_info, _compute_export_tariff,
    # _apply_export_tariff_to_spot, extra_state_attributes
    # hooks as above, delegating to its export methods
```

Rules for the hooks:

- `_price_now` and `_price_period` are one-line delegations to the existing `_apply_*_to_spot` and `_compute_*` methods. Those methods keep their names, because tests call and patch them.
- `_priced_code` is a property that reads `_tariff_code` or `_export_code`, so a sensor built through `__new__` needs no new instance attribute.
- `TariffEntityBase.__init__` sets `_region`, `_distributor`, `_entry` and `_store` in the order they are set today, after `super().__init__(coordinator)`. Each subclass then sets its code fields, unique id, name and caches as today.

### As implemented

The interfaces above hold, with these deviations:

- The hooks are declared in `TariffEntityBase` inside an `if TYPE_CHECKING:` block, not as runtime methods. The fifteen methods listed above already reach the limit of 15, so four more stub methods would have broken it; the declarations give mypy the hook signatures, and a sensor that leaves one out fails at the call rather than inheriting a stand-in. Counting the declarations, the class holds 20 `def` statements, of which 15 exist at runtime.
- `_lookup_period_info` is declared there as a fifth hook, because `_forecast_entries` calls it. Both sensors already implement it under that name, so neither changed.
- To bring `TariffEntityBase` under 250 lines (247, 15 methods), its docstring is one line, the `__init__` signature sits on one line and the `async_track_point_in_time` call in `_schedule_next_boundary` is on one line. The three calibrated-spot methods are byte for byte as they were.
- The base `native_value` docstring now says it tries the dispatch price first; the code and the debug log text are unchanged. The per-key comments in `_forecast_entries` are the import loop's, so the export entries now carry them too.
- A two-line comment above `_calibrated_value` replaces the comment block that explained the assignments.
- `tests/test_tariff_entity_base.py` also pins that the day 2-7 dictionary equals the import one apart from the trimmed forecast, that the cutoff is not read without price data, and the key order with and without price data.

Sizes after the change: `TariffEntityBase` 247 lines and 15 methods, `NemPd7dayTariffSensor` 324 and 17 (from 537 and 25; still on the baseline), `TariffForecastDays27Sensor` 35 and 3, `NemPd7dayExportTariffSensor` 175 and 11 (off the baseline). No function in the file exceeds 60 lines.

## Invariants

1. The golden master is identical, including every entity's availability, registry fields and device info.
2. No method is shared by assignment anywhere in `tariff_sensor.py`. A test asserts that no class attribute of any class in the module is a function defined on another class (`value.__qualname__.split(".")[0] != cls.__name__`).
3. Attribute key order is unchanged for all three sensors. The snapshot sorts keys, so a new test in `tests/test_tariff_entity_base.py` compares `list(extra_state_attributes)` against the literal key lists taken from today's code.
4. A sensor built through `__new__` with exactly today's attribute set still works. The existing builders are not edited.
5. The MRO of each tariff class has `TariffEntityBase` immediately before `CoordinatorEntity`, so `super().async_added_to_hass()` still reaches `CoordinatorEntity`.
6. Both sensors keep the boundary tick and the dispatch subscription with identical behaviour. A new test pins both for the import and the export sensor: the boundary returned for times either side of :00 and :30, the tick writing once and rescheduling once, and exactly one dispatch listener registered through `async_on_remove` when a dispatch coordinator exists and none otherwise. Today only the import subscription is pinned.

## Migration

Each step leaves the suite and the golden master green.

1. Add `TariffEntityBase` holding the lifecycle, device, price data, `available`, `_current_period`, fee and hooks. Make both sensors inherit it and delete their copies. Add `tests/test_tariff_entity_base.py` with invariants 2 (initially marked `xfail(strict=True)` until step 2), 3, 5 and 6.
2. Move the three calibrated-spot methods into the base verbatim, delete the assignments at `:866-871`, and remove the `xfail` from invariant 2.
3. Move `native_value` into the base behind the hooks.
4. Add `_forecast_entries` and `_forecast_periods`. Build all three forecast lists through them, and delete the day 2-7 `extra_state_attributes`.
5. Update `scripts/size_baseline.json` with `--update` (tightening only) and `mypy_baseline.txt` downward.

No existing test file is edited. If a step seems to need an edit to an existing test, stop and report it; do not make the edit.

## Non-goals

- Moving the calibrated-spot methods out of the tariff classes. That is spec 003, together with `sensor.py`'s `CalibratedWriteMixin`.
- Any change to pricing, caches or attribute values. The attribute dictionaries of the import and export sensors stay separate: they differ in keys and order.
- The em dash in the fallback debug log and in older docstrings stays as it is. Log text is not rewritten in a refactor.
- #171 (`daily_supply_charge_$` in c/day) stays as it is.
- The export sensor's unused `_cached_tariff_periods = []` stays, because tests read it.

## Acceptance

- [ ] Golden master identical, with no file under `tests/golden/snapshots/` changed.
- [ ] Full suite passes on 3.13 and 3.11. `git diff origin/main -- tests` touches only `tests/test_tariff_entity_base.py`. The new tests cover invariants 2, 3, 5 and 6.
- [ ] Coverage gate passes.
- [ ] mypy drops from 58 to 55: the three "Invalid self argument" errors in `tariff_sensor.py` are gone. `tariff_sensor.py` has no new error.
- [ ] Import contracts: no new violation.
- [ ] Size: `TariffEntityBase` is under 250 lines and at most 15 methods. `NemPd7dayExportTariffSensor` falls under both limits and leaves `scripts/size_baseline.json`. `NemPd7dayTariffSensor` shrinks from 537 lines and 25 methods. No function exceeds 60 lines.
- [ ] `grep -nE "^\s+_[a-z_]+ = [A-Z][A-Za-z0-9]+\._" custom_components/nem_pd7day/tariff_sensor.py` finds nothing.
- [ ] `tariff_sensor.py` has no second copy of `_next_nem_boundary`, `_schedule_next_boundary`, `_handle_interval_tick`, `device_info`, `_price_data`, `_current_period` or `_get_additional_fee`.
- [ ] PR title starts with `refactor:`.

## Rollback

The change is a single squash commit with no stored data and no change to entity ids. Revert it and release.
