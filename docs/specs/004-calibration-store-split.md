# Spec 004: Calibration store split

Status: draft, 27 September 2026, against `main` at 72a0e59 (v3.17.5)
Plan: docs/architecture/tech-debt-plan.md, step 004

## Responsibility

`CalibrationStore` (`custom_components/nem_pd7day/calibration_store.py:67-768`, 702 lines, 24 methods) holds six responsibilities that change for different reasons:

1. **Persistence of the coefficient file and the forecast-history file.** This includes the legacy-key migration for each (`:193-236`), which is written twice and identical but for its names.
2. **Forecast ingest** (`:260-359`). It joins a PD7DAY run with STPASA, gas and QNI per interval, deduplicates by run, and prunes by age. It changes when the inputs AEMO publishes change.
3. **Actual recording** (`:366-483`). It matches an actual price against the forecasts that covered its interval, keeps a running average of five-minute readings per pair, and builds observations with STPASA features (#43, #132, #147). It changes when the observation schema changes.
4. **Refit orchestration** (`:498-595`). It converts observations for the engine, builds the STPASA feature map, fits stage 1 then stage 2 in the executor, bumps the fit generation, saves coefficients, and keeps the compression-ratio history. It changes when the fitting pipeline changes.
5. **Serving** (`:640-722`): `apply_to_price` and the spike-credibility annotation (#176). This is spec 005's.
6. **Diagnostics** (`:724-768`): the oldest observation, the effective window (#127) and the summary attributes.

Observation persistence already left in #130: `ObservationLog` owns the daily segments, and the store holds one.

This spec moves responsibilities 1 to 4 and 6 into units that do not import Home Assistant:

- `ForecastHistory` (ingest and pruning);
- `ActualRecorder` (matching and averaging);
- `RefitService` (engine inputs, the stage 1 and stage 2 fits, and the history record);
- `JsonRepository` (load with legacy migration, save);
- `calibration_summary` (the diagnostics).

`CalibrationStore` stays as the façade every caller uses. It keeps the state, the clock reads, the fit generation and the order of every await, so its public surface and its private attributes are unchanged. Serving stays on the façade for spec 005.

Amendments to the plan:

- **State stays on the façade.** The plan named `ObservationRepository`, `ForecastHistoryRepository`, `CalibrationModelRepository`, `ForecastIngest`, `ActualRecorder` and `RefitService` holding state behind a thin façade. Here the state stays on the façade, and the new units are functions or small objects that operate on state passed to them. `tests/support.make_store` and `tests/test_calibration_store.py:1544` build the store through `__new__` and set `_hass`, `_region`, `_log`, `_obs_store`, `_coeff_store`, `_fh_store`, `_engine`, `_observations`, `_calibration`, `_forecast_history` and `_actual_accum` directly. Twelve test files read or set those attributes, and none of them may be edited.
- **No new observation repository.** `ObservationLog` is already that repository (#130).

## Current behaviour this must preserve

"Golden" means the golden-master snapshots in `tests/golden/snapshots/`. The harness (`tests/golden/harness.py:596-668`) drives the real store through `async_load`, `ingest_forecast`, `async_record_actual` and `async_refit` from storage payloads the scenarios write.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| `async_load`, observations: loaded through `ObservationLog.async_load` with two legacy loaders, the single-file store then the unscoped legacy key. Every store that yielded rows is then removed, if its type has `async_remove` | `:158-191` | `test_calibration_store.py` load and migration tests; `test_observation_log.py` |
| `async_load`, coefficients: the scoped key. If that is None, the legacy key; if that has data, log "Migrating calibration coefficients from legacy storage key to nem_pd7day.%s.calibration_coefficients" at INFO, save it under the scoped key and use it. On data, `engine.from_storage`, then `_fit_generation += 1` and an INFO log. On an exception, a WARNING log and no generation bump | `:193-221` | `test_calibration_store.py` migration tests; golden (prefit scenarios) |
| `async_load`, forecast history: the same legacy pattern with "Migrating forecast history from legacy storage key to nem_pd7day.%s.forecast_history". `_forecast_history = (fh_data or {}).get("forecast_history", {})` | `:223-236` | `test_calibration_store.py` |
| `async_load` then rebuilds `_actual_accum` from the observations as `{(interval_time, forecast_run_at): {"sum": actual_rrp, "count": 1, "obs": o}}`, holding the dict itself (#132), and logs the INFO summary | `:238-256` | `test_calibration_store.py` accumulator tests |
| `ingest_forecast`: `run_at` is `forecast_generated_at` or `_now_nem().isoformat()`. Interval keys are the period's `time`. STPASA is joined by interval start, gas by the date of `nemtime` (the lookup) and of the interval key (the match), and QNI by `time`. An entry for a `(key, run_at)` already present is skipped. Entry fields are written in this order: `run_at`, `forecast_price`, `gas_tj`, `qni_mwflow`, `qni_violation`, `is_intervention`, `region`, then the seven `stpasa_*` fields when joined. Entries older than `_now_nem() - MAX_FORECAST_AGE_DAYS` are pruned by string compare. The whole history is saved as `{"forecast_history": ...}` | `:260-363` | `test_calibration_store.py` ingest tests; `test_coordinator.py`; golden |
| `async_record_actual`: with no history for the interval, a DEBUG log and 0. The region filter applies, the run time must parse, and the horizon must be in `[0, MAX_HORIZON_HOURS]`. A repeat pair updates the running average in place, rounded to 6, calls `_log.touch(obs)` and a DEBUG log. A first pair builds the observation with fields in the current order, adds STPASA features only when `stpasa_feature_values` returns them (#43, #147), then appends and seeds the accumulator. If anything changed: prune to `MAX_TOTAL_OBS`, drop the pruned pairs from the accumulator, save, and a DEBUG log. The return value is the count | `:366-483` | `test_calibration_store.py`, `test_actual_price_service.py`, `test_lifecycle.py`; golden |
| `build_stpasa_feature_map`: only observations carrying all four features, keyed `interval_time|forecast_run_at` | `:498-530` | `test_calibration_store.py` lines 1077-1300 |
| `async_refit`: build `Observation`s. Fit stage 1 in the executor, then assign `_calibration` and `_fit_generation += 1` **before** any further await. If the STPASA map is non-empty, fit stage 2 in the executor, attach `result.ols_models`, `_fit_generation += 1` and an INFO log; on an exception, a WARNING log and the stage 1 result stays. Save `engine.to_storage(result)`. Append `{fitted_at, buckets: {key: compression_ratio}}` to `_iso_history`, keeping the last 48. Return the result | `:532-595` | `test_calibration_store.py` refit and generation tests; `test_calibration_memo.py` (a refit landing during a warm); golden |
| Accessors: `calibration`, `fit_generation`, `observations` (the live list, not a copy), `observation_count`, `active_bucket_count`, `iso_history` | `:597-638` | callers in `__init__.py`, `camera.py`, `coordinator.py`, `sensor.py`; golden |
| `apply_to_price` and the spike annotation | `:640-699` | spec 005's; unchanged here |
| `oldest_observation`, `effective_window_days` (read through this module's `_now_nem`), `summary_attributes` | `:724-768` | `test_calibration_store.py:1301-1322`, which patches `calibration_store._now_nem`; golden (calibration sensor) |
| `test_calibration_store.py:53` replaces `calibration_store._now_nem` for the whole module, and ingest pruning must read it | `:62`, `:274`, `:354` | every ingest test in that file |
| `test_coordinator.py:698` counts package DEBUG lines in a dispatch cycle | the store's debug logs | `test_coordinator.py:698-720` |

## Interfaces

New modules, services layer, none importing Home Assistant.

```python
# forecast_history.py
def ingest_run(
    history: dict[str, list[dict]],
    *, region: str, run_at: str, price_data: Any, interconnectors: Mapping[str, Any],
    case: Any | None, market_summary: Any | None, stpasa: Any | None,
) -> None: ...                                           # appends in place, as today
def prune_history(history: dict[str, list[dict]], cutoff_iso: str) -> dict[str, list[dict]]: ...

# actual_recorder.py
class ObservationSink(Protocol):
    observations: list[dict]
    def append(self, obs: dict) -> None: ...
    def touch(self, obs: dict) -> None: ...
    def prune(self, max_total: int) -> Iterable[dict]: ...

def rebuild_accumulator(observations: Iterable[dict]) -> dict[tuple[str, str], dict]: ...
def record_actual(
    history: Mapping[str, list[dict]], accum: dict[tuple[str, str], dict], log: ObservationSink,
    *, interval_time: str, actual_rrp: float, calibration_region: str | None, source: str,
) -> int: ...                                            # prunes and trims accum; the caller saves

# refit_service.py
def engine_observations(rows: Iterable[dict]) -> list[Observation]: ...
def stpasa_feature_map(rows: Iterable[dict]) -> dict[str, StpasaFeatures]: ...
def iso_history_record(result: CalibrationResult) -> dict: ...
ISO_HISTORY_LIMIT: Final = 48

# json_repository.py
class JsonRepository:
    """One Home Assistant Store holding a JSON document, with its legacy-key migration."""
    def __init__(self, store: Any, legacy: Callable[[], Any], migration_message: str) -> None: ...
    async def load(self, region: str) -> dict | None: ...   # scoped, else legacy, migrating as today
    async def save(self, data: dict) -> None: ...

# calibration_summary.py
def oldest_observation(observations: Iterable[dict]) -> str | None: ...
def effective_window_days(oldest: str | None, now: datetime) -> float | None: ...
def summary_attributes(calibration: CalibrationResult | None, *, observation_count: int,
                       oldest: str | None, window_days: float | None,
                       active_buckets: int) -> dict: ...
```

`CalibrationStore` keeps every public name and every private attribute listed above. Its methods become short compositions:

- `async_load` calls `ObservationLog` as today, then `JsonRepository.load` twice, then `rebuild_accumulator`.
- `ingest_forecast` computes `run_at` and the cutoff from `_now_nem()`, calls `ingest_run` and `prune_history`, and saves.
- `async_record_actual` calls `record_actual`, then saves when it returned a count.
- `async_refit` keeps its awaits and generation bumps in place and takes its inputs from `refit_service`.
- The diagnostics call `calibration_summary`, passing `_now_nem()`.

The repositories are built on access from `_coeff_store` and `_fh_store`, so a store built through `__new__` works. `_parse_nem_iso` moves into `actual_recorder`. The unused `apply_calibration` wrapper, which nothing in the package or tests calls, is deleted.

## Invariants

1. The golden master is identical.
2. **Stored bytes are identical.** A new test drives a fixed sequence through the real store:
   - load from legacy payloads;
   - ingest two runs with STPASA, gas and QNI, including a duplicate run;
   - record actuals: first and repeat readings, a region mismatch, horizons outside the window, and enough to prune;
   - refit with stage 2 data, then refit again with stage 2 failing.

   The test captures every `async_save` payload and `async_remove` call in order, as `json.dumps(payload)` with insertion order kept. It compares them with a fixture recorded from the base commit, `tests/fixtures/calibration_store_io.json`. The fixture is generated by a script committed alongside it, run on the base commit before any code moves.
3. **Ordering in `async_refit`:** `_calibration` is assigned and the generation bumped before the stage 2 await, and the second bump follows the attach. The generation sequence over load, refit and failed stage 2 is pinned by the same test.
4. **Log lines:** the same messages at the same levels, in the same number per call. A logger may move only to another `custom_components.nem_pd7day.*` module.
5. **Clock reads** stay in `calibration_store.py` through its `_now_nem`, and the new units take times as arguments.
6. **Imports:** none of the new modules imports `homeassistant`, `calibration_store`, `coordinator` or `sensor`, and import-linter lists them in the services layer.
7. **Tests:** no existing test file is edited.

## Migration

Each step leaves the suite and the golden master green.

1. On the base commit, add `scripts/record_calibration_store_io.py` and the fixture it writes, plus the invariant 2 test. The test passes on the base code before anything moves.
2. Add `json_repository.py` and point `async_load`'s coefficient and history loads, and both saves, at it.
3. Add `forecast_history.py` and point `ingest_forecast` at it.
4. Add `actual_recorder.py` and point `async_record_actual` and the accumulator rebuild at it.
5. Add `refit_service.py` and point `async_refit` and `build_stpasa_feature_map` at it.
6. Add `calibration_summary.py` and point the diagnostics at it. Delete `apply_calibration`.
7. Add contract tests for each new module without Home Assistant stubs, update `.importlinter`, tighten `scripts/size_baseline.json` with `--update`, and lower `mypy_baseline.txt` if it moved.

## Non-goals

- `apply_to_price` and the spike annotation. They are spec 005, with the serving gates.
- Moving state off the façade, or giving the runtime ownership of the repositories. That is spec 008.
- Changing the forecast-history format, which today saves the whole history on every ingest. That is a performance question for an issue, not this spec.
- `ObservationLog`, which is unchanged.

## Acceptance

- [ ] Golden master identical, with no file under `tests/golden/snapshots/` changed.
- [ ] Invariant 2's I/O fixture test passes on the base commit (evidence: its commit, before any code moves) and on the head.
- [ ] Full suite passes on 3.13 and 3.11. `git diff origin/main -- tests` adds files only: the fixture, the I/O test and the contract tests.
- [ ] Coverage gate passes.
- [ ] mypy is no higher than 41, with zero errors in the new modules.
- [ ] Import contracts: the five new modules are in the services layer, and there is no new violation.
- [ ] Size: `calibration_store:CalibrationStore` shrinks from 702 lines and 24 methods. The public accessors keep it over 15 methods, so it stays on the baseline with lower numbers. Each new unit is under 250 lines and 15 methods, and no function exceeds 60 lines.
- [ ] Every log text in the behaviour table is unchanged.
- [ ] PR title starts with `refactor:`.

## Rollback

The change is a single squash commit, and the stored formats are identical by invariant 2, so a revert reads what the new code wrote. Revert it and release.
