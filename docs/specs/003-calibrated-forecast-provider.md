# Spec 003: Calibrated forecast provider

Status: draft, 27 September 2026, against `main` at a6b8838 (v3.17.4)
Plan: docs/architecture/tech-debt-plan.md, step 003

## Responsibility

Every sensor that publishes a calibrated price for an interval of a PD7DAY run reads one per-region memo of the calibrated forecast. That memo has one reason to change: the rules for when a calibrated forecast is current, and how it is shared without racing.

Those rules are now spread over three places:

- `custom_components/nem_pd7day/sensor.py:277-522`: `CalibratedWriteMixin`, which warms the memo off the loop and publishes only under a live key (#58, #60, #61, #76).
- `sensor.py:572-630`: the key, lazy memo and build methods on `PD7DayForecastSensor`. `SpotPriceForecastDays27Sensor` (`:1029-1031`) and `PD7DayDataSensor` (`:1660-1664`) borrow them by assignment.
- `tariff_sensor.py:213-295`: the tariff sensors' calibrated spot view of the same memo (#62), over helpers in `calibration_inputs.py:581-710`.

Two copies also sit beside them:

- `SpotPriceForecastDays27Sensor._calibrate_period` (`sensor.py:981-1024`) is a copy of `PD7DayForecastSensor._calibrate_period` (`:728-776`).
- Both classes' `native_value` repeat the same current-interval calibration (`:694-726`, `:953-979`).
- The mixin keeps its own memo accessors (`:306-328`), duplicating `calibration_inputs._memo_dict` and `_memo_entry` (`:626-658`).

This spec makes one collaborator own the memo protocol: the key, reading and publishing the memo slot, the lazy build, the currency check, the guarded publish after an off-loop build, and the tariff spot view. The collaborator is `CalibratedForecast`, in a new services-layer module `calibrated_forecast.py` that does not import Home Assistant.

The entities keep what is theirs:

- Home Assistant lifecycle: the executor hop, background tasks, cancellation on removal and the state write.
- The per-interval entry builder `_calibrate_period`, as one real method in place of two copies.
- Every method name that tests, `scarcity_sensor.py` and `camera` tests call, now delegating.

The borrowed methods and the duplicate bodies go.

Amendment to the plan: the plan has the provider "owned by EntryRuntime". Here it is a stateless view built from `(coordinator, store, region)` on each access. The memo slots stay where they are, on the coordinator (`_calibrated_forecast_cache`, `_calibrated_spot_cache`). There are two reasons:

- Every memo test builds sensors through `__new__` over a bare or mocked coordinator, and reads or replaces those slots directly.
- Moving state off the coordinator is spec 007's subject ("coordinator derived state"). Spec 007 then makes the provider an object owned by the runtime, with the slots inside it.

## Current behaviour this must preserve

"Golden" means the golden-master snapshots in `tests/golden/snapshots/`. The memo tests in `tests/test_calibration_memo.py` (#35, #58, #60, #61, #62, #76) are the main pin, and not one of them may be edited.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Memo key `(region, forecast_generated_at, len(forecast), coordinator._stpasa_index_run, store.fit_generation)`, refreshing the STPASA index first and swallowing its exceptions | `calibration_inputs.py:585-623`, via `sensor.py:572-586` | `test_calibration_memo.py` lines 1377-1380 (one key implementation, docstring present); the key-moves tests |
| One memo slot per region on the coordinator, `{region: (key, list)}`, shared by the three sharing classes. It is created on the coordinator when missing; a missing or mismatched entry, or a None value, is a miss | `sensor.py:306-328`, `:604-618` | `test_calibration_memo.py` lines 345-440 (one pass for three entities; regions kept apart) |
| Lazy build on the loop: take the key, return the cached list on a hit, else build `[self._calibrate_period(p, run_at) for p in d.forecast]` and store it under that key. The build calls `_calibrate_period` through the instance, because tests replace it per instance to count calibrations | `sensor.py:588-630` | `test_calibration_memo.py` `_count_calibrations`, `_instrument`, `_count_during_write` (lines 191-265) |
| Warm: with no price data, return. Take the key on the loop; on a hit return with no executor hop. Build `_calibrated_forecast_values` in the executor. Discard, with the debug log "Calibrated forecast key moved during the warm for %s, discarding the result rather than publishing it", when `_price_data` is no longer the same object or the key moved. Otherwise publish. An executor failure logs "Calibrated forecast warm failed for %s, falling back to the lazy path" and returns | `sensor.py:330-392` | `test_calibration_memo.py` lines 823-970 (the #76 sibling-publish and key-moved cases), `test_scarcity_sensor.py:140` |
| Currency check: True with no price data, else whether the memo holds the live key. It has no await, so it cannot go stale before the write | `sensor.py:394-414` | `test_calibration_memo.py` warm-until-current tests |
| Warm until current: up to `_MAX_CALIBRATION_WARM_ATTEMPTS` (3) warms, stopping when current, with the debug log "Calibrated forecast key moved during warm attempt %s/%s for %s" | `sensor.py:416-451` | `test_calibration_memo.py` line 324 onward (refit landing during the warm) |
| `async_added_to_hass` calls `super()` then warms until current. The warm-then-write task is held, named `nem_pd7day warm state write {entity_id}`, and cancelled on removal (#106). `_handle_coordinator_update` schedules the warm write | `sensor.py:453-522` | `test_calibration_memo.py` lines 626-695 (MRO and hook reached through every override). The task name and the cancellation on removal (#106) are not pinned by any test today; see invariant 7 |
| Entry per interval: `nemtime`, `time`, `raw_value`, `horizon_hours` rounded to 1 place. When `calibrate_interval` returns a result, add `calibrated`, `p10`, `p50`, `p90`, `ols_mae`, `calibrated_source`, `band_source`, `n_obs`, `value`, `spike_credible` (through `_published_spike_credible`) and `stpasa_run_at` when present. Otherwise `value` is the raw price. The hour falls back to 0 on a parse error | `sensor.py:728-776` (copy at `:981-1024`) | `test_sensor.py` lines 363-399; `test_calibration_memo.py` line 475, which patches `calibrate_interval` in the globals of `PD7DayForecastSensor._calibrate_period`; golden `forecast` |
| Current interval, `native_value` of both forecast sensors: dispatch rrp first; else, with a store, `calibrate_interval` for the current period with the hour falling back to `now_nem().hour`, giving None when that returns None and `calibrated` otherwise; else the raw value | `sensor.py:694-726`, `:953-979` | `test_sensor.py` lines 413-440; golden state |
| Tariff spot view: raw passthrough without a store. `calibrated_spot_for_period` with the run's `forecast_generated_at`. `calibrated_spot_map` keyed on the loop, None without a store or data, and None with the debug log "calibrated spot memo unavailable for %s, calibrating inline" on any exception. A memo hit is served, a miss falls through to `_calibrated_value` | `tariff_sensor.py:213-295`; `calibration_inputs.py:537-710` | `test_calibration_memo.py` lines 1149-1360; `test_tariff_calibration_parity.py`; `test_export_tariff.py:225-241` (patches `_calibrated_value` on the class); golden `spot` |
| `_published_spike_credible` importable from `sensor` | `sensor.py:246-274` | `test_spike_credible_short_lead.py:17` |
| `scarcity_sensor` reads `_base._async_warm_calibrated_forecast()`, `_base._price_data`, `_base._cached_calibrated_forecast(_base._calibrated_forecast_key(data))` | `scarcity_sensor.py:139-144` | `test_scarcity_sensor.py`; golden (QLD1 scarcity premium) |

## Interfaces

New module `custom_components/nem_pd7day/calibrated_forecast.py`, services layer. It may import `calibration_inputs` and `const`, and must not import Home Assistant.

```python
Build = Callable[[Any], list[dict]]              # the entity's _calibrated_forecast_values
RunInExecutor = Callable[..., Awaitable[Any]]    # hass.async_add_executor_job

class WarmOutcome(enum.Enum):
    NO_DATA = "no_data"
    HIT = "hit"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"

class CalibratedForecast:
    """One region's calibrated forecast memo: key, read, publish, lazy build, guarded warm, spot view."""

    def __init__(self, coordinator: Any, store: Any, region: str) -> None: ...

    def key(self, d: Any) -> tuple: ...                      # calibration_inputs.calibrated_forecast_key
    def cached(self, key: tuple) -> list[dict] | None: ...   # the region's forecast slot under key, else None
    def publish(self, key: tuple, value: list[dict]) -> None: ...
    def is_current(self, d: Any | None) -> bool: ...
    def forecast(self, d: Any, build: Build) -> list[dict]: ...          # lazy, loop only, no await
    async def warm(self, d: Any | None, build: Build, run: RunInExecutor,
                   price_data_now: Callable[[], Any]) -> WarmOutcome: ...  # raises what the executor raises

    # tariff spot view (#62)
    def spot(self, period: Any, run_at: str | None) -> float | None: ...     # raw passthrough without a store
    def spot_map(self, d: Any | None) -> dict | None: ...                    # None without a store or data; may raise
    def spot_memoised(self, period: Any, spot_map: dict | None) -> tuple[bool, float | None]: ...  # (hit, value)
```

`calibration_inputs` keeps `calibrated_forecast_key`, `calibrated_spot_for_period`, `calibrated_spot_map`, `_memo_dict` and `_memo_entry`, and the provider calls them. One implementation of each stays the rule (#66).

In `sensor.py`, the mixin is split into two parts:

- `CalibratedForecastMixin` gives the entity its forecast accessors, each delegating to `CalibratedForecast`:
  - `_calibrated` (a property returning the provider);
  - `_calibrated_forecast_key`, `_cached_calibrated_forecast`, `_calibrated_forecast` and `_calibrated_forecast_values` (the build loop, still calling `self._calibrate_period`);
  - `_calibrate_period`, the one real entry builder, whose body stays in `sensor.py` so its globals are `sensor.py`'s;
  - `_calibrated_current(d, period)`, the one current-interval calibration both `native_value` methods call.
- `CalibratedWriteMixin(CalibratedForecastMixin)` keeps the Home Assistant write path: `_async_warm_calibrated_forecast`, whose logs stay here and which is driven by `WarmOutcome`; `_calibrated_cache_is_current`; `_async_warm_until_current`; `async_added_to_hass`; `_async_warm_then_write`; `_schedule_warm_state_write`; `_cancel_pending_warm_writes`; `async_will_remove_from_hass`; and `_handle_coordinator_update`.
- The three sharing classes keep `CalibratedWriteMixin` as their first base, so the MRO the memo tests assert does not change.
- `_calibrated_memo` and the unused `_covariates_for_interval` (assigned at `:1660` but called by nothing) are deleted.
- The attributes the mixins rely on (`coordinator`, `_region`, `_store`, `_price_data`, `hass`, `entity_id`, `async_write_ha_state`) are declared for the type checker, so the `attr-defined` errors at `sensor.py:314-479` go.

In `tariff_sensor.py`, the `TariffEntityBase` methods `_calibrated_value`, `_calibrated_value_memoised` and `_calibrated_spot_map` keep their names and signatures and delegate to `CalibratedForecast`. The `try`/`except` and its debug log stay in `_calibrated_spot_map`. `_calibrated_value_memoised` still falls through to `self._calibrated_value(period)` on a miss, because a test patches that on the class.

## Invariants

1. The golden master is identical.
2. There is still exactly one memo slot per region per memo, and the three sharing classes and the tariff sensors of a region share it. The tests at `test_calibration_memo.py` lines 345-440 and 1149-1360 pass unedited.
3. Every guarantee of #58, #60, #61 and #76 holds:
   - The key is taken on the loop before the executor hop.
   - A hit makes no executor hop.
   - The publish happens only when `price_data_now()` is the same object and the key is unchanged, with no await between that check and the publish.
   - The currency check has no await.
   - A failed warm leaves the lazy path to supply the value.
   - Warming is bounded at three attempts.
4. No method is shared by assignment in `sensor.py` or `tariff_sensor.py`, and `_calibrate_period` has one body. A new test asserts both, the second by checking that `SpotPriceForecastDays27Sensor._calibrate_period is PD7DayForecastSensor._calibrate_period` through inheritance.
5. `calibrated_forecast.py` imports neither `homeassistant` nor `sensor` nor `tariff_sensor`, and import-linter lists it in the services layer.
6. A warm on a sensor built through `__new__` with only `coordinator`, `_region`, `_store` and `hass` still works: the provider is built on access and needs nothing else.
7. The warm write task keeps its name and is cancelled when the entity is removed. A new test pins this for the three sharing classes: schedule a write, remove the entity, and check that the task is cancelled and no state is written.

## Migration

Each step leaves the suite and the golden master green.

1. Add `calibrated_forecast.py` with `CalibratedForecast` and `WarmOutcome`, and add `tests/test_calibrated_forecast.py`. It uses a plain coordinator stand-in and a counting build, with no Home Assistant stubs, and covers:
   - hit and miss;
   - slot creation;
   - regions kept apart;
   - the lazy build publishing under the key taken first;
   - `warm` returning each `WarmOutcome`, including SUPERSEDED when the key moves during the executor hop and when `price_data_now()` returns a different object;
   - the spot view with and without a store.

   Add the module to the services layer in `.importlinter`.
2. Add `CalibratedForecastMixin`, make `CalibratedWriteMixin` extend it and delegate to the provider, and delete `_calibrated_memo`.
3. Move `_calibrate_period`, `_calibrated_forecast_key`, `_calibrated_forecast`, `_calibrated_forecast_values` and a new `_calibrated_current` onto `CalibratedForecastMixin`. Delete them from `PD7DayForecastSensor`, delete the copy and the three assignments on `SpotPriceForecastDays27Sensor`, delete the five assignments on `PD7DayDataSensor`, and point both `native_value` methods at `_calibrated_current`.
4. Point the three `TariffEntityBase` methods at the provider.
5. Add the invariant 4 and 7 tests. Update `scripts/size_baseline.json` with `--update` (tightening only) and lower `mypy_baseline.txt`.

No existing test file is edited. If a step seems to need an edit to an existing test, stop and report it; do not make the edit.

## Non-goals

- Moving the memo slots off the coordinator, or giving the runtime ownership of the provider. That is spec 007.
- The duplicated lifecycle of `PD7DayForecastSensor` and `SpotPriceForecastDays27Sensor`: `_price_data`, `available`, `_current_period`, the tick and dispatch subscription. That is the same shape as spec 002 on the tariff side. It is filed as a follow-up spec, not done here, to keep this diff about calibration.
- `camera.py`'s own calibration of chart points (`camera.py:446-470`). It calls the shared `calibrate_interval` already, and it is spec 009's.
- Any change to what is calibrated, to the key's fields, or to the warm attempt bound.
- The remaining `sensor.py` type errors at `:189`, `:801-802`, `:858` and `:1057-1058`. None is in the calibration path.

## Acceptance

- [ ] Golden master identical, with no file under `tests/golden/snapshots/` changed.
- [ ] Full suite passes on 3.13 and 3.11. `git diff origin/main -- tests` touches only new test files: `tests/test_calibrated_forecast.py` and the invariant 4 and 7 tests.
- [ ] Coverage gate passes.
- [ ] mypy drops from 55 to at most 41: the 12 errors at `sensor.py:314-479` and the 2 "Invalid self argument" errors at `:1039` and `:1648` are gone. `calibrated_forecast.py` has none.
- [ ] Import contracts: `calibrated_forecast` is in the services layer, and there is no new violation.
- [ ] Size: `CalibratedForecast`, `CalibratedForecastMixin` and `CalibratedWriteMixin` are each under 250 lines and at most 15 methods. `sensor:PD7DayForecastSensor` shrinks or leaves the baseline. `sensor:CalibratedWriteMixin._async_warm_calibrated_forecast` shrinks or leaves the baseline. No new function over 60 lines.
- [ ] `grep -nE "^\s+_[a-z_]+ = [A-Z][A-Za-z0-9]+\._" custom_components/nem_pd7day/sensor.py custom_components/nem_pd7day/tariff_sensor.py` finds nothing.
- [ ] Every debug log text in the behaviour table is unchanged.
- [ ] PR title starts with `refactor:`.

## Rollback

The change is a single squash commit. No stored data is involved and the memo is in memory only. Revert it and release.
